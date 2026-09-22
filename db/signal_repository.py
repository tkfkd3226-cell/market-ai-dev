import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from signals.engine import SignalResult, encode_details

from .models import SignalRun


def save_signal_run(session: Session, result: SignalResult) -> SignalRun:
    row = SignalRun(
        created_at=result.created_at,
        engine_version=result.engine_version,
        kospi_score=result.kospi_score,
        semiconductor_score=result.semiconductor_score,
        gap_up_probability=result.gap_up_probability,
        up_close_probability=result.up_close_probability,
        confidence=result.confidence,
        data_completeness=result.data_completeness,
        calibrated=result.calibrated,
        details_json=encode_details(result.details),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_latest_signal_run(session: Session) -> SignalRun | None:
    return session.scalar(
        select(SignalRun).order_by(SignalRun.created_at.desc(), SignalRun.id.desc()).limit(1)
    )


def get_signal_history(session: Session, *, limit: int) -> list[SignalRun]:
    return list(
        session.scalars(
            select(SignalRun)
            .order_by(SignalRun.created_at.desc(), SignalRun.id.desc())
            .limit(limit)
        ).all()
    )


def decode_signal_details(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
