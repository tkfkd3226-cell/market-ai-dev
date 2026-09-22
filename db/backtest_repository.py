from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import MarketOutcome, SignalEvaluation, SignalRun


def _json_dict(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_backtest_counts(session: Session) -> dict[str, int]:
    return {
        "signal_runs": int(session.scalar(select(func.count()).select_from(SignalRun)) or 0),
        "outcomes": int(session.scalar(select(func.count()).select_from(MarketOutcome)) or 0),
        "final_outcomes": int(
            session.scalar(
                select(func.count()).select_from(MarketOutcome).where(MarketOutcome.is_final.is_(True))
            )
            or 0
        ),
        "evaluations": int(session.scalar(select(func.count()).select_from(SignalEvaluation)) or 0),
    }


def get_latest_market_outcome(session: Session) -> MarketOutcome | None:
    return session.scalar(
        select(MarketOutcome)
        .order_by(MarketOutcome.session_date.desc(), MarketOutcome.id.desc())
        .limit(1)
    )


def get_market_outcome_by_date(session: Session, session_date: date) -> MarketOutcome | None:
    return session.scalar(
        select(MarketOutcome).where(MarketOutcome.session_date == session_date).limit(1)
    )


def get_market_outcomes(session: Session, *, limit: int) -> list[MarketOutcome]:
    return list(
        session.scalars(
            select(MarketOutcome)
            .order_by(MarketOutcome.session_date.desc(), MarketOutcome.id.desc())
            .limit(limit)
        ).all()
    )


def upsert_market_outcome(
    session: Session,
    *,
    session_date: date,
    source: str,
    source_reference: str | None,
    kospi_prev_close: float,
    kospi_open: float,
    kospi_close: float,
    semiconductor_return_pct: float | None,
    is_final: bool,
    details: dict[str, object],
) -> MarketOutcome:
    row = get_market_outcome_by_date(session, session_date)
    if row is None:
        row = MarketOutcome(session_date=session_date)
        session.add(row)

    gap_pct = (float(kospi_open) / float(kospi_prev_close) - 1.0) * 100.0
    close_return_pct = (float(kospi_close) / float(kospi_prev_close) - 1.0) * 100.0

    row.finalized_at = datetime.now(timezone.utc)
    row.source = source.strip()
    row.source_reference = source_reference.strip() if source_reference else None
    row.kospi_prev_close = float(kospi_prev_close)
    row.kospi_open = float(kospi_open)
    row.kospi_close = float(kospi_close)
    row.kospi_gap_pct = round(gap_pct, 8)
    row.kospi_close_return_pct = round(close_return_pct, 8)
    row.semiconductor_return_pct = (
        None if semiconductor_return_pct is None else float(semiconductor_return_pct)
    )
    row.is_final = bool(is_final)
    row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))

    session.flush()
    return row


def get_signal_evaluation_for_outcome(
    session: Session,
    outcome_id: int,
) -> SignalEvaluation | None:
    return session.scalar(
        select(SignalEvaluation)
        .where(SignalEvaluation.market_outcome_id == outcome_id)
        .limit(1)
    )


def get_signal_evaluations(session: Session, *, limit: int) -> list[SignalEvaluation]:
    return list(
        session.scalars(
            select(SignalEvaluation)
            .order_by(SignalEvaluation.session_date.desc(), SignalEvaluation.id.desc())
            .limit(limit)
        ).all()
    )


def get_evaluation_dataset(
    session: Session,
    *,
    limit: int,
) -> list[tuple[SignalEvaluation, SignalRun, MarketOutcome]]:
    return list(
        session.execute(
            select(SignalEvaluation, SignalRun, MarketOutcome)
            .join(SignalRun, SignalRun.id == SignalEvaluation.signal_run_id)
            .join(MarketOutcome, MarketOutcome.id == SignalEvaluation.market_outcome_id)
            .order_by(SignalEvaluation.session_date.desc(), SignalEvaluation.id.desc())
            .limit(limit)
        ).all()
    )


def decode_outcome_details(row: MarketOutcome) -> dict[str, object]:
    return _json_dict(row.details_json)
