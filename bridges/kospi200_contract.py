from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Iterable

import pandas as pd
import exchange_calendars as xcals
from korean_lunar_calendar import KoreanLunarCalendar


KST = timezone(timedelta(hours=9), name="KST")
KOSPI200_PRODUCT_PREFIX = "01"
KOSPI200_QUARTER_MONTHS = (3, 6, 9, 12)
KOSPI200_DAY_START = time(8, 45)
KOSPI200_DAY_END = time(15, 45)
KOSPI200_NIGHT_START = time(18, 0)
KOSPI200_NIGHT_END = time(6, 0)
KOSPI200_EXPIRY_DAY_END = time(15, 20)

# Legacy year letters are retained for deterministic historical/backtest helpers.
# KIS' new short-code scheme is used for expiry years 2026 and later.
_LEGACY_YEAR_CODES = {
    2024: "V",
    2025: "W",
}


@dataclass(frozen=True)
class Kospi200ContractResolution:
    instrument_code: str
    trade_date: date
    expiry_year: int
    expiry_month: int
    nominal_last_trading_day: date
    actual_last_trading_day: date
    calendar_source: str


@dataclass(frozen=True)
class Kospi200SessionResolution:
    service: str | None
    session: str
    market_open: bool
    session_start_date: date | None
    calendar_source: str


def _as_kst(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).astimezone(KST)
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def second_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_until_thursday = (3 - first.weekday()) % 7
    return first + timedelta(days=days_until_thursday + 7)


def kospi200_trade_date(value: datetime | None = None) -> date:
    """Return the KRX trade date that owns the current KOSPI200 futures session.

    KRX night trading starts at 18:00 and belongs to the date on which that
    session ends. Therefore a session beginning at 18:00 on T is part of T+1.
    """

    local = _as_kst(value)
    if local.time() >= KOSPI200_NIGHT_START:
        return local.date() + timedelta(days=1)
    return local.date()


def _normalize_dates(values: Iterable[date] | None) -> frozenset[date]:
    return frozenset(values or ())


@lru_cache(maxsize=1)
def _xkrx_calendar():
    return xcals.get_calendar("XKRX")


def _xkrx_session(day: date) -> bool | None:
    """Return an authoritative XKRX answer when the bundled calendar covers day.

    exchange_calendars ships explicit KRX schedules only through its supported
    range. Outside that range we deliberately fall back to the deterministic
    Korean public-holiday calculator below instead of guessing that every
    weekday is open.
    """

    cal = _xkrx_calendar()
    ts = pd.Timestamp(day.isoformat())
    if ts < cal.first_session or ts > cal.last_session:
        return None
    return bool(cal.is_session(ts))


def _solar_from_lunar(year: int, month: int, day: int) -> date:
    calendar = KoreanLunarCalendar()
    if not calendar.setLunarDate(year, month, day, False):
        raise ValueError(f"unable to convert Korean lunar date {year}-{month:02d}-{day:02d}")
    return date.fromisoformat(calendar.SolarIsoFormat())


def _next_non_holiday(start: date, holidays: set[date]) -> date:
    candidate = start
    while candidate.weekday() >= 5 or candidate in holidays:
        candidate += timedelta(days=1)
    return candidate


@lru_cache(maxsize=64)
def _fallback_korean_public_holidays(year: int) -> frozenset[date]:
    """Build predictable Korean public holidays for dates outside XKRX coverage.

    This fallback covers statutory fixed holidays, Seollal, Buddha's Birthday,
    Chuseok and their ordinary substitute-holiday rules, plus KRX-specific
    closures for Labor Day (May 1) and the year-end closing day. One-off exchange
    closures (temporary holidays/elections) can be supplied through the
    MARKET_AI_KRX_CLOSED_DATES override and take precedence over this set.
    """

    holidays: set[date] = {
        date(year, 1, 1),
        date(year, 3, 1),
        date(year, 5, 1),  # KRX Labor Day closure
        date(year, 5, 5),
        date(year, 6, 6),
        date(year, 8, 15),
        date(year, 10, 3),
        date(year, 10, 9),
        date(year, 12, 25),
    }

    seollal = _solar_from_lunar(year, 1, 1)
    seollal_group = {seollal - timedelta(days=1), seollal, seollal + timedelta(days=1)}
    chuseok = _solar_from_lunar(year, 8, 15)
    chuseok_group = {chuseok - timedelta(days=1), chuseok, chuseok + timedelta(days=1)}
    buddha = _solar_from_lunar(year, 4, 8)
    holidays.update(seollal_group)
    holidays.update(chuseok_group)
    holidays.add(buddha)

    # Lunar New Year and Chuseok receive a substitute holiday when the holiday
    # block overlaps Sunday. This is enough to preserve the trading-day boundary
    # around quarterly expiries; XKRX remains the preferred source where covered.
    for group in (seollal_group, chuseok_group):
        if any(day.weekday() == 6 for day in group):
            holidays.add(_next_non_holiday(max(group) + timedelta(days=1), holidays))

    # Children's Day has had weekend substitution for the whole range relevant
    # to this project. From 2021, the listed national holidays below also use
    # substitutes; Buddha's Birthday and Christmas joined from 2023.
    substitute_candidates: list[date] = [date(year, 5, 5)]
    if year >= 2021:
        substitute_candidates.extend(
            [
                date(year, 3, 1),
                date(year, 8, 15),
                date(year, 10, 3),
                date(year, 10, 9),
            ]
        )
    if year >= 2023:
        substitute_candidates.extend([buddha, date(year, 12, 25)])

    for holiday in substitute_candidates:
        if holiday.weekday() >= 5:
            holidays.add(_next_non_holiday(holiday + timedelta(days=1), holidays))

    # KRX year-end closure is Dec 31. If Dec 31 is already a holiday or
    # Saturday/Sunday, the closure moves to the immediately preceding trading
    # day. Resolve that day against the public-holiday set built above.
    year_end = date(year, 12, 31)
    if year_end.weekday() >= 5 or year_end in holidays:
        candidate = year_end - timedelta(days=1)
        while candidate.weekday() >= 5 or candidate in holidays:
            candidate -= timedelta(days=1)
        year_end = candidate
    holidays.add(year_end)

    return frozenset(holidays)


def is_krx_trading_day(
    day: date,
    *,
    closed_dates: Iterable[date] | None = None,
    open_dates: Iterable[date] | None = None,
) -> tuple[bool, str]:
    closed = _normalize_dates(closed_dates)
    opened = _normalize_dates(open_dates)
    if day in opened:
        return True, "open_override"
    if day in closed:
        return False, "closed_override"

    answer = _xkrx_session(day)
    if answer is not None:
        return answer, "xkrx"

    if day.weekday() >= 5:
        return False, "korean_holiday_fallback"
    if day in _fallback_korean_public_holidays(day.year):
        return False, "korean_holiday_fallback"
    return True, "korean_holiday_fallback"


def resolve_kospi200_market_session(
    value: datetime | None = None,
    *,
    closed_dates: Iterable[date] | None = None,
    open_dates: Iterable[date] | None = None,
    night_closed_dates: Iterable[date] | None = None,
) -> Kospi200SessionResolution:
    """Resolve the live FC_R / CMEC_R session using KRX holiday rules.

    Regular trading is open only on a KRX trading day. Night-session holidays
    are decided by the calendar date on which the night session begins. Thus a
    Thursday 18:00 session may continue into a Friday holiday until 06:00, while
    a night session scheduled to begin on the holiday itself remains closed.
    """

    local = _as_kst(value)
    current_time = local.time()
    night_closed = _normalize_dates(night_closed_dates)

    if KOSPI200_DAY_START <= current_time < KOSPI200_DAY_END:
        start_date = local.date()
        is_open, source = is_krx_trading_day(
            start_date,
            closed_dates=closed_dates,
            open_dates=open_dates,
        )
        if is_open:
            return Kospi200SessionResolution(
                service="FC_R",
                session="day",
                market_open=True,
                session_start_date=start_date,
                calendar_source=source,
            )
        return Kospi200SessionResolution(
            service=None,
            session="closed",
            market_open=False,
            session_start_date=start_date,
            calendar_source=source,
        )

    if current_time >= KOSPI200_NIGHT_START or current_time < KOSPI200_NIGHT_END:
        start_date = (
            local.date()
            if current_time >= KOSPI200_NIGHT_START
            else local.date() - timedelta(days=1)
        )
        if start_date in night_closed:
            return Kospi200SessionResolution(
                service=None,
                session="closed",
                market_open=False,
                session_start_date=start_date,
                calendar_source="night_closed_override",
            )
        is_open, source = is_krx_trading_day(
            start_date,
            closed_dates=closed_dates,
            open_dates=open_dates,
        )
        if is_open:
            return Kospi200SessionResolution(
                service="CMEC_R",
                session="night",
                market_open=True,
                session_start_date=start_date,
                calendar_source=source,
            )
        return Kospi200SessionResolution(
            service=None,
            session="closed",
            market_open=False,
            session_start_date=start_date,
            calendar_source=source,
        )

    return Kospi200SessionResolution(
        service=None,
        session="closed",
        market_open=False,
        session_start_date=None,
        calendar_source="time_window",
    )


def actual_last_trading_day(
    year: int,
    month: int,
    *,
    closed_dates: Iterable[date] | None = None,
    open_dates: Iterable[date] | None = None,
) -> tuple[date, str]:
    candidate = second_thursday(year, month)
    sources: list[str] = []
    while True:
        is_open, source = is_krx_trading_day(
            candidate,
            closed_dates=closed_dates,
            open_dates=open_dates,
        )
        sources.append(source)
        if is_open:
            # Prefer the source that made a closure decision when a preceding
            # holiday moved expiry earlier; otherwise report the final-day source.
            if "closed_override" in sources:
                calendar_source = "closed_override"
            elif "xkrx" in sources:
                calendar_source = "xkrx"
            else:
                calendar_source = sources[-1]
            return candidate, calendar_source
        candidate -= timedelta(days=1)


def front_month_for_trade_date(
    trade_date: date,
    *,
    closed_dates: Iterable[date] | None = None,
    open_dates: Iterable[date] | None = None,
) -> tuple[int, int]:
    """Date-only compatibility helper.

    The date alone cannot express the 15:20 expiry cutoff. Call
    resolve_kospi200_front_month() for live routing.
    """

    year = trade_date.year
    month = trade_date.month
    while True:
        if month in KOSPI200_QUARTER_MONTHS:
            last_day, _ = actual_last_trading_day(
                year,
                month,
                closed_dates=closed_dates,
                open_dates=open_dates,
            )
            if trade_date <= last_day:
                return year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def kospi200_short_code(expiry_year: int, expiry_month: int) -> str:
    if expiry_month not in KOSPI200_QUARTER_MONTHS:
        raise ValueError("KOSPI200 futures expiry month must be 3, 6, 9, or 12")

    legacy_year = _LEGACY_YEAR_CODES.get(expiry_year)
    if legacy_year is not None:
        return f"1{KOSPI200_PRODUCT_PREFIX}{legacy_year}{expiry_month:02d}"
    if expiry_year < 2024:
        raise ValueError("unsupported historical KOSPI200 futures short-code year")
    return f"A{KOSPI200_PRODUCT_PREFIX}{expiry_year % 10}{expiry_month:02d}"


def resolve_kospi200_front_month(
    value: datetime | None = None,
    *,
    closed_dates: Iterable[date] | None = None,
    open_dates: Iterable[date] | None = None,
) -> Kospi200ContractResolution:
    local = _as_kst(value)
    trade_date = kospi200_trade_date(local)
    year = trade_date.year
    month = trade_date.month

    while True:
        if month in KOSPI200_QUARTER_MONTHS:
            nominal = second_thursday(year, month)
            actual, calendar_source = actual_last_trading_day(
                year,
                month,
                closed_dates=closed_dates,
                open_dates=open_dates,
            )
            use_contract = trade_date < actual
            if trade_date == actual:
                # A night session that starts on the previous calendar day and
                # belongs to expiry trade-date still uses the expiring contract.
                # On the actual expiry calendar day, KRX ends that contract at
                # 15:20; the next listed month remains tradable until 15:45.
                before_cutoff = local.date() != actual or local.time() < KOSPI200_EXPIRY_DAY_END
                use_contract = before_cutoff

            if use_contract:
                return Kospi200ContractResolution(
                    instrument_code=kospi200_short_code(year, month),
                    trade_date=trade_date,
                    expiry_year=year,
                    expiry_month=month,
                    nominal_last_trading_day=nominal,
                    actual_last_trading_day=actual,
                    calendar_source=calendar_source,
                )

        month += 1
        if month > 12:
            month = 1
            year += 1
