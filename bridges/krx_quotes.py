from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from threading import Lock
from typing import Iterable

from bridges.kospi200_contract import KST, is_krx_trading_day


KRX_CASH_START = time(9, 0)
KRX_CASH_END = time(15, 30)
KRX_EXTENDED_END = time(20, 0)
SIGNAL_BASELINE_QUOTE_TICKERS = ("005930", "000660")
# Backward-compatible name for tests/importers that mean the immutable Signal baseline.
BASELINE_QUOTE_TICKERS = SIGNAL_BASELINE_QUOTE_TICKERS
TICKER_PATTERN = re.compile(r"^[0-9A-Z]{6}$")
CLIENT_ID_PATTERN = re.compile(r"^[0-9A-Za-z._:-]{1,80}$")
REMOTE_CLIENT_PREFIX = "remote-"
DEFAULT_MAX_REMOTE_CLIENTS = 16


@dataclass(frozen=True)
class KrxQuote:
    ticker: str
    price: float
    change_pct: float | None
    business_time: str | None
    cumulative_volume: int | None
    ask1: float | None
    bid1: float | None
    bridge_tick_count: int | None
    observed_at: datetime
    source: str
    change_amount: float | None = None
    # Bridge-side event time from the SC_R payload.  `observed_at` is the
    # Market AI server receive time, while this timestamp lets us order a
    # heartbeat snapshot against the actual tick event even when the two HTTP
    # requests complete out of order.
    bridge_sent_at: datetime | None = None


@dataclass(frozen=True)
class KrxSubscriptionHealth:
    ticker: str
    subscribed: bool
    last_error: str | None
    last_tick_at: datetime | None
    tick_count: int
    forward_success_count: int
    reported_at: datetime
    last_forwarded_tick_count: int | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


class KrxQuoteService:
    """Process-memory KRX quote universe and latest quote store.

    The dashboard owns positions, cost basis and valuation math. This service owns
    only the requested ticker universe plus the latest eFriend SC_R quote per ticker.
    Nothing here writes portfolio state or historical valuation snapshots.
    """

    def __init__(
        self,
        *,
        baseline_tickers: Iterable[str] = BASELINE_QUOTE_TICKERS,
        bootstrap_tickers: Iterable[str] = (),
        max_tickers: int = 64,
        krx_closed_dates: frozenset[date] | None = None,
        krx_open_dates: frozenset[date] | None = None,
        client_lease_seconds: int = 120,
        max_remote_clients: int = DEFAULT_MAX_REMOTE_CLIENTS,
    ) -> None:
        self.max_tickers = max(2, int(max_tickers))
        self.krx_closed_dates = frozenset(krx_closed_dates or ())
        self.krx_open_dates = frozenset(krx_open_dates or ())
        self.client_lease_seconds = max(20, int(client_lease_seconds))
        self.max_remote_clients = max(1, int(max_remote_clients))
        self._lock = Lock()
        self._baseline = frozenset(self.normalize_ticker(value) for value in baseline_tickers)
        self._bootstrap = frozenset(self.normalize_ticker(value) for value in bootstrap_tickers)
        initial_universe = self._baseline | self._bootstrap
        if len(initial_universe) > self.max_tickers:
            raise ValueError(f"too many KRX tickers; maximum is {self.max_tickers}")
        self._bootstrap_active = bool(self._bootstrap)
        self._dashboard_universe = frozenset(self._bootstrap)
        self._universe = frozenset(initial_universe)
        self._version = 1
        self._universe_updated_at = datetime.now(timezone.utc)
        self._quotes: dict[str, KrxQuote] = {}
        self._subscription_health: dict[str, KrxSubscriptionHealth] = {}
        self._subscription_health_reported_at: datetime | None = None
        self._fresh_tick_required: set[str] = set()
        self._client_leases: dict[str, tuple[frozenset[str], datetime]] = {}

    @staticmethod
    def _health_snapshot_predates_quote(
        health: KrxSubscriptionHealth,
        quote: KrxQuote | None,
        *,
        reported_at: datetime,
    ) -> bool:
        """Return whether an accepted quote is causally newer than this heartbeat item.

        Heartbeat and SC_R forwarding use independent HTTP requests in the native
        Bridge.  Network scheduling can therefore deliver an older heartbeat after
        a newer tick.  Prefer Bridge-native timestamps/counters over server arrival
        order so the delayed heartbeat cannot invalidate a quote that the server has
        already accepted.
        """
        if quote is None:
            return False

        bridge_sent_at = quote.bridge_sent_at
        quote_tick_count = quote.bridge_tick_count
        if bridge_sent_at is not None and reported_at <= _as_utc(bridge_sent_at):
            return True

        if quote_tick_count is None:
            return False

        if health.tick_count < quote_tick_count:
            return True
        if health.tick_count > quote_tick_count:
            return False

        # Same-tick heartbeats need an acknowledgement marker, not a comparison
        # between unrelated counters. TickCount counts realtime reads while
        # ForwardSuccessCount counts throttled HTTP successes, so the latter can be
        # much smaller during completely normal operation. The native Bridge now
        # reports the exact TickCount whose quote POST it has seen succeed.
        if health.last_forwarded_tick_count is not None:
            return health.last_forwarded_tick_count < quote_tick_count

        # Compatibility for a rolling deployment with an older Bridge. Keep the
        # previous same-tick heuristic only when the new causal marker is absent.
        # Once the new Bridge is active, last_forwarded_tick_count is authoritative.
        return health.forward_success_count < quote_tick_count

    def mark_bridge_reconnected(self, *, disconnected_since: datetime) -> dict[str, object]:
        """Require post-gap ticks before pre-disconnect quotes can become live again.

        `disconnected_since` is the server-side instant at which the previous
        heartbeat became stale.  A quote received after that cutoff is already proof
        that the SC_R forward path recovered, so only quotes at/before the cutoff are
        armed for a mandatory fresh tick.
        """
        cutoff = _as_utc(disconnected_since)
        with self._lock:
            armed: list[str] = []
            for ticker in self._universe:
                quote = self._quotes.get(ticker)
                if quote is None:
                    continue
                if _as_utc(quote.observed_at) <= cutoff:
                    self._fresh_tick_required.add(ticker)
                    armed.append(ticker)
            return {
                "disconnected_since": _iso(cutoff),
                "fresh_tick_required_count": len(armed),
                "fresh_tick_required": sorted(armed),
            }

    @staticmethod
    def normalize_ticker(value: str) -> str:
        ticker = str(value or "").strip().upper()
        if not TICKER_PATTERN.fullmatch(ticker):
            raise ValueError(f"invalid KRX ticker: {value!r}")
        return ticker

    def normalize_tickers(self, values: Iterable[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            ticker = self.normalize_ticker(value)
            if ticker in seen:
                continue
            seen.add(ticker)
            normalized.append(ticker)
        if len(normalized) > self.max_tickers:
            raise ValueError(f"too many KRX tickers; maximum is {self.max_tickers}")
        return tuple(normalized)

    @staticmethod
    def parse_ticker_query(raw: str | None) -> tuple[str, ...]:
        if raw is None or not raw.strip():
            return ()
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    @staticmethod
    def normalize_client_id(value: str | None) -> str:
        client_id = str(value or "").strip()
        if not client_id:
            return "legacy"
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ValueError("invalid quote client_id")
        return client_id

    @staticmethod
    def is_remote_client_id(client_id: str | None) -> bool:
        return str(client_id or "").startswith(REMOTE_CLIENT_PREFIX)

    def _prune_expired_clients_locked(self, current: datetime) -> None:
        expired = [
            client_id
            for client_id, (_tickers, expires_at) in self._client_leases.items()
            if expires_at <= current
        ]
        for client_id in expired:
            self._client_leases.pop(client_id, None)

    def _remote_client_count_locked(self, leases: dict[str, tuple[frozenset[str], datetime]] | None = None) -> int:
        source = self._client_leases if leases is None else leases
        return sum(1 for client_id in source if self.is_remote_client_id(client_id))

    def _dashboard_union_for_leases_locked(
        self,
        leases: dict[str, tuple[frozenset[str], datetime]],
        *,
        bootstrap_active: bool | None = None,
    ) -> frozenset[str]:
        use_bootstrap = self._bootstrap_active if bootstrap_active is None else bool(bootstrap_active)
        desired: set[str] = set(self._bootstrap if use_bootstrap else ())
        for tickers, _expires_at in leases.values():
            desired.update(tickers)
        return frozenset(desired)

    def _local_dashboard_union_locked(
        self,
        leases: dict[str, tuple[frozenset[str], datetime]] | None = None,
    ) -> frozenset[str]:
        source = self._client_leases if leases is None else leases
        desired: set[str] = set()
        for client_id, (tickers, _expires_at) in source.items():
            if not self.is_remote_client_id(client_id):
                desired.update(tickers)
        return frozenset(desired)

    def _universe_for_leases_locked(
        self,
        leases: dict[str, tuple[frozenset[str], datetime]],
        *,
        bootstrap_active: bool | None = None,
    ) -> frozenset[str]:
        return frozenset(
            set(self._baseline)
            | set(self._dashboard_union_for_leases_locked(leases, bootstrap_active=bootstrap_active))
        )

    def _evict_remote_leases_for_local_capacity_locked(
        self,
        leases: dict[str, tuple[frozenset[str], datetime]],
        *,
        bootstrap_active: bool | None = None,
    ) -> tuple[dict[str, tuple[frozenset[str], datetime]], tuple[str, ...]]:
        working = dict(leases)
        if len(self._universe_for_leases_locked(working, bootstrap_active=bootstrap_active)) <= self.max_tickers:
            return working, ()

        remote_candidates = sorted(
            (
                (client_id, value[1])
                for client_id, value in working.items()
                if self.is_remote_client_id(client_id)
            ),
            key=lambda item: (item[1], item[0]),
        )
        evicted: list[str] = []
        for client_id, _expires_at in remote_candidates:
            working.pop(client_id, None)
            evicted.append(client_id)
            if len(self._universe_for_leases_locked(working, bootstrap_active=bootstrap_active)) <= self.max_tickers:
                break
        return working, tuple(evicted)

    def _desired_universe_locked(self) -> frozenset[str]:
        desired = set(self._baseline)
        if self._bootstrap_active:
            desired.update(self._bootstrap)
        for tickers, _expires_at in self._client_leases.values():
            desired.update(tickers)
        return frozenset(desired)

    def _apply_universe_locked(self, desired: frozenset[str], current: datetime) -> None:
        if desired == self._universe:
            return
        removed = self._universe - desired
        for ticker in removed:
            self._quotes.pop(ticker, None)
            self._subscription_health.pop(ticker, None)
            self._fresh_tick_required.discard(ticker)
        self._universe = desired
        self._version += 1
        self._universe_updated_at = current

    def request_universe(
        self,
        requested: Iterable[str],
        *,
        client_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        normalized = frozenset(self.normalize_tickers(requested))
        normalized_client_id = self.normalize_client_id(client_id)
        current = _as_utc(now or datetime.now(timezone.utc))

        with self._lock:
            self._prune_expired_clients_locked(current)
            previous_dashboard_universe = self._dashboard_universe
            is_remote = self.is_remote_client_id(normalized_client_id)

            # The bootstrap set is a durable local warm-start hint. Remote/Tailscale
            # clients are lower priority and must not displace it before a local Dashboard
            # has supplied the first authoritative lease. Treat bootstrap release as part
            # of the candidate transaction: a rejected first local request must not mutate
            # live state. The first *admitted* local request, including explicit [], is
            # authoritative and releases bootstrap ownership at commit time below.
            candidate_bootstrap_active = self._bootstrap_active if is_remote else False

            candidate_leases = dict(self._client_leases)
            is_new_remote = is_remote and normalized_client_id not in candidate_leases
            if is_new_remote and self._remote_client_count_locked(candidate_leases) >= self.max_remote_clients:
                raise ValueError(f"too many remote quote clients; maximum is {self.max_remote_clients}")

            candidate_leases[normalized_client_id] = (
                normalized,
                current + timedelta(seconds=self.client_lease_seconds),
            )

            if is_remote:
                # Remote/Tailscale Dashboard requests are admitted only from spare capacity.
                # They never evict or deny an already active local Dashboard lease.
                desired = self._universe_for_leases_locked(
                    candidate_leases,
                    bootstrap_active=candidate_bootstrap_active,
                )
                if len(desired) > self.max_tickers:
                    raise ValueError(f"too many KRX tickers; maximum is {self.max_tickers}")
                evicted_remote_clients: tuple[str, ...] = ()
            else:
                # Local Dashboard leases are the primary runtime contract. If remote leases
                # consumed capacity first, reclaim the oldest remote leases atomically under
                # this same service lock before deciding whether the local request fits.
                local_only = {
                    key: value
                    for key, value in candidate_leases.items()
                    if not self.is_remote_client_id(key)
                }
                local_desired = self._universe_for_leases_locked(
                    local_only,
                    bootstrap_active=candidate_bootstrap_active,
                )
                if len(local_desired) > self.max_tickers:
                    raise ValueError(f"too many KRX tickers; maximum is {self.max_tickers}")
                candidate_leases, evicted_remote_clients = self._evict_remote_leases_for_local_capacity_locked(
                    candidate_leases,
                    bootstrap_active=candidate_bootstrap_active,
                )
                desired = self._universe_for_leases_locked(
                    candidate_leases,
                    bootstrap_active=candidate_bootstrap_active,
                )
                if len(desired) > self.max_tickers:
                    raise ValueError(f"too many KRX tickers; maximum is {self.max_tickers}")

            self._client_leases = candidate_leases
            self._bootstrap_active = candidate_bootstrap_active
            next_dashboard_universe = self._dashboard_union_for_leases_locked(
                candidate_leases,
                bootstrap_active=candidate_bootstrap_active,
            )
            # A ticker that leaves the Dashboard universe must lose its valuation cache
            # even when it remains physically subscribed as a Signal baseline stream.
            # Otherwise 005930/000660 could be re-added with an old/pre-request quote
            # while ordinary holdings correctly return to WARMING.
            for ticker in previous_dashboard_universe - next_dashboard_universe:
                self._quotes.pop(ticker, None)
                self._fresh_tick_required.discard(ticker)
            self._dashboard_universe = next_dashboard_universe
            self._apply_universe_locked(frozenset(desired), current)
            payload = self._universe_payload_locked()
            payload["evicted_remote_client_count"] = len(evicted_remote_clients)
            return payload

    def bridge_universe(self, *, now: datetime | None = None) -> dict[str, object]:
        current = _as_utc(now or datetime.now(timezone.utc))
        market_state, calendar_source = self._market_context(current)
        with self._lock:
            # Client leases prevent one active dashboard from deleting another active
            # dashboard's tickers. Expiry alone does not shrink the Bridge universe:
            # keeping the last subscribed set preserves same-day closing quotes while
            # every dashboard is hidden/backgrounded. The next client request performs
            # the authoritative reconcile and removes no-longer-requested tickers.
            self._prune_expired_clients_locked(current)
            return {
                **self._universe_payload_locked(),
                "market_state": market_state,
                "market_calendar_source": calendar_source,
            }

    def monitor_quotes(
        self,
        *,
        bridge_connected: bool,
        ticker_types: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return current Dashboard quotes for monitor display without renewing a client lease."""

        current = _as_utc(now or datetime.now(timezone.utc))
        market_state, calendar_source = self._market_context(current)
        with self._lock:
            requested_tickers = tuple(sorted(self._dashboard_universe))
            quote_copy = {ticker: self._quotes.get(ticker) for ticker in requested_tickers}
            health_copy = {
                ticker: self._subscription_health.get(ticker)
                for ticker in requested_tickers
            }
            health_known = self._subscription_health_reported_at is not None
            health_reported_at = self._subscription_health_reported_at
            fresh_tick_required = {
                ticker for ticker in requested_tickers if ticker in self._fresh_tick_required
            }

        items = [
            self._serialize_quote(
                ticker,
                quote_copy[ticker],
                health=health_copy[ticker],
                health_known=health_known,
                health_reported_at=health_reported_at,
                fresh_tick_required=ticker in fresh_tick_required,
                current=current,
                bridge_connected=bridge_connected,
                market_state=self.market_state_for_instrument(
                    (ticker_types or {}).get(ticker),
                    now=current,
                ),
            )
            for ticker in requested_tickers
        ]
        return {
            "market_state": market_state,
            "market_calendar_source": calendar_source,
            "bridge_connected": bool(bridge_connected),
            "generated_at": _iso(current),
            "items": items,
        }


    @staticmethod
    def _is_individual_stock_type(value: object) -> bool:
        normalized = str(value or "").strip().lower().replace(" ", "")
        if not normalized:
            return False
        if normalized in {"개별주식", "주식", "stock", "equity", "commonstock"}:
            return True
        return "개별주식" in normalized

    def market_state_for_instrument(
        self,
        instrument_type: object,
        *,
        now: datetime | None = None,
    ) -> str:
        current = _as_utc(now or datetime.now(timezone.utc))
        local = current.astimezone(KST)
        trading_day, _calendar_source = is_krx_trading_day(
            local.date(),
            closed_dates=self.krx_closed_dates,
            open_dates=self.krx_open_dates,
        )
        if not trading_day:
            return "closed"
        if KRX_CASH_START <= local.time() < KRX_CASH_END:
            return "open"
        if self._is_individual_stock_type(instrument_type) and KRX_CASH_END <= local.time() < KRX_EXTENDED_END:
            # 15:30~16:00 is the post-close session; 16:00~20:00 is the KRX After Market.
            # The UI groups both under one user-facing "시간외" state so an individual
            # stock is not mislabeled as fully closed immediately at 15:30.
            return "extended"
        return "closed"

    def latest_completed_session_date(self, now: datetime | None = None) -> date:
        """Return the most recent completed KRX cash-session date.

        Before today's regular session completes, the previous KRX trading day owns
        the durable closing snapshot. This is intentionally trading-calendar based so
        midnight, weekends and exchange holidays do not invalidate the last completed
        close. Once today's cash session has completed (15:30 KST), today becomes the
        completed session date.
        """

        current = _as_utc(now or datetime.now(timezone.utc))
        local = current.astimezone(KST)
        candidate = local.date()
        trading_day, _calendar_source = is_krx_trading_day(
            candidate,
            closed_dates=self.krx_closed_dates,
            open_dates=self.krx_open_dates,
        )
        if trading_day and local.time() >= KRX_CASH_END:
            return candidate

        candidate -= timedelta(days=1)
        # Two weeks comfortably spans ordinary long weekends and Korean holiday
        # blocks while keeping a deterministic fail-closed bound.
        for _ in range(14):
            trading_day, _calendar_source = is_krx_trading_day(
                candidate,
                closed_dates=self.krx_closed_dates,
                open_dates=self.krx_open_dates,
            )
            if trading_day:
                return candidate
            candidate -= timedelta(days=1)
        return local.date()

    def _market_context(self, current: datetime) -> tuple[str, str]:
        local = current.astimezone(KST)
        trading_day, calendar_source = is_krx_trading_day(
            local.date(),
            closed_dates=self.krx_closed_dates,
            open_dates=self.krx_open_dates,
        )
        market_open = trading_day and KRX_CASH_START <= local.time() < KRX_CASH_END
        return ("open" if market_open else "closed", calendar_source)

    def _universe_payload_locked(self) -> dict[str, object]:
        tickers = sorted(self._universe)
        return {
            "version": self._version,
            "tickers": tickers,
            "symbols": [f"KRX:{ticker}" for ticker in tickers],
            "dashboard_tickers": sorted(self._dashboard_universe),
            "signal_baseline_tickers": sorted(self._baseline),
            "updated_at": _iso(self._universe_updated_at),
            "active_client_count": len(self._client_leases),
            "remote_client_count": self._remote_client_count_locked(),
            "max_remote_clients": self.max_remote_clients,
            "client_lease_seconds": self.client_lease_seconds,
            "bootstrap_active": self._bootstrap_active,
        }

    def is_requested(self, ticker: str) -> bool:
        normalized = self.normalize_ticker(ticker)
        with self._lock:
            return normalized in self._universe

    def is_dashboard_requested(self, ticker: str) -> bool:
        """Return whether a ticker currently belongs to the Dashboard valuation universe.

        Signal baseline streams can remain physically subscribed even when the Dashboard
        no longer holds them. Keeping that distinction prevents 005930/000660 from
        retaining a privileged valuation quote after an authoritative empty/remove.
        """
        normalized = self.normalize_ticker(ticker)
        with self._lock:
            return normalized in self._dashboard_universe

    def local_dashboard_tickers(self) -> tuple[str, ...]:
        """Return the current local-only Dashboard lease union under the service lock."""
        with self._lock:
            return tuple(sorted(self._local_dashboard_union_locked()))

    def update_subscription_health(
        self,
        items: Iterable[dict[str, object]],
        *,
        reported_at: datetime | None = None,
    ) -> dict[str, object]:
        current = _as_utc(reported_at or datetime.now(timezone.utc))
        normalized: dict[str, KrxSubscriptionHealth] = {}

        for raw in items:
            ticker = self.normalize_ticker(str(raw.get("ticker") or ""))
            subscribed = bool(raw.get("subscribed"))
            error_text = str(raw.get("last_error") or "").strip()
            if len(error_text) > 512:
                error_text = error_text[:512]
            last_tick_raw = raw.get("last_tick_at")
            if isinstance(last_tick_raw, datetime):
                last_tick_at = _as_utc(last_tick_raw)
            else:
                last_tick_at = None
            tick_count = max(0, int(raw.get("tick_count") or 0))
            forward_success_count = max(0, int(raw.get("forward_success_count") or 0))
            last_forwarded_raw = raw.get("last_forwarded_tick_count")
            last_forwarded_tick_count = (
                None
                if last_forwarded_raw is None
                else max(0, int(last_forwarded_raw))
            )
            normalized[ticker] = KrxSubscriptionHealth(
                ticker=ticker,
                subscribed=subscribed,
                last_error=error_text or None,
                last_tick_at=last_tick_at,
                tick_count=tick_count,
                forward_success_count=forward_success_count,
                reported_at=current,
                last_forwarded_tick_count=last_forwarded_tick_count,
            )

        with self._lock:
            # Heartbeat subscription health is a full snapshot. Enforce latest-wins
            # by the Bridge-reported timestamp so a delayed older unhealthy snapshot
            # cannot overwrite a newer recovery snapshot. Equal timestamps are
            # treated as idempotent/retry-safe.
            if (
                self._subscription_health_reported_at is not None
                and current < self._subscription_health_reported_at
            ):
                return {
                    "reported_at": _iso(current),
                    "stream_count": len(self._subscription_health),
                    "unhealthy_count": sum(
                        1
                        for health in self._subscription_health.values()
                        if not health.subscribed or health.last_error
                    ),
                    "stale_snapshot_ignored": True,
                    "latest_reported_at": _iso(self._subscription_health_reported_at),
                }

            # Ignore entries that raced with a universe shrink, and fail closed for
            # any requested ticker whose stream is explicitly unhealthy.
            current_health = {
                ticker: health
                for ticker, health in normalized.items()
                if ticker in self._universe
            }
            effective_health: dict[str, KrxSubscriptionHealth] = {}
            for ticker, health in current_health.items():
                previous = self._subscription_health.get(ticker)
                quote = self._quotes.get(ticker)
                unhealthy = not health.subscribed or bool(health.last_error)

                # Cross-request causal ordering: an accepted SC_R quote can be newer
                # than an *unhealthy* heartbeat that happened to arrive later at the
                # server.  A healthy heartbeat is harmless/useful even when older than
                # the latest quote, so keep it as subscription evidence.  Do not let
                # only the causally stale unhealthy item overwrite a newer state or
                # re-arm freshness.
                if unhealthy and self._health_snapshot_predates_quote(
                    health,
                    quote,
                    reported_at=current,
                ):
                    if previous is not None:
                        effective_health[ticker] = previous
                    continue

                effective_health[ticker] = health
                counter_reset = (
                    previous is not None
                    and (
                        health.tick_count < previous.tick_count
                        or health.forward_success_count < previous.forward_success_count
                    )
                )
                if counter_reset:
                    # A fast Bridge/stream restart can happen inside the heartbeat
                    # stale window, so `bridge_connected` may never visibly flip to
                    # false.  Counter regression is the stream-epoch boundary.  Keep
                    # the old quote stale unless an accepted quote clearly belongs to
                    # this new epoch already.
                    quote_confirms_new_epoch = False
                    if quote is not None and quote.bridge_sent_at is not None:
                        quote_tick_count = quote.bridge_tick_count
                        quote_confirms_new_epoch = (
                            _as_utc(quote.bridge_sent_at) > previous.reported_at
                            and (
                                quote_tick_count is None
                                or quote_tick_count <= health.tick_count
                            )
                        )
                    if not quote_confirms_new_epoch:
                        self._fresh_tick_required.add(ticker)

                previous_unhealthy = (
                    previous is not None
                    and (not previous.subscribed or bool(previous.last_error))
                )
                if unhealthy and not previous_unhealthy:
                    # A recovered stream must produce a new tick before an older
                    # same-day quote becomes usable again. Only the transition into
                    # unhealthy marks this requirement; repeated heartbeats must not
                    # re-arm it after a newly accepted quote clears the requirement.
                    self._fresh_tick_required.add(ticker)
            self._subscription_health = effective_health
            self._subscription_health_reported_at = current

            unhealthy = sum(
                1
                for health in effective_health.values()
                if not health.subscribed or health.last_error
            )
            return {
                "reported_at": _iso(current),
                "stream_count": len(effective_health),
                "unhealthy_count": unhealthy,
                "stale_snapshot_ignored": False,
            }

    def ingest_quote(
        self,
        *,
        ticker: str,
        price: float,
        change_pct: float | None,
        change_amount: float | None = None,
        business_time: str | None,
        cumulative_volume: int | None,
        ask1: float | None,
        bid1: float | None,
        bridge_tick_count: int | None,
        bridge_sent_at: datetime | None = None,
        observed_at: datetime | None = None,
        source: str | None = None,
    ) -> KrxQuote:
        normalized = self.normalize_ticker(ticker)
        if price <= 0:
            raise ValueError("price must be positive")
        now = _as_utc(observed_at or datetime.now(timezone.utc))

        with self._lock:
            if normalized not in self._universe:
                raise ValueError(f"ticker is not in active quote universe: {normalized}")
            quote = KrxQuote(
                ticker=normalized,
                price=float(price),
                change_pct=None if change_pct is None else float(change_pct),
                change_amount=None if change_amount is None else float(change_amount),
                business_time=business_time,
                cumulative_volume=cumulative_volume,
                ask1=ask1,
                bid1=bid1,
                bridge_tick_count=bridge_tick_count,
                observed_at=now,
                source=source or f"kis-efriend:SC_R:{normalized}",
                bridge_sent_at=None if bridge_sent_at is None else _as_utc(bridge_sent_at),
            )
            self._quotes[normalized] = quote
            self._fresh_tick_required.discard(normalized)
            return quote

    def quote_snapshot(
        self,
        requested: Iterable[str],
        *,
        bridge_connected: bool,
        client_id: str | None = None,
        ticker_types: dict[str, str] | None = None,
        durable_closed_quotes: dict[str, KrxQuote] | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        requested_tickers = self.normalize_tickers(requested)
        current = _as_utc(now or datetime.now(timezone.utc))
        universe = self.request_universe(requested_tickers, client_id=client_id, now=current)
        market_state, calendar_source = self._market_context(current)

        with self._lock:
            quote_copy = {ticker: self._quotes.get(ticker) for ticker in requested_tickers}
            health_copy = {
                ticker: self._subscription_health.get(ticker)
                for ticker in requested_tickers
            }
            health_known = self._subscription_health_reported_at is not None
            health_reported_at = self._subscription_health_reported_at
            fresh_tick_required = {
                ticker for ticker in requested_tickers if ticker in self._fresh_tick_required
            }

        durable_closed_quotes = durable_closed_quotes or {}
        items = []
        for ticker in requested_tickers:
            item = self._serialize_quote(
                ticker,
                quote_copy[ticker],
                health=health_copy[ticker],
                health_known=health_known,
                health_reported_at=health_reported_at,
                fresh_tick_required=ticker in fresh_tick_required,
                current=current,
                bridge_connected=bridge_connected,
                market_state=self.market_state_for_instrument(
                    (ticker_types or {}).get(ticker),
                    now=current,
                ),
            )
            durable_quote = durable_closed_quotes.get(ticker)
            if (
                durable_quote is not None
                and item["usable"] is not True
                and item["market_state"] == "closed"
                and item["subscription_state"] == "subscribed"
                and ticker not in fresh_tick_required
            ):
                item = self._recover_durable_closed_quote(
                    item,
                    durable_quote,
                    current=current,
                )
            items.append(item)
        usable_count = sum(1 for item in items if item["usable"])
        if not items:
            status = "ok"
        elif usable_count == len(items):
            status = "ok"
        elif usable_count:
            status = "partial"
        else:
            status = "unavailable"

        return {
            "status": status,
            "market_state": market_state,
            "market_calendar_source": calendar_source,
            "bridge_connected": bool(bridge_connected),
            "universe_version": universe["version"],
            "requested_count": len(items),
            "usable_count": usable_count,
            "generated_at": _iso(current),
            "items": items,
        }


    def _recover_durable_closed_quote(
        self,
        base: dict[str, object],
        quote: KrxQuote,
        *,
        current: datetime,
    ) -> dict[str, object]:
        ticker = str(base.get("ticker") or "")
        if quote.ticker != ticker or quote.price <= 0:
            return base
        expected_source = f"kis-efriend:SC_R:{ticker}"
        if str(quote.source or "") != expected_source:
            return base
        observed = _as_utc(quote.observed_at)
        if observed.astimezone(KST).date() != self.latest_completed_session_date(current):
            return base
        age_seconds = max(0.0, (current - observed).total_seconds())
        return {
            **base,
            "price": quote.price,
            "change_amount": quote.change_amount,
            "change_pct": quote.change_pct,
            "business_time": quote.business_time,
            "cumulative_volume": quote.cumulative_volume,
            "ask1": quote.ask1,
            "bid1": quote.bid1,
            "observed_at": _iso(observed),
            "age_seconds": round(age_seconds, 3),
            "source": quote.source,
            "state": "closed",
            "usable": True,
        }

    def _serialize_quote(
        self,
        ticker: str,
        quote: KrxQuote | None,
        *,
        health: KrxSubscriptionHealth | None,
        health_known: bool,
        health_reported_at: datetime | None,
        fresh_tick_required: bool,
        current: datetime,
        bridge_connected: bool,
        market_state: str,
    ) -> dict[str, object]:
        if not bridge_connected:
            subscription_state = "unknown"
        elif not health_known:
            subscription_state = "requested"
        elif health is None:
            subscription_state = "unknown"
        elif not health.subscribed:
            subscription_state = "not_subscribed"
        elif health.last_error:
            subscription_state = "error"
        else:
            subscription_state = "subscribed"

        base = {
            "ticker": ticker,
            "symbol": f"KRX:{ticker}",
            "service": "SC_R",
            "price": None,
            "change_amount": None,
            "change_pct": None,
            "business_time": None,
            "cumulative_volume": None,
            "ask1": None,
            "bid1": None,
            "observed_at": None,
            "age_seconds": None,
            "source": None,
            "subscription_state": subscription_state,
            "subscription_error": None if health is None else health.last_error,
            "subscription_reported_at": _iso(health_reported_at),
            "subscription_last_tick_at": None if health is None else _iso(health.last_tick_at),
            "subscription_tick_count": None if health is None else health.tick_count,
            "subscription_forward_success_count": (
                None if health is None else health.forward_success_count
            ),
            "subscription_last_forwarded_tick_count": (
                None if health is None else health.last_forwarded_tick_count
            ),
            "market_state": market_state,
            "state": "warming" if bridge_connected else "unavailable",
            "usable": False,
        }
        if quote is None:
            if bridge_connected and health_known and subscription_state in {"not_subscribed", "error"}:
                return {**base, "state": "unavailable"}
            return base

        observed = _as_utc(quote.observed_at)
        age_seconds = max(0.0, (current - observed).total_seconds())
        same_kst_date = observed.astimezone(KST).date() == current.astimezone(KST).date()

        if not same_kst_date:
            state = "stale"
            usable = False
        elif not bridge_connected:
            state = "stale"
            usable = False
        elif health_known and subscription_state != "subscribed":
            state = "stale"
            usable = False
        elif fresh_tick_required:
            state = "stale"
            usable = False
        elif market_state in {"open", "extended"}:
            state = "live"
            usable = True
        else:
            state = "closed"
            usable = True

        return {
            **base,
            "price": quote.price,
            "change_amount": quote.change_amount,
            "change_pct": quote.change_pct,
            "business_time": quote.business_time,
            "cumulative_volume": quote.cumulative_volume,
            "ask1": quote.ask1,
            "bid1": quote.bid1,
            "observed_at": _iso(observed),
            "age_seconds": round(age_seconds, 3),
            "source": quote.source,
            "state": state,
            "usable": usable,
        }

    def status(self) -> dict[str, object]:
        current = datetime.now(timezone.utc)
        with self._lock:
            self._prune_expired_clients_locked(current)
            return {
                **self._universe_payload_locked(),
                "quote_count": len(self._quotes),
                "subscription_health_count": len(self._subscription_health),
                "subscription_health_reported_at": _iso(self._subscription_health_reported_at),
                "fresh_tick_required": sorted(self._fresh_tick_required),
                "max_tickers": self.max_tickers,
            }
