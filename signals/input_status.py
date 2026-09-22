"""Session-aware input availability; elapsed wall time is not a quality score."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from bridges.kospi200_contract import (
    KST, KOSPI200_DAY_START, KOSPI200_DAY_END,
    KOSPI200_NIGHT_START, KOSPI200_NIGHT_END, is_krx_trading_day,
    resolve_kospi200_front_month,
)
from config import (
    KIS_EFRIEND_FALLBACK_AFTER_SECONDS,
    KIS_EFRIEND_KOSPI200_CODE,
    KIS_EFRIEND_KRX_CLOSED_DATES, KIS_EFRIEND_KRX_OPEN_DATES,
    KIS_EFRIEND_KRX_NIGHT_CLOSED_DATES,
)

UTC = timezone.utc
POLICY_VERSION = "session_input_v3"
# Operational allowances, not claims about provider latency or price accuracy.
YAHOO_MAX_LAG_SECONDS = 20 * 60
FX_MAX_LAG_SECONDS = 30 * 60
FINAL_BAR_ALLOWANCE_SECONDS = 120
# KOSPI200 actual KIS futures can legitimately have no trade in the final few
# minutes.  Treat only a source/session-matched tick from the final 30 minutes
# of the completed session as its closing candidate; older in-session ticks stay
# unavailable so a mid-session Bridge outage is not silently promoted to close.
KOSPI200_FINAL_TICK_ALLOWANCE_SECONDS = 30 * 60


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def observation_time(row) -> datetime:
    observed = as_utc(row.observed_at)
    # KIS bootstrap can arrive later than the actual exchange event.
    business = getattr(row, "business_time", None)
    if row.source.startswith("kis-efriend:") and business:
        try:
            clock = datetime.strptime(business, "%H%M%S").time()
            local = observed.astimezone(KST)
            candidate = datetime.combine(local.date(), clock, KST)
            if candidate > local + timedelta(minutes=1):
                candidate -= timedelta(days=1)
            observed = candidate.astimezone(UTC)
        except (ValueError, TypeError):
            pass
    return observed


@lru_cache(maxsize=3)
def _calendar(name: str):
    return xcals.get_calendar(name)


def _at(day: date, clock: time, tz=KST) -> datetime:
    return datetime.combine(day, clock, tz).astimezone(UTC)


@lru_cache(maxsize=64)
def _windows(market: str, anchor: date) -> tuple[tuple[datetime, datetime], ...]:
    """Recent completed windows plus the current/next window, including holidays."""
    first, last = anchor - timedelta(days=20), anchor + timedelta(days=2)
    result = []
    if market in {"krx_cash", "krx_futures"}:
        day = first
        while day <= last:
            opened, _ = is_krx_trading_day(
                day, closed_dates=KIS_EFRIEND_KRX_CLOSED_DATES,
                open_dates=KIS_EFRIEND_KRX_OPEN_DATES,
            )
            if opened:
                if market == "krx_cash":
                    result.append((_at(day, time(9)), _at(day, time(15, 30))))
                else:
                    result.append((_at(day, KOSPI200_DAY_START), _at(day, KOSPI200_DAY_END)))
                    if day not in KIS_EFRIEND_KRX_NIGHT_CLOSED_DATES:
                        result.append((_at(day, KOSPI200_NIGHT_START),
                                       _at(day + timedelta(days=1), KOSPI200_NIGHT_END)))
            day += timedelta(days=1)
    elif market in {"us_cash", "nq_futures"}:
        cal = _calendar("XNYS" if market == "us_cash" else "CMES")
        sessions = cal.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))
        for session in sessions:
            start = cal.session_open(session).to_pydatetime()
            end = cal.session_close(session).to_pydatetime()
            if market == "nq_futures":
                # Generic CMES closes at 17 CT. NQ has a 16-17 CT maintenance
                # break; retain the calendar's earlier holiday close, if any.
                end = min(end, _at(session.date(), time(16), ZoneInfo("America/Chicago")))
            result.append((start, end))
    elif market == "fx":
        # Yahoo KRW=X is a 24/5 indicative FX feed, not KRX cash trading.
        day = first
        while day <= last:
            if day.weekday() == 6:
                tz = ZoneInfo("America/New_York")
                result.append((_at(day, time(17), tz), _at(day + timedelta(days=5), time(17), tz)))
            day += timedelta(days=1)
    return tuple(sorted(result))


def _kis_kospi200_source_identity(row) -> tuple[str, str] | None:
    source = str(getattr(row, "source", "") or "").strip()
    if ":proxy" in source:
        return None
    parts = source.split(":")
    if len(parts) < 4 or parts[0] != "kis-efriend":
        return None
    session_name, service, instrument_code = parts[1], parts[2], parts[3].strip().upper()
    if not instrument_code:
        return None
    if session_name == "day" and service == "FC_R":
        return "day", instrument_code
    if session_name == "night" and service == "CMEC_R":
        return "night", instrument_code
    return None


def _kospi200_window_session(window: tuple[datetime, datetime]) -> str | None:
    start, _ = window
    local_start = start.astimezone(KST).time().replace(tzinfo=None)
    if local_start == KOSPI200_DAY_START:
        return "day"
    if local_start == KOSPI200_NIGHT_START:
        return "night"
    return None


def _expected_kospi200_close_instrument(previous) -> str | None:
    if previous is None:
        return None
    override = str(KIS_EFRIEND_KOSPI200_CODE or "").strip().upper()
    if override:
        return override
    _, end = previous
    # Resolve the canonical contract at the completed session's actual close.
    # On expiry day this intentionally lands after the 15:20 rollover so an
    # expiring-contract tick from 15:15-15:19 cannot masquerade as 15:45 close.
    try:
        resolution = resolve_kospi200_front_month(
            end - timedelta(microseconds=1),
            closed_dates=KIS_EFRIEND_KRX_CLOSED_DATES,
            open_dates=KIS_EFRIEND_KRX_OPEN_DATES,
        )
    except Exception:
        return None
    return str(resolution.instrument_code or "").strip().upper() or None


def _has_verified_kospi200_close_tick(row, observed: datetime, previous) -> bool:
    """Accept a route/contract verified KIS tick from the completed session tail.

    K200 futures do not guarantee a trade exactly at 15:45/06:00. A close
    candidate therefore may come from the final 30 minutes, but its FC_R/CMEC_R
    route *and* instrument code must match the canonical contract at that
    completed session's close. This preserves the 15:20 expiry-day rollover and
    still fails closed for a Bridge outage well before close.
    """
    if previous is None:
        return False
    source_identity = _kis_kospi200_source_identity(row)
    expected_session = _kospi200_window_session(previous)
    expected_instrument = _expected_kospi200_close_instrument(previous)
    if source_identity is None or expected_session is None or expected_instrument is None:
        return False
    source_session, source_instrument = source_identity
    if source_session != expected_session or source_instrument != expected_instrument:
        return False
    start, end = previous
    tail_start = max(start, end - timedelta(seconds=KOSPI200_FINAL_TICK_ALLOWANCE_SECONDS))
    return tail_start <= observed < end


def input_status(symbol: str, row, now: datetime) -> dict[str, object]:
    now = as_utc(now)
    observed = observation_time(row)
    market = (
        "krx_futures" if symbol == "FUTURES:KOSPI200" else
        "krx_cash" if symbol == "INDEX:KOSPI" or symbol.startswith("KRX:") else
        "us_cash" if symbol == "INDEX:SOX" or symbol.startswith("NASDAQ:") else
        "nq_futures" if symbol == "FUTURES:NQ" else
        "fx" if symbol == "FX:USDKRW" else "unknown"
    )
    realtime = row.source.startswith("kis-efriend:")
    lag = (max(30, KIS_EFRIEND_FALLBACK_AFTER_SECONDS) if realtime else
           FX_MAX_LAG_SECONDS if market == "fx" else YAHOO_MAX_LAG_SECONDS)
    age = (now - observed).total_seconds()
    info = {
        "policy": POLICY_VERSION, "market": market,
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "age_seconds": round(max(0, age), 1), "max_lag_seconds": lag,
    }

    def state(available: bool, status: str, reason: str):
        return {**info, "available": available, "status": status, "reason": reason}

    if age < -60:
        return state(False, "invalid_time", "관측 시각이 현재보다 미래입니다.")
    try:
        windows = _windows(market, now.date())
    except Exception:
        return state(False, "calendar_unknown", "거래 세션을 확인할 수 없습니다.")
    active = next(((start, end) for start, end in windows if start <= now < end), None)
    completed = [(start, end) for start, end in windows if end <= now]
    previous = completed[-1] if completed else None
    # A closed cash index needs the closing event; Yahoo bars may be stamped
    # at the start of the final minute. Futures may have no tick at exact close.
    final_allowance = 0 if realtime and market == "krx_cash" else FINAL_BAR_ALLOWANCE_SECONDS
    verified_kospi200_close = bool(
        realtime
        and market == "krx_futures"
        and _has_verified_kospi200_close_tick(row, observed, previous)
    )
    has_previous_close = bool(
        previous
        and (
            verified_kospi200_close
            if realtime and market == "krx_futures"
            else observed >= previous[1] - timedelta(seconds=final_allowance)
        )
    )
    if active:
        start, end = active
        info.update(session_open=start.isoformat(), session_close=end.isoformat())
        if observed >= start and age <= lag:
            return state(True, "realtime" if realtime else "within_delay", "허용 수신 지연 범위입니다.")
        if now < start + timedelta(seconds=lag) and observed < start and has_previous_close:
            return state(True, "awaiting_session", "새 세션의 첫 수신 대기 중이며 직전 마감값을 사용합니다.")
        return state(False, "stale", "현재 거래 세션의 입력이 없거나 허용 지연을 초과했습니다.")
    if previous:
        info["session_close"] = previous[1].isoformat()
        if has_previous_close:
            reason = (
                "최근 종료 KOSPI200 세션의 검증된 실제 KIS 마지막 구간 값입니다."
                if verified_kospi200_close
                else "최근 종료 세션의 마감 부근 또는 이후 관측값입니다."
            )
            return state(True, "closed_latest", reason)
        if now - previous[1] <= timedelta(seconds=lag) and age <= lag:
            return state(True, "closing_pending", "장 종료 직후 마감 데이터 수신을 기다리고 있습니다.")
        return state(False, "missing_close", "최근 종료 세션의 마감 데이터가 없습니다.")
    return state(False, "calendar_unknown", "사용 가능한 거래 세션을 확인할 수 없습니다.")
