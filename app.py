from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import logging
import os
from pathlib import Path
import sys
from threading import Lock

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from ai.openai_analyzer import OpenAINewsAnalyzer
from backtest.schemas import MarketOutcomeInput
from backtest.service import build_backtest_summary, evaluate_all_final_outcomes, evaluate_outcome
from calibration.service import (
    CALIBRATION_METHOD,
    TARGETS as CALIBRATION_TARGETS,
    calibration_performance,
    calibration_readiness,
    get_active_models,
    get_signal_calibration,
    get_signal_calibration_map,
    list_calibration_models,
    serialize_model as serialize_calibration_model,
    serialize_signal_calibration,
    train_all_targets,
)
from ai.schemas import AI_CATEGORIES
from ai.service import NewsAIService
from collectors.service import MarketCollectorService
from bridges.krx_quotes import KrxQuote, KrxQuoteService
from bridges.kospi200_contract import KST
from bridges.dashboard_holdings import (
    load_dashboard_holdings,
    order_dashboard_tickers_for_display,
    persist_dashboard_universe,
)
from bridges.kis_efriend import (
    KisEFriendBridgeService,
    KisEFriendHeartbeat,
    KisEFriendMarketTick,
    KisEFriendTick,
)
from collectors.yfinance_collector import YFinanceCollector
from config import (
    BACKTEST_MAX_FORECAST_AGE_HOURS,
    CALIBRATION_BIN_COUNT,
    CALIBRATION_MIN_CLASS_COUNT,
    CALIBRATION_MIN_SAMPLES,
    CALIBRATION_PRIOR_STRENGTH,
    AI_NEWS_ACTIVE,
    AI_NEWS_BATCH_SIZE,
    AI_NEWS_ENABLED,
    AI_NEWS_POLL_SECONDS,
    ALLOW_KOSPI200_INDEX_PROXY,
    COLLECTOR_ENABLED,
    COLLECTOR_POLL_SECONDS,
    GDELT_TIMEOUT_SECONDS,
    NEWS_COLLECTOR_ENABLED,
    NEWS_COLLECTOR_POLL_SECONDS,
    NEWS_LOOKBACK_HOURS,
    NEWS_MAX_RECORDS_PER_TOPIC,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    OPENAI_TIMEOUT_SECONDS,
    SIGNAL_ENGINE_ENABLED,
    SIGNAL_ENGINE_POLL_SECONDS,
    SIGNAL_MINIMUM_DATA_WEIGHT,
    SIGNAL_NEWS_LOOKBACK_HOURS,
    YFINANCE_TIMEOUT_SECONDS,
    KIS_EFRIEND_HISTORY_INTERVAL_SECONDS,
    KIS_EFRIEND_HEARTBEAT_STALE_SECONDS,
    KIS_EFRIEND_FALLBACK_AFTER_SECONDS,
    KIS_EFRIEND_KOSPI200_CODE,
    KIS_EFRIEND_KRX_CLOSED_DATES,
    KIS_EFRIEND_KRX_OPEN_DATES,
    KIS_EFRIEND_KRX_NIGHT_CLOSED_DATES,
)
from db.backtest_repository import (
    decode_outcome_details,
    get_backtest_counts,
    get_evaluation_dataset,
    get_latest_market_outcome,
    get_market_outcomes,
    upsert_market_outcome,
)
from db.database import Base, SessionLocal, check_database, engine
from db.market_repository import get_market_history, get_market_snapshot
from db.migrations import (
    migrate_market_snapshot_business_time,
    migrate_market_snapshot_change_amount,
    migrate_stage3_symbols,
)
from db.models import MarketSignal, MarketSnapshot, NewsAIAnalysis, NewsArticle
from db.news_ai_repository import (
    decode_affected_assets,
    get_ai_usage_summary,
    get_latest_news_ai_analyses,
    get_news_ai_counts,
)
from db.news_repository import get_latest_news, get_news_topics_for_articles
from db.signal_repository import decode_signal_details, get_latest_signal_run, get_signal_history
from market.catalog import MARKET_INSTRUMENTS, MARKET_SYMBOLS
from market.provider_map import get_yahoo_mappings
from news.gdelt_collector import GDELTNewsCollector
from news.service import NewsCollectorService
from news.topics import NEWS_TOPICS, NEWS_TOPIC_KEYS
from signals.engine import (
    COMPONENT_SPECS,
    ENGINE_VERSION,
    GAP_UP_WEIGHTS,
    KOSPI_WEIGHTS,
    SEMICONDUCTOR_WEIGHTS,
    UP_CLOSE_PREOPEN_WEIGHTS,
    UP_CLOSE_INTRADAY_WEIGHTS,
    UP_CLOSE_WEIGHTS,
)
from signals.input_status import input_status as market_input_status
from signals.service import SignalService


logger = logging.getLogger(__name__)


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)

    return value.isoformat().replace("+00:00", "Z")


def resolve_monitor_directory() -> Path:
    """Resolve the three-file Web Monitor in source and frozen runtimes."""

    candidates: list[Path] = []
    configured_root = os.environ.get("MARKET_AI_HOME", "").strip()
    if configured_root:
        candidates.append(Path(configured_root).expanduser().resolve() / "monitor")

    candidates.append(Path(__file__).resolve().parent / "monitor")
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root).resolve() / "monitor")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def normalize_business_time(value: object) -> str | None:
    candidate = str(value or "").strip()
    if len(candidate) != 6 or not candidate.isdigit():
        return None
    hour = int(candidate[0:2])
    minute = int(candidate[2:4])
    second = int(candidate[4:6])
    if hour > 23 or minute > 59 or second > 59:
        return None
    return candidate


def serialize_market_row(row: object) -> dict[str, object]:
    return {
        "symbol": row.symbol,
        "price": row.price,
        "change_amount": getattr(row, "change_amount", None),
        "change_pct": row.change_pct,
        "source": row.source,
        "observed_at": to_utc_iso(row.observed_at),
        "business_time": normalize_business_time(getattr(row, "business_time", None)),
    }


def serialize_market_snapshot_row(row: object, now: datetime) -> dict[str, object]:
    payload = serialize_market_row(row)
    payload["input_status"] = market_input_status(row.symbol, row, now)
    return payload


def latest_completed_krx_session_date(now: datetime | None = None) -> date:
    # Keep one calendar contract for the Web Monitor and Dashboard quote recovery.
    # The quote service owns KRX open/closed overrides, so delegate instead of
    # maintaining a second date algorithm here.
    return krx_quote_service.latest_completed_session_date(now)


def serialize_news_article(
    article: NewsArticle,
    topics: list[str],
) -> dict[str, object]:
    return {
        "id": article.id,
        "published_at": None if article.published_at is None else to_utc_iso(article.published_at),
        "collected_at": to_utc_iso(article.collected_at),
        "provider": article.provider,
        "title": article.title,
        "url": article.url,
        "domain": article.domain,
        "language": article.language,
        "source_country": article.source_country,
        "social_image": article.social_image,
        "topics": topics,
        "ai_status": article.ai_status,
    }


def serialize_news_ai_analysis(
    analysis: NewsAIAnalysis,
    article: NewsArticle,
    topics: list[str],
) -> dict[str, object]:
    return {
        "article": {
            "id": article.id,
            "published_at": None if article.published_at is None else to_utc_iso(article.published_at),
            "provider": article.provider,
            "title": article.title,
            "url": article.url,
            "domain": article.domain,
            "topics": topics,
        },
        "analysis": {
            "analyzed_at": to_utc_iso(analysis.analyzed_at),
            "model": analysis.model,
            "prompt_version": analysis.prompt_version,
            "category": analysis.category,
            "event_type": analysis.event_type,
            "market_relevance": analysis.market_relevance,
            "sentiment": analysis.sentiment,
            "severity": analysis.severity,
            "confidence": analysis.confidence,
            "novelty": analysis.novelty,
            "time_horizon": analysis.time_horizon,
            "affected_assets": decode_affected_assets(analysis.affected_assets_json),
            "impact": {
                "kospi": analysis.kospi_impact,
                "semiconductors": analysis.semiconductor_impact,
                "nasdaq100": analysis.nasdaq100_impact,
                "oil": analysis.oil_impact,
                "rates": analysis.rates_impact,
                "usdkrw": analysis.usdkrw_impact,
            },
            "rationale": analysis.rationale,
        },
    }


def serialize_market_outcome(row: object) -> dict[str, object]:
    return {
        "id": row.id,
        "session_date": row.session_date.isoformat(),
        "finalized_at": to_utc_iso(row.finalized_at),
        "source": row.source,
        "source_reference": row.source_reference,
        "kospi_prev_close": row.kospi_prev_close,
        "kospi_open": row.kospi_open,
        "kospi_close": row.kospi_close,
        "kospi_gap_pct": row.kospi_gap_pct,
        "kospi_close_return_pct": row.kospi_close_return_pct,
        "semiconductor_return_pct": row.semiconductor_return_pct,
        "is_final": row.is_final,
        "actual": {
            "kospi_up": row.kospi_close_return_pct > 0,
            "gap_up": row.kospi_gap_pct > 0,
            "up_close": row.kospi_close_return_pct > 0,
            "semiconductor_up": (
                None
                if row.semiconductor_return_pct is None
                else row.semiconductor_return_pct > 0
            ),
        },
        "details": decode_outcome_details(row),
    }


def serialize_backtest_row(evaluation: object, signal: object, outcome: object) -> dict[str, object]:
    return {
        "session_date": evaluation.session_date.isoformat(),
        "evaluated_at": to_utc_iso(evaluation.evaluated_at),
        "selection_rule": evaluation.selection_rule,
        "forecast": {
            "signal_run_id": signal.id,
            "forecast_at": to_utc_iso(signal.created_at),
            "age_to_open_minutes": evaluation.forecast_age_minutes,
            "engine_version": signal.engine_version,
            "kospi_score": signal.kospi_score,
            "semiconductor_score": signal.semiconductor_score,
            "gap_up_score": signal.gap_up_probability,
            "up_close_score": signal.up_close_probability,
            "confidence": signal.confidence,
            "data_completeness": signal.data_completeness,
            "calibrated": signal.calibrated,
        },
        "actual": {
            "source": outcome.source,
            "kospi_gap_pct": outcome.kospi_gap_pct,
            "kospi_close_return_pct": outcome.kospi_close_return_pct,
            "semiconductor_return_pct": outcome.semiconductor_return_pct,
            "kospi_up": evaluation.kospi_up_actual,
            "semiconductor_up": evaluation.semiconductor_up_actual,
            "gap_up": evaluation.gap_up_actual,
            "up_close": evaluation.up_close_actual,
        },
        "correct": {
            "kospi_direction": evaluation.kospi_correct,
            "semiconductor_direction": evaluation.semiconductor_correct,
            "gap_up": evaluation.gap_up_correct,
            "up_close": evaluation.up_close_correct,
        },
    }


def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as session:
        migrate_market_snapshot_business_time(session)
        migrate_market_snapshot_change_amount(session)
        migrate_stage3_symbols(session)
        latest_signal = session.scalar(
            select(MarketSignal).order_by(MarketSignal.id.desc()).limit(1)
        )
        if latest_signal is None:
            session.add(
                MarketSignal(
                    kospi_score=50.0,
                    semiconductor_score=50.0,
                    gap_up_probability=50.0,
                    source="stage1_dummy",
                )
            )
            session.commit()


collector_service = MarketCollectorService(
    YFinanceCollector(
        get_yahoo_mappings(
            allow_kospi200_index_proxy=ALLOW_KOSPI200_INDEX_PROXY,
        ),
        timeout_seconds=YFINANCE_TIMEOUT_SECONDS,
    ),
    enabled=COLLECTOR_ENABLED,
    poll_seconds=COLLECTOR_POLL_SECONDS,
    realtime_fallback_after_seconds=KIS_EFRIEND_FALLBACK_AFTER_SECONDS,
)

news_service = NewsCollectorService(
    GDELTNewsCollector(
        timeout_seconds=GDELT_TIMEOUT_SECONDS,
        lookback_hours=NEWS_LOOKBACK_HOURS,
        max_records_per_topic=NEWS_MAX_RECORDS_PER_TOPIC,
    ),
    enabled=NEWS_COLLECTOR_ENABLED,
    poll_seconds=NEWS_COLLECTOR_POLL_SECONDS,
)


ai_news_service = NewsAIService(
    OpenAINewsAnalyzer(
        api_key=OPENAI_API_KEY,
        model=OPENAI_MODEL,
        timeout_seconds=OPENAI_TIMEOUT_SECONDS,
    ),
    enabled=AI_NEWS_ENABLED,
    poll_seconds=AI_NEWS_POLL_SECONDS,
    batch_size=AI_NEWS_BATCH_SIZE,
)


dashboard_holdings_bootstrap = load_dashboard_holdings()
dashboard_quote_names = dict(dashboard_holdings_bootstrap.get("names", {}))
dashboard_quote_types = dict(dashboard_holdings_bootstrap.get("types", {}))
dashboard_quote_positions = dict(dashboard_holdings_bootstrap.get("positions", {}))
dashboard_quote_names_lock = Lock()
dashboard_universe_persist_lock = Lock()

def refresh_dashboard_quote_metadata() -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, float]],
]:
    # Serialize the read+publish sequence itself. If two worker threads read the
    # portfolio around an external file update, a slower stale read must not
    # publish after a newer one merely because the lock covered only assignment.
    with dashboard_quote_names_lock:
        latest = load_dashboard_holdings()
        if latest.get("source") == "portfolio":
            dashboard_quote_names.clear()
            dashboard_quote_names.update(dict(latest.get("names", {})))
            dashboard_quote_types.clear()
            dashboard_quote_types.update(dict(latest.get("types", {})))
            dashboard_quote_positions.clear()
            dashboard_quote_positions.update(dict(latest.get("positions", {})))
        return (
            dict(dashboard_quote_names),
            dict(dashboard_quote_types),
            {
                str(ticker): dict(position)
                for ticker, position in dashboard_quote_positions.items()
                if isinstance(position, dict)
            },
        )


def refresh_dashboard_quote_names() -> dict[str, str]:
    names, _types, _positions = refresh_dashboard_quote_metadata()
    return names


krx_quote_service = KrxQuoteService(
    bootstrap_tickers=dashboard_holdings_bootstrap.get("tickers", ()),
    krx_closed_dates=KIS_EFRIEND_KRX_CLOSED_DATES,
    krx_open_dates=KIS_EFRIEND_KRX_OPEN_DATES,
)


kis_efriend_bridge_service = KisEFriendBridgeService(
    history_interval_seconds=KIS_EFRIEND_HISTORY_INTERVAL_SECONDS,
    heartbeat_stale_seconds=KIS_EFRIEND_HEARTBEAT_STALE_SECONDS,
    expected_instrument_code=KIS_EFRIEND_KOSPI200_CODE,
    quote_service=krx_quote_service,
    krx_closed_dates=KIS_EFRIEND_KRX_CLOSED_DATES,
    krx_open_dates=KIS_EFRIEND_KRX_OPEN_DATES,
    krx_night_closed_dates=KIS_EFRIEND_KRX_NIGHT_CLOSED_DATES,
)


signal_service = SignalService(
    enabled=SIGNAL_ENGINE_ENABLED,
    poll_seconds=SIGNAL_ENGINE_POLL_SECONDS,
    news_lookback_hours=SIGNAL_NEWS_LOOKBACK_HOURS,
    minimum_data_weight=SIGNAL_MINIMUM_DATA_WEIGHT,
    ai_news_active=ai_news_service.active,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    await collector_service.start()
    await news_service.start()
    await ai_news_service.start()
    await signal_service.start()
    try:
        yield
    finally:
        await signal_service.stop()
        await ai_news_service.stop()
        await news_service.stop()
        await collector_service.stop()


app = FastAPI(
    title="Market AI API",
    version="0.12.2",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://tkfkd3226-cell.github.io",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

MONITOR_NO_STORE_PATHS = frozenset({
    "/monitor",
    "/monitor/",
    "/monitor/index.html",
    "/monitor/monitor.css",
    "/monitor/monitor.js",
})


@app.middleware("http")
async def disable_monitor_static_cache(request, call_next):
    response = await call_next(request)
    if request.url.path in MONITOR_NO_STORE_PATHS:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


monitor_directory = resolve_monitor_directory()
if monitor_directory.is_dir():
    app.mount(
        "/monitor",
        StaticFiles(directory=str(monitor_directory), html=True),
        name="monitor",
    )


@app.get("/api/health")
def health() -> dict[str, object]:
    try:
        check_database()
        return {
            "status": "ok",
            "database": "ok",
            "collector_enabled": collector_service.enabled,
            "collector_running": collector_service.status()["running"],
            "news_enabled": news_service.enabled,
            "news_running": news_service.status()["running"],
            "ai_enabled": ai_news_service.enabled,
            "ai_configured": ai_news_service.analyzer.configured,
            "ai_active": ai_news_service.active,
            "ai_state": ai_news_service.status()["state"],
            "ai_running": ai_news_service.status()["running"],
            "signal_enabled": signal_service.enabled,
            "signal_running": signal_service.status()["running"],
            "kis_efriend_bridge": kis_efriend_bridge_service.status(),
            "krx_quote_service": krx_quote_service.status(),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc


@app.get("/api/market-signal")
def market_signal() -> dict[str, object]:
    with SessionLocal() as session:
        stage6 = get_latest_signal_run(session)
        if stage6 is not None:
            return {
                "kospi_score": stage6.kospi_score,
                "semiconductor_score": stage6.semiconductor_score,
                "gap_up_probability": stage6.gap_up_probability,
                "up_close_probability": stage6.up_close_probability,
                "confidence": stage6.confidence,
                "data_completeness": stage6.data_completeness,
                "calibrated": stage6.calibrated,
                "calibration": serialize_signal_calibration(
                    get_signal_calibration(session, stage6.id)
                ),
                "source": stage6.engine_version,
                "updated_at": to_utc_iso(stage6.created_at),
            }

        signal = session.scalar(
            select(MarketSignal).order_by(MarketSignal.id.desc()).limit(1)
        )
        if signal is None:
            raise HTTPException(status_code=404, detail="market signal not found")

        return {
            "kospi_score": signal.kospi_score,
            "semiconductor_score": signal.semiconductor_score,
            "gap_up_probability": signal.gap_up_probability,
            "up_close_probability": None,
            "confidence": None,
            "data_completeness": None,
            "calibrated": False,
            "source": signal.source,
            "updated_at": to_utc_iso(signal.created_at),
        }


@app.get("/api/market-data/catalog")
def market_data_catalog() -> dict[str, object]:
    return {
        "count": len(MARKET_INSTRUMENTS),
        "items": list(MARKET_INSTRUMENTS),
    }


@app.get("/api/market-data/snapshot")
def market_data_snapshot() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        rows = [row for row in get_market_snapshot(session) if row.symbol in MARKET_SYMBOLS]
        return {
            "count": len(rows),
            "items": [serialize_market_snapshot_row(row, now) for row in rows],
        }


@app.get("/api/market-data/history/{symbol}")
def market_data_history(
    symbol: str,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, object]:
    if symbol not in MARKET_SYMBOLS:
        raise HTTPException(status_code=404, detail="unknown market symbol")

    with SessionLocal() as session:
        rows = get_market_history(session, symbol, limit=limit)
        return {
            "symbol": symbol,
            "count": len(rows),
            "items": [serialize_market_row(row) for row in rows],
        }


def load_dashboard_durable_closed_quotes(
    requested: tuple[str, ...],
    quote_types: dict[str, str],
    *,
    bridge_connected: bool,
    now: datetime,
) -> dict[str, KrxQuote]:
    if not bridge_connected or not requested:
        return {}

    closed_tickers = [
        ticker
        for ticker in requested
        if krx_quote_service.market_state_for_instrument(quote_types.get(ticker), now=now) == "closed"
    ]
    if not closed_tickers:
        return {}

    recovered: dict[str, KrxQuote] = {}
    with SessionLocal() as session:
        for ticker in closed_tickers:
            row = session.get(MarketSnapshot, f"KRX:{ticker}")
            if row is None:
                continue
            source = str(row.source or "")
            if source != f"kis-efriend:SC_R:{ticker}":
                continue
            observed_at = row.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            else:
                observed_at = observed_at.astimezone(timezone.utc)
            if observed_at.astimezone(KST).date() != latest_completed_krx_session_date(now):
                continue
            recovered[ticker] = KrxQuote(
                ticker=ticker,
                price=float(row.price),
                change_amount=None if row.change_amount is None else float(row.change_amount),
                change_pct=None if row.change_pct is None else float(row.change_pct),
                business_time=row.business_time,
                cumulative_volume=None,
                ask1=None,
                bid1=None,
                bridge_tick_count=None,
                observed_at=observed_at,
                source=source,
            )
    return recovered


@app.get("/api/market-data/krx-quotes")
def market_data_krx_quotes(
    tickers: str = Query(default="", max_length=1024),
    client_id: str = Query(default="", max_length=80),
) -> dict[str, object]:
    try:
        requested = krx_quote_service.parse_ticker_query(tickers)
        quote_names, quote_types, _quote_positions = refresh_dashboard_quote_metadata()
        bridge_status = kis_efriend_bridge_service.status()
        bridge_connected = bool(bridge_status["connected"])
        current = datetime.now(timezone.utc)
        durable_closed_quotes = load_dashboard_durable_closed_quotes(
            requested,
            quote_types,
            bridge_connected=bridge_connected,
            now=current,
        )
        snapshot = krx_quote_service.quote_snapshot(
            requested,
            bridge_connected=bridge_connected,
            client_id=client_id,
            ticker_types=quote_types,
            durable_closed_quotes=durable_closed_quotes,
            now=current,
        )
        # Remote/Tailscale quote GETs may change the in-memory Bridge universe, but they
        # must never become the durable restart bootstrap. Persist only the local
        # Dashboard lease union, and only when the caller itself is local.
        if not krx_quote_service.is_remote_client_id(client_id):
            # Persistence is intentionally outside the quote-service lock, but local
            # requests can still overlap in FastAPI worker threads. Serialize the file
            # write and re-read the *current* local union after acquiring that lock so an
            # older response cannot overwrite a newer local-tab universe.
            with dashboard_universe_persist_lock:
                local_dashboard_tickers = list(krx_quote_service.local_dashboard_tickers())
                try:
                    persist_dashboard_universe(
                        local_dashboard_tickers,
                        {
                            ticker: quote_names.get(ticker, ticker)
                            for ticker in local_dashboard_tickers
                        },
                        {
                            ticker: quote_types.get(ticker, "")
                            for ticker in local_dashboard_tickers
                        },
                    )
                except OSError as exc:
                    # The file is a restart warm-up aid, not the transaction owner
                    # for an already-admitted in-memory local lease. Returning 500
                    # here would report failure after state had legitimately changed.
                    logger.warning(
                        "Dashboard quote-universe persistence failed after admission; "
                        "continuing with in-memory state: %s",
                        exc,
                    )
        return snapshot
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/bridge/kis-efriend/quote-universe")
def kis_efriend_bridge_quote_universe() -> dict[str, object]:
    current = datetime.now(timezone.utc)
    quote_names, quote_types, quote_positions = refresh_dashboard_quote_metadata()
    universe = krx_quote_service.bridge_universe(now=current)
    dashboard_tickers = [str(value) for value in universe.get("dashboard_tickers", [])]
    symbols = ["FUTURES:KOSPI200", "INDEX:KOSPI"] + [
        f"KRX:{ticker}" for ticker in dashboard_tickers
    ]

    bridge_status = kis_efriend_bridge_service.status()
    bridge_connected = bool(bridge_status.get("connected"))
    quote_monitor = krx_quote_service.monitor_quotes(
        bridge_connected=bridge_connected,
        ticker_types=quote_types,
        now=current,
    )

    # The monitor endpoint is read-only: it must not create/renew Dashboard client leases.
    # Prefer process-memory realtime values, then use the throttled durable snapshot only
    # as restart/closed-session fallback. The 30-second DB write throttle is unchanged.
    live_snapshots: dict[str, dict[str, object]] = {}

    futures_price = bridge_status.get("price")
    futures_observed_at = bridge_status.get("last_received_at")
    if futures_price is not None and futures_observed_at:
        futures_service = str(bridge_status.get("service") or "-")
        futures_code = str(bridge_status.get("instrument_code") or "-")
        live_snapshots["FUTURES:KOSPI200"] = {
            "symbol": "FUTURES:KOSPI200",
            "price": futures_price,
            "change_pct": bridge_status.get("change_pct"),
            "source": f"kis-efriend:{futures_service}:{futures_code}",
            "observed_at": futures_observed_at,
            "business_time": bridge_status.get("business_time"),
            "session": bridge_status.get("session"),
        }

    market_realtime = bridge_status.get("market_realtime")
    if isinstance(market_realtime, dict):
        kospi_state = market_realtime.get("INDEX:KOSPI")
        if isinstance(kospi_state, dict):
            kospi_price = kospi_state.get("price")
            kospi_observed_at = kospi_state.get("last_received_at")
            if kospi_price is not None and kospi_observed_at:
                service = str(kospi_state.get("service") or "JUC_R")
                code = str(kospi_state.get("instrument_code") or "0001")
                live_snapshots["INDEX:KOSPI"] = {
                    "symbol": "INDEX:KOSPI",
                    "price": kospi_price,
                    "change_pct": kospi_state.get("change_pct"),
                    "source": f"kis-efriend:{service}:{code}",
                    "observed_at": kospi_observed_at,
                    "business_time": kospi_state.get("business_time"),
                    "session": "regular",
                }

    for item in quote_monitor.get("items", []):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol and item.get("price") is not None and item.get("observed_at"):
            live_snapshots[symbol] = dict(item)

    completed_cash_date = latest_completed_krx_session_date(current)
    durable_snapshots: dict[str, dict[str, object]] = {}
    with SessionLocal() as session:
        for symbol in symbols:
            row = session.get(MarketSnapshot, symbol)
            if row is None:
                continue
            if symbol == "INDEX:KOSPI" or symbol.startswith("KRX:"):
                observed_at = row.observed_at
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                if observed_at.astimezone(KST).date() != completed_cash_date:
                    continue
            snapshot = serialize_market_row(row)
            snapshot["snapshot_origin"] = "durable"
            if symbol == "INDEX:KOSPI":
                cash_state = krx_quote_service.market_state_for_instrument(None, now=current)
                snapshot["market_state"] = cash_state
                snapshot["state"] = "closed" if cash_state == "closed" else "stale"
            elif symbol.startswith("KRX:"):
                ticker = symbol.split(":", 1)[1]
                cash_state = krx_quote_service.market_state_for_instrument(
                    quote_types.get(ticker),
                    now=current,
                )
                snapshot["market_state"] = cash_state
                # A durable cache may be displayed while the current session is active,
                # but it is not a fresh post-restart tick. Only a closed session may
                # present that cache as the closing value.
                snapshot["state"] = "closed" if cash_state == "closed" else "stale"
            durable_snapshots[symbol] = snapshot

    monitor_snapshots = []
    for symbol in symbols:
        live = live_snapshots.get(symbol)
        durable = durable_snapshots.get(symbol)
        # A prior-session memory quote is intentionally stale after midnight, but
        # must not hide the verified latest-completed-session closing snapshot.
        if isinstance(live, dict) and isinstance(durable, dict):
            if live.get("subscription_state") in {"not_subscribed", "error"}:
                monitor_snapshots.append({
                    **durable,
                    "state": "unavailable",
                    "subscription_state": live.get("subscription_state"),
                    "subscription_error": live.get("subscription_error"),
                })
                continue
            if live.get("state") == "stale" and live.get("market_state") == "closed":
                monitor_snapshots.append(durable)
                continue
        if live or durable:
            monitor_snapshots.append(live or durable)

    display_prices: dict[str, float] = {}
    for ticker in dashboard_tickers:
        snapshot = (
            live_snapshots.get(f"KRX:{ticker}")
            or durable_snapshots.get(f"KRX:{ticker}")
        )
        if not isinstance(snapshot, dict):
            continue
        try:
            price = float(snapshot.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            display_prices[ticker] = price

    dashboard_display_tickers = order_dashboard_tickers_for_display(
        dashboard_tickers,
        quote_positions,
        display_prices,
        quote_names,
    )

    return {
        **universe,
        "bridge_connected": bridge_connected,
        "bridge_last_received_at": bridge_status.get("last_received_at"),
        "k200_market_open": bool(bridge_status.get("market_open")),
        "dashboard_display_tickers": dashboard_display_tickers,
        "dashboard_names": {
            ticker: quote_names.get(ticker, ticker)
            for ticker in dashboard_tickers
        },
        "dashboard_types": {
            ticker: quote_types.get(ticker, "")
            for ticker in dashboard_tickers
        },
        "dashboard_market_states": {
            ticker: krx_quote_service.market_state_for_instrument(
                quote_types.get(ticker),
                now=current,
            )
            for ticker in dashboard_tickers
        },
        "monitor_snapshots": monitor_snapshots,
    }


@app.get("/api/bridge/kis-efriend/status")
def kis_efriend_bridge_status() -> dict[str, object]:
    return kis_efriend_bridge_service.status()


@app.get("/api/bridge/kis-efriend/contract")
def kis_efriend_bridge_contract(
    at: datetime | None = Query(default=None),
) -> dict[str, object]:
    return kis_efriend_bridge_service.expected_contract(at)


@app.get("/api/bridge/kis-efriend/contract-code", response_class=PlainTextResponse)
def kis_efriend_bridge_contract_code() -> str:
    return str(kis_efriend_bridge_service.expected_contract()["instrument_code"])


@app.get("/api/bridge/kis-efriend/route")
def kis_efriend_bridge_route(
    at: datetime | None = Query(default=None),
) -> dict[str, object]:
    return kis_efriend_bridge_service.expected_route(at)


@app.get("/api/bridge/kis-efriend/route-code", response_class=PlainTextResponse)
def kis_efriend_bridge_route_code() -> str:
    route = kis_efriend_bridge_service.expected_route()
    service = route["service"] or "CLOSED"
    return f'{route["instrument_code"]}|{service}|{route["session"]}'


@app.post("/api/bridge/kis-efriend/tick")
def kis_efriend_bridge_tick(payload: KisEFriendTick) -> dict[str, object]:
    try:
        return kis_efriend_bridge_service.ingest_tick(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="failed to persist KIS eFriend tick") from exc


@app.post("/api/bridge/kis-efriend/market-tick")
def kis_efriend_bridge_market_tick(payload: KisEFriendMarketTick) -> dict[str, object]:
    try:
        return kis_efriend_bridge_service.ingest_market_tick(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="failed to persist KIS eFriend market tick") from exc


@app.post("/api/bridge/kis-efriend/heartbeat")
def kis_efriend_bridge_heartbeat(payload: KisEFriendHeartbeat) -> dict[str, object]:
    try:
        return kis_efriend_bridge_service.ingest_heartbeat(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/collector/status")
def collector_status() -> dict[str, object]:
    return collector_service.status()


@app.get("/api/collector/mappings")
def collector_mappings() -> dict[str, object]:
    items = []
    for mapping in collector_service.provider.mappings:
        items.append(
            {
                "symbol": mapping.symbol,
                "provider": collector_service.provider.source_name,
                "provider_symbol": mapping.provider_symbol,
                "enabled": mapping.provider_symbol is not None,
                "is_proxy": mapping.is_proxy,
                "note": mapping.note,
            }
        )

    return {
        "count": len(items),
        "items": items,
    }


@app.post("/api/collector/run-once")
async def collector_run_once() -> dict[str, object]:
    return await collector_service.run_once()


@app.get("/api/news/status")
def news_status() -> dict[str, object]:
    return news_service.status()


@app.get("/api/news/topics")
def news_topics() -> dict[str, object]:
    return {
        "count": len(NEWS_TOPICS),
        "items": [
            {
                "key": topic.key,
                "label": topic.label,
                "query": topic.query,
            }
            for topic in NEWS_TOPICS
        ],
    }


@app.get("/api/news/latest")
def news_latest(
    limit: int = Query(default=50, ge=1, le=200),
    topic: str | None = Query(default=None),
) -> dict[str, object]:
    if topic is not None and topic not in NEWS_TOPIC_KEYS:
        raise HTTPException(status_code=404, detail="unknown news topic")

    with SessionLocal() as session:
        articles = get_latest_news(session, limit=limit, topic=topic)
        topic_map = get_news_topics_for_articles(
            session,
            [article.id for article in articles],
        )
        return {
            "topic": topic,
            "count": len(articles),
            "items": [
                serialize_news_article(article, topic_map.get(article.id, []))
                for article in articles
            ],
        }


@app.post("/api/news/run-once")
async def news_run_once() -> dict[str, object]:
    return await news_service.run_once()


@app.get("/api/ai-news/status")
def ai_news_status() -> dict[str, object]:
    with SessionLocal() as session:
        counts = get_news_ai_counts(session)
        usage = get_ai_usage_summary(session)
    return {
        **ai_news_service.status(),
        "database": counts,
        "usage": usage,
    }


@app.get("/api/ai-news/categories")
def ai_news_categories() -> dict[str, object]:
    return {
        "count": len(AI_CATEGORIES),
        "items": list(AI_CATEGORIES),
    }


@app.get("/api/ai-news/latest")
def ai_news_latest(
    limit: int = Query(default=50, ge=1, le=200),
    category: str | None = Query(default=None),
) -> dict[str, object]:
    if category is not None and category not in AI_CATEGORIES:
        raise HTTPException(status_code=404, detail="unknown AI news category")

    with SessionLocal() as session:
        rows = get_latest_news_ai_analyses(
            session,
            limit=limit,
            category=category,
        )
        topic_map = get_news_topics_for_articles(
            session,
            [article.id for _, article in rows],
        )
        return {
            "category": category,
            "count": len(rows),
            "items": [
                serialize_news_ai_analysis(
                    analysis,
                    article,
                    topic_map.get(article.id, []),
                )
                for analysis, article in rows
            ],
        }


@app.post("/api/ai-news/run-once")
async def ai_news_run_once(
    limit: int | None = Query(default=None, ge=1, le=25),
) -> dict[str, object]:
    return await ai_news_service.run_once(limit=limit)

@app.get("/api/backtest/status")
def backtest_status() -> dict[str, object]:
    with SessionLocal() as session:
        counts = get_backtest_counts(session)
        latest_outcome = get_latest_market_outcome(session)
        latest_signal = get_latest_signal_run(session)
    return {
        "prediction_ledger": "signal_runs",
        "checkpoint": "latest signal before 09:00 KST",
        "max_forecast_age_hours": BACKTEST_MAX_FORECAST_AGE_HOURS,
        "actual_outcome_policy": (
            "Stage 8 stores only explicitly supplied, source-labeled market outcomes. "
            "It does not infer missing KOSPI open/close values from unrelated snapshots."
        ),
        "counts": counts,
        "latest_signal_at": None if latest_signal is None else to_utc_iso(latest_signal.created_at),
        "latest_outcome_date": (
            None if latest_outcome is None else latest_outcome.session_date.isoformat()
        ),
    }


@app.get("/api/backtest/forecasts")
def backtest_forecasts(
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, object]:
    with SessionLocal() as session:
        rows = get_signal_history(session, limit=limit)
        return {
            "count": len(rows),
            "ledger": "signal_runs",
            "items": [
                {
                    "signal_run_id": row.id,
                    "forecast_at": to_utc_iso(row.created_at),
                    "engine_version": row.engine_version,
                    "kospi_score": row.kospi_score,
                    "semiconductor_score": row.semiconductor_score,
                    "gap_up_score": row.gap_up_probability,
                    "up_close_score": row.up_close_probability,
                    "confidence": row.confidence,
                    "data_completeness": row.data_completeness,
                    "calibrated": row.calibrated,
                }
                for row in rows
            ],
        }


@app.get("/api/backtest/outcomes")
def backtest_outcomes(
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, object]:
    with SessionLocal() as session:
        rows = get_market_outcomes(session, limit=limit)
        return {
            "count": len(rows),
            "items": [serialize_market_outcome(row) for row in rows],
        }


@app.post("/api/backtest/outcomes")
def backtest_save_outcome(payload: MarketOutcomeInput) -> dict[str, object]:
    with SessionLocal() as session:
        outcome = upsert_market_outcome(
            session,
            session_date=payload.session_date,
            source=payload.source,
            source_reference=payload.source_reference,
            kospi_prev_close=payload.kospi_prev_close,
            kospi_open=payload.kospi_open,
            kospi_close=payload.kospi_close,
            semiconductor_return_pct=payload.semiconductor_return_pct,
            is_final=payload.is_final,
            details=payload.details,
        )
        evaluation = evaluate_outcome(
            session,
            outcome,
            max_forecast_age_hours=BACKTEST_MAX_FORECAST_AGE_HOURS,
        )
        session.commit()
        return {
            "outcome": serialize_market_outcome(outcome),
            "evaluation_created": evaluation.evaluation is not None,
            "evaluation_reason": evaluation.reason,
        }


@app.post("/api/backtest/evaluate")
def backtest_evaluate() -> dict[str, object]:
    with SessionLocal() as session:
        return evaluate_all_final_outcomes(
            session,
            max_forecast_age_hours=BACKTEST_MAX_FORECAST_AGE_HOURS,
        )


@app.get("/api/backtest/evaluations")
def backtest_evaluations(
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, object]:
    with SessionLocal() as session:
        rows = get_evaluation_dataset(session, limit=limit)
        return {
            "count": len(rows),
            "items": [serialize_backtest_row(*row) for row in rows],
        }


@app.get("/api/backtest/summary")
def backtest_summary(
    limit: int = Query(default=5000, ge=1, le=50000),
) -> dict[str, object]:
    with SessionLocal() as session:
        summary = build_backtest_summary(session, limit=limit)
    return {
        **summary,
        "max_forecast_age_hours": BACKTEST_MAX_FORECAST_AGE_HOURS,
    }


@app.get("/api/backtest/dataset")
def backtest_dataset(
    limit: int = Query(default=1000, ge=1, le=50000),
) -> dict[str, object]:
    with SessionLocal() as session:
        rows = get_evaluation_dataset(session, limit=limit)
        active = get_active_models(session, engine_version=ENGINE_VERSION)
        return {
            "count": len(rows),
            "calibration_ready": bool(active),
            "active_calibration_targets": sorted(active),
            "note": (
                "These are the raw evaluation rows used to train Stage 9 calibration. "
                "Active models are applied only to future SignalRuns and never retroactively."
            ),
            "items": [serialize_backtest_row(*row) for row in rows],
        }


@app.get("/api/calibration/status")
def calibration_status() -> dict[str, object]:
    with SessionLocal() as session:
        active = get_active_models(session, engine_version=ENGINE_VERSION)
        readiness = calibration_readiness(
            session,
            engine_version=ENGINE_VERSION,
            min_samples=CALIBRATION_MIN_SAMPLES,
            min_class_count=CALIBRATION_MIN_CLASS_COUNT,
        )
        return {
            "engine_version": ENGINE_VERSION,
            "method": CALIBRATION_METHOD,
            "config": {
                "min_samples": CALIBRATION_MIN_SAMPLES,
                "min_class_count": CALIBRATION_MIN_CLASS_COUNT,
                "bin_count": CALIBRATION_BIN_COUNT,
                "prior_strength": CALIBRATION_PRIOR_STRENGTH,
            },
            "targets": list(CALIBRATION_TARGETS),
            "active_target_count": len(active),
            "readiness": readiness,
            "active_models": {
                target: serialize_calibration_model(model, include_nodes=False)
                for target, model in active.items()
            },
            "safety": {
                "retroactive_application": False,
                "raw_signal_fields_preserved": True,
                "minimum_data_required": True,
            },
        }


@app.post("/api/calibration/train")
def calibration_train() -> dict[str, object]:
    with SessionLocal() as session:
        return train_all_targets(
            session,
            engine_version=ENGINE_VERSION,
            min_samples=CALIBRATION_MIN_SAMPLES,
            min_class_count=CALIBRATION_MIN_CLASS_COUNT,
            bin_count=CALIBRATION_BIN_COUNT,
            prior_strength=CALIBRATION_PRIOR_STRENGTH,
        )


@app.get("/api/calibration/models")
def calibration_models(
    limit: int = Query(default=50, ge=1, le=500),
    active_only: bool = Query(default=False),
    include_nodes: bool = Query(default=False),
) -> dict[str, object]:
    with SessionLocal() as session:
        rows = list_calibration_models(
            session,
            engine_version=ENGINE_VERSION,
            active_only=active_only,
            limit=limit,
        )
        return {
            "engine_version": ENGINE_VERSION,
            "count": len(rows),
            "items": [
                serialize_calibration_model(row, include_nodes=include_nodes)
                for row in rows
            ],
        }


@app.get("/api/calibration/performance")
def calibration_performance_api(
    limit: int = Query(default=5000, ge=1, le=20000),
) -> dict[str, object]:
    with SessionLocal() as session:
        return calibration_performance(session, limit=limit)


@app.get("/api/signal/status")
def signal_status() -> dict[str, object]:
    with SessionLocal() as session:
        latest = get_latest_signal_run(session)
    return {
        **signal_service.status(),
        "latest": None if latest is None else {
            "updated_at": to_utc_iso(latest.created_at),
            "engine_version": latest.engine_version,
            "kospi_score": latest.kospi_score,
            "semiconductor_score": latest.semiconductor_score,
            "gap_up_probability": latest.gap_up_probability,
            "up_close_probability": latest.up_close_probability,
            "confidence": latest.confidence,
            "data_completeness": latest.data_completeness,
            "calibrated": latest.calibrated,
            "calibration": serialize_signal_calibration(
                get_signal_calibration(session, latest.id)
            ),
        },
    }


@app.get("/api/signal/weights")
def signal_weights() -> dict[str, object]:
    return {
        "engine_version": ENGINE_VERSION,
        "calibrated": False,
        "probability_note": (
            "The four top-level Stage 6 fields are direct weighted 0-100 Rule Scores. "
            "Legacy field names are preserved for API/DB compatibility; Stage 9 calibrated "
            "probabilities are returned separately under the signal calibration object when eligible."
        ),
        "components": {
            key: {
                "label": spec.label,
                "symbols": list(spec.symbols),
                "scale_pct": spec.scale_pct,
                "invert": spec.invert,
            }
            for key, spec in COMPONENT_SPECS.items()
        },
        "weights": {
            "kospi": KOSPI_WEIGHTS,
            "semiconductors": SEMICONDUCTOR_WEIGHTS,
            "gap_up": GAP_UP_WEIGHTS,
            "up_close": UP_CLOSE_WEIGHTS,
        },
        "phase_policy": {
            "gap_up": {
                "preopen": GAP_UP_WEIGHTS,
                "intraday": "lock latest signal created before 09:00 KST",
                "post_close": "forecast the next KRX trading session",
            },
            "up_close": {
                "preopen": UP_CLOSE_PREOPEN_WEIGHTS,
                "intraday": UP_CLOSE_INTRADAY_WEIGHTS,
                "post_close": "actual KOSPI close direction once the 15:30+ snapshot is available",
            },
        },
    }


@app.get("/api/signal/latest")
def signal_latest(
    include_details: bool = Query(default=True),
) -> dict[str, object]:
    with SessionLocal() as session:
        row = get_latest_signal_run(session)
        if row is None:
            raise HTTPException(status_code=404, detail="signal run not found")
        payload = {
            "updated_at": to_utc_iso(row.created_at),
            "engine_version": row.engine_version,
            "kospi_score": row.kospi_score,
            "semiconductor_score": row.semiconductor_score,
            "gap_up_probability": row.gap_up_probability,
            "up_close_probability": row.up_close_probability,
            "confidence": row.confidence,
            "data_completeness": row.data_completeness,
            "calibrated": row.calibrated,
            "calibration": serialize_signal_calibration(
                get_signal_calibration(session, row.id)
            ),
        }
        if include_details:
            payload["details"] = decode_signal_details(row.details_json)
        return payload


@app.get("/api/signal/history")
def signal_history(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, object]:
    with SessionLocal() as session:
        rows = get_signal_history(session, limit=limit)
        calibration_map = get_signal_calibration_map(session, [row.id for row in rows])
        return {
            "count": len(rows),
            "items": [
                {
                    "updated_at": to_utc_iso(row.created_at),
                    "engine_version": row.engine_version,
                    "kospi_score": row.kospi_score,
                    "semiconductor_score": row.semiconductor_score,
                    "gap_up_probability": row.gap_up_probability,
                    "up_close_probability": row.up_close_probability,
                    "confidence": row.confidence,
                    "data_completeness": row.data_completeness,
                    "calibrated": row.calibrated,
                    "calibration": serialize_signal_calibration(calibration_map.get(row.id)),
                }
                for row in rows
            ],
        }


@app.post("/api/signal/run-once")
async def signal_run_once() -> dict[str, object]:
    return await signal_service.run_once()
