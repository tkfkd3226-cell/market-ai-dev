from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from calibration.service import _training_points
from db.database import Base
from db.models import MarketOutcome, SignalEvaluation, SignalRun
from signals.engine import ENGINE_VERSION, build_signal


def _signal_run(*, created_at: datetime, details: dict[str, object]) -> SignalRun:
    return SignalRun(
        created_at=created_at,
        engine_version=ENGINE_VERSION,
        kospi_score=50.0,
        semiconductor_score=50.0,
        gap_up_probability=50.0,
        up_close_probability=50.0,
        confidence=0.0,
        data_completeness=0.0,
        calibrated=False,
        details_json=json.dumps(details, ensure_ascii=False, separators=(",", ":")),
    )


def _memory_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine, autoflush=False, expire_on_commit=False)


def test_intraday_zero_weight_gap_checkpoint_remains_unavailable():
    with _memory_session() as session:
        # 08:50 KST pre-open checkpoint with no effective gap inputs.
        session.add(
            _signal_run(
                created_at=datetime(2026, 9, 14, 23, 50, tzinfo=timezone.utc),
                details={"effective_weight": {"gap_up": 0.0}},
            )
        )
        session.commit()

        result = build_signal(
            session,
            news_lookback_hours=24,
            minimum_data_weight=0.0,
            ai_news_active=False,
            now=datetime(2026, 9, 15, 0, 5, tzinfo=timezone.utc),  # 09:05 KST
        )

        assert result is not None
        gap_state = result.details["signal_state"]["gap_up"]
        assert gap_state["mode"] == "locked_preopen"
        assert gap_state["available"] is False
        assert result.details["effective_weight"]["gap_up"] == 0.0
        assert "gap_up" not in result.details["calibration_eligible_targets"]


def test_post_close_zero_weight_up_close_checkpoint_remains_unavailable():
    with _memory_session() as session:
        # 15:00 KST intraday checkpoint with no effective up-close inputs.
        session.add(
            _signal_run(
                created_at=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc),
                details={"effective_weight": {"up_close": 0.0}},
            )
        )
        session.commit()

        result = build_signal(
            session,
            news_lookback_hours=24,
            minimum_data_weight=0.0,
            ai_news_active=False,
            now=datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc),  # 16:00 KST
        )

        assert result is not None
        up_close_state = result.details["signal_state"]["up_close"]
        assert up_close_state["mode"] == "post_close_pending"
        assert up_close_state["available"] is False
        assert result.details["effective_weight"]["up_close"] == 0.0
        assert "up_close" not in result.details["calibration_eligible_targets"]


def _add_evaluated_signal(
    session: Session,
    *,
    session_date: date,
    details: dict[str, object],
) -> SignalRun:
    signal = _signal_run(
        created_at=datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        details=details,
    )
    session.add(signal)
    session.flush()

    outcome = MarketOutcome(
        session_date=session_date,
        finalized_at=datetime(
            session_date.year,
            session_date.month,
            session_date.day,
            7,
            0,
            tzinfo=timezone.utc,
        ),
        source="test",
        source_reference=None,
        kospi_prev_close=100.0,
        kospi_open=101.0,
        kospi_close=102.0,
        kospi_gap_pct=1.0,
        kospi_close_return_pct=2.0,
        semiconductor_return_pct=1.5,
        is_final=True,
        details_json="{}",
    )
    session.add(outcome)
    session.flush()

    session.add(
        SignalEvaluation(
            signal_run_id=signal.id,
            market_outcome_id=outcome.id,
            evaluated_at=outcome.finalized_at,
            session_date=session_date,
            forecast_at=signal.created_at,
            forecast_age_minutes=0.0,
            selection_rule="test",
            kospi_up_actual=True,
            semiconductor_up_actual=True,
            gap_up_actual=True,
            up_close_actual=True,
            kospi_correct=True,
            semiconductor_correct=True,
            gap_up_correct=True,
            up_close_correct=True,
        )
    )
    session.flush()
    return signal


def test_training_points_respect_target_calibration_eligibility():
    with _memory_session() as session:
        _add_evaluated_signal(
            session,
            session_date=date(2026, 9, 15),
            details={"calibration_eligible_targets": ["semiconductor_up"]},
        )
        session.commit()

        assert _training_points(session, target="kospi_up", engine_version=ENGINE_VERSION) == []
        assert _training_points(session, target="gap_up", engine_version=ENGINE_VERSION) == []
        semiconductor = _training_points(
            session,
            target="semiconductor_up",
            engine_version=ENGINE_VERSION,
        )
        assert len(semiconductor) == 1
        assert semiconductor[0].score == 50.0


def test_training_points_keep_legacy_rows_without_eligibility_metadata():
    with _memory_session() as session:
        _add_evaluated_signal(
            session,
            session_date=date(2026, 9, 15),
            details={},
        )
        session.commit()

        # Legacy rows predate calibration_eligible_targets and retain the previous
        # training behavior instead of being silently discarded.
        gap_points = _training_points(session, target="gap_up", engine_version=ENGINE_VERSION)
        assert len(gap_points) == 1
        assert gap_points[0].score == 50.0
