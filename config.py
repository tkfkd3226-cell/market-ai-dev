import os
from datetime import date

from dotenv import load_dotenv


load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError:
        return default

    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


COLLECTOR_ENABLED = _env_bool("MARKET_AI_COLLECTOR_ENABLED", True)
COLLECTOR_POLL_SECONDS = _env_int(
    "MARKET_AI_COLLECTOR_POLL_SECONDS",
    60,
    minimum=30,
)
YFINANCE_TIMEOUT_SECONDS = _env_int(
    "MARKET_AI_YFINANCE_TIMEOUT_SECONDS",
    15,
    minimum=5,
)
ALLOW_KOSPI200_INDEX_PROXY = _env_bool(
    "MARKET_AI_ALLOW_KOSPI200_INDEX_PROXY",
    False,
)

NEWS_COLLECTOR_ENABLED = _env_bool("MARKET_AI_NEWS_ENABLED", True)
NEWS_COLLECTOR_POLL_SECONDS = _env_int(
    "MARKET_AI_NEWS_POLL_SECONDS",
    300,
    minimum=60,
)
NEWS_LOOKBACK_HOURS = _env_int(
    "MARKET_AI_NEWS_LOOKBACK_HOURS",
    3,
    minimum=1,
    maximum=24,
)
NEWS_MAX_RECORDS_PER_TOPIC = _env_int(
    "MARKET_AI_NEWS_MAX_RECORDS_PER_TOPIC",
    75,
    minimum=10,
    maximum=250,
)
GDELT_TIMEOUT_SECONDS = _env_int(
    "MARKET_AI_GDELT_TIMEOUT_SECONDS",
    20,
    minimum=5,
    maximum=120,
)




def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default

    try:
        value = float(raw)
    except ValueError:
        return default

    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value



def _env_date_set(name: str) -> frozenset[date]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    values: set[date] = set()
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            values.add(date.fromisoformat(item))
        except ValueError:
            continue
    return frozenset(values)

def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


AI_NEWS_ENABLED = _env_bool("MARKET_AI_AI_ENABLED", False)
AI_NEWS_POLL_SECONDS = _env_int(
    "MARKET_AI_AI_POLL_SECONDS",
    60,
    minimum=30,
)
AI_NEWS_BATCH_SIZE = _env_int(
    "MARKET_AI_AI_BATCH_SIZE",
    10,
    minimum=1,
    maximum=25,
)
OPENAI_API_KEY = _env_str("OPENAI_API_KEY")
AI_NEWS_ACTIVE = AI_NEWS_ENABLED and bool(OPENAI_API_KEY)
OPENAI_MODEL = _env_str("MARKET_AI_OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_TIMEOUT_SECONDS = _env_int(
    "MARKET_AI_OPENAI_TIMEOUT_SECONDS",
    45,
    minimum=10,
    maximum=180,
)

SIGNAL_ENGINE_ENABLED = _env_bool("MARKET_AI_SIGNAL_ENABLED", True)
SIGNAL_ENGINE_POLL_SECONDS = _env_int(
    "MARKET_AI_SIGNAL_POLL_SECONDS",
    60,
    minimum=30,
)
SIGNAL_NEWS_LOOKBACK_HOURS = _env_int(
    "MARKET_AI_SIGNAL_NEWS_LOOKBACK_HOURS",
    12,
    minimum=1,
    maximum=72,
)
SIGNAL_MINIMUM_DATA_WEIGHT = _env_float(
    "MARKET_AI_SIGNAL_MIN_DATA_WEIGHT",
    0.35,
    minimum=0.10,
    maximum=1.0,
)


BACKTEST_MAX_FORECAST_AGE_HOURS = _env_int(
    "MARKET_AI_BACKTEST_MAX_FORECAST_AGE_HOURS",
    12,
    minimum=1,
    maximum=72,
)


CALIBRATION_MIN_SAMPLES = _env_int(
    "MARKET_AI_CALIBRATION_MIN_SAMPLES",
    30,
    minimum=10,
    maximum=5000,
)
CALIBRATION_MIN_CLASS_COUNT = _env_int(
    "MARKET_AI_CALIBRATION_MIN_CLASS_COUNT",
    5,
    minimum=1,
    maximum=500,
)
CALIBRATION_BIN_COUNT = _env_int(
    "MARKET_AI_CALIBRATION_BIN_COUNT",
    5,
    minimum=2,
    maximum=20,
)
CALIBRATION_PRIOR_STRENGTH = _env_float(
    "MARKET_AI_CALIBRATION_PRIOR_STRENGTH",
    8.0,
    minimum=0.0,
    maximum=100.0,
)

# KIS eFriend local bridge. The bridge runs on the same PC and posts verified KOSPI200
# futures plus KOSPI/Samsung/SK hynix realtime ticks to localhost:8001. History is sampled
# more slowly than live snapshots to keep SQLite growth bounded while preserving freshness.
KIS_EFRIEND_HISTORY_INTERVAL_SECONDS = _env_int(
    "MARKET_AI_KIS_HISTORY_INTERVAL_SECONDS",
    60,
    minimum=5,
    maximum=3600,
)
KIS_EFRIEND_HEARTBEAT_STALE_SECONDS = _env_int(
    "MARKET_AI_KIS_HEARTBEAT_STALE_SECONDS",
    30,
    minimum=10,
    maximum=300,
)
KIS_EFRIEND_FALLBACK_AFTER_SECONDS = _env_int(
    "MARKET_AI_KIS_FALLBACK_AFTER_SECONDS",
    90,
    minimum=30,
    maximum=900,
)
KIS_EFRIEND_KOSPI200_CODE = _env_str(
    "MARKET_AI_KIS_KOSPI200_CODE",
    "",
).upper()

KIS_EFRIEND_KRX_CLOSED_DATES = _env_date_set("MARKET_AI_KRX_CLOSED_DATES")
KIS_EFRIEND_KRX_OPEN_DATES = _env_date_set("MARKET_AI_KRX_OPEN_DATES")
KIS_EFRIEND_KRX_NIGHT_CLOSED_DATES = _env_date_set(
    "MARKET_AI_KRX_NIGHT_CLOSED_DATES"
)
