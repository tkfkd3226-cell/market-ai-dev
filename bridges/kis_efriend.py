from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Literal

from bridges.krx_quotes import KrxQuoteService
from bridges.kospi200_contract import (
    resolve_kospi200_front_month,
    resolve_kospi200_market_session,
)

from pydantic import BaseModel, Field
from sqlalchemy import select

from db.database import SessionLocal
from db.market_repository import save_market_observation
from db.models import MarketPrice, MarketSnapshot


KOSPI200_SYMBOL = "FUTURES:KOSPI200"
KIS_SOURCE_PREFIX = "kis-efriend"
DYNAMIC_KRX_SNAPSHOT_INTERVAL_SECONDS = 30

KIS_EFRIEND_REALTIME_INSTRUMENTS = {
    "INDEX:KOSPI": {"service": "JUC_R", "instrument_code": "0001"},
    "KRX:005930": {"service": "SC_R", "instrument_code": "005930"},
    "KRX:000660": {"service": "SC_R", "instrument_code": "000660"},
}


class KisEFriendTick(BaseModel):
    instrument_code: str = Field(min_length=1, max_length=9)
    service: Literal["FC_R", "CMEC_R"]
    session: Literal["day", "night"]
    business_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3])[0-5]\d[0-5]\d$")
    price: float = Field(gt=0)
    change_pct: float | None = Field(default=None, ge=-100, le=100)
    cumulative_volume: int | None = Field(default=None, ge=0)
    ask1: float | None = Field(default=None, ge=0)
    bid1: float | None = Field(default=None, ge=0)
    sent_at: datetime | None = None
    tick_count: int | None = Field(default=None, ge=0)


class KisEFriendMarketTick(BaseModel):
    symbol: str = Field(pattern=r"^(?:INDEX:KOSPI|KRX:[0-9A-Z]{6})$")
    instrument_code: str = Field(min_length=1, max_length=9)
    service: Literal["JUC_R", "SC_R"]
    business_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3])[0-5]\d[0-5]\d$")
    price: float = Field(gt=0)
    change_amount: float | None = None
    change_pct: float | None = Field(default=None, ge=-100, le=100)
    cumulative_volume: int | None = Field(default=None, ge=0)
    ask1: float | None = Field(default=None, ge=0)
    bid1: float | None = Field(default=None, ge=0)
    sent_at: datetime | None = None
    tick_count: int | None = Field(default=None, ge=0)


class KisEFriendQuoteSubscription(BaseModel):
    ticker: str = Field(pattern=r"^[0-9A-Z]{6}$")
    subscribed: bool
    last_tick_at: datetime | None = None
    tick_count: int = Field(default=0, ge=0)
    forward_success_count: int = Field(default=0, ge=0)
    last_forwarded_tick_count: int | None = Field(default=None, ge=0)
    last_error: str | None = Field(default=None, max_length=512)


class KisEFriendHeartbeat(BaseModel):
    instrument_code: str = Field(min_length=1, max_length=9)
    service: Literal["FC_R", "CMEC_R"] | None = None
    session: Literal["day", "night", "closed"]
    bridge_time: datetime
    last_tick_at: datetime | None = None
    tick_count: int = Field(default=0, ge=0)
    quote_subscriptions: list[KisEFriendQuoteSubscription] | None = Field(default=None, max_length=64)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


class KisEFriendBridgeService:
    """Accept localhost eFriend Expert ticks and persist verified realtime market data.

    KOSPI200 keeps its server-resolved day/night route validation. KOSPI, Samsung Electronics,
    and SK hynix use fixed Viewer-verified service/code pairs. A live bridge heartbeat does not
    make an old quote fresh; only newly received realtime ticks update snapshot timestamps.
    """

    def __init__(
        self,
        *,
        history_interval_seconds: int = 60,
        heartbeat_stale_seconds: int = 30,
        expected_instrument_code: str = "",
        quote_service: KrxQuoteService | None = None,
        krx_closed_dates: frozenset[date] | None = None,
        krx_open_dates: frozenset[date] | None = None,
        krx_night_closed_dates: frozenset[date] | None = None,
    ) -> None:
        self.history_interval_seconds = max(1, int(history_interval_seconds))
        self.heartbeat_stale_seconds = max(5, int(heartbeat_stale_seconds))
        self.instrument_code_override = expected_instrument_code.strip().upper()
        self.quote_service = quote_service
        self.krx_closed_dates = frozenset(krx_closed_dates or ())
        self.krx_open_dates = frozenset(krx_open_dates or ())
        self.krx_night_closed_dates = frozenset(krx_night_closed_dates or ())
        self._lock = Lock()

        self.total_ticks_received = 0
        self.total_snapshot_updates = 0
        self.total_history_rows = 0
        self.total_heartbeats = 0
        self.last_received_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_tick_at_reported: datetime | None = None
        self.last_instrument_code: str | None = None
        self.last_service: str | None = None
        self.last_session: str | None = None
        self.last_business_time: str | None = None
        self.last_price: float | None = None
        self.last_change_pct: float | None = None
        self.last_volume: int | None = None
        self.last_ask1: float | None = None
        self.last_bid1: float | None = None
        self.last_bridge_tick_count: int | None = None
        self.last_error: str | None = None
        self.market_realtime: dict[str, dict[str, object]] = {
            symbol: {
                "service": spec["service"],
                "instrument_code": spec["instrument_code"],
                "last_received_at": None,
                "business_time": None,
                "price": None,
                "change_amount": None,
                "change_pct": None,
                "cumulative_volume": None,
                "ask1": None,
                "bid1": None,
                "bridge_tick_count": None,
                "total_ticks_received": 0,
                "total_snapshot_updates": 0,
                "total_history_rows": 0,
                "last_error": None,
            }
            for symbol, spec in KIS_EFRIEND_REALTIME_INSTRUMENTS.items()
        }

    def expected_contract(self, at: datetime | None = None) -> dict[str, object]:
        if self.instrument_code_override:
            return {
                "instrument_code": self.instrument_code_override,
                "mode": "override",
                "trade_date": None,
                "expiry": None,
                "nominal_last_trading_day": None,
                "actual_last_trading_day": None,
                "calendar_source": "override",
            }

        resolution = resolve_kospi200_front_month(
            at,
            closed_dates=self.krx_closed_dates,
            open_dates=self.krx_open_dates,
        )
        return {
            "instrument_code": resolution.instrument_code,
            "mode": "auto",
            "trade_date": resolution.trade_date.isoformat(),
            "expiry": f"{resolution.expiry_year:04d}-{resolution.expiry_month:02d}",
            "nominal_last_trading_day": resolution.nominal_last_trading_day.isoformat(),
            "actual_last_trading_day": resolution.actual_last_trading_day.isoformat(),
            "calendar_source": resolution.calendar_source,
        }

    def expected_route(self, at: datetime | None = None) -> dict[str, object]:
        # Freeze one server-side instant for both contract and session resolution so
        # a boundary crossing cannot produce a mixed route (old contract/new session).
        route_at = at or datetime.now(timezone.utc)
        contract = self.expected_contract(route_at)
        session = resolve_kospi200_market_session(
            route_at,
            closed_dates=self.krx_closed_dates,
            open_dates=self.krx_open_dates,
            night_closed_dates=self.krx_night_closed_dates,
        )
        return {
            **contract,
            "service": session.service,
            "session": session.session,
            "market_open": session.market_open,
            "session_start_date": (
                None if session.session_start_date is None else session.session_start_date.isoformat()
            ),
            "session_calendar_source": session.calendar_source,
        }

    @staticmethod
    def _validate_service_session(service: str, session_name: str) -> None:
        expected = "day" if service == "FC_R" else "night"
        if session_name != expected:
            raise ValueError(f"{service} requires session={expected}")

    def _validate_expected_route(
        self,
        instrument_code: str,
        service: str | None,
        session_name: str,
    ) -> str:
        """Require the bridge payload to match the server's current AUTO route.

        This is deliberately stricter than validating only the service/session pair:
        an old or manually misrouted bridge must not be able to persist a night tick
        during the day session (or vice versa), and CLOSED accepts heartbeat only.
        """

        route = self.expected_route()
        code = instrument_code.strip().upper()
        expected_code = str(route["instrument_code"])
        expected_service = route["service"]
        expected_session = str(route["session"])

        if code != expected_code:
            raise ValueError(
                f"instrument_code must be {expected_code} for {KOSPI200_SYMBOL}"
            )

        if service != expected_service or session_name != expected_session:
            expected_service_label = expected_service or "CLOSED"
            actual_service_label = service or "CLOSED"
            raise ValueError(
                "bridge route must match current server route "
                f"{expected_code}|{expected_service_label}|{expected_session}; "
                f"got {code}|{actual_service_label}|{session_name}"
            )
        return code

    @staticmethod
    def _source(tick: KisEFriendTick) -> str:
        return f"{KIS_SOURCE_PREFIX}:{tick.session}:{tick.service}:{tick.instrument_code.strip().upper()}"

    def ingest_tick(self, tick: KisEFriendTick) -> dict[str, object]:
        self._validate_service_session(tick.service, tick.session)
        code = self._validate_expected_route(
            tick.instrument_code,
            tick.service,
            tick.session,
        )
        now = datetime.now(timezone.utc)
        source = self._source(tick)

        try:
            with SessionLocal() as db:
                latest_history = db.scalar(
                    select(MarketPrice)
                    .where(MarketPrice.symbol == KOSPI200_SYMBOL)
                    .order_by(MarketPrice.observed_at.desc(), MarketPrice.id.desc())
                    .limit(1)
                )

                write_history = latest_history is None
                if latest_history is not None:
                    history_time = _as_utc(latest_history.observed_at)
                    history_age = max(0.0, (now - history_time).total_seconds())
                    write_history = (
                        latest_history.source != source
                        or history_age >= self.history_interval_seconds
                    )

                save_market_observation(
                    db,
                    symbol=KOSPI200_SYMBOL,
                    price=tick.price,
                    change_pct=tick.change_pct,
                    source=source,
                    observed_at=now,
                    business_time=tick.business_time,
                    write_history=write_history,
                )

            with self._lock:
                self.total_ticks_received += 1
                self.total_snapshot_updates += 1
                if write_history:
                    self.total_history_rows += 1
                self.last_received_at = now
                self.last_instrument_code = code
                self.last_service = tick.service
                self.last_session = tick.session
                self.last_business_time = tick.business_time
                self.last_price = float(tick.price)
                self.last_change_pct = None if tick.change_pct is None else float(tick.change_pct)
                self.last_volume = tick.cumulative_volume
                self.last_ask1 = tick.ask1
                self.last_bid1 = tick.bid1
                self.last_bridge_tick_count = tick.tick_count
                self.last_error = None

            return {
                "accepted": True,
                "symbol": KOSPI200_SYMBOL,
                "source": source,
                "observed_at": _iso(now),
                "snapshot_updated": True,
                "history_written": write_history,
            }
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
            raise

    def _validate_market_tick(self, tick: KisEFriendMarketTick) -> tuple[str, str, str]:
        symbol = tick.symbol.strip().upper()
        code = tick.instrument_code.strip().upper()
        service = tick.service.strip().upper()

        if symbol == "INDEX:KOSPI":
            spec = KIS_EFRIEND_REALTIME_INSTRUMENTS[symbol]
            if code != spec["instrument_code"]:
                raise ValueError(
                    f"instrument_code must be {spec['instrument_code']} for {symbol}"
                )
            if service != spec["service"]:
                raise ValueError(f"service must be {spec['service']} for {symbol}")
            return symbol, code, service

        if not symbol.startswith("KRX:"):
            raise ValueError(f"unsupported realtime market symbol: {symbol}")
        ticker = symbol.split(":", 1)[1]
        if code != ticker:
            raise ValueError(f"instrument_code must match symbol ticker {ticker}")
        if service != "SC_R":
            raise ValueError(f"service must be SC_R for {symbol}")
        if self.quote_service is None:
            raise ValueError("KRX quote service is unavailable")
        if not self.quote_service.is_requested(ticker):
            raise ValueError(f"ticker is not in active quote universe: {ticker}")
        return symbol, code, service

    def ingest_market_tick(
        self,
        tick: KisEFriendMarketTick,
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, object]:
        symbol, code, service = self._validate_market_tick(tick)
        now = _as_utc(observed_at or datetime.now(timezone.utc))
        source = f"{KIS_SOURCE_PREFIX}:{service}:{code}"
        persist_history = symbol in KIS_EFRIEND_REALTIME_INSTRUMENTS
        write_history = False
        durable_snapshot_written = False

        try:
            # Every accepted KRX/JUC_R stream keeps a durable latest-value snapshot so
            # the Bridge monitor can recover the closing/latest value after a process
            # restart. Dynamic dashboard holdings remain snapshot-only (no history rows)
            # and are throttled to 30 seconds to avoid turning realtime forwarding into
            # excessive SQLite writes. Signal baseline/KOSPI history behavior is unchanged.
            with SessionLocal() as db:
                snapshot = db.get(MarketSnapshot, symbol)
                snapshot_due = persist_history or snapshot is None
                if snapshot is not None and not persist_history:
                    snapshot_time = _as_utc(snapshot.observed_at)
                    snapshot_age = max(0.0, (now - snapshot_time).total_seconds())
                    snapshot_due = (
                        snapshot.source != source
                        or snapshot_age >= DYNAMIC_KRX_SNAPSHOT_INTERVAL_SECONDS
                    )

                if persist_history:
                    latest_history = db.scalar(
                        select(MarketPrice)
                        .where(MarketPrice.symbol == symbol)
                        .order_by(MarketPrice.observed_at.desc(), MarketPrice.id.desc())
                        .limit(1)
                    )

                    write_history = latest_history is None
                    if latest_history is not None:
                        history_time = _as_utc(latest_history.observed_at)
                        history_age = max(0.0, (now - history_time).total_seconds())
                        write_history = (
                            latest_history.source != source
                            or history_age >= self.history_interval_seconds
                        )

                if snapshot_due or write_history:
                    save_market_observation(
                        db,
                        symbol=symbol,
                        price=tick.price,
                        change_amount=tick.change_amount,
                        change_pct=tick.change_pct,
                        source=source,
                        observed_at=now,
                        business_time=tick.business_time,
                        write_history=write_history,
                    )
                    durable_snapshot_written = True

            quote_store_updated = False
            if (
                symbol.startswith("KRX:")
                and self.quote_service is not None
                and self.quote_service.is_dashboard_requested(code)
            ):
                self.quote_service.ingest_quote(
                    ticker=code,
                    price=tick.price,
                    change_amount=tick.change_amount,
                    change_pct=tick.change_pct,
                    business_time=tick.business_time,
                    cumulative_volume=tick.cumulative_volume,
                    ask1=tick.ask1,
                    bid1=tick.bid1,
                    bridge_tick_count=tick.tick_count,
                    bridge_sent_at=tick.sent_at,
                    observed_at=now,
                    source=source,
                )
                quote_store_updated = True

            with self._lock:
                state = self.market_realtime.setdefault(
                    symbol,
                    {
                        "service": service,
                        "instrument_code": code,
                        "last_received_at": None,
                        "business_time": None,
                        "price": None,
                        "change_amount": None,
                        "change_pct": None,
                        "cumulative_volume": None,
                        "ask1": None,
                        "bid1": None,
                        "bridge_tick_count": None,
                        "total_ticks_received": 0,
                        "total_snapshot_updates": 0,
                        "total_history_rows": 0,
                        "last_error": None,
                    },
                )
                state["last_received_at"] = now
                state["business_time"] = tick.business_time
                state["price"] = float(tick.price)
                state["change_amount"] = (
                    None if tick.change_amount is None else float(tick.change_amount)
                )
                state["change_pct"] = (
                    None if tick.change_pct is None else float(tick.change_pct)
                )
                state["cumulative_volume"] = tick.cumulative_volume
                state["ask1"] = tick.ask1
                state["bid1"] = tick.bid1
                state["bridge_tick_count"] = tick.tick_count
                state["total_ticks_received"] = int(state["total_ticks_received"]) + 1
                state["total_snapshot_updates"] = int(state["total_snapshot_updates"]) + 1
                if write_history:
                    state["total_history_rows"] = int(state["total_history_rows"]) + 1
                state["last_error"] = None

            return {
                "accepted": True,
                "symbol": symbol,
                "source": source,
                "observed_at": _iso(now),
                "snapshot_updated": True,
                "durable_snapshot_written": durable_snapshot_written,
                "history_written": write_history,
                "quote_store_updated": quote_store_updated,
            }
        except Exception as exc:
            with self._lock:
                state = self.market_realtime.get(symbol)
                if state is not None:
                    state["last_error"] = str(exc)
            raise

    def ingest_heartbeat(self, heartbeat: KisEFriendHeartbeat) -> dict[str, object]:
        if heartbeat.session == "closed":
            if heartbeat.service is not None:
                raise ValueError("session=closed requires service=null")
        else:
            if heartbeat.service is None:
                raise ValueError(f"session={heartbeat.session} requires a service")
            self._validate_service_session(heartbeat.service, heartbeat.session)

        code = self._validate_expected_route(
            heartbeat.instrument_code,
            heartbeat.service,
            heartbeat.session,
        )
        now = datetime.now(timezone.utc)
        bridge_reported_at = _as_utc(heartbeat.bridge_time)
        quote_health = None

        # Detect a real heartbeat gap before recording this recovery heartbeat.
        # Quote forwarding and heartbeat forwarding are independent requests, so a
        # tick may already have reached Market AI during the stale interval.  The
        # quote service therefore arms only quotes that were last received at/before
        # the instant the old heartbeat actually became stale.
        reconnect_cutoff = None
        with self._lock:
            previous_heartbeat_at = self.last_heartbeat_at
            if previous_heartbeat_at is not None:
                stale_at = previous_heartbeat_at + timedelta(seconds=self.heartbeat_stale_seconds)
                if now > stale_at:
                    reconnect_cutoff = stale_at

        reconnect_freshness = None
        if self.quote_service is not None and reconnect_cutoff is not None:
            reconnect_freshness = self.quote_service.mark_bridge_reconnected(
                disconnected_since=reconnect_cutoff,
            )

        if self.quote_service is not None and heartbeat.quote_subscriptions is not None:
            quote_health = self.quote_service.update_subscription_health(
                [
                    {
                        "ticker": item.ticker,
                        "subscribed": item.subscribed,
                        "last_tick_at": item.last_tick_at,
                        "tick_count": item.tick_count,
                        "forward_success_count": item.forward_success_count,
                        "last_forwarded_tick_count": item.last_forwarded_tick_count,
                        "last_error": item.last_error,
                    }
                    for item in heartbeat.quote_subscriptions
                ],
                reported_at=bridge_reported_at,
            )
        with self._lock:
            self.total_heartbeats += 1
            self.last_heartbeat_at = now
            self.last_tick_at_reported = (
                None if heartbeat.last_tick_at is None else _as_utc(heartbeat.last_tick_at)
            )
            self.last_instrument_code = code
            self.last_service = heartbeat.service
            self.last_session = heartbeat.session
            self.last_bridge_tick_count = heartbeat.tick_count

        return {
            "accepted": True,
            "received_at": _iso(now),
            "quote_subscription_health": quote_health,
            "reconnect_freshness": reconnect_freshness,
        }

    def status(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        with self._lock:
            heartbeat_age = (
                None
                if self.last_heartbeat_at is None
                else max(0.0, (now - self.last_heartbeat_at).total_seconds())
            )
            tick_age = (
                None
                if self.last_received_at is None
                else max(0.0, (now - self.last_received_at).total_seconds())
            )
            connected = heartbeat_age is not None and heartbeat_age <= self.heartbeat_stale_seconds

            expected_route = self.expected_route()
            market_realtime = {}
            for symbol, state in self.market_realtime.items():
                last_received_at = state["last_received_at"]
                age_seconds = (
                    None
                    if last_received_at is None
                    else max(0.0, (now - _as_utc(last_received_at)).total_seconds())
                )
                market_realtime[symbol] = {
                    **state,
                    "last_received_at": _iso(last_received_at),
                    "tick_age_seconds": (
                        None if age_seconds is None else round(age_seconds, 3)
                    ),
                }
            return {
                "provider": KIS_SOURCE_PREFIX,
                "symbol": KOSPI200_SYMBOL,
                "market_realtime": market_realtime,
                "expected_instrument_code": expected_route["instrument_code"],
                "instrument_code_mode": expected_route["mode"],
                "contract_trade_date": expected_route["trade_date"],
                "contract_expiry": expected_route["expiry"],
                "nominal_last_trading_day": expected_route["nominal_last_trading_day"],
                "actual_last_trading_day": expected_route["actual_last_trading_day"],
                "contract_calendar_source": expected_route["calendar_source"],
                "expected_service": expected_route["service"],
                "expected_session": expected_route["session"],
                "market_open": expected_route["market_open"],
                "session_start_date": expected_route["session_start_date"],
                "session_calendar_source": expected_route["session_calendar_source"],
                "connected": connected,
                "heartbeat_stale_seconds": self.heartbeat_stale_seconds,
                "history_interval_seconds": self.history_interval_seconds,
                "last_heartbeat_at": _iso(self.last_heartbeat_at),
                "heartbeat_age_seconds": None if heartbeat_age is None else round(heartbeat_age, 3),
                "last_received_at": _iso(self.last_received_at),
                "tick_age_seconds": None if tick_age is None else round(tick_age, 3),
                "last_tick_at_reported": _iso(self.last_tick_at_reported),
                "instrument_code": self.last_instrument_code,
                "service": self.last_service,
                "session": self.last_session,
                "business_time": self.last_business_time,
                "price": self.last_price,
                "change_pct": self.last_change_pct,
                "cumulative_volume": self.last_volume,
                "ask1": self.last_ask1,
                "bid1": self.last_bid1,
                "bridge_tick_count": self.last_bridge_tick_count,
                "total_ticks_received": self.total_ticks_received,
                "total_snapshot_updates": self.total_snapshot_updates,
                "total_history_rows": self.total_history_rows,
                "total_heartbeats": self.total_heartbeats,
                "last_error": self.last_error,
            }
