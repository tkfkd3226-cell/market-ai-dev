from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from bridges.kospi200_contract import is_krx_trading_day
from config import KIS_EFRIEND_KRX_CLOSED_DATES, KIS_EFRIEND_KRX_OPEN_DATES
from db.models import MarketSnapshot, SignalRun
from signals.input_status import POLICY_VERSION, input_status, observation_time


ENGINE_VERSION = "stage6_rule_v8"


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    label: str
    symbols: tuple[str, ...]
    scale_pct: float
    invert: bool = False


# Signal names are intentionally literal: each score uses only inputs that match
# the meaning shown in the dashboard tooltip. Macro/news inputs are collected by
# Market AI for other uses, but they are not blended into these four rule scores.
COMPONENT_SPECS = {
    "kospi_index": ComponentSpec(
        key="kospi_index",
        label="KOSPI spot index",
        symbols=("INDEX:KOSPI",),
        scale_pct=2.0,
    ),
    "kospi200_futures": ComponentSpec(
        key="kospi200_futures",
        label="KOSPI 200 futures",
        symbols=("FUTURES:KOSPI200",),
        scale_pct=2.0,
    ),
    "samsung_electronics": ComponentSpec(
        key="samsung_electronics",
        label="Samsung Electronics",
        symbols=("KRX:005930",),
        scale_pct=4.0,
    ),
    "sk_hynix": ComponentSpec(
        key="sk_hynix",
        label="SK hynix",
        symbols=("KRX:000660",),
        scale_pct=4.0,
    ),
    "sox_index": ComponentSpec(
        key="sox_index",
        label="PHLX Semiconductor Index",
        symbols=("INDEX:SOX",),
        scale_pct=3.0,
    ),
    "nvidia": ComponentSpec(
        key="nvidia",
        label="NVIDIA",
        symbols=("NASDAQ:NVDA",),
        scale_pct=4.0,
    ),
    "micron": ComponentSpec(
        key="micron",
        label="Micron",
        symbols=("NASDAQ:MU",),
        scale_pct=4.0,
    ),
    "sk_hynix_adr": ComponentSpec(
        key="sk_hynix_adr",
        label="SK hynix ADR",
        symbols=("NASDAQ:SKHY",),
        scale_pct=4.0,
    ),
    "nasdaq100_futures": ComponentSpec(
        key="nasdaq100_futures",
        label="Nasdaq 100 futures",
        symbols=("FUTURES:NQ",),
        scale_pct=2.0,
    ),
    "usdkrw": ComponentSpec(
        key="usdkrw",
        label="USD/KRW",
        symbols=("FX:USDKRW",),
        scale_pct=1.2,
        invert=True,
    ),
}


# Literal KOSPI direction: current KOSPI spot + leading KOSPI200 futures.
KOSPI_WEIGHTS = {
    "kospi_index": 0.35,
    "kospi200_futures": 0.65,
}

# Semiconductor direction: only direct Korean/US semiconductor assets and SOX.
SEMICONDUCTOR_WEIGHTS = {
    "samsung_electronics": 0.20,
    "sk_hynix": 0.20,
    "sox_index": 0.20,
    "nvidia": 0.15,
    "sk_hynix_adr": 0.15,
    "micron": 0.10,
}

# Pre-open gap signal: leading overnight inputs in the requested priority order.
GAP_UP_WEIGHTS = {
    "kospi200_futures": 0.50,
    "sox_index": 0.25,
    "nasdaq100_futures": 0.20,
    "usdkrw": 0.05,
}

# Up-close changes its input profile by KRX cash-session phase.
# Before the cash open, only leading overnight indicators are used.
UP_CLOSE_PREOPEN_WEIGHTS = {
    "kospi200_futures": 0.50,
    "sox_index": 0.30,
    "nasdaq100_futures": 0.20,
}

# During the KRX cash session, live KOSPI direction becomes the primary input.
UP_CLOSE_INTRADAY_WEIGHTS = {
    "kospi_index": 0.45,
    "kospi200_futures": 0.35,
    "sox_index": 0.12,
    "nasdaq100_futures": 0.08,
}

# Backward-compatible alias for callers that still expect one up-close map.
UP_CLOSE_WEIGHTS = UP_CLOSE_INTRADAY_WEIGHTS

KST = timezone(timedelta(hours=9), name="KST")
KRX_CASH_OPEN = time(hour=9, minute=0)
KRX_CASH_CLOSE = time(hour=15, minute=30)
CHECKPOINT_ENGINE_VERSIONS = (ENGINE_VERSION, "stage6_rule_v7", "stage6_rule_v6")


@dataclass
class SignalResult:
    created_at: datetime
    engine_version: str
    kospi_score: float
    semiconductor_score: float
    gap_up_probability: float
    up_close_probability: float
    confidence: float
    data_completeness: float
    calibrated: bool
    details: dict[str, object]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _direction_to_score(direction: float) -> float:
    return round(50.0 + 50.0 * _clamp(direction, -1.0, 1.0), 2)


def _normalize_change(change_pct: float, scale_pct: float, invert: bool) -> float:
    direction = math.tanh(change_pct / max(0.01, scale_pct))
    return -direction if invert else direction


def _build_market_component(
    spec: ComponentSpec,
    snapshot_map: dict[str, MarketSnapshot],
    now: datetime,
) -> dict[str, object]:
    inputs: list[dict[str, object]] = []
    weighted_directions: list[tuple[float, float]] = []

    for symbol in spec.symbols:
        row = snapshot_map.get(symbol)
        if row is None or row.change_pct is None:
            inputs.append({
                "symbol": symbol,
                "available": False,
                "status": "missing",
                "reason": "missing snapshot or change_pct",
            })
            continue

        if not math.isfinite(float(row.price)) or not math.isfinite(float(row.change_pct)):
            inputs.append({"symbol": symbol, "available": False,
                           "status": "invalid_value", "reason": "가격 또는 등락률이 유효하지 않습니다."})
            continue
        status = input_status(symbol, row, now)
        freshness = 1.0 if status["available"] else 0.0
        direction = _normalize_change(float(row.change_pct), spec.scale_pct, spec.invert)
        is_proxy = ":proxy" in row.source
        if spec.key == "kospi200_futures" and is_proxy:
            inputs.append({
                "symbol": symbol,
                "available": False,
                "price": float(row.price),
                "change_pct": float(row.change_pct),
                "source": row.source,
                "observed_at": _as_utc(row.observed_at).isoformat().replace("+00:00", "Z"),
                "reason": "KOSPI200 spot proxy is visible but excluded from futures signal",
            })
            continue

        if freshness > 0:
            weighted_directions.append((direction, freshness))

        inputs.append({
            "symbol": symbol,
            **status,
            "available": freshness > 0,
            "price": float(row.price),
            "change_pct": float(row.change_pct),
            "source": row.source,
            "received_at": _as_utc(row.observed_at).isoformat().replace("+00:00", "Z"),
            "freshness_weight": round(freshness, 4),
            "direction": round(direction, 4),
        })

    quality = _clamp(
        sum(weight for _, weight in weighted_directions) / max(1, len(spec.symbols)),
        0.0,
        1.0,
    )
    if not weighted_directions or quality <= 0:
        return {
            "key": spec.key,
            "label": spec.label,
            "available": False,
            "direction": None,
            "quality": 0.0,
            "score": None,
            "inputs": inputs,
        }

    total_weight = sum(weight for _, weight in weighted_directions)
    direction = sum(value * weight for value, weight in weighted_directions) / total_weight
    return {
        "key": spec.key,
        "label": spec.label,
        "available": True,
        "direction": round(direction, 4),
        "quality": round(quality, 4),
        "score": _direction_to_score(direction),
        "inputs": inputs,
    }


def _combine(
    weights: dict[str, float],
    directions: dict[str, float | None],
    qualities: dict[str, float],
) -> tuple[float, float]:
    effective_weight = 0.0
    weighted_sum = 0.0
    for key, configured_weight in weights.items():
        direction = directions.get(key)
        quality = _clamp(float(qualities.get(key, 0.0)), 0.0, 1.0)
        if direction is None or quality <= 0:
            continue
        component_weight = configured_weight * quality
        effective_weight += component_weight
        weighted_sum += direction * component_weight

    if effective_weight <= 0:
        return 0.0, 0.0
    return _clamp(weighted_sum / effective_weight, -1.0, 1.0), effective_weight


def _effective_component_weights(
    weights: dict[str, float],
    directions: dict[str, float | None],
    qualities: dict[str, float],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for key, configured_weight in weights.items():
        quality = _clamp(float(qualities.get(key, 0.0)), 0.0, 1.0)
        available = directions.get(key) is not None and quality > 0
        result[key] = {
            "configured_weight": round(float(configured_weight), 6),
            "effective_weight": round(float(configured_weight) * quality, 6) if available else 0.0,
            "quality": round(quality, 4),
            "available": available,
        }
    total = sum(item["effective_weight"] for item in result.values())
    for item in result.values():
        item["normalized_weight"] = round(item["effective_weight"] / total, 8) if total else 0.0
    return result


def _input_summary(weights, directions, qualities, components, *, at: datetime) -> dict[str, object]:
    records = _effective_component_weights(weights, directions, qualities)
    basis = []
    for key, record in records.items():
        component = components.get(key, {})
        inputs = component.get("inputs", [])
        basis.append({"key": key, **record, "inputs": inputs,
                      "reason": "; ".join(str(item.get("reason", "")) for item in inputs)})
    eligible = sum(weights.values())
    usable = sum(item["effective_weight"] for item in records.values())
    return {
        "policy": POLICY_VERSION, "basis_at": at.isoformat().replace("+00:00", "Z"),
        "frozen": False, "actual": False,
        "input_coverage": round(usable / eligible, 6) if eligible else 0.0,
        "configured_weight_total": round(eligible, 6), "usable_weight": round(usable, 6),
        "basis": basis,
        "missing_inputs": [item for item in basis if not item["available"]],
    }


def _directional_agreement(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    differences = [
        abs(left - right) / 2.0
        for index, left in enumerate(values)
        for right in values[index + 1:]
    ]
    return _clamp(1.0 - sum(differences) / max(1, len(differences)), 0.0, 1.0)


def _krx_trading_day(day: date) -> tuple[bool, str]:
    return is_krx_trading_day(
        day,
        closed_dates=KIS_EFRIEND_KRX_CLOSED_DATES,
        open_dates=KIS_EFRIEND_KRX_OPEN_DATES,
    )


def _next_krx_trading_day(day: date) -> tuple[date, str]:
    candidate = day
    for _ in range(16):
        is_open, source = _krx_trading_day(candidate)
        if is_open:
            return candidate, source
        candidate += timedelta(days=1)
    raise RuntimeError(f"unable to resolve next KRX trading day from {day.isoformat()}")


def _kst_at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=KST)


def _session_context(now_utc: datetime) -> dict[str, object]:
    local = now_utc.astimezone(KST)
    today = local.date()
    trading_today, calendar_source = _krx_trading_day(today)

    if not trading_today:
        target_date, target_source = _next_krx_trading_day(today + timedelta(days=1))
        return {
            "phase": "preopen_next_session",
            "trading_today": False,
            "calendar_source": calendar_source,
            "target_session_date": target_date,
            "target_calendar_source": target_source,
            "local_now": local,
        }

    if local.time() < KRX_CASH_OPEN:
        phase = "preopen"
    elif local.time() < KRX_CASH_CLOSE:
        phase = "intraday"
    else:
        phase = "post_close"

    return {
        "phase": phase,
        "trading_today": True,
        "calendar_source": calendar_source,
        "target_session_date": today,
        "target_calendar_source": calendar_source,
        "local_now": local,
    }


def _signal_score(row: SignalRun, target: str) -> float:
    if target == "gap_up":
        return float(row.gap_up_probability)
    if target == "up_close":
        return float(row.up_close_probability)
    raise KeyError(target)


def _decode_row_details(row: SignalRun) -> dict[str, object]:
    try:
        payload = json.loads(row.details_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _checkpoint_basis(details: dict[str, object], target: str) -> list[dict[str, object]]:
    weights_root = details.get("weights")
    weights = weights_root.get(target, {}) if isinstance(weights_root, dict) else {}
    effective_root = details.get("effective_weights")
    effective_weights = (
        effective_root.get(target, {}) if isinstance(effective_root, dict) else {}
    )
    qualities = details.get("qualities") if isinstance(details.get("qualities"), dict) else {}
    components = (
        details.get("market_components")
        if isinstance(details.get("market_components"), dict)
        else {}
    )
    result: list[dict[str, object]] = []
    if not isinstance(weights, dict):
        return result
    for key, configured_weight in weights.items():
        try:
            weight = float(configured_weight)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(weight) or weight <= 0:
            continue
        record = components.get(key, {}) if isinstance(components.get(key), dict) else {}
        if record.get("available") is False:
            continue
        quality_raw = qualities.get(key, record.get("quality"))
        try:
            quality = _clamp(float(quality_raw), 0.0, 1.0)
        except (TypeError, ValueError):
            quality = None

        effective_record = (
            effective_weights.get(key, {})
            if isinstance(effective_weights, dict)
            and isinstance(effective_weights.get(key), dict)
            else {}
        )
        try:
            effective_weight = float(effective_record.get("effective_weight"))
        except (TypeError, ValueError):
            effective_weight = weight * quality if quality is not None else weight
        if not math.isfinite(effective_weight) or effective_weight < 0:
            effective_weight = 0.0

        result.append({
            "key": key,
            # Legacy `weight` is kept for old dashboard adapters/checkpoints.
            "weight": round(weight, 6),
            "configured_weight": round(weight, 6),
            "effective_weight": round(effective_weight, 6),
            "available": effective_weight > 0,
            "quality": None if quality is None else round(quality, 4),
        })
    total = sum(item["effective_weight"] for item in result)
    for item in result:
        item["normalized_weight"] = round(item["effective_weight"] / total, 8) if total > 0 else 0.0
    return result


def _latest_checkpoint(
    session: Session,
    *,
    target: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, object] | None:
    rows = session.scalars(
        select(SignalRun)
        .where(SignalRun.engine_version.in_(CHECKPOINT_ENGINE_VERSIONS))
        .where(SignalRun.created_at >= start_at.astimezone(timezone.utc))
        .where(SignalRun.created_at < end_at.astimezone(timezone.utc))
        .order_by(SignalRun.created_at.desc(), SignalRun.id.desc())
    )
    row = None
    target_date = end_at.astimezone(KST).date().isoformat()
    allowed_modes = {"live_preopen", "next_session_preopen"} if target == "gap_up" else {"intraday_forecast"}
    for candidate in rows:
        candidate_details = _decode_row_details(candidate)
        states = candidate_details.get("signal_state", {})
        state = states.get(target, {}) if isinstance(states, dict) else {}
        if isinstance(state, dict) and state:
            if state.get("mode") not in allowed_modes:
                continue
            if state.get("target_session_date") != target_date:
                continue
        row = candidate
        break
    if row is None:
        return None
    details = _decode_row_details(row)
    summaries = details.get("signal_inputs", {})
    summary = summaries.get(target) if isinstance(summaries, dict) else None
    if isinstance(summary, dict):
        summary = deepcopy(summary)
    else:
        # Legacy decayed weight is not an input-coverage measurement. Preserve
        # the old calculation basis without inventing v8 coverage for it.
        summary = {
            "policy": "legacy", "input_coverage": None,
            "basis_at": _as_utc(row.created_at).isoformat().replace("+00:00", "Z"),
            "basis": _checkpoint_basis(details, target), "missing_inputs": [],
            "coverage_note": "이전 엔진의 체크포인트에는 입력 충족률이 저장되지 않았습니다.",
        }
    summary["frozen"] = True
    summary["checkpoint_engine_version"] = row.engine_version
    effective_root = details.get("effective_weight")
    effective = effective_root.get(target) if isinstance(effective_root, dict) else None
    try:
        effective_weight = float(effective)
    except (TypeError, ValueError):
        effective_weight = sum(item.get("effective_weight", 0.0) for item in summary["basis"])
    if not math.isfinite(effective_weight):
        effective_weight = 0.0
    score = _signal_score(row, target)
    if not math.isfinite(score):
        return None
    return {
        "score": score,
        "direction": _clamp((score - 50.0) / 50.0, -1.0, 1.0),
        "created_at": _as_utc(row.created_at),
        "engine_version": row.engine_version,
        "effective_weight": _clamp(effective_weight, 0.0, 1.0),
        "basis": summary["basis"],
        "input_summary": summary,
    }


def _final_kospi_state(
    snapshot_map: dict[str, MarketSnapshot],
    *,
    session_date: date,
    now: datetime,
) -> dict[str, object] | None:
    row = snapshot_map.get("INDEX:KOSPI")
    if row is None or row.change_pct is None:
        return None
    if (":proxy" in row.source or not math.isfinite(float(row.change_pct))
            or not math.isfinite(float(row.price))):
        return None
    observed = observation_time(row)
    if observed > now + timedelta(minutes=1):
        return None
    observed_local = observed.astimezone(KST)
    if observed_local.date() != session_date or observed_local.time() < KRX_CASH_CLOSE:
        return None
    change_pct = float(row.change_pct)
    if change_pct > 0:
        direction, label = 1.0, "상승"
    elif change_pct < 0:
        direction, label = -1.0, "하락"
    else:
        direction, label = 0.0, "보합"
    return {
        "direction": direction,
        "score": _direction_to_score(direction),
        "label": label,
        "change_pct": change_pct,
        "observed_at": observed,
    }


def build_signal(
    session: Session,
    *,
    news_lookback_hours: int,
    minimum_data_weight: float,
    ai_news_active: bool,
    now: datetime | None = None,
) -> SignalResult | None:
    # News arguments remain in the public signature for service/API compatibility.
    del news_lookback_hours, ai_news_active

    now_utc = _as_utc(now or datetime.now(timezone.utc))
    session_ctx = _session_context(now_utc)
    phase = str(session_ctx["phase"])
    local_now = session_ctx["local_now"]
    today = local_now.date()
    current_session_date = session_ctx["target_session_date"]

    snapshots = list(session.scalars(select(MarketSnapshot)).all())
    snapshot_map = {row.symbol: row for row in snapshots}

    market_components = {
        key: _build_market_component(spec, snapshot_map, now_utc)
        for key, spec in COMPONENT_SPECS.items()
    }

    directions: dict[str, float | None] = {
        key: (None if not component["available"] else float(component["direction"]))
        for key, component in market_components.items()
    }
    qualities: dict[str, float] = {
        key: (0.0 if not component["available"] else float(component.get("quality", 0.0)))
        for key, component in market_components.items()
    }

    kospi_direction, kospi_available_weight = _combine(KOSPI_WEIGHTS, directions, qualities)
    semi_direction, semi_available_weight = _combine(SEMICONDUCTOR_WEIGHTS, directions, qualities)

    frozen_summaries: dict[str, dict[str, object]] = {}
    gap_state: dict[str, object]
    gap_weights = GAP_UP_WEIGHTS
    if phase == "intraday":
        open_at = _kst_at(today, KRX_CASH_OPEN)
        checkpoint = _latest_checkpoint(
            session,
            target="gap_up",
            start_at=open_at - timedelta(hours=18),
            end_at=open_at,
        )
        if checkpoint is None:
            gap_direction, gap_available_weight, gap_score = 0.0, 0.0, 50.0
            gap_state = {
                "mode": "locked_preopen",
                "available": False,
                "target_session_date": today.isoformat(),
                "note": "장 시작 전 저장된 갭상 신호가 없어 장중에는 새 예측을 만들지 않습니다.",
            }
        else:
            frozen_summaries["gap_up"] = checkpoint["input_summary"]
            gap_direction = float(checkpoint["direction"])
            gap_available_weight = float(checkpoint["effective_weight"])
            gap_score = float(checkpoint["score"])
            checkpoint_available = gap_available_weight > 0
            gap_state = {
                "mode": "locked_preopen",
                "available": checkpoint_available,
                "target_session_date": today.isoformat(),
                "forecast_at": checkpoint["created_at"].isoformat().replace("+00:00", "Z"),
                "checkpoint_engine_version": checkpoint["engine_version"],
                "basis": checkpoint["basis"],
                "note": (
                    "09:00 개장 직전 마지막 갭상 예측값을 장중 고정 표시합니다."
                    if checkpoint_available
                    else "장전 체크포인트는 있으나 유효한 갭상 입력이 없어 장중 신호를 표시하지 않습니다."
                ),
            }
    else:
        gap_direction, gap_available_weight = _combine(GAP_UP_WEIGHTS, directions, qualities)
        gap_score = _direction_to_score(gap_direction)
        if phase == "post_close":
            gap_target_date, gap_calendar_source = _next_krx_trading_day(today + timedelta(days=1))
            gap_mode = "next_session_preopen"
        elif phase == "preopen_next_session":
            gap_target_date = current_session_date
            gap_calendar_source = session_ctx["target_calendar_source"]
            gap_mode = "next_session_preopen"
        else:
            gap_target_date = current_session_date
            gap_calendar_source = session_ctx["target_calendar_source"]
            gap_mode = "live_preopen"
        gap_state = {
            "mode": gap_mode,
            "available": gap_available_weight > 0,
            "target_session_date": gap_target_date.isoformat(),
            "calendar_source": gap_calendar_source,
            "forecast_at": now_utc.isoformat().replace("+00:00", "Z"),
            "note": "KOSPI 시가 이전/다음 거래일의 갭 방향을 선행시장으로 예측합니다.",
        }

    if phase in {"preopen", "preopen_next_session"}:
        up_close_weights = UP_CLOSE_PREOPEN_WEIGHTS
        up_close_direction, up_close_available_weight = _combine(
            up_close_weights, directions, qualities
        )
        up_close_score = _direction_to_score(up_close_direction)
        up_close_state = {
            "mode": "preopen_forecast",
            "available": up_close_available_weight > 0,
            "target_session_date": current_session_date.isoformat(),
            "forecast_at": now_utc.isoformat().replace("+00:00", "Z"),
            "note": "장전에는 K200 선물·SOX·NQ100 선물만으로 상승마감을 예측합니다.",
        }
    elif phase == "intraday":
        up_close_weights = UP_CLOSE_INTRADAY_WEIGHTS
        up_close_direction, up_close_available_weight = _combine(
            up_close_weights, directions, qualities
        )
        up_close_score = _direction_to_score(up_close_direction)
        up_close_state = {
            "mode": "intraday_forecast",
            "available": up_close_available_weight > 0,
            "target_session_date": today.isoformat(),
            "forecast_at": now_utc.isoformat().replace("+00:00", "Z"),
            "note": "장중에는 KOSPI 현물 흐름을 포함해 상승마감을 계속 갱신합니다.",
        }
    else:
        final_state = _final_kospi_state(snapshot_map, session_date=today, now=now_utc)
        if final_state is not None:
            up_close_weights = {"kospi_index": 1.0}
            up_close_direction = float(final_state["direction"])
            up_close_available_weight = 1.0
            up_close_score = float(final_state["score"])
            up_close_state = {
                "mode": "actual_close",
                "available": True,
                "target_session_date": today.isoformat(),
                "actual_label": final_state["label"],
                "actual_change_pct": round(float(final_state["change_pct"]), 4),
                "actual_at": final_state["observed_at"].isoformat().replace("+00:00", "Z"),
                "note": "15:30 이후에는 예측값 대신 실제 KOSPI 상승·하락 마감 결과를 표시합니다.",
            }
        else:
            close_at = _kst_at(today, KRX_CASH_CLOSE)
            checkpoint = _latest_checkpoint(
                session,
                target="up_close",
                start_at=_kst_at(today, KRX_CASH_OPEN),
                end_at=close_at,
            )
            up_close_weights = UP_CLOSE_INTRADAY_WEIGHTS
            if checkpoint is None:
                up_close_direction, up_close_available_weight, up_close_score = 0.0, 0.0, 50.0
                up_close_state = {
                    "mode": "post_close_pending",
                    "available": False,
                    "target_session_date": today.isoformat(),
                    "note": "종가 snapshot과 장중 체크포인트가 아직 없어 마감 결과를 확정할 수 없습니다.",
                }
            else:
                frozen_summaries["up_close"] = checkpoint["input_summary"]
                up_close_direction = float(checkpoint["direction"])
                up_close_available_weight = float(checkpoint["effective_weight"])
                up_close_score = float(checkpoint["score"])
                checkpoint_available = up_close_available_weight > 0
                up_close_state = {
                    "mode": "post_close_pending",
                    "available": checkpoint_available,
                    "target_session_date": today.isoformat(),
                    "forecast_at": checkpoint["created_at"].isoformat().replace("+00:00", "Z"),
                    "checkpoint_engine_version": checkpoint["engine_version"],
                    "basis": checkpoint["basis"],
                    "note": (
                        "15:30 종가 snapshot 확정 전까지 마지막 장중 예측값을 유지합니다."
                        if checkpoint_available
                        else "장중 체크포인트는 있으나 유효한 상승마감 입력이 없어 마감 대기 신호를 표시하지 않습니다."
                    ),
                }

    eligible_weights = {
        "kospi": sum(KOSPI_WEIGHTS.values()),
        "semiconductors": sum(SEMICONDUCTOR_WEIGHTS.values()),
        "gap_up": sum(gap_weights.values()),
        "up_close": sum(up_close_weights.values()),
    }
    available_weights = {
        "kospi": kospi_available_weight,
        "semiconductors": semi_available_weight,
        "gap_up": gap_available_weight,
        "up_close": up_close_available_weight,
    }
    target_weights = {
        "kospi": KOSPI_WEIGHTS, "semiconductors": SEMICONDUCTOR_WEIGHTS,
        "gap_up": gap_weights, "up_close": up_close_weights,
    }
    signal_inputs = {
        key: _input_summary(weights, directions, qualities, market_components, at=now_utc)
        for key, weights in target_weights.items()
    }
    # A locked score must carry the inputs from that same historical forecast.
    signal_inputs.update(frozen_summaries)
    if phase == "intraday" and "gap_up" not in frozen_summaries:
        signal_inputs["gap_up"] = _input_summary(gap_weights, {}, {}, {}, at=now_utc)
        signal_inputs["gap_up"]["frozen"] = True
    if up_close_state["mode"] == "post_close_pending" and "up_close" not in frozen_summaries:
        signal_inputs["up_close"] = _input_summary(up_close_weights, {}, {}, {}, at=now_utc)
        signal_inputs["up_close"]["frozen"] = True
    if up_close_state["mode"] == "actual_close":
        signal_inputs["up_close"] = _input_summary(
            {"kospi_index": 1.0}, {"kospi_index": up_close_direction},
            {"kospi_index": 1.0}, market_components, at=final_state["observed_at"],
        )
        signal_inputs["up_close"]["actual"] = True

    signal_states = {
        "kospi": {"available": kospi_available_weight > 0},
        "semiconductors": {"available": semi_available_weight > 0},
        "gap_up": gap_state, "up_close": up_close_state,
    }
    for key, state in signal_states.items():
        summary = signal_inputs[key]
        coverage = summary.get("input_coverage")
        basis_weight = available_weights[key] if coverage is None else float(coverage)
        sufficient = basis_weight > 0 and basis_weight + 1e-9 >= minimum_data_weight
        if summary.get("frozen") and "available" in summary:
            sufficient = bool(summary["available"])
        state["available"] = bool(state.get("available")) and sufficient
        summary["available"] = state["available"]
        summary.setdefault("minimum_input_coverage", minimum_data_weight)
        state["input_coverage"] = coverage
        if not state["available"]:
            state["note"] = state.get("note", "") + " 신호별 최소 입력 조건을 충족하지 못했습니다."

    # Always persist unavailable states. Returning None here leaves the last
    # successful signal visible indefinitely during a feed outage.
    coverage_values = [
        float(summary["input_coverage"]) for summary in signal_inputs.values()
        if summary.get("input_coverage") is not None
    ]
    data_completeness = round(sum(coverage_values) / len(coverage_values), 4) if coverage_values else 0.0
    kospi_score = _direction_to_score(kospi_direction) if signal_states["kospi"]["available"] else 50.0
    semiconductor_score = _direction_to_score(semi_direction) if signal_states["semiconductors"]["available"] else 50.0
    if not gap_state["available"]:
        gap_score = 50.0
    if not up_close_state["available"]:
        up_close_score = 50.0

    # Legacy DB/API compatibility only; the current UI does not consume this heuristic.
    agreement_values = [direction for key, direction in (
        ("kospi", kospi_direction), ("semiconductors", semi_direction),
        ("gap_up", gap_direction), ("up_close", up_close_direction),
    ) if signal_states[key]["available"]]
    confidence = round(_clamp(
        0.65 * data_completeness + 0.35 * _directional_agreement(agreement_values), 0.0, 1.0,
    ), 4) if agreement_values else 0.0

    calibration_eligible = []
    for key, target in (("kospi", "kospi_up"), ("semiconductors", "semiconductor_up"),
                        ("gap_up", "gap_up"), ("up_close", "up_close")):
        if not signal_states[key]["available"]:
            continue
        checkpoint_version = signal_inputs[key].get("checkpoint_engine_version", ENGINE_VERSION)
        if checkpoint_version != ENGINE_VERSION:
            continue
        if key == "up_close" and up_close_state["mode"] != "preopen_forecast":
            continue
        calibration_eligible.append(target)

    effective_weights = {
        key: {item["key"]: {field: item.get(field) for field in
              ("configured_weight", "effective_weight", "normalized_weight", "quality", "available")}
              for item in summary["basis"]}
        for key, summary in signal_inputs.items()
    }

    details = {
        "method": ENGINE_VERSION,
        "calibrated": False,
        "probability_note": (
            "Rule scores are bounded heuristic 0-100 signals; they are not statistically "
            "calibrated unless a Stage 9 calibration model is returned separately."
        ),
        "created_at": now_utc.isoformat().replace("+00:00", "Z"),
        "session_phase": {
            "phase": phase,
            "kst": local_now.isoformat(),
            "trading_today": bool(session_ctx["trading_today"]),
            "calendar_source": session_ctx["calendar_source"],
        },
        "signal_state": signal_states,
        "signal_inputs": signal_inputs,
        "input_coverage": {key: value.get("input_coverage") for key, value in signal_inputs.items()},
        "deprecated_fields": ["confidence", "data_completeness"],
        "calibration_eligible_targets": calibration_eligible,
        "input_policy": {
            "version": POLICY_VERSION,
            "news_used": False,
            "macro_used": ["USD/KRW"],
            "note": (
                "v8 uses session-aware binary input availability, per-target input coverage "
                "and normalized weights. Closed-session data is not discounted by wall time. "
                "Frozen forecasts preserve their original inputs; legacy coverage stays unknown."
            ),
        },
        "weights": {
            "kospi": KOSPI_WEIGHTS,
            "semiconductors": SEMICONDUCTOR_WEIGHTS,
            "gap_up": gap_weights,
            "up_close": up_close_weights,
        },
        "eligible_weight": {
            key: round(value, 4) for key, value in eligible_weights.items()
        },
        "effective_weight": {
            key: round(value, 4) for key, value in available_weights.items()
        },
        "effective_weights": effective_weights,
        "market_components": market_components,
        "directions": {
            key: None if value is None else round(value, 4)
            for key, value in directions.items()
        },
        "qualities": {key: round(value, 4) for key, value in qualities.items()},
        "scores": {
            "kospi": kospi_score,
            "semiconductors": semiconductor_score,
            "gap_up": round(gap_score, 2),
            "up_close": round(up_close_score, 2),
        },
    }

    return SignalResult(
        created_at=now_utc,
        engine_version=ENGINE_VERSION,
        kospi_score=kospi_score,
        semiconductor_score=semiconductor_score,
        gap_up_probability=round(gap_score, 2),
        up_close_probability=round(up_close_score, 2),
        confidence=confidence,
        data_completeness=data_completeness,
        calibrated=False,
        details=details,
    )

def encode_details(details: dict[str, object]) -> str:
    return json.dumps(details, ensure_ascii=False, separators=(",", ":"))
