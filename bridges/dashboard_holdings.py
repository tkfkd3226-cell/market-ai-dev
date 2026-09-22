from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Iterable
from threading import Lock


TICKER_PATTERN = re.compile(r"^[0-9A-Z]{6}$")
STATE_FILENAME = "dashboard_quote_universe.json"
_STATE_WRITE_LOCK = Lock()


def _runtime_root(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("MARKET_AI_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd().resolve()


def _portfolio_path(root: Path) -> Path:
    configured = os.environ.get("MARKET_AI_DASHBOARD_PORTFOLIO_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (root.parent / "investment-dashboard" / "data" / "portfolio.json").resolve()


def _state_path(root: Path) -> Path:
    return root / "db" / STATE_FILENAME


def _normalize_ticker(value: object) -> str | None:
    ticker = str(value or "").strip().upper()
    return ticker if TICKER_PATTERN.fullmatch(ticker) else None


def _normalize_name(value: object, ticker: str) -> str:
    name = str(value or "").strip()
    return name or ticker


def _normalize_type(value: object, *, default: str = "") -> str:
    instrument_type = str(value or "").strip()
    return instrument_type or default


def _extract_portfolio_holdings(
    payload: object,
) -> tuple[
    tuple[str, ...],
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, float]],
]:
    if not isinstance(payload, dict):
        raise ValueError("dashboard portfolio root must be an object")

    names: dict[str, str] = {}
    types: dict[str, str] = {}
    positions: dict[str, dict[str, float]] = {}
    tickers: set[str] = set()
    for section_name in ("securities", "pension"):
        section = payload.get(section_name, [])
        if not isinstance(section, list):
            raise ValueError(f"dashboard portfolio {section_name} must be an array")
        for item in section:
            if not isinstance(item, dict):
                continue
            ticker = _normalize_ticker(item.get("ticker"))
            if ticker is None:
                continue
            try:
                qty = float(item.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            tickers.add(ticker)
            names[ticker] = _normalize_name(item.get("name"), ticker)
            position = positions.setdefault(
                ticker,
                {"securities_qty": 0.0, "pension_qty": 0.0},
            )
            quantity_key = (
                "securities_qty" if section_name == "securities" else "pension_qty"
            )
            position[quantity_key] = float(position.get(quantity_key, 0.0)) + qty
            candidate_type = _normalize_type(
                item.get("type"),
                default="ETF" if section_name == "pension" else "",
            )
            if candidate_type and not types.get(ticker):
                types[ticker] = candidate_type

    ordered = tuple(sorted(tickers))
    return (
        ordered,
        {ticker: names.get(ticker, ticker) for ticker in ordered},
        {ticker: types.get(ticker, "") for ticker in ordered},
        {
            ticker: {
                "securities_qty": float(positions.get(ticker, {}).get("securities_qty", 0.0)),
                "pension_qty": float(positions.get(ticker, {}).get("pension_qty", 0.0)),
            }
            for ticker in ordered
        },
    )


def _extract_state_holdings(
    payload: object,
) -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("dashboard universe state must be an object")
    raw_tickers = payload.get("tickers", [])
    raw_names = payload.get("names", {})
    raw_types = payload.get("types", {})
    if (
        not isinstance(raw_tickers, list)
        or not isinstance(raw_names, dict)
        or not isinstance(raw_types, dict)
    ):
        raise ValueError("dashboard universe state has invalid shape")

    tickers = sorted({ticker for value in raw_tickers if (ticker := _normalize_ticker(value))})
    names = {
        ticker: _normalize_name(raw_names.get(ticker), ticker)
        for ticker in tickers
    }
    types = {
        ticker: _normalize_type(raw_types.get(ticker))
        for ticker in tickers
    }
    return tuple(tickers), names, types


def load_dashboard_holdings(root: Path | None = None) -> dict[str, object]:
    """Load the current Dashboard holdings without embedding a ticker list in Market AI.

    The sibling investment-dashboard portfolio is authoritative when readable, including
    the valid zero-holdings case. A small runtime state file is only a resilience fallback
    when the Dashboard repository is temporarily unavailable.
    """

    runtime_root = _runtime_root(root)
    portfolio = _portfolio_path(runtime_root)
    if portfolio.is_file():
        try:
            payload = json.loads(portfolio.read_text(encoding="utf-8-sig"))
            tickers, names, types, positions = _extract_portfolio_holdings(payload)
            return {
                "tickers": tickers,
                "names": names,
                "types": types,
                "positions": positions,
                "source": "portfolio",
                "path": str(portfolio),
            }
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    state_file = _state_path(runtime_root)
    if state_file.is_file():
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8-sig"))
            tickers, names, types = _extract_state_holdings(payload)
            return {
                "tickers": tickers,
                "names": names,
                "types": types,
                "positions": {},
                "source": "runtime-state",
                "path": str(state_file),
            }
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    return {
        "tickers": (),
        "names": {},
        "types": {},
        "positions": {},
        "source": "empty",
        "path": None,
    }


def order_dashboard_tickers_for_display(
    tickers: Iterable[str],
    positions: dict[str, dict[str, float]] | None,
    prices: dict[str, float] | None,
    names: dict[str, str] | None = None,
) -> list[str]:
    """Order monitor holdings like the Dashboard holdings tables.

    Securities holdings come first and are sorted by evaluation amount descending.
    Pension-only holdings follow with the same rule. A ticker held in both accounts is
    shown once in the securities section. Unknown/fallback tickers retain the incoming
    universe order so subscription semantics remain independent from display ordering.
    """

    normalized: list[str] = []
    seen: set[str] = set()
    for value in tickers:
        ticker = _normalize_ticker(value)
        if ticker is None or ticker in seen:
            continue
        seen.add(ticker)
        normalized.append(ticker)

    source_positions = positions or {}
    source_prices = prices or {}
    source_names = names or {}

    securities: list[str] = []
    pension_only: list[str] = []
    unknown: list[str] = []
    for ticker in normalized:
        position = source_positions.get(ticker, {})
        try:
            securities_qty = float(position.get("securities_qty", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            securities_qty = 0.0
        try:
            pension_qty = float(position.get("pension_qty", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            pension_qty = 0.0
        if securities_qty > 0:
            securities.append(ticker)
        elif pension_qty > 0:
            pension_only.append(ticker)
        else:
            unknown.append(ticker)

    def evaluation_amount(ticker: str, quantity_key: str) -> float:
        position = source_positions.get(ticker, {})
        try:
            quantity = float(position.get(quantity_key, 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            quantity = 0.0
        try:
            price = float(source_prices.get(ticker, 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            price = 0.0
        return max(0.0, quantity) * max(0.0, price)

    def sort_group(group: list[str], quantity_key: str) -> list[str]:
        return sorted(
            group,
            key=lambda ticker: (
                -evaluation_amount(ticker, quantity_key),
                str(source_names.get(ticker, ticker)),
                ticker,
            ),
        )

    return (
        sort_group(securities, "securities_qty")
        + sort_group(pension_only, "pension_qty")
        + unknown
    )


def persist_dashboard_universe(
    tickers: Iterable[str],
    names: dict[str, str] | None = None,
    types: dict[str, str] | None = None,
    *,
    root: Path | None = None,
) -> bool:
    """Persist only the latest authoritative Dashboard universe for restart warm-up.

    Returns True only when the durable state changed. The file lives beside the mutable
    SQLite DB and is not a source file or Git-tracked deployment asset.
    """

    runtime_root = _runtime_root(root)
    normalized = sorted({ticker for value in tickers if (ticker := _normalize_ticker(value))})
    source_names = names or {}
    normalized_names = {
        ticker: _normalize_name(source_names.get(ticker), ticker)
        for ticker in normalized
    }
    source_types = types or {}
    normalized_types = {
        ticker: _normalize_type(source_types.get(ticker))
        for ticker in normalized
    }

    state_file = _state_path(runtime_root)
    comparable = {
        "tickers": normalized,
        "names": normalized_names,
        "types": normalized_types,
    }
    # FastAPI sync routes can run concurrently in worker threads. Serialize the
    # shared temp-file compare/replace sequence so two local tabs cannot corrupt
    # the restart bootstrap file or race over the same .tmp path.
    with _STATE_WRITE_LOCK:
        if state_file.is_file():
            try:
                existing = json.loads(state_file.read_text(encoding="utf-8-sig"))
                if isinstance(existing, dict) and {
                    "tickers": existing.get("tickers", []),
                    "names": existing.get("names", {}),
                    "types": existing.get("types", {}),
                } == comparable:
                    return False
            except (OSError, json.JSONDecodeError):
                pass

        state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            **comparable,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        temp_file = state_file.with_suffix(state_file.suffix + ".tmp")
        temp_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_file.replace(state_file)
        return True
