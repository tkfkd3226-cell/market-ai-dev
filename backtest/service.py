from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.backtest_repository import (
    get_evaluation_dataset,
    get_signal_evaluation_for_outcome,
)
from db.models import MarketOutcome, SignalEvaluation, SignalRun


KST = ZoneInfo("Asia/Seoul")
KRX_OPEN_TIME = time(hour=9, minute=0)
SELECTION_RULE = "latest_signal_before_krx_open"


@dataclass(frozen=True)
class EvaluationResult:
    evaluation: SignalEvaluation | None
    reason: str | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _krx_open_utc(session_date: date) -> datetime:
    return datetime.combine(session_date, KRX_OPEN_TIME, tzinfo=KST).astimezone(timezone.utc)


def _prediction_from_score(score: float) -> bool | None:
    value = float(score)
    if value > 50.0:
        return True
    if value < 50.0:
        return False
    return None


def _correct(prediction: bool | None, actual: bool | None) -> bool | None:
    if prediction is None or actual is None:
        return None
    return prediction == actual


def _latest_preopen_signal(
    session: Session,
    *,
    session_date: date,
    max_forecast_age_hours: int,
) -> SignalRun | None:
    open_at = _krx_open_utc(session_date)
    oldest = open_at - timedelta(hours=max_forecast_age_hours)
    return session.scalar(
        select(SignalRun)
        .where(SignalRun.created_at < open_at)
        .where(SignalRun.created_at >= oldest)
        .order_by(SignalRun.created_at.desc(), SignalRun.id.desc())
        .limit(1)
    )


def evaluate_outcome(
    session: Session,
    outcome: MarketOutcome,
    *,
    max_forecast_age_hours: int,
) -> EvaluationResult:
    existing = get_signal_evaluation_for_outcome(session, outcome.id)
    if not outcome.is_final:
        if existing is not None:
            session.delete(existing)
            session.flush()
        return EvaluationResult(None, "outcome is not final")

    signal = _latest_preopen_signal(
        session,
        session_date=outcome.session_date,
        max_forecast_age_hours=max_forecast_age_hours,
    )
    if signal is None:
        if existing is not None:
            session.delete(existing)
            session.flush()
        return EvaluationResult(
            None,
            f"no signal within {max_forecast_age_hours}h before KRX open",
        )

    open_at = _krx_open_utc(outcome.session_date)
    forecast_at = _as_utc(signal.created_at)
    forecast_age_minutes = max(0.0, (open_at - forecast_at).total_seconds() / 60.0)

    kospi_up_actual = float(outcome.kospi_close_return_pct) > 0.0
    gap_up_actual = float(outcome.kospi_gap_pct) > 0.0
    up_close_actual = kospi_up_actual
    semiconductor_up_actual = (
        None
        if outcome.semiconductor_return_pct is None
        else float(outcome.semiconductor_return_pct) > 0.0
    )

    row = existing
    if row is None:
        row = SignalEvaluation(market_outcome_id=outcome.id)
        session.add(row)

    row.signal_run_id = signal.id
    row.evaluated_at = datetime.now(timezone.utc)
    row.session_date = outcome.session_date
    row.forecast_at = forecast_at
    row.forecast_age_minutes = round(forecast_age_minutes, 4)
    row.selection_rule = SELECTION_RULE

    row.kospi_up_actual = kospi_up_actual
    row.semiconductor_up_actual = semiconductor_up_actual
    row.gap_up_actual = gap_up_actual
    row.up_close_actual = up_close_actual

    row.kospi_correct = _correct(_prediction_from_score(signal.kospi_score), kospi_up_actual)
    row.semiconductor_correct = _correct(
        _prediction_from_score(signal.semiconductor_score),
        semiconductor_up_actual,
    )
    row.gap_up_correct = _correct(
        _prediction_from_score(signal.gap_up_probability),
        gap_up_actual,
    )
    row.up_close_correct = _correct(
        _prediction_from_score(signal.up_close_probability),
        up_close_actual,
    )

    session.flush()
    return EvaluationResult(row, None)


def evaluate_all_final_outcomes(
    session: Session,
    *,
    max_forecast_age_hours: int,
) -> dict[str, object]:
    outcomes = list(
        session.scalars(
            select(MarketOutcome)
            .where(MarketOutcome.is_final.is_(True))
            .order_by(MarketOutcome.session_date.asc(), MarketOutcome.id.asc())
        ).all()
    )

    evaluated = 0
    skipped = 0
    reasons: dict[str, int] = {}
    for outcome in outcomes:
        result = evaluate_outcome(
            session,
            outcome,
            max_forecast_age_hours=max_forecast_age_hours,
        )
        if result.evaluation is None:
            skipped += 1
            reason = result.reason or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            evaluated += 1

    session.commit()
    return {
        "final_outcomes": len(outcomes),
        "evaluated": evaluated,
        "skipped": skipped,
        "skip_reasons": reasons,
    }


def _accuracy(values: list[bool | None]) -> dict[str, object]:
    eligible = [value for value in values if value is not None]
    correct = sum(1 for value in eligible if value)
    return {
        "n": len(eligible),
        "correct": correct,
        "accuracy": None if not eligible else round(correct / len(eligible), 4),
    }


def build_backtest_summary(session: Session, *, limit: int = 5000) -> dict[str, object]:
    rows = get_evaluation_dataset(session, limit=limit)
    evaluations = [evaluation for evaluation, _, _ in rows]

    return {
        "evaluation_count": len(evaluations),
        "note": (
            "Directional hit rates are based on raw Signal Engine scores. Stage 9 probability "
            "calibration is stored and evaluated separately without rewriting these rows."
        ),
        "checkpoint": {
            "selection_rule": SELECTION_RULE,
            "market_open_kst": "09:00",
        },
        "accuracy": {
            "kospi_direction": _accuracy([row.kospi_correct for row in evaluations]),
            "semiconductor_direction": _accuracy(
                [row.semiconductor_correct for row in evaluations]
            ),
            "gap_up": _accuracy([row.gap_up_correct for row in evaluations]),
            "up_close": _accuracy([row.up_close_correct for row in evaluations]),
        },
    }
