from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

# Keep direct `python tests/test_krx_quotes.py` execution self-contained and off the developer/operating DB.
_TEST_ROOT = Path(__file__).resolve().parents[1]
if str(_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(_TEST_ROOT))

_DIRECT_TEST_RUNTIME = None
if os.environ.get("MARKET_AI_TEST_ISOLATED") != "1":
    _DIRECT_TEST_RUNTIME = tempfile.TemporaryDirectory(prefix="market-ai-unittest-")
    _direct_root = Path(_DIRECT_TEST_RUNTIME.name)
    (_direct_root / "db").mkdir(parents=True, exist_ok=True)
    os.environ["MARKET_AI_HOME"] = str(_direct_root)
    os.environ["MARKET_AI_DB_PATH"] = str(_direct_root / "db" / "market_signal.db")
    os.environ["MARKET_AI_TEST_ISOLATED"] = "1"

from sqlalchemy import delete, select

from db.database import Base, SessionLocal, engine
from db.models import MarketPrice, MarketSnapshot
from db.migrations import migrate_market_snapshot_business_time, migrate_market_snapshot_change_amount
from bridges.krx_quotes import KrxQuote, KrxQuoteService
from bridges.kis_efriend import (
    KisEFriendBridgeService,
    KisEFriendHeartbeat,
    KisEFriendMarketTick,
    KisEFriendTick,
)


OPEN_DAY = date(2026, 9, 11)

TEST_BOOTSTRAP_TICKERS = (
    "005930", "000660", "005380", "069500", "0163Y0",
    "395160", "445290", "009150", "278530", "448330",
)

OPEN_NOW = datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)  # 10:00 KST


class KrxQuoteServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            migrate_market_snapshot_business_time(db)
            migrate_market_snapshot_change_amount(db)

    def make_service(self):
        return KrxQuoteService(krx_open_dates=frozenset({OPEN_DAY}))



    def test_kospi200_ingest_does_not_require_stock_change_amount(self):
        class FakeDb:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def scalar(self, *_args, **_kwargs):
                return None

        bridge = KisEFriendBridgeService()
        tick = KisEFriendTick(
            instrument_code="101V6000",
            service="FC_R",
            session="day",
            business_time="100000",
            price=550.25,
            change_pct=0.42,
        )

        with patch.object(
            bridge,
            "_validate_expected_route",
            return_value=tick.instrument_code,
        ), patch(
            "bridges.kis_efriend.SessionLocal",
            side_effect=lambda: FakeDb(),
        ), patch(
            "bridges.kis_efriend.save_market_observation"
        ) as save_observation:
            result = bridge.ingest_tick(tick)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["symbol"], "FUTURES:KOSPI200")
        save_observation.assert_called_once()
        kwargs = save_observation.call_args.kwargs
        self.assertEqual(kwargs["price"], 550.25)
        self.assertEqual(kwargs["change_pct"], 0.42)
        self.assertNotIn("change_amount", kwargs)

    def test_change_amount_is_preserved_in_live_quote_snapshot(self):
        service = KrxQuoteService(
            bootstrap_tickers=["005930"],
            krx_open_dates=frozenset({OPEN_DAY}),
        )
        observed_at = datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)
        service.ingest_quote(
            ticker="005930",
            price=80300.0,
            change_pct=1.52,
            change_amount=1200.0,
            business_time="140000",
            cumulative_volume=123,
            ask1=None,
            bid1=None,
            bridge_tick_count=7,
            observed_at=observed_at,
        )
        item = service.quote_snapshot(
            ["005930"],
            bridge_connected=True,
            client_id="desktop",
            ticker_types={"005930": "개별주식"},
            now=observed_at,
        )["items"][0]
        self.assertEqual(item["change_amount"], 1200.0)
        self.assertEqual(item["change_pct"], 1.52)

    def test_individual_stock_extended_session_boundaries_do_not_change_etf_close(self):
        service = self.make_service()
        before_close = datetime(2026, 9, 11, 6, 29, 59, tzinfo=timezone.utc)  # 15:29:59 KST
        regular_close = datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)  # 15:30 KST
        after_market = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)  # 16:00 KST
        before_extended_end = datetime(2026, 9, 11, 10, 59, 59, tzinfo=timezone.utc)  # 19:59:59 KST
        extended_end = datetime(2026, 9, 11, 11, 0, tzinfo=timezone.utc)  # 20:00 KST

        self.assertEqual(service.market_state_for_instrument("개별주식", now=before_close), "open")
        self.assertEqual(service.market_state_for_instrument("ETF", now=before_close), "open")
        for current in (regular_close, after_market, before_extended_end):
            self.assertEqual(service.market_state_for_instrument("개별주식", now=current), "extended")
            self.assertEqual(service.market_state_for_instrument("ETF", now=current), "closed")
        self.assertEqual(service.market_state_for_instrument("개별주식", now=extended_end), "closed")
        weekend_after_market = datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc)  # Saturday 16:00 KST
        self.assertEqual(service.market_state_for_instrument("개별주식", now=weekend_after_market), "closed")

    def test_extended_stock_quote_remains_live_but_etf_quote_is_closed(self):
        service = KrxQuoteService(
            bootstrap_tickers=["005930", "069500"],
            krx_open_dates=frozenset({OPEN_DAY}),
        )
        after_close = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)  # 16:00 KST
        for ticker in ("005930", "069500"):
            service.ingest_quote(
                ticker=ticker,
                price=10000,
                change_pct=0.1,
                business_time="160000",
                cumulative_volume=1,
                ask1=None,
                bid1=None,
                bridge_tick_count=1,
                observed_at=after_close,
            )
        snapshot = service.quote_snapshot(
            ["005930", "069500"],
            bridge_connected=True,
            client_id="desktop",
            ticker_types={"005930": "개별주식", "069500": "ETF"},
            now=after_close,
        )
        items = {item["ticker"]: item for item in snapshot["items"]}
        self.assertEqual(items["005930"]["market_state"], "extended")
        self.assertEqual(items["005930"]["state"], "live")
        self.assertTrue(items["005930"]["usable"])
        self.assertEqual(items["069500"]["market_state"], "closed")
        self.assertEqual(items["069500"]["state"], "closed")
        self.assertTrue(items["069500"]["usable"])

    def test_closed_etf_recovers_same_day_kis_durable_quote_after_process_restart(self):
        service = KrxQuoteService(
            bootstrap_tickers=["069500"],
            krx_open_dates=frozenset({OPEN_DAY}),
        )
        after_close = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)  # 16:00 KST
        service.update_subscription_health(
            [{
                "ticker": "069500",
                "subscribed": True,
                "last_tick_at": datetime(2026, 9, 11, 6, 29, 50, tzinfo=timezone.utc),
                "tick_count": 10,
                "forward_success_count": 10,
                "last_error": None,
            }],
            reported_at=after_close,
        )
        durable = KrxQuote(
            ticker="069500",
            price=50123.0,
            change_amount=600.0,
            change_pct=0.42,
            business_time="152950",
            cumulative_volume=None,
            ask1=None,
            bid1=None,
            bridge_tick_count=None,
            observed_at=datetime(2026, 9, 11, 6, 29, 50, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:069500",
        )
        item = service.quote_snapshot(
            ["069500"],
            bridge_connected=True,
            client_id="desktop",
            ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": durable},
            now=after_close,
        )["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "closed")
        self.assertEqual(item["change_amount"], 600.0)
        self.assertEqual(item["market_state"], "closed")
        self.assertEqual(item["price"], 50123.0)
        self.assertEqual(item["source"], "kis-efriend:SC_R:069500")

    def test_preopen_restart_recovers_previous_completed_session_durable_quote(self):
        previous_session = date(2026, 9, 16)
        current_session = date(2026, 9, 17)
        service = KrxQuoteService(
            bootstrap_tickers=["069500"],
            krx_open_dates=frozenset({previous_session, current_session}),
        )
        preopen = datetime(2026, 9, 16, 17, 20, tzinfo=timezone.utc)  # 09/17 02:20 KST
        service.update_subscription_health(
            [{
                "ticker": "069500", "subscribed": True,
                "last_tick_at": None, "tick_count": 0, "forward_success_count": 0, "last_error": None,
            }],
            reported_at=preopen,
        )
        durable = KrxQuote(
            ticker="069500", price=50123.0, change_pct=0.42, business_time="152950",
            cumulative_volume=None, ask1=None, bid1=None, bridge_tick_count=None,
            observed_at=datetime(2026, 9, 16, 6, 29, 50, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:069500",
        )

        self.assertEqual(service.latest_completed_session_date(preopen), previous_session)
        item = service.quote_snapshot(
            ["069500"], bridge_connected=True, ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": durable}, now=preopen,
        )["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "closed")
        self.assertEqual(item["price"], 50123.0)
        self.assertEqual(item["observed_at"], "2026-09-16T06:29:50Z")

    def test_regular_open_does_not_revive_previous_completed_session_durable_quote(self):
        previous_session = date(2026, 9, 16)
        current_session = date(2026, 9, 17)
        service = KrxQuoteService(
            bootstrap_tickers=["069500"],
            krx_open_dates=frozenset({previous_session, current_session}),
        )
        regular_open = datetime(2026, 9, 17, 0, 1, tzinfo=timezone.utc)  # 09:01 KST
        service.update_subscription_health(
            [{
                "ticker": "069500", "subscribed": True,
                "last_tick_at": None, "tick_count": 0, "forward_success_count": 0, "last_error": None,
            }],
            reported_at=regular_open,
        )
        durable = KrxQuote(
            ticker="069500", price=50123.0, change_pct=0.42, business_time="152950",
            cumulative_volume=None, ask1=None, bid1=None, bridge_tick_count=None,
            observed_at=datetime(2026, 9, 16, 6, 29, 50, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:069500",
        )

        item = service.quote_snapshot(
            ["069500"], bridge_connected=True, ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": durable}, now=regular_open,
        )["items"][0]
        self.assertEqual(item["market_state"], "open")
        self.assertFalse(item["usable"])
        self.assertEqual(item["state"], "warming")

    def test_closed_holiday_uses_most_recent_completed_trading_session(self):
        previous_session = date(2026, 9, 16)
        holiday = date(2026, 9, 17)
        service = KrxQuoteService(
            bootstrap_tickers=["069500"],
            krx_open_dates=frozenset({previous_session}),
            krx_closed_dates=frozenset({holiday}),
        )
        holiday_noon = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)  # 12:00 KST
        self.assertEqual(service.latest_completed_session_date(holiday_noon), previous_session)

    def test_closed_durable_quote_does_not_revive_previous_day_or_unhealthy_subscription(self):
        after_close = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)
        previous_day = KrxQuote(
            ticker="069500",
            price=49000.0,
            change_pct=0.1,
            business_time="152900",
            cumulative_volume=None,
            ask1=None,
            bid1=None,
            bridge_tick_count=None,
            observed_at=datetime(2026, 9, 10, 6, 29, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:069500",
        )

        service = KrxQuoteService(bootstrap_tickers=["069500"], krx_open_dates=frozenset({OPEN_DAY}))
        service.update_subscription_health(
            [{
                "ticker": "069500", "subscribed": True,
                "last_tick_at": None, "tick_count": 0, "forward_success_count": 0, "last_error": None,
            }],
            reported_at=after_close,
        )
        stale_item = service.quote_snapshot(
            ["069500"], bridge_connected=True, ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": previous_day}, now=after_close,
        )["items"][0]
        self.assertFalse(stale_item["usable"])

        same_day = KrxQuote(
            ticker="069500", price=50123.0, change_pct=0.42, business_time="152950",
            cumulative_volume=None, ask1=None, bid1=None, bridge_tick_count=None,
            observed_at=datetime(2026, 9, 11, 6, 29, 50, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:069500",
        )
        unhealthy = KrxQuoteService(bootstrap_tickers=["069500"], krx_open_dates=frozenset({OPEN_DAY}))
        unhealthy.update_subscription_health(
            [{
                "ticker": "069500", "subscribed": False,
                "last_tick_at": None, "tick_count": 0, "forward_success_count": 0,
                "last_error": "RequestRealData failed",
            }],
            reported_at=after_close,
        )
        unhealthy_item = unhealthy.quote_snapshot(
            ["069500"], bridge_connected=True, ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": same_day}, now=after_close,
        )["items"][0]
        self.assertFalse(unhealthy_item["usable"])
        self.assertEqual(unhealthy_item["subscription_state"], "not_subscribed")

        unhealthy.update_subscription_health(
            [{
                "ticker": "069500", "subscribed": True,
                "last_tick_at": None, "tick_count": 0, "forward_success_count": 0,
                "last_error": None,
            }],
            reported_at=after_close.replace(second=1),
        )
        recovered_without_new_tick = unhealthy.quote_snapshot(
            ["069500"], bridge_connected=True, ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": same_day}, now=after_close.replace(second=1),
        )["items"][0]
        self.assertEqual(recovered_without_new_tick["subscription_state"], "subscribed")
        self.assertFalse(recovered_without_new_tick["usable"])
        self.assertEqual(recovered_without_new_tick["state"], "warming")

    def test_extended_stock_does_not_use_closed_durable_fallback_before_20_00(self):
        service = KrxQuoteService(bootstrap_tickers=["005930"], krx_open_dates=frozenset({OPEN_DAY}))
        at_1800 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)  # 18:00 KST
        service.update_subscription_health(
            [{
                "ticker": "005930", "subscribed": True,
                "last_tick_at": None, "tick_count": 0, "forward_success_count": 0, "last_error": None,
            }],
            reported_at=at_1800,
        )
        durable = KrxQuote(
            ticker="005930", price=80000.0, change_pct=0.5, business_time="152950",
            cumulative_volume=None, ask1=None, bid1=None, bridge_tick_count=None,
            observed_at=datetime(2026, 9, 11, 6, 29, 50, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:005930",
        )
        item = service.quote_snapshot(
            ["005930"], bridge_connected=True, ticker_types={"005930": "개별주식"},
            durable_closed_quotes={"005930": durable}, now=at_1800,
        )["items"][0]
        self.assertEqual(item["market_state"], "extended")
        self.assertFalse(item["usable"])
        self.assertEqual(item["state"], "warming")

    def test_closed_durable_quote_requires_bridge_connection(self):
        service = KrxQuoteService(bootstrap_tickers=["069500"], krx_open_dates=frozenset({OPEN_DAY}))
        after_close = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)
        durable = KrxQuote(
            ticker="069500", price=50123.0, change_pct=0.42, business_time="152950",
            cumulative_volume=None, ask1=None, bid1=None, bridge_tick_count=None,
            observed_at=datetime(2026, 9, 11, 6, 29, 50, tzinfo=timezone.utc),
            source="kis-efriend:SC_R:069500",
        )
        item = service.quote_snapshot(
            ["069500"], bridge_connected=False, ticker_types={"069500": "ETF"},
            durable_closed_quotes={"069500": durable}, now=after_close,
        )["items"][0]
        self.assertFalse(item["usable"])
        self.assertEqual(item["state"], "unavailable")

    def test_dashboard_bootstrap_warms_all_current_holdings_before_first_client_request(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )

        bootstrap = service.bridge_universe(now=OPEN_NOW)
        self.assertTrue(bootstrap["bootstrap_active"])
        self.assertEqual(
            bootstrap["tickers"],
            sorted(TEST_BOOTSTRAP_TICKERS),
        )
        # A non-Signal holding must be a valid SC_R target before Dashboard opens.
        service.ingest_quote(
            ticker="069500",
            price=50000,
            change_pct=0.3,
            business_time="095959",
            cumulative_volume=10,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            observed_at=OPEN_NOW,
        )

    def test_all_bootstrap_holdings_can_be_live_before_first_dashboard_snapshot(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )
        for index, ticker in enumerate(TEST_BOOTSTRAP_TICKERS, start=1):
            service.ingest_quote(
                ticker=ticker,
                price=10000 + index,
                change_pct=0.1,
                business_time="095959",
                cumulative_volume=index,
                ask1=None,
                bid1=None,
                bridge_tick_count=index,
                observed_at=OPEN_NOW,
            )

        snapshot = service.quote_snapshot(
            TEST_BOOTSTRAP_TICKERS,
            bridge_connected=True,
            client_id="desktop",
            now=OPEN_NOW,
        )
        self.assertEqual(snapshot["usable_count"], len(TEST_BOOTSTRAP_TICKERS))
        self.assertTrue(all(item["usable"] for item in snapshot["items"]))
        self.assertTrue(all(item["state"] == "live" for item in snapshot["items"]))

    def test_readd_lifecycle_is_equal_for_signal_baseline_and_other_dashboard_holdings(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )
        sample = ["005930", "000660", "069500", "005380", "395160", "278530"]

        for index, ticker in enumerate(sample, start=1):
            service.ingest_quote(
                ticker=ticker,
                price=10000 + index,
                change_pct=0.1,
                business_time="095959",
                cumulative_volume=index,
                ask1=None,
                bid1=None,
                bridge_tick_count=index,
                observed_at=OPEN_NOW,
            )

        initial = service.quote_snapshot(
            sample, bridge_connected=True, client_id="desktop", now=OPEN_NOW
        )
        self.assertEqual([item["state"] for item in initial["items"]], ["live"] * len(sample))

        service.request_universe([], client_id="desktop", now=OPEN_NOW)
        readded = service.quote_snapshot(
            sample, bridge_connected=True, client_id="desktop", now=OPEN_NOW
        )
        self.assertEqual(
            [item["state"] for item in readded["items"]],
            ["warming"] * len(sample),
        )
        self.assertTrue(all(not item["usable"] for item in readded["items"]))

        for index, ticker in enumerate(sample, start=1):
            service.ingest_quote(
                ticker=ticker,
                price=20000 + index,
                change_pct=0.2,
                business_time="100001",
                cumulative_volume=index + 10,
                ask1=None,
                bid1=None,
                bridge_tick_count=index + 10,
                observed_at=OPEN_NOW,
            )

        refreshed = service.quote_snapshot(
            sample, bridge_connected=True, client_id="desktop", now=OPEN_NOW
        )
        self.assertEqual([item["state"] for item in refreshed["items"]], ["live"] * len(sample))
        self.assertTrue(all(item["usable"] for item in refreshed["items"]))

    def test_remote_request_cannot_displace_local_restart_bootstrap_before_first_local_lease(self):
        bootstrap = [f"{300000 + index:06d}" for index in range(20)]
        service = KrxQuoteService(
            bootstrap_tickers=bootstrap,
            krx_open_dates=frozenset({OPEN_DAY}),
        )

        # A remote client is lower priority than the durable local warm-start set.
        # Even an explicit empty remote lease must not release bootstrap ownership.
        remote_state = service.request_universe(
            [],
            client_id="remote-phone",
            now=OPEN_NOW,
        )
        self.assertTrue(remote_state["bootstrap_active"])
        self.assertTrue(set(bootstrap).issubset(set(remote_state["dashboard_tickers"])))
        self.assertTrue(set(bootstrap).issubset(set(remote_state["tickers"])))

        # Remote admission must also account for the reserved bootstrap capacity.
        remote_extra = [f"{400000 + index:06d}" for index in range(44)]
        with self.assertRaisesRegex(ValueError, "too many KRX tickers"):
            service.request_universe(
                remote_extra,
                client_id="remote-phone",
                now=OPEN_NOW + timedelta(seconds=1),
            )

        # The first local lease is authoritative and releases the bootstrap set.
        local = ["069500", "395160"]
        local_state = service.request_universe(
            local,
            client_id="desktop-local",
            now=OPEN_NOW + timedelta(seconds=2),
        )
        self.assertFalse(local_state["bootstrap_active"])
        self.assertEqual(local_state["dashboard_tickers"], sorted(local))
        self.assertTrue(set(bootstrap).isdisjoint(set(local_state["dashboard_tickers"])))

    def test_rejected_first_local_request_does_not_release_bootstrap(self):
        bootstrap = ["300001", "300002"]
        service = KrxQuoteService(
            bootstrap_tickers=bootstrap,
            max_tickers=4,
            krx_open_dates=frozenset({OPEN_DAY}),
        )

        # Four local tickers are individually within max_tickers, but together with the
        # immutable two-ticker Signal baseline they exceed physical capacity. Rejection
        # must be transactional: the durable local restart bootstrap remains protected.
        with self.assertRaisesRegex(ValueError, "too many KRX tickers"):
            service.request_universe(
                ["400001", "400002", "400003", "400004"],
                client_id="desktop-local",
                now=OPEN_NOW,
            )

        state = service.bridge_universe(now=OPEN_NOW)
        self.assertTrue(state["bootstrap_active"])
        self.assertEqual(state["active_client_count"], 0)
        self.assertEqual(state["dashboard_tickers"], bootstrap)
        self.assertEqual(
            state["tickers"],
            sorted({"005930", "000660", *bootstrap}),
        )

    def test_first_authoritative_dashboard_request_replaces_bootstrap_but_keeps_signal_baseline(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )

        reconciled = service.request_universe(
            ["005380", "069500"],
            client_id="desktop",
            now=OPEN_NOW,
        )
        self.assertFalse(reconciled["bootstrap_active"])
        self.assertEqual(
            reconciled["tickers"],
            ["000660", "005380", "005930", "069500"],
        )
        self.assertNotIn("395160", reconciled["tickers"])

    def test_empty_authoritative_dashboard_request_releases_bootstrap_holdings(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )

        reconciled = service.request_universe([], client_id="desktop", now=OPEN_NOW)
        self.assertFalse(reconciled["bootstrap_active"])
        self.assertEqual(reconciled["tickers"], ["000660", "005930"])

    def test_empty_ticker_query_parses_as_empty_authoritative_universe(self):
        service = self.make_service()
        self.assertEqual(service.parse_ticker_query(""), ())
        self.assertEqual(service.parse_ticker_query("   "), ())


    def test_bridge_universe_separates_dashboard_holdings_from_signal_baseline(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )

        bootstrap = service.bridge_universe(now=OPEN_NOW)
        self.assertEqual(
            bootstrap["dashboard_tickers"],
            sorted(TEST_BOOTSTRAP_TICKERS),
        )
        self.assertEqual(bootstrap["signal_baseline_tickers"], ["000660", "005930"])
        self.assertEqual(bootstrap["market_state"], "open")

        service.request_universe([], client_id="desktop", now=OPEN_NOW)
        empty = service.bridge_universe(now=OPEN_NOW)
        self.assertEqual(empty["dashboard_tickers"], [])
        self.assertEqual(empty["tickers"], ["000660", "005930"])

        service.request_universe(["005930", "069500"], client_id="desktop", now=OPEN_NOW)
        active = service.bridge_universe(now=OPEN_NOW)
        self.assertEqual(active["dashboard_tickers"], ["005930", "069500"])
        self.assertEqual(active["tickers"], ["000660", "005930", "069500"])

    def test_dashboard_monitor_universe_tracks_multi_client_union_including_baseline_holdings(self):
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        service.request_universe(["005930"], client_id="desktop", now=OPEN_NOW)
        service.request_universe(["069500"], client_id="iphone", now=OPEN_NOW)

        combined = service.bridge_universe(now=OPEN_NOW)
        self.assertEqual(combined["dashboard_tickers"], ["005930", "069500"])
        self.assertEqual(combined["tickers"], ["000660", "005930", "069500"])

        service.request_universe([], client_id="desktop", now=OPEN_NOW.replace(second=5))
        remaining = service.bridge_universe(now=OPEN_NOW.replace(second=5))
        self.assertEqual(remaining["dashboard_tickers"], ["069500"])
        # 005930 remains subscribed only because it is a Signal baseline, not a holding.
        self.assertEqual(remaining["tickers"], ["000660", "005930", "069500"])

    def test_dashboard_monitor_universe_stays_sticky_after_lease_expiry_until_next_reconcile(self):
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        service.request_universe(["005380", "395160"], client_id="desktop", now=OPEN_NOW)

        sticky = service.bridge_universe(now=OPEN_NOW.replace(second=21))
        self.assertEqual(sticky["active_client_count"], 0)
        self.assertEqual(sticky["dashboard_tickers"], ["005380", "395160"])

        service.request_universe([], client_id="desktop", now=OPEN_NOW.replace(second=22))
        reconciled = service.bridge_universe(now=OPEN_NOW.replace(second=22))
        self.assertEqual(reconciled["dashboard_tickers"], [])

    def test_web_monitor_read_only_snapshot_does_not_renew_client_lease(self):
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        service.request_universe(["005380"], client_id="desktop", now=OPEN_NOW)

        snapshot = service.monitor_quotes(
            bridge_connected=True,
            now=OPEN_NOW.replace(second=15),
        )
        self.assertEqual([item["ticker"] for item in snapshot["items"]], ["005380"])

        expired = service.bridge_universe(now=OPEN_NOW.replace(second=21))
        self.assertEqual(expired["active_client_count"], 0)
        self.assertEqual(expired["dashboard_tickers"], ["005380"])

    def test_ticker_identity_preserves_leading_zero_and_letters(self):
        service = self.make_service()
        self.assertEqual(service.normalize_ticker("005930"), "005930")
        self.assertEqual(service.normalize_ticker("0163y0"), "0163Y0")
        with self.assertRaises(ValueError):
            service.normalize_ticker("5930")

    def test_universe_is_deduplicated_versioned_and_keeps_baseline(self):
        service = self.make_service()
        first = service.request_universe(["005380", "395160", "395160"])
        self.assertEqual(first["version"], 2)
        self.assertEqual(
            first["tickers"],
            ["000660", "005380", "005930", "395160"],
        )
        same = service.request_universe(["395160", "005380"])
        self.assertEqual(same["version"], 2)
        changed = service.request_universe(["0163Y0"])
        self.assertEqual(changed["version"], 3)
        self.assertEqual(changed["tickers"], ["000660", "005930", "0163Y0"])

    def test_removed_ticker_quote_is_pruned_before_readd(self):
        service = self.make_service()
        service.request_universe(["005380"])
        service.ingest_quote(
            ticker="005380",
            price=200000,
            change_pct=1.0,
            business_time="100000",
            cumulative_volume=1,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            observed_at=OPEN_NOW,
        )
        service.request_universe([])
        service.request_universe(["005380"])
        snapshot = service.quote_snapshot(["005380"], bridge_connected=True, now=OPEN_NOW)
        self.assertFalse(snapshot["items"][0]["usable"])
        self.assertEqual(snapshot["items"][0]["state"], "warming")

    def test_live_quote_does_not_use_age_as_stale_signal(self):
        service = self.make_service()
        service.request_universe(["0163Y0"])
        old_same_day = datetime(2026, 9, 10, 23, 30, tzinfo=timezone.utc)  # 08:30 KST
        service.ingest_quote(
            ticker="0163Y0",
            price=12345,
            change_pct=0.1,
            business_time="083000",
            cumulative_volume=5,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            observed_at=old_same_day,
        )
        snapshot = service.quote_snapshot(["0163Y0"], bridge_connected=True, now=OPEN_NOW)
        item = snapshot["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "live")
        self.assertGreater(item["age_seconds"], 1000)

    def test_disconnect_marks_existing_quote_stale(self):
        service = self.make_service()
        service.request_universe(["005380"])
        service.ingest_quote(
            ticker="005380",
            price=200000,
            change_pct=None,
            business_time="100000",
            cumulative_volume=None,
            ask1=None,
            bid1=None,
            bridge_tick_count=3,
            observed_at=OPEN_NOW,
        )
        snapshot = service.quote_snapshot(["005380"], bridge_connected=False, now=OPEN_NOW)
        item = snapshot["items"][0]
        self.assertFalse(item["usable"])
        self.assertEqual(item["state"], "stale")

    def test_dynamic_market_tick_requires_universe_and_persists_snapshot_without_history(self):
        ticker = "123456"
        symbol = f"KRX:{ticker}"
        with SessionLocal() as db:
            db.execute(delete(MarketPrice).where(MarketPrice.symbol == symbol))
            snapshot = db.get(MarketSnapshot, symbol)
            if snapshot is not None:
                db.delete(snapshot)
            db.commit()

        try:
            quotes = self.make_service()
            bridge = KisEFriendBridgeService(quote_service=quotes)
            tick = KisEFriendMarketTick(
                symbol=symbol,
                instrument_code=ticker,
                service="SC_R",
                business_time="100000",
                price=200000,
                change_pct=0.25,
            )
            with self.assertRaises(ValueError):
                bridge.ingest_market_tick(tick)

            quotes.request_universe([ticker])
            result = bridge.ingest_market_tick(tick)
            self.assertTrue(result["accepted"])
            self.assertTrue(result["quote_store_updated"])
            self.assertTrue(result["durable_snapshot_written"])
            self.assertFalse(result["history_written"])

            with SessionLocal() as db:
                snapshot = db.get(MarketSnapshot, symbol)
                self.assertIsNotNone(snapshot)
                self.assertEqual(float(snapshot.price), 200000.0)
                self.assertEqual(float(snapshot.change_pct), 0.25)
                self.assertEqual(snapshot.business_time, "100000")
                history = db.scalar(
                    select(MarketPrice.id).where(MarketPrice.symbol == symbol).limit(1)
                )
                self.assertIsNone(history)
        finally:
            with SessionLocal() as db:
                db.execute(delete(MarketPrice).where(MarketPrice.symbol == symbol))
                snapshot = db.get(MarketSnapshot, symbol)
                if snapshot is not None:
                    db.delete(snapshot)
                db.commit()


    def test_default_client_lease_has_safe_margin_for_10_second_dashboard_polling(self):
        service = self.make_service()
        self.assertEqual(service.client_lease_seconds, 120)

        service.request_universe(["005380"], client_id="desktop", now=OPEN_NOW)
        later = service.request_universe(
            ["395160"],
            client_id="iphone",
            now=OPEN_NOW + timedelta(seconds=50),
        )
        # Several delayed 10s polling cycles must not expire another still-live visible client.
        self.assertEqual(later["active_client_count"], 2)
        self.assertIn("005380", later["dashboard_tickers"])
        self.assertIn("395160", later["dashboard_tickers"])

    def test_signal_baseline_readd_waits_for_fresh_dashboard_tick_like_other_holdings(self):
        service = KrxQuoteService(
            bootstrap_tickers=TEST_BOOTSTRAP_TICKERS,
            krx_open_dates=frozenset({OPEN_DAY}),
        )
        # Bootstrap can prewarm the initial holdings, including the Signal baseline.
        service.ingest_quote(
            ticker="005930",
            price=259000,
            change_pct=-3.0,
            business_time="095959",
            cumulative_volume=10,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            observed_at=OPEN_NOW,
        )

        # Authoritative empty removes Dashboard valuation eligibility/cache while the
        # physical Signal baseline stream is still allowed to exist.
        service.request_universe([], client_id="desktop", now=OPEN_NOW)
        self.assertFalse(service.is_dashboard_requested("005930"))
        self.assertTrue(service.is_requested("005930"))

        class FakeDb:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def get(self, *_args, **_kwargs):
                return None
            def scalar(self, *_args, **_kwargs):
                return None

        bridge = KisEFriendBridgeService(quote_service=service)
        tick = KisEFriendMarketTick(
            symbol="KRX:005930",
            instrument_code="005930",
            service="SC_R",
            business_time="100000",
            price=260000,
            change_pct=-2.5,
        )

        with patch("bridges.kis_efriend.SessionLocal", side_effect=lambda: FakeDb()), patch(
            "bridges.kis_efriend.save_market_observation"
        ):
            signal_only = bridge.ingest_market_tick(tick, observed_at=OPEN_NOW)
        self.assertTrue(signal_only["accepted"])
        self.assertFalse(signal_only["quote_store_updated"])

        readded = service.quote_snapshot(
            ["005930"],
            bridge_connected=True,
            client_id="desktop",
            now=OPEN_NOW,
        )
        self.assertEqual(readded["items"][0]["state"], "warming")
        self.assertFalse(readded["items"][0]["usable"])

        with patch("bridges.kis_efriend.SessionLocal", side_effect=lambda: FakeDb()), patch(
            "bridges.kis_efriend.save_market_observation"
        ):
            fresh = bridge.ingest_market_tick(tick, observed_at=OPEN_NOW)
        self.assertTrue(fresh["quote_store_updated"])

        live = service.quote_snapshot(
            ["005930"],
            bridge_connected=True,
            client_id="desktop",
            now=OPEN_NOW,
        )
        self.assertEqual(live["items"][0]["state"], "live")
        self.assertTrue(live["items"][0]["usable"])

    def test_multi_client_leases_union_without_universe_thrashing(self):
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        first = service.request_universe(["005380"], client_id="desktop", now=OPEN_NOW)
        self.assertEqual(first["version"], 2)
        second = service.request_universe(["395160"], client_id="iphone", now=OPEN_NOW)
        self.assertEqual(second["version"], 3)
        self.assertEqual(
            second["tickers"],
            ["000660", "005380", "005930", "395160"],
        )
        # A refresh from the older/smaller client must not delete the other client's ticker.
        refreshed = service.request_universe(
            ["005380"],
            client_id="desktop",
            now=OPEN_NOW.replace(second=5),
        )
        self.assertEqual(refreshed["version"], 3)
        self.assertEqual(refreshed["tickers"], second["tickers"])
        self.assertEqual(refreshed["active_client_count"], 2)

    def test_client_empty_universe_authoritatively_removes_only_that_clients_dynamic_tickers(self):
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        service.request_universe(["005380", "009150"], client_id="desktop", now=OPEN_NOW)
        service.request_universe(["395160"], client_id="iphone", now=OPEN_NOW)

        reconciled = service.request_universe(
            [],
            client_id="desktop",
            now=OPEN_NOW.replace(second=5),
        )

        self.assertEqual(reconciled["active_client_count"], 2)
        self.assertEqual(reconciled["tickers"], ["000660", "005930", "395160"])
        self.assertNotIn("005380", reconciled["tickers"])
        self.assertNotIn("009150", reconciled["tickers"])

        rejoined = service.request_universe(
            ["005380"],
            client_id="desktop",
            now=OPEN_NOW.replace(second=6),
        )
        self.assertEqual(rejoined["tickers"], ["000660", "005380", "005930", "395160"])

    def test_expired_client_lease_prunes_only_its_tickers_and_cached_quote(self):
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        service.request_universe(["005380"], client_id="desktop", now=OPEN_NOW)
        service.request_universe(["395160"], client_id="iphone", now=OPEN_NOW)
        service.ingest_quote(
            ticker="395160",
            price=12000,
            change_pct=0.5,
            business_time="100000",
            cumulative_volume=10,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            observed_at=OPEN_NOW,
        )
        # Refresh only desktop before the original 20s lease expires.
        service.request_universe(
            ["005380"],
            client_id="desktop",
            now=OPEN_NOW.replace(second=15),
        )
        sticky = service.bridge_universe(now=OPEN_NOW.replace(second=21))
        self.assertEqual(sticky["tickers"], ["000660", "005380", "005930", "395160"])
        self.assertEqual(sticky["active_client_count"], 1)

        # Lease expiry alone is sticky so a hidden phone cannot destroy the closing quote.
        # The next active client request is authoritative and removes the expired client's ticker.
        reconciled = service.request_universe(
            ["005380"],
            client_id="desktop",
            now=OPEN_NOW.replace(second=22),
        )
        self.assertEqual(reconciled["tickers"], ["000660", "005380", "005930"])

        # Rejoining iphone after an explicit reconcile must warm from a new tick;
        # the old cached quote was pruned when the active client removed it.
        snapshot = service.quote_snapshot(
            ["395160"],
            bridge_connected=True,
            client_id="iphone",
            now=OPEN_NOW.replace(second=23),
        )
        self.assertEqual(snapshot["items"][0]["state"], "warming")
        self.assertFalse(snapshot["items"][0]["usable"])


    def test_expired_lease_does_not_drop_closing_quote_until_an_active_client_reconciles(self):
        closed_now = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)  # 16:00 KST
        service = KrxQuoteService(
            krx_open_dates=frozenset({OPEN_DAY}),
            client_lease_seconds=20,
        )
        service.request_universe(["395160"], client_id="iphone", now=OPEN_NOW)
        service.ingest_quote(
            ticker="395160",
            price=12345,
            change_pct=0.2,
            business_time="152959",
            cumulative_volume=100,
            ask1=None,
            bid1=None,
            bridge_tick_count=4,
            observed_at=datetime(2026, 9, 11, 6, 29, 59, tzinfo=timezone.utc),
        )
        # The iphone lease is long expired, but Bridge polling alone keeps the subscribed
        # universe/quote sticky so reopening after the close can still return CLOSED.
        universe = service.bridge_universe(now=closed_now)
        self.assertIn("395160", universe["tickers"])
        snapshot = service.quote_snapshot(
            ["395160"],
            bridge_connected=True,
            client_id="iphone",
            now=closed_now,
        )
        self.assertTrue(snapshot["items"][0]["usable"])
        self.assertEqual(snapshot["items"][0]["state"], "closed")

    def test_per_ticker_subscription_health_marks_only_failed_stream_stale(self):
        service = self.make_service()
        service.request_universe(["005380", "009150"])
        for ticker, price in (("005380", 200000), ("009150", 150000)):
            service.ingest_quote(
                ticker=ticker,
                price=price,
                change_pct=0.1,
                business_time="100000",
                cumulative_volume=10,
                ask1=None,
                bid1=None,
                bridge_tick_count=1,
                observed_at=OPEN_NOW,
            )

        service.update_subscription_health(
            [
                {
                    "ticker": "005380",
                    "subscribed": True,
                    "last_tick_at": OPEN_NOW,
                    "tick_count": 10,
                    "forward_success_count": 10,
                    "last_error": None,
                },
                {
                    "ticker": "009150",
                    "subscribed": False,
                    "last_tick_at": OPEN_NOW,
                    "tick_count": 8,
                    "forward_success_count": 8,
                    "last_error": "RequestRealData failed",
                },
            ],
            reported_at=OPEN_NOW,
        )
        snapshot = service.quote_snapshot(
            ["005380", "009150"],
            bridge_connected=True,
            now=OPEN_NOW,
        )
        items = {item["ticker"]: item for item in snapshot["items"]}
        self.assertTrue(items["005380"]["usable"])
        self.assertEqual(items["005380"]["state"], "live")
        self.assertEqual(items["005380"]["subscription_state"], "subscribed")
        self.assertFalse(items["009150"]["usable"])
        self.assertEqual(items["009150"]["state"], "stale")
        self.assertEqual(items["009150"]["subscription_state"], "not_subscribed")
        self.assertEqual(snapshot["usable_count"], 1)

    def test_recovered_subscription_requires_new_tick_before_old_quote_is_usable(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=150000,
            change_pct=0.1,
            business_time="100000",
            cumulative_volume=10,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            observed_at=OPEN_NOW,
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": False,
                "last_tick_at": OPEN_NOW,
                "tick_count": 1,
                "forward_success_count": 1,
                "last_error": "stream failed",
            }],
            reported_at=OPEN_NOW,
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 1,
                "forward_success_count": 1,
                "last_error": None,
            }],
            reported_at=OPEN_NOW.replace(second=5),
        )

        recovered_without_tick = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW.replace(second=5),
        )["items"][0]
        self.assertEqual(recovered_without_tick["subscription_state"], "subscribed")
        self.assertEqual(recovered_without_tick["state"], "stale")
        self.assertFalse(recovered_without_tick["usable"])

        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100005",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            observed_at=OPEN_NOW.replace(second=5),
        )
        after_new_tick = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW.replace(second=5),
        )["items"][0]
        self.assertTrue(after_new_tick["usable"])
        self.assertEqual(after_new_tick["state"], "live")
        self.assertEqual(after_new_tick["price"], 151000)

    def test_repeated_unhealthy_heartbeat_does_not_rearm_after_new_quote_arrives(self):
        service = self.make_service()
        service.request_universe(["009150"])
        unhealthy = [{
            "ticker": "009150",
            "subscribed": True,
            "last_tick_at": OPEN_NOW,
            "tick_count": 2,
            "forward_success_count": 1,
            "last_error": "previous forward error",
        }]
        service.update_subscription_health(unhealthy, reported_at=OPEN_NOW)
        # The Market AI server accepts the next quote before the Bridge async call
        # has necessarily cleared its previous LastError locally.
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100001",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            observed_at=OPEN_NOW.replace(second=1),
        )
        service.update_subscription_health(unhealthy, reported_at=OPEN_NOW.replace(second=2))
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW.replace(second=1),
                "tick_count": 2,
                "forward_success_count": 2,
                "last_error": None,
            }],
            reported_at=OPEN_NOW.replace(second=3),
        )
        item = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW.replace(second=3),
        )["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "live")
        self.assertEqual(item["price"], 151000)

    def test_delayed_unhealthy_heartbeat_older_than_accepted_tick_cannot_rearm_freshness(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 1,
                "forward_success_count": 1,
                "last_error": None,
            }],
            reported_at=OPEN_NOW,
        )

        # The SC_R event is generated after the heartbeat snapshot, but its HTTP
        # request completes first.  Server arrival order must not let the delayed
        # older unhealthy heartbeat invalidate this accepted quote.
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100003",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            bridge_sent_at=OPEN_NOW + timedelta(seconds=3),
            observed_at=OPEN_NOW + timedelta(seconds=4),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": False,
                "last_tick_at": OPEN_NOW,
                "tick_count": 1,
                "forward_success_count": 1,
                "last_error": "old delayed error",
            }],
            reported_at=OPEN_NOW + timedelta(seconds=2),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW + timedelta(seconds=3),
                "tick_count": 2,
                "forward_success_count": 2,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=5),
        )

        item = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW + timedelta(seconds=6),
        )["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "live")
        self.assertEqual(item["price"], 151000)
        self.assertNotIn("009150", service.status()["fresh_tick_required"])

    def test_new_error_after_ack_stales_quote_when_forward_success_count_lags_tick_count(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100000",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=100,
            bridge_sent_at=OPEN_NOW,
            observed_at=OPEN_NOW + timedelta(seconds=1),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 100,
                "forward_success_count": 3,
                "last_forwarded_tick_count": 100,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=2),
        )

        # A later COM/read failure can happen without a new successful tick, so
        # TickCount stays at 100. ForwardSuccessCount is only a throttled HTTP
        # success counter and can legitimately be far smaller. The explicit
        # acknowledged tick marker proves this error happened after quote 100.
        unhealthy = [{
            "ticker": "009150",
            "subscribed": True,
            "last_tick_at": OPEN_NOW,
            "tick_count": 100,
            "forward_success_count": 3,
            "last_forwarded_tick_count": 100,
            "last_error": "COM read failed",
        }]
        service.update_subscription_health(
            unhealthy,
            reported_at=OPEN_NOW + timedelta(seconds=10),
        )

        first = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=10)
        )["items"][0]
        self.assertFalse(first["usable"])
        self.assertEqual(first["state"], "stale")
        self.assertEqual(first["subscription_state"], "error")
        self.assertEqual(first["subscription_error"], "COM read failed")

        # Repeated heartbeats with no fresh tick must not resurrect the old quote.
        service.update_subscription_health(
            unhealthy,
            reported_at=OPEN_NOW + timedelta(seconds=60),
        )
        repeated = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=60)
        )["items"][0]
        self.assertFalse(repeated["usable"])
        self.assertEqual(repeated["state"], "stale")

    def test_b01_error_recovers_only_after_new_tick(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100000",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=100,
            bridge_sent_at=OPEN_NOW,
            observed_at=OPEN_NOW + timedelta(seconds=1),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 100,
                "forward_success_count": 3,
                "last_forwarded_tick_count": 100,
                "last_error": "COM read failed",
            }],
            reported_at=OPEN_NOW + timedelta(seconds=10),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 100,
                "forward_success_count": 3,
                "last_forwarded_tick_count": 100,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=20),
        )

        heartbeat_only = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=20)
        )["items"][0]
        self.assertFalse(heartbeat_only["usable"])
        self.assertEqual(heartbeat_only["state"], "stale")

        service.ingest_quote(
            ticker="009150",
            price=152000,
            change_pct=0.3,
            business_time="100021",
            cumulative_volume=12,
            ask1=None,
            bid1=None,
            bridge_tick_count=101,
            bridge_sent_at=OPEN_NOW + timedelta(seconds=21),
            observed_at=OPEN_NOW + timedelta(seconds=21),
        )
        recovered = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=22)
        )["items"][0]
        self.assertTrue(recovered["usable"])
        self.assertEqual(recovered["state"], "live")
        self.assertEqual(recovered["price"], 152000)

    def test_pre_ack_heartbeat_for_same_tick_cannot_override_accepted_quote(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 1,
                "forward_success_count": 1,
                "last_forwarded_tick_count": 1,
                "last_error": None,
            }],
            reported_at=OPEN_NOW,
        )
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100003",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            bridge_sent_at=OPEN_NOW + timedelta(seconds=2),
            observed_at=OPEN_NOW + timedelta(seconds=3),
        )

        # Heartbeat was built after the SC_R event but before the Bridge received
        # the successful tick POST response. The exact acknowledgement marker still
        # points at tick 1, so this same-tick unhealthy snapshot predates quote 2.
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW + timedelta(seconds=2),
                "tick_count": 2,
                "forward_success_count": 1,
                "last_forwarded_tick_count": 1,
                "last_error": "previous forward error",
            }],
            reported_at=OPEN_NOW + timedelta(seconds=2, microseconds=500000),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW + timedelta(seconds=2),
                "tick_count": 2,
                "forward_success_count": 2,
                "last_forwarded_tick_count": 2,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=4),
        )
        item = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=5)
        )["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "live")

    def test_bridge_reconnect_requires_post_gap_tick_before_old_quote_revives(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=150000,
            change_pct=0.1,
            business_time="100000",
            cumulative_volume=10,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            bridge_sent_at=OPEN_NOW,
            observed_at=OPEN_NOW,
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 1,
                "forward_success_count": 1,
                "last_error": None,
            }],
            reported_at=OPEN_NOW,
        )

        reconnect = service.mark_bridge_reconnected(
            disconnected_since=OPEN_NOW + timedelta(seconds=30),
        )
        self.assertEqual(reconnect["fresh_tick_required"], ["009150"])

        recovered_heartbeat_only = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW + timedelta(seconds=31),
        )["items"][0]
        self.assertFalse(recovered_heartbeat_only["usable"])
        self.assertEqual(recovered_heartbeat_only["state"], "stale")

        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100032",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            bridge_sent_at=OPEN_NOW + timedelta(seconds=32),
            observed_at=OPEN_NOW + timedelta(seconds=32),
        )
        after_new_tick = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW + timedelta(seconds=33),
        )["items"][0]
        self.assertTrue(after_new_tick["usable"])
        self.assertEqual(after_new_tick["state"], "live")

    def test_tick_received_during_stale_gap_is_already_recovery_evidence(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100031",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            bridge_sent_at=OPEN_NOW + timedelta(seconds=31),
            observed_at=OPEN_NOW + timedelta(seconds=31),
        )
        reconnect = service.mark_bridge_reconnected(
            disconnected_since=OPEN_NOW + timedelta(seconds=30),
        )
        self.assertEqual(reconnect["fresh_tick_required"], [])
        self.assertNotIn("009150", service.status()["fresh_tick_required"])

    def test_fast_bridge_epoch_reset_requires_new_tick_even_without_stale_heartbeat_gap(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=150000,
            change_pct=0.1,
            business_time="100000",
            cumulative_volume=10,
            ask1=None,
            bid1=None,
            bridge_tick_count=10,
            bridge_sent_at=OPEN_NOW,
            observed_at=OPEN_NOW,
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW,
                "tick_count": 10,
                "forward_success_count": 10,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=1),
        )

        # Native Bridge/stream restarts quickly enough that heartbeat connectivity
        # never visibly expires, but its per-stream counters reset to a new epoch.
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": None,
                "tick_count": 0,
                "forward_success_count": 0,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=5),
        )
        old_quote = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=5)
        )["items"][0]
        self.assertFalse(old_quote["usable"])
        self.assertEqual(old_quote["state"], "stale")

        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100006",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=1,
            bridge_sent_at=OPEN_NOW + timedelta(seconds=6),
            observed_at=OPEN_NOW + timedelta(seconds=6),
        )
        service.update_subscription_health(
            [{
                "ticker": "009150",
                "subscribed": True,
                "last_tick_at": OPEN_NOW + timedelta(seconds=6),
                "tick_count": 1,
                "forward_success_count": 1,
                "last_error": None,
            }],
            reported_at=OPEN_NOW + timedelta(seconds=7),
        )
        new_quote = service.quote_snapshot(
            ["009150"], bridge_connected=True, now=OPEN_NOW + timedelta(seconds=8)
        )["items"][0]
        self.assertTrue(new_quote["usable"])
        self.assertEqual(new_quote["state"], "live")
        self.assertEqual(new_quote["price"], 151000)

    def test_bridge_service_detects_stale_heartbeat_gap_and_arms_quote_freshness(self):
        quotes = self.make_service()
        quotes.request_universe(["009150"])
        bridge = KisEFriendBridgeService(
            quote_service=quotes,
            heartbeat_stale_seconds=5,
        )
        bridge.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        heartbeat = KisEFriendHeartbeat(
            instrument_code="101V6000",
            service="FC_R",
            session="day",
            bridge_time=datetime.now(timezone.utc),
            tick_count=1,
            quote_subscriptions=[],
        )

        with patch.object(bridge, "_validate_service_session"), patch.object(
            bridge, "_validate_expected_route", return_value=heartbeat.instrument_code
        ), patch.object(quotes, "mark_bridge_reconnected", wraps=quotes.mark_bridge_reconnected) as mark:
            result = bridge.ingest_heartbeat(heartbeat)

        mark.assert_called_once()
        self.assertIsNotNone(result["reconnect_freshness"])


    def test_out_of_order_subscription_health_snapshot_is_ignored(self):
        service = self.make_service()
        service.request_universe(["009150"])
        service.ingest_quote(
            ticker="009150",
            price=151000,
            change_pct=0.2,
            business_time="100004",
            cumulative_volume=11,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            observed_at=OPEN_NOW.replace(second=4),
        )

        healthy = [{
            "ticker": "009150",
            "subscribed": True,
            "last_tick_at": OPEN_NOW.replace(second=4),
            "tick_count": 2,
            "forward_success_count": 2,
            "last_error": None,
        }]
        service.update_subscription_health(
            healthy,
            reported_at=OPEN_NOW.replace(second=5),
        )

        delayed_unhealthy = [{
            "ticker": "009150",
            "subscribed": True,
            "last_tick_at": OPEN_NOW,
            "tick_count": 1,
            "forward_success_count": 1,
            "last_error": "old delayed error",
        }]
        result = service.update_subscription_health(
            delayed_unhealthy,
            reported_at=OPEN_NOW.replace(second=2),
        )
        self.assertTrue(result["stale_snapshot_ignored"])

        item = service.quote_snapshot(
            ["009150"],
            bridge_connected=True,
            now=OPEN_NOW.replace(second=6),
        )["items"][0]
        self.assertEqual(item["subscription_state"], "subscribed")
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "live")
        self.assertEqual(item["price"], 151000)

    def test_low_liquidity_quote_remains_usable_when_subscription_health_is_good(self):
        service = self.make_service()
        service.request_universe(["0163Y0"])
        old_same_day = datetime(2026, 9, 10, 23, 30, tzinfo=timezone.utc)
        service.ingest_quote(
            ticker="0163Y0",
            price=12345,
            change_pct=0.1,
            business_time="083000",
            cumulative_volume=5,
            ask1=None,
            bid1=None,
            bridge_tick_count=2,
            observed_at=old_same_day,
        )
        service.update_subscription_health(
            [{
                "ticker": "0163Y0",
                "subscribed": True,
                "last_tick_at": old_same_day,
                "tick_count": 2,
                "forward_success_count": 2,
                "last_error": None,
            }],
            reported_at=OPEN_NOW,
        )
        item = service.quote_snapshot(
            ["0163Y0"],
            bridge_connected=True,
            now=OPEN_NOW,
        )["items"][0]
        self.assertTrue(item["usable"])
        self.assertEqual(item["state"], "live")
        self.assertGreater(item["age_seconds"], 1000)

    def test_local_request_reclaims_capacity_from_remote_leases(self):
        service = self.make_service()
        remote = [f"{100000 + index:06d}" for index in range(62)]
        local = [f"{200000 + index:06d}" for index in range(10)]

        remote_state = service.request_universe(remote, client_id="remote-attacker", now=OPEN_NOW)
        self.assertEqual(remote_state["remote_client_count"], 1)
        self.assertEqual(len(remote_state["tickers"]), 64)

        local_state = service.request_universe(
            local,
            client_id="desktop-local",
            now=OPEN_NOW + timedelta(seconds=1),
        )
        self.assertEqual(local_state["evicted_remote_client_count"], 1)
        self.assertEqual(local_state["remote_client_count"], 0)
        self.assertEqual(local_state["active_client_count"], 1)
        self.assertEqual(local_state["dashboard_tickers"], sorted(local))
        self.assertEqual(len(local_state["tickers"]), 12)

    def test_remote_request_uses_backend_union_capacity_not_fixed_62_cutoff(self):
        service = self.make_service()
        # 64 requested tickers are valid when the two Signal baseline tickers are
        # included in that request: total physical universe remains exactly 64.
        requested = ["005930", "000660"] + [f"{100000 + index:06d}" for index in range(62)]
        state = service.request_universe(requested, client_id="remote-full", now=OPEN_NOW)
        self.assertEqual(len(state["dashboard_tickers"]), 64)
        self.assertEqual(len(state["tickers"]), 64)

        service = self.make_service()
        nonbaseline = [f"{200000 + index:06d}" for index in range(64)]
        with self.assertRaisesRegex(ValueError, "too many KRX tickers"):
            service.request_universe(nonbaseline, client_id="remote-over-capacity", now=OPEN_NOW)

    def test_remote_request_cannot_evict_or_deny_local_capacity(self):
        service = self.make_service()
        local = [f"{200000 + index:06d}" for index in range(10)]
        remote = [f"{100000 + index:06d}" for index in range(62)]
        service.request_universe(local, client_id="desktop-local", now=OPEN_NOW)

        with self.assertRaisesRegex(ValueError, "too many KRX tickers"):
            service.request_universe(
                remote,
                client_id="remote-attacker",
                now=OPEN_NOW + timedelta(seconds=1),
            )

        state = service.bridge_universe(now=OPEN_NOW + timedelta(seconds=1))
        self.assertEqual(state["dashboard_tickers"], sorted(local))
        self.assertEqual(state["remote_client_count"], 0)
        self.assertEqual(state["active_client_count"], 1)

    def test_remote_client_count_is_bounded_in_backend_source_of_truth(self):
        service = self.make_service()
        for index in range(service.max_remote_clients):
            state = service.request_universe(
                ["069500"],
                client_id=f"remote-client-{index}",
                now=OPEN_NOW,
            )
            self.assertEqual(state["remote_client_count"], index + 1)

        with self.assertRaisesRegex(ValueError, "too many remote quote clients"):
            service.request_universe(
                ["069500"],
                client_id="remote-client-overflow",
                now=OPEN_NOW,
            )

    def test_local_priority_evicts_oldest_remote_clients_only_as_needed(self):
        service = self.make_service()
        remote_old = [f"{100000 + index:06d}" for index in range(30)]
        remote_new = [f"{110000 + index:06d}" for index in range(30)]
        local = [f"{200000 + index:06d}" for index in range(20)]
        service.request_universe(remote_old, client_id="remote-old", now=OPEN_NOW)
        service.request_universe(
            remote_new,
            client_id="remote-new",
            now=OPEN_NOW + timedelta(seconds=1),
        )

        state = service.request_universe(
            local,
            client_id="desktop-local",
            now=OPEN_NOW + timedelta(seconds=2),
        )
        self.assertEqual(state["evicted_remote_client_count"], 1)
        self.assertEqual(state["remote_client_count"], 1)
        self.assertEqual(state["active_client_count"], 2)
        self.assertTrue(set(local).issubset(set(state["dashboard_tickers"])))
        self.assertTrue(set(remote_new).issubset(set(state["dashboard_tickers"])))
        self.assertTrue(set(remote_old).isdisjoint(set(state["dashboard_tickers"])))

    def test_remote_client_can_rejoin_when_request_fits_remaining_local_capacity(self):
        service = self.make_service()
        remote_large = [f"{100000 + index:06d}" for index in range(62)]
        local = [f"{200000 + index:06d}" for index in range(10)]
        service.request_universe(remote_large, client_id="remote-phone", now=OPEN_NOW)
        service.request_universe(
            local,
            client_id="desktop-local",
            now=OPEN_NOW + timedelta(seconds=1),
        )

        state = service.request_universe(
            local,
            client_id="remote-phone",
            now=OPEN_NOW + timedelta(seconds=2),
        )
        self.assertEqual(state["remote_client_count"], 1)
        self.assertEqual(state["active_client_count"], 2)
        self.assertEqual(state["dashboard_tickers"], sorted(local))

    def test_local_dashboard_persistence_scope_excludes_remote_leases(self):
        service = self.make_service()
        service.request_universe(["005380", "069500"], client_id="desktop-local", now=OPEN_NOW)
        state = service.request_universe(
            ["069500", "395160"],
            client_id="remote-phone",
            now=OPEN_NOW + timedelta(seconds=1),
        )
        self.assertEqual(state["dashboard_tickers"], ["005380", "069500", "395160"])
        self.assertEqual(state["remote_client_count"], 1)
        self.assertEqual(service.local_dashboard_tickers(), ("005380", "069500"))

    def test_quote_client_id_validation_is_fail_closed(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.request_universe(["005380"], client_id="bad client id")

    def test_symbol_and_code_must_match(self):
        quotes = self.make_service()
        quotes.request_universe(["0163Y0"])
        bridge = KisEFriendBridgeService(quote_service=quotes)
        tick = KisEFriendMarketTick(
            symbol="KRX:0163Y0",
            instrument_code="005930",
            service="SC_R",
            business_time="100000",
            price=10000,
        )
        with self.assertRaises(ValueError):
            bridge.ingest_market_tick(tick)


if __name__ == "__main__":
    unittest.main()
