from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class MarketSignal(Base):
    __tablename__ = "market_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    kospi_score: Mapped[float] = mapped_column(Float, nullable=False)
    semiconductor_score: Mapped[float] = mapped_column(Float, nullable=False)
    gap_up_probability: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="stage1_dummy")


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (
        Index("ix_market_prices_symbol_observed_at", "symbol", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshot"

    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    change_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    # Exchange/business clock from verified realtime feeds. Unlike observed_at, this
    # keeps the monitor's displayed market time stable across process restarts.
    business_time: Mapped[str | None] = mapped_column(String(6), nullable=True)


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        Index("ix_news_articles_published_at", "published_at"),
        Index("ix_news_articles_ai_status", "ai_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    social_image: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    ai_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")


class NewsArticleTopic(Base):
    __tablename__ = "news_article_topics"
    __table_args__ = (
        UniqueConstraint("article_id", "topic", name="uq_news_article_topic"),
        Index("ix_news_article_topics_topic", "topic"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    topic: Mapped[str] = mapped_column(String(40), nullable=False)


class NewsAIAnalysis(Base):
    __tablename__ = "news_ai_analyses"
    __table_args__ = (
        Index("ix_news_ai_analyses_analyzed_at", "analyzed_at"),
        Index("ix_news_ai_analyses_category", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    market_relevance: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    novelty: Mapped[float] = mapped_column(Float, nullable=False)
    time_horizon: Mapped[str] = mapped_column(String(20), nullable=False)
    affected_assets_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    kospi_impact: Mapped[float] = mapped_column(Float, nullable=False)
    semiconductor_impact: Mapped[float] = mapped_column(Float, nullable=False)
    nasdaq100_impact: Mapped[float] = mapped_column(Float, nullable=False)
    oil_impact: Mapped[float] = mapped_column(Float, nullable=False)
    rates_impact: Mapped[float] = mapped_column(Float, nullable=False)
    usdkrw_impact: Mapped[float] = mapped_column(Float, nullable=False)
    rationale: Mapped[str] = mapped_column(String(1000), nullable=False)


class AIAPIUsage(Base):
    __tablename__ = "ai_api_usage"
    __table_args__ = (
        Index("ix_ai_api_usage_created_at", "created_at"),
        Index("ix_ai_api_usage_model", "model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    response_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    input_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    cached_input_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

class SignalRun(Base):
    __tablename__ = "signal_runs"
    __table_args__ = (
        Index("ix_signal_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    engine_version: Mapped[str] = mapped_column(String(80), nullable=False)
    kospi_score: Mapped[float] = mapped_column(Float, nullable=False)
    semiconductor_score: Mapped[float] = mapped_column(Float, nullable=False)
    gap_up_probability: Mapped[float] = mapped_column(Float, nullable=False)
    up_close_probability: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    data_completeness: Mapped[float] = mapped_column(Float, nullable=False)
    calibrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False)



class MarketOutcome(Base):
    __tablename__ = "market_outcomes"
    __table_args__ = (
        Index("ix_market_outcomes_session_date", "session_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    source_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kospi_prev_close: Mapped[float] = mapped_column(Float, nullable=False)
    kospi_open: Mapped[float] = mapped_column(Float, nullable=False)
    kospi_close: Mapped[float] = mapped_column(Float, nullable=False)
    kospi_gap_pct: Mapped[float] = mapped_column(Float, nullable=False)
    kospi_close_return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    semiconductor_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class SignalEvaluation(Base):
    __tablename__ = "signal_evaluations"
    __table_args__ = (
        Index("ix_signal_evaluations_session_date", "session_date"),
        Index("ix_signal_evaluations_evaluated_at", "evaluated_at"),
        UniqueConstraint("market_outcome_id", name="uq_signal_evaluation_outcome"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_run_id: Mapped[int] = mapped_column(
        ForeignKey("signal_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    market_outcome_id: Mapped[int] = mapped_column(
        ForeignKey("market_outcomes.id", ondelete="CASCADE"),
        nullable=False,
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_age_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    selection_rule: Mapped[str] = mapped_column(String(80), nullable=False)

    kospi_up_actual: Mapped[bool] = mapped_column(Boolean, nullable=False)
    semiconductor_up_actual: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gap_up_actual: Mapped[bool] = mapped_column(Boolean, nullable=False)
    up_close_actual: Mapped[bool] = mapped_column(Boolean, nullable=False)

    kospi_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    semiconductor_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gap_up_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    up_close_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class CalibrationModel(Base):
    __tablename__ = "calibration_models"
    __table_args__ = (
        Index("ix_calibration_models_target_engine_active", "target", "engine_version", "active"),
        Index("ix_calibration_models_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    target: Mapped[str] = mapped_column(String(40), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(80), nullable=False)
    method: Mapped[str] = mapped_column(String(80), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    positive_count: Mapped[int] = mapped_column(Integer, nullable=False)
    base_rate: Mapped[float] = mapped_column(Float, nullable=False)
    bin_count: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_strength: Mapped[float] = mapped_column(Float, nullable=False)
    trained_from_date: Mapped[date] = mapped_column(Date, nullable=False)
    trained_through_date: Mapped[date] = mapped_column(Date, nullable=False)
    brier_raw: Mapped[float] = mapped_column(Float, nullable=False)
    brier_calibrated: Mapped[float] = mapped_column(Float, nullable=False)
    expected_calibration_error: Mapped[float] = mapped_column(Float, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    model_json: Mapped[str] = mapped_column(Text, nullable=False)


class SignalCalibration(Base):
    __tablename__ = "signal_calibrations"
    __table_args__ = (
        UniqueConstraint("signal_run_id", name="uq_signal_calibration_signal_run"),
        Index("ix_signal_calibrations_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_run_id: Mapped[int] = mapped_column(
        ForeignKey("signal_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    engine_version: Mapped[str] = mapped_column(String(80), nullable=False)
    kospi_up_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    semiconductor_up_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_up_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    up_close_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
