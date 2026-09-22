from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

pytest.importorskip(
    "yfinance",
    reason="optional source-QA dependency unavailable; install is not attempted automatically",
)

import pandas as pd
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app import serialize_market_snapshot_row
from collectors.yfinance_collector import YFinanceCollector
from db.database import Base
from db.models import MarketSnapshot, SignalRun
from db.signal_repository import get_latest_signal_run, save_signal_run
from market.provider_map import YahooMapping
from signals.engine import ENGINE_VERSION, build_signal
from signals.input_status import input_status


# Signal availability, coverage and checkpoint policy
def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def row(symbol, at, change=1.0, business_time=None):
    domestic = symbol.startswith("KRX:") or symbol in {"INDEX:KOSPI", "FUTURES:KOSPI200"}
    observed = utc(at) if isinstance(at, str) else at
    source = "kis-efriend:real" if domestic else "yfinance:test"
    if symbol == "FUTURES:KOSPI200":
        local = observed.astimezone(timezone(timedelta(hours=9)))
        night = local.time().hour < 6 or local.time().hour >= 18
        source = (
            "kis-efriend:night:CMEC_R:A01612"
            if night else "kis-efriend:day:FC_R:A01612"
        )
    return MarketSnapshot(symbol=symbol, observed_at=observed, price=100.0, change_pct=change,
                          source=source, business_time=business_time)


@pytest.mark.parametrize("symbol,observed,now,available,status", [
    # Closed Korean cash is not downgraded after fifteen minutes.
    ("INDEX:KOSPI", "2026-09-16T06:30Z", "2026-09-16T08:00Z", True, "closed_latest"),
    ("KRX:005930", "2026-09-16T06:00Z", "2026-09-16T08:00Z", False, "missing_close"),
    ("INDEX:KOSPI", "2026-09-16T01:00Z", "2026-09-16T01:03Z", False, "stale"),
    # Delayed feed allowance differs from KIS realtime.
    ("INDEX:SOX", "2026-09-16T14:00Z", "2026-09-16T14:15Z", True, "within_delay"),
    ("INDEX:SOX", "2026-09-16T14:00Z", "2026-09-16T14:21Z", False, "stale"),
    ("INDEX:SOX", "2026-09-15T19:59Z", "2026-09-16T08:00Z", True, "closed_latest"),
    ("INDEX:SOX", "2026-09-15T18:00Z", "2026-09-16T08:00Z", False, "missing_close"),
    # Labor Day: Friday close stays current, Friday midday outage does not.
    ("INDEX:SOX", "2026-09-04T20:00Z", "2026-09-07T18:00Z", True, "closed_latest"),
    ("INDEX:SOX", "2026-09-04T18:00Z", "2026-09-07T18:00Z", False, "missing_close"),
    ("INDEX:SOX", "2026-09-04T20:00Z", "2026-09-08T13:35Z", True, "awaiting_session"),
    ("INDEX:SOX", "2026-09-04T20:00Z", "2026-09-08T13:51Z", False, "stale"),
    # US early close and standard/daylight time are taken from the calendar.
    ("INDEX:SOX", "2026-11-27T17:59Z", "2026-11-28T08:00Z", True, "closed_latest"),
    ("INDEX:SOX", "2026-11-27T16:00Z", "2026-11-28T08:00Z", False, "missing_close"),
    ("INDEX:SOX", "2026-11-09T14:30Z", "2026-11-09T14:40Z", True, "within_delay"),
    # K200 maintenance and the reopening of its night session.
    ("FUTURES:KOSPI200", "2026-09-16T06:44:59Z", "2026-09-16T08:00Z", True, "closed_latest"),
    ("FUTURES:KOSPI200", "2026-09-16T06:44:59Z", "2026-09-16T09:03Z", False, "stale"),
    ("FUTURES:KOSPI200", "2026-09-16T14:59:50Z", "2026-09-16T15:00Z", True, "realtime"),
    # NQ maintenance 16-17 Chicago, then delayed quote grace at reopen.
    ("FUTURES:NQ", "2026-09-16T20:59Z", "2026-09-16T21:30Z", True, "closed_latest"),
    ("FUTURES:NQ", "2026-09-16T20:59Z", "2026-09-16T22:10Z", True, "awaiting_session"),
    ("FUTURES:NQ", "2026-09-16T20:59Z", "2026-09-16T22:21Z", False, "stale"),
    ("FX:USDKRW", "2026-09-18T20:59Z", "2026-09-19T12:00Z", True, "closed_latest"),
    ("FX:USDKRW", "2026-09-18T20:59Z", "2026-09-20T21:31Z", False, "stale"),
    ("INDEX:SOX", "2026-09-16T15:00Z", "2026-09-16T14:00Z", False, "invalid_time"),
])
def test_session_availability(symbol, observed, now, available, status):
    result = input_status(symbol, row(symbol, observed), utc(now))
    assert result["available"] is available
    assert result["status"] == status


def test_kis_bootstrap_timestamp_does_not_turn_old_intraday_value_into_close():
    snapshot = row("INDEX:KOSPI", "2026-09-16T08:00Z", business_time="140000")
    result = input_status(snapshot.symbol, snapshot, utc("2026-09-16T08:00Z"))
    assert result["available"] is False
    snapshot.business_time = "153000"
    assert input_status(snapshot.symbol, snapshot, utc("2026-09-16T08:00Z"))["available"] is True


def test_k200_completed_night_session_accepts_verified_kis_tail_tick():
    snapshot = row("FUTURES:KOSPI200", "2026-09-18T20:57:00Z", business_time="055700")
    snapshot.source = "kis-efriend:night:CMEC_R:A01612"
    result = input_status(snapshot.symbol, snapshot, utc("2026-09-19T00:30:00Z"))
    assert result["available"] is True
    assert result["status"] == "closed_latest"
    assert "검증된 실제 KIS" in result["reason"]


def test_k200_completed_session_tail_rejects_old_or_wrong_route_tick():
    old_tick = row("FUTURES:KOSPI200", "2026-09-18T20:20:00Z", business_time="052000")
    old_tick.source = "kis-efriend:night:CMEC_R:A01612"
    old_result = input_status(old_tick.symbol, old_tick, utc("2026-09-19T00:30:00Z"))
    assert old_result["available"] is False
    assert old_result["status"] == "missing_close"

    wrong_route = row("FUTURES:KOSPI200", "2026-09-18T20:57:00Z", business_time="055700")
    wrong_route.source = "kis-efriend:day:FC_R:A01612"
    wrong_result = input_status(wrong_route.symbol, wrong_route, utc("2026-09-19T00:30:00Z"))
    assert wrong_result["available"] is False
    assert wrong_result["status"] == "missing_close"


def test_k200_completed_day_session_accepts_verified_kis_tail_tick():
    snapshot = row("FUTURES:KOSPI200", "2026-09-18T06:40:00Z", business_time="154000")
    snapshot.source = "kis-efriend:day:FC_R:A01612"
    result = input_status(snapshot.symbol, snapshot, utc("2026-09-18T08:00:00Z"))
    assert result["available"] is True
    assert result["status"] == "closed_latest"


def test_k200_expiry_day_tail_requires_contract_after_1520_rollover():
    expiring = row("FUTURES:KOSPI200", "2026-09-10T06:18:00Z", business_time="151800")
    expiring.source = "kis-efriend:day:FC_R:A01609"
    result = input_status(expiring.symbol, expiring, utc("2026-09-10T07:00:00Z"))
    assert result["available"] is False
    assert result["status"] == "missing_close"

    next_contract = row("FUTURES:KOSPI200", "2026-09-10T06:40:00Z", business_time="154000")
    next_contract.source = "kis-efriend:day:FC_R:A01612"
    result = input_status(next_contract.symbol, next_contract, utc("2026-09-10T07:00:00Z"))
    assert result["available"] is True
    assert result["status"] == "closed_latest"


def test_k200_close_tail_respects_explicit_instrument_override(monkeypatch):
    import signals.input_status as module
    monkeypatch.setattr(module, "KIS_EFRIEND_KOSPI200_CODE", "A01609")
    snapshot = row("FUTURES:KOSPI200", "2026-09-10T06:18:00Z", business_time="151800")
    snapshot.source = "kis-efriend:day:FC_R:A01609"
    result = module.input_status(snapshot.symbol, snapshot, utc("2026-09-10T07:00:00Z"))
    assert result["available"] is True
    assert result["status"] == "closed_latest"


def test_calendar_error_is_not_silently_treated_as_usable(monkeypatch):
    import signals.input_status as module
    monkeypatch.setattr(module, "_windows", lambda *args: (_ for _ in ()).throw(ValueError("range")))
    snapshot = row("INDEX:SOX", "2026-09-16T14:00Z")
    result = module.input_status(snapshot.symbol, snapshot, utc("2026-09-16T14:01Z"))
    assert result["available"] is False and result["status"] == "calendar_unknown"


def test_snapshot_api_row_exposes_calendar_aware_input_status():
    snapshot = row("INDEX:SOX", "2026-09-04T20:00Z")
    payload = serialize_market_snapshot_row(snapshot, utc("2026-09-07T18:00Z"))
    assert payload["input_status"]["status"] == "closed_latest"
    assert payload["input_status"]["available"] is True


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value
    engine.dispose()


def populate(session, now):
    # Fixtures for Sep 16, 17:00 KST: Korean close and US previous regular close.
    for symbol in ("INDEX:KOSPI", "KRX:005930", "KRX:000660"):
        session.add(row(symbol, "2026-09-16T06:30Z"))
    session.add(row("FUTURES:KOSPI200", "2026-09-16T06:44:59Z"))
    for symbol in ("INDEX:SOX", "NASDAQ:NVDA", "NASDAQ:MU", "NASDAQ:SKHY"):
        session.add(row(symbol, "2026-09-15T20:00Z"))
    for symbol in ("FUTURES:NQ", "FX:USDKRW"):
        session.add(row(symbol, now - timedelta(minutes=10)))
    session.commit()


def calculate(session, now="2026-09-16T08:00Z", minimum=.35):
    return build_signal(session, news_lookback_hours=24, minimum_data_weight=minimum,
                        ai_news_active=False, now=utc(now))


def test_all_normal_closed_and_delayed_inputs_have_full_coverage(session):
    populate(session, utc("2026-09-16T08:00Z"))
    result = calculate(session)
    assert result.engine_version == "stage6_rule_v8"
    assert set(result.details["input_coverage"].values()) == {1.0}
    for name, summary in result.details["signal_inputs"].items():
        assert sum(item["normalized_weight"] for item in summary["basis"]) == pytest.approx(1)
        assert summary["missing_inputs"] == []
        assert summary["available"] is True
    weights = result.details["effective_weights"]["kospi"]
    assert weights["kospi_index"]["normalized_weight"] == .35
    assert weights["kospi200_futures"]["normalized_weight"] == .65
    assert result.details["signal_state"]["up_close"]["mode"] == "actual_close"
    assert "up_close" not in result.details["calibration_eligible_targets"]


def test_coverage_is_per_signal_and_remaining_weights_are_normalized(session):
    populate(session, utc("2026-09-16T08:00Z"))
    session.delete(session.get(MarketSnapshot, "INDEX:SOX"))
    session.commit()
    result = calculate(session)
    assert result.details["input_coverage"] == {
        "kospi": 1, "semiconductors": .8, "gap_up": .75, "up_close": 1,
    }
    semi = result.details["signal_inputs"]["semiconductors"]
    assert [item["key"] for item in semi["missing_inputs"]] == ["sox_index"]
    weights = result.details["effective_weights"]["semiconductors"]
    assert weights["samsung_electronics"]["normalized_weight"] == .25
    assert weights["sox_index"]["normalized_weight"] == 0


def test_all_inputs_lost_persists_unavailable_instead_of_old_signal(session):
    populate(session, utc("2026-09-16T08:00Z"))
    save_signal_run(session, calculate(session))
    session.execute(delete(MarketSnapshot))
    session.commit()
    result = calculate(session, now="2026-09-16T08:02Z")
    assert result is not None
    assert all(not state["available"] for state in result.details["signal_state"].values())
    save_signal_run(session, result)
    latest = get_latest_signal_run(session)
    assert json.loads(latest.details_json)["input_coverage"] == dict.fromkeys(
        ["kospi", "semiconductors", "gap_up", "up_close"], 0)
    assert latest.confidence == 0


def test_minimum_is_applied_per_signal_not_global_average(session):
    session.add(row("FX:USDKRW", "2026-09-16T08:00Z"))
    session.commit()
    result = calculate(session)
    assert result.details["input_coverage"]["gap_up"] == .05
    assert result.details["signal_state"]["gap_up"]["available"] is False
    assert result.details["calibration_eligible_targets"] == []


def test_gap_checkpoint_freezes_score_coverage_weights_and_missing_reasons(session):
    now = utc("2026-09-15T23:50Z")  # Sep 16 08:50 KST
    for symbol in ("FUTURES:KOSPI200", "FUTURES:NQ", "FX:USDKRW"):
        session.add(row(symbol, now - timedelta(seconds=10)))
    session.commit()  # SOX intentionally missing at forecast time.
    original = calculate(session, now=now.isoformat())
    save_signal_run(session, original)
    session.add(row("INDEX:SOX", "2026-09-15T20:00Z"))
    session.get(MarketSnapshot, "FUTURES:NQ").change_pct = -4
    session.commit()
    locked = calculate(session, now="2026-09-16T01:00Z")
    assert locked.gap_up_probability == original.gap_up_probability
    before = original.details["signal_inputs"]["gap_up"]
    after = locked.details["signal_inputs"]["gap_up"]
    for key in ("input_coverage", "basis", "missing_inputs", "basis_at"):
        assert after[key] == before[key]
    assert after["frozen"] is True and after["input_coverage"] == .75
    assert locked.details["effective_weights"]["gap_up"]["sox_index"]["effective_weight"] == 0


def test_legacy_checkpoint_preserves_weights_without_inventing_coverage(session):
    session.add(SignalRun(created_at=utc("2026-09-15T23:50Z"), engine_version="stage6_rule_v7",
                         kospi_score=60, semiconductor_score=60, gap_up_probability=70,
                         up_close_probability=60, confidence=.87, data_completeness=.95,
                         calibrated=False, details_json=json.dumps({
                             "weights": {"gap_up": {"kospi200_futures": .5, "sox_index": .25}},
                             "qualities": {"kospi200_futures": 1, "sox_index": .65},
                             "effective_weight": {"gap_up": .6625},
                         })))
    session.commit()
    result = calculate(session, now="2026-09-16T01:00Z")
    assert result.gap_up_probability == 70
    assert result.details["input_coverage"]["gap_up"] is None
    assert result.details["signal_inputs"]["gap_up"]["policy"] == "legacy"
    assert result.details["effective_weights"]["gap_up"]["sox_index"]["effective_weight"] == .1625
    assert "gap_up" not in result.details["calibration_eligible_targets"]


def test_legacy_checkpoint_without_any_input_evidence_is_not_full_coverage(session):
    session.add(SignalRun(created_at=utc("2026-09-15T23:50Z"), engine_version="stage6_rule_v7",
                         kospi_score=60, semiconductor_score=60, gap_up_probability=70,
                         up_close_probability=60, confidence=.87, data_completeness=.95,
                         calibrated=False, details_json="{}"))
    session.commit()
    result = calculate(session, now="2026-09-16T01:00Z")
    assert result.details["signal_state"]["gap_up"]["available"] is False


def test_late_kis_bootstrap_does_not_create_actual_close(session):
    session.add(row("INDEX:KOSPI", "2026-09-16T08:00Z", business_time="140000"))
    session.commit()
    assert calculate(session).details["signal_state"]["up_close"]["mode"] == "post_close_pending"


def test_pending_close_preserves_last_intraday_input_basis(session):
    at = utc("2026-09-16T05:00Z")
    for symbol in ("INDEX:KOSPI", "FUTURES:KOSPI200", "FUTURES:NQ"):
        session.add(row(symbol, at))
    session.add(row("INDEX:SOX", "2026-09-15T20:00Z"))
    session.commit()
    original = calculate(session, now=at.isoformat())
    save_signal_run(session, original)
    pending = calculate(session, now="2026-09-16T08:00Z")
    assert pending.details["signal_state"]["up_close"]["mode"] == "post_close_pending"
    assert pending.up_close_probability == original.up_close_probability
    assert pending.details["signal_inputs"]["up_close"]["basis"] == original.details["signal_inputs"]["up_close"]["basis"]
    assert pending.details["input_coverage"]["up_close"] == 1.0


def test_configured_krx_closure_and_night_closure_are_honored(monkeypatch):
    import signals.input_status as module
    day = utc("2026-09-16T00:00Z").date()
    module._windows.cache_clear()
    with monkeypatch.context() as patcher:
        patcher.setattr(module, "KIS_EFRIEND_KRX_CLOSED_DATES", frozenset({day}))
        patcher.setattr(module, "KIS_EFRIEND_KRX_NIGHT_CLOSED_DATES", frozenset({day}))
        cash = row("INDEX:KOSPI", "2026-09-15T06:30Z")
        assert module.input_status(cash.symbol, cash, utc("2026-09-16T01:00Z"))["status"] == "closed_latest"
        future = row("FUTURES:KOSPI200", "2026-09-15T20:59Z")
        assert module.input_status(future.symbol, future, utc("2026-09-16T10:00Z"))["status"] == "closed_latest"
    module._windows.cache_clear()


def test_yesterdays_locked_gap_is_not_relabelled_as_todays_forecast(session):
    session.add(SignalRun(created_at=utc("2026-09-15T06:15Z"), engine_version="stage6_rule_v7",
                         kospi_score=60, semiconductor_score=60, gap_up_probability=70,
                         up_close_probability=60, confidence=.87, data_completeness=.95,
                         calibrated=False, details_json=json.dumps({
                             "effective_weight": {"gap_up": 1},
                             "signal_state": {"gap_up": {"mode": "locked_preopen",
                                                          "target_session_date": "2026-09-15"}},
                         })))
    session.commit()
    result = calculate(session, now="2026-09-16T01:00Z")
    assert result.details["signal_state"]["gap_up"]["available"] is False


# Yahoo futures quote normalization and signal-input boundary
NQ = YahooMapping("FUTURES:NQ", "NQ=F", "America/Chicago")
SOX = YahooMapping("INDEX:SOX", "^SOX", "America/New_York")
AT = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


def frame(prices, times):
    return pd.DataFrame({"Close": prices}, index=pd.to_datetime(times))


def quote(**changes):
    result = {
        "symbol": "NQ=F", "quoteType": "FUTURE",
        "regularMarketPrice": 29344.25,
        "regularMarketPreviousClose": 29246.75,
        "regularMarketTime": AT.timestamp() + 30,
        # Deliberately incompatible fields that must never become the base.
        "previousClose": 28956.24, "chartPreviousClose": 28000,
    }
    result.update(changes)
    return result


def build(payload, *, mapping=NQ, intraday=None, daily=None):
    return YFinanceCollector((mapping,))._build_observation(
        mapping,
        frame([29300], [AT]) if intraday is None else intraday,
        frame([28956.24], ["2026-09-15"]) if daily is None else daily,
        futures_quote=payload,
    )


def test_futures_uses_one_quote_price_base_and_time_not_daily_or_minute():
    result = build(quote())
    assert result.price == 29344.25
    assert result.change_pct == pytest.approx(0.3333704, abs=1e-6)
    assert result.observed_at.timestamp() == AT.timestamp() + 30
    assert result.source == "yfinance:NQ=F"


@pytest.mark.parametrize("base", [None, 0, -1, float("nan"), float("inf"), "bad", True])
def test_missing_or_invalid_quote_base_keeps_price_without_percentage(base):
    result = build(quote(regularMarketPreviousClose=base))
    assert result.price == 29344.25
    assert result.change_pct is None


@pytest.mark.parametrize("changes", [
    {"symbol": "MNQ=F"}, {"quoteType": "INDEX"},
    {"regularMarketTime": None}, {"regularMarketTime": 1e100},
    {"regularMarketPrice": float("nan")},
    {"regularMarketTime": AT.timestamp() - 60},
])
def test_incompatible_or_older_quote_keeps_bar_price_only(changes):
    result = build(quote(**changes))
    assert result.price == 29300
    assert result.observed_at == AT
    assert result.change_pct is None


def test_no_quote_never_falls_back_to_daily_previous_close():
    result = build(None)
    assert result.price == 29300
    assert result.change_pct is None


def test_quote_survives_empty_history():
    result = build(quote(), intraday=pd.DataFrame(), daily=pd.DataFrame())
    assert result.price == 29344.25
    assert result.change_pct == pytest.approx((29344.25 / 29246.75 - 1) * 100)
    assert build(None, intraday=pd.DataFrame()) is None


def test_new_session_or_rolled_price_does_not_reuse_prior_quote_base():
    collector = YFinanceCollector((NQ,))
    old = quote()
    new_time = pd.Timestamp(AT) + pd.Timedelta(days=1)
    minute = frame([30000], [new_time])
    daily = frame([28956.24], ["2026-09-15"])
    stale = collector._build_observation(NQ, minute, daily, futures_quote=old)
    assert stale.price == 30000 and stale.change_pct is None
    new_quote = quote(regularMarketPrice=30003, regularMarketPreviousClose=29700,
                      regularMarketTime=new_time.timestamp() + 20)
    fresh = collector._build_observation(NQ, minute, daily, futures_quote=new_quote)
    assert fresh.change_pct == pytest.approx((30003 / 29700 - 1) * 100)
    failed = collector._build_observation(NQ, minute, daily)
    assert failed.change_pct is None


def test_cash_index_previous_intraday_session_correction_is_preserved():
    minute = frame([11131.28, 11175.55], ["2026-09-15T20:00Z", "2026-09-16T19:00Z"])
    daily = frame([11000], ["2026-09-14"])
    result = build(None, mapping=SOX, intraday=minute, daily=daily)
    assert result.change_pct == pytest.approx((11175.55 / 11131.28 - 1) * 100)


def test_cash_index_proxy_keeps_daily_policy_without_futures_quote():
    proxy = YahooMapping("FUTURES:KOSPI200", "^KS200", "Asia/Seoul", is_proxy=True)
    result = build(None, mapping=proxy)
    assert result.change_pct == pytest.approx((29300 / 28956.24 - 1) * 100)
    assert result.source.endswith(":proxy")


def test_batch_quote_lookup_uses_timeout_and_filters_unrequested_symbols():
    collector = YFinanceCollector((NQ,), timeout_seconds=7)
    with patch("collectors.yfinance_collector.YfData") as client:
        client.return_value.get_raw_json.return_value = {
            "quoteResponse": {"result": [quote(), quote(symbol="OTHER")], "error": None}
        }
        result = collector._fetch_futures_quotes(["NQ=F"])
        assert list(result) == ["NQ=F"]
        call = client.return_value.get_raw_json.call_args
        assert call.kwargs["params"] == {"symbols": "NQ=F", "formatted": "false"}
        assert call.kwargs["timeout"] == 7


def test_quote_failure_is_isolated_and_exposes_price_only_warning():
    minute = pd.concat({"NQ=F": frame([29300], [AT]), "^SOX": frame([11175.55], [AT])}, axis=1)
    daily = pd.concat({"NQ=F": frame([28956.24], ["2026-09-15"]),
                       "^SOX": frame([11131.28], ["2026-09-15"])}, axis=1)
    collector = YFinanceCollector((NQ, SOX))
    with patch("collectors.yfinance_collector.yf.download", side_effect=[minute, daily]), \
         patch.object(collector, "_fetch_futures_quotes", side_effect=RuntimeError("rate limited")):
        observations, errors = collector.fetch()
    rows = {row.symbol: row for row in observations}
    assert rows[NQ.symbol].price == 29300 and rows[NQ.symbol].change_pct is None
    assert rows[SOX.symbol].change_pct == pytest.approx((11175.55 / 11131.28 - 1) * 100)
    assert set(errors) == {NQ.symbol}


def test_no_future_mapping_makes_no_extra_quote_request():
    collector = YFinanceCollector((SOX,))
    with patch("collectors.yfinance_collector.yf.download", side_effect=[
        frame([11175.55], [AT]), frame([11131.28], ["2026-09-15"])
    ]), patch.object(collector, "_fetch_futures_quotes") as request:
        observations, errors = collector.fetch()
    request.assert_not_called()
    assert len(observations) == 1 and not errors


def test_missing_futures_percentage_is_excluded_from_signal_input():
    from db.models import MarketSnapshot
    from signals.engine import COMPONENT_SPECS, _build_market_component

    observation = build(None)
    row = MarketSnapshot(symbol=NQ.symbol, price=observation.price,
                         observed_at=observation.observed_at,
                         change_pct=observation.change_pct, source=observation.source)
    result = _build_market_component(COMPONENT_SPECS["nasdaq100_futures"], {NQ.symbol: row}, AT)
    assert result["available"] is False
    assert result["quality"] == 0
