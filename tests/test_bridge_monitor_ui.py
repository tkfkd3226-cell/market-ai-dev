from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN_FORM = ROOT / "KisKospi200Bridge" / "MainForm.cs"
APP = ROOT / "app.py"
KIS_BRIDGE = ROOT / "bridges" / "kis_efriend.py"
KRX_QUOTES = ROOT / "bridges" / "krx_quotes.py"
BUILD_MARKET_AI = ROOT / "build-market-ai.ps1"
MONITOR_DIR = ROOT / "monitor"


class BridgeMonitorUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_FORM.read_text(encoding="utf-8-sig")

    def test_summary_exposes_expected_lifecycle_buckets(self):
        source = self.source
        for label in ("전체", "정상", "시간외", "장마감", "대기", "지연", "오류"):
            self.assertIn(f'CreateSummaryItem("{label}"', source)

    def test_dashboard_display_universe_is_backend_owned_and_separate_from_signal_baseline(self):
        source = self.source
        for token in (
            "SignalBaselineQuoteTickers",
            "payload.dashboard_display_tickers",
            "payload.dashboard_names",
            "dashboardQuoteDisplayOrder",
            "dashboardQuoteNames",
            "DashboardDisplayTickers()",
        ):
            self.assertIn(token, source)
        self.assertNotIn("DashboardBootstrapQuoteTickers", source)
        self.assertNotIn("OrderDashboardTickers", source)
        self.assertNotIn('return "삼성전자"', source)
        self.assertNotIn('return "KODEX 200"', source)

    def test_user_close_hides_bridge_instead_of_terminating_it(self):
        source = self.source
        self.assertIn("protected override void WndProc", source)
        self.assertIn("HideBridgeWindow();", source)
        self.assertIn("ShowInTaskbar = false;", source)
        self.assertIn("RequestApplicationExit", source)

    def test_system_status_includes_market_and_holding_stream_errors(self):
        source = self.source
        self.assertIn("lastTradeError", source)
        self.assertIn("kospiSpot.LastError", source)
        self.assertIn("quoteStreams.Values", source)
        self.assertIn('return "확인 필요";', source)

    def test_recovered_stream_requires_fresh_tick_before_normal_state(self):
        source = self.source
        self.assertIn("FreshTickRequired = true;", source)
        self.assertIn("FreshTickRequired = false;", source)
        self.assertIn('FreshTickRequired) return "지연";', source)

    def test_receive_and_forward_errors_have_independent_ownership(self):
        source = self.source
        receive = source[source.index("private void ReceiveSpot("):source.index("private SpotTickSnapshot BuildSpotTickSnapshot(")]
        forward = source[source.index("private async Task SendSpotTickAsync("):source.index("private void UpdateDomesticStatus(")]
        state = source[source.index("private sealed class SpotStreamState"):source.index("private sealed class SpotTickSnapshot")]

        self.assertIn('state.StreamError = "";', receive)
        self.assertIn('state.StreamError = ex.GetType().Name + " - " + ex.Message;', receive)
        self.assertNotIn("state.ForwardError =", receive)

        self.assertIn('state.ForwardError = "";', forward)
        self.assertIn("state.ForwardError = forwardError;", forward)
        self.assertNotIn("state.StreamError =", forward)
        self.assertNotIn('state.LastError = "";', forward)

        self.assertIn('public string StreamError { get; set; } = "";', state)
        self.assertIn('public string ForwardError { get; set; } = "";', state)
        self.assertIn("if (!string.IsNullOrEmpty(StreamError)) return StreamError;", state)
        self.assertIn('return ForwardError ?? "";', state)

    def test_native_status_uses_semantic_session_and_valid_business_clock(self):
        source = self.source
        self.assertIn("payload.dashboard_market_states", source)
        self.assertIn('"시간외"', source)
        self.assertIn('NewPlainValueLabel("세션"', source)
        self.assertNotIn('NewPlainValueLabel("서비스"', source)
        self.assertIn("hour > 23 || minute > 59 || second > 59", source)
        self.assertIn('return "--:--:--";', source)

    def test_durable_monitor_snapshot_is_replaced_by_real_ticks(self):
        source = self.source
        for token in (
            "payload.monitor_snapshots",
            "ApplyMonitorSnapshots",
            "CachedObservedUtc",
            '"FUTURES:KOSPI200"',
            '"INDEX:KOSPI"',
            "state.CachedObservedUtc = null;",
            "lastTradeCachedObservedUtc = null;",
        ):
            self.assertIn(token, source)


class BridgeMonitorBackendCacheContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = APP.read_text(encoding="utf-8-sig")

    def test_quote_universe_exposes_read_only_snapshots_and_display_order(self):
        source = self.app_source
        self.assertIn('"monitor_snapshots": monitor_snapshots', source)
        self.assertIn('"snapshot_origin"] = "durable"', source)
        self.assertIn('"state"] = "closed" if cash_state == "closed" else "stale"', source)
        self.assertIn('"dashboard_display_tickers": dashboard_display_tickers', source)
        self.assertIn("order_dashboard_tickers_for_display(", source)


class WebMonitorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = APP.read_text(encoding="utf-8-sig")
        cls.main_form_source = MAIN_FORM.read_text(encoding="utf-8-sig")
        cls.quote_source = KRX_QUOTES.read_text(encoding="utf-8-sig")
        cls.build_source = BUILD_MARKET_AI.read_text(encoding="utf-8-sig")
        cls.index_source = (MONITOR_DIR / "index.html").read_text(encoding="utf-8-sig")
        cls.js_source = (MONITOR_DIR / "monitor.js").read_text(encoding="utf-8-sig")

    def test_monitor_runtime_assets_are_mounted_and_packaged(self):
        runtime_assets = {path.name for path in MONITOR_DIR.iterdir() if path.is_file()}
        self.assertTrue({"index.html", "monitor.css", "monitor.js"}.issubset(runtime_assets))
        self.assertIn('href="./monitor.css"', self.index_source)
        self.assertIn('src="./monitor.js"', self.index_source)
        self.assertIn('"/monitor"', self.app_source)
        self.assertIn("StaticFiles", self.app_source)
        self.assertIn("MONITOR_NO_STORE_PATHS", self.app_source)
        self.assertIn('response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"', self.app_source)
        self.assertIn('response.headers["Pragma"] = "no-cache"', self.app_source)
        self.assertIn('response.headers["Expires"] = "0"', self.app_source)
        self.assertIn('"--add-data", "$MonitorPath;monitor"', self.build_source)
        for asset in ("/monitor/", "/monitor/monitor.css", "/monitor/monitor.js"):
            self.assertIn(asset, self.build_source)

    def test_holding_change_amount_reaches_native_and_web_monitor_without_pct_backcalculation(self):
        kis_source = KIS_BRIDGE.read_text(encoding="utf-8-sig")
        self.assertIn("change_amount", self.main_form_source)
        self.assertIn("FormatHoldingChange", self.main_form_source)
        self.assertIn("change_amount: float | None = None", kis_source)
        self.assertIn('"change_amount": quote.change_amount', self.quote_source)
        self.assertIn("effectiveSnapshot?.change_amount", self.js_source)
        self.assertIn("formatChangeAmount", self.js_source)
        self.assertNotIn("changePct /", self.js_source)

    def test_kospi_monitor_prefers_explicit_backend_snapshot_state(self):
        source = self.js_source
        resolver = source.split("function resolveKospiStatus(snapshot) {", 1)[1].split(
            "function resolveK200Status(snapshot) {", 1
        )[0]
        self.assertIn("snapshot?.state", resolver)
        self.assertIn("snapshot?.status", resolver)
        self.assertLess(resolver.index("if (explicitStatus)"), resolver.index("isCashMarketClosed()"))

    def test_web_monitor_uses_backend_dashboard_metadata_and_semantic_status(self):
        self.assertIn("const POLL_INTERVAL_MS = 10_000;", self.js_source)
        self.assertIn('/api/bridge/kis-efriend/quote-universe', self.js_source)
        self.assertIn("payload?.dashboard_names", self.js_source)
        self.assertIn("payload?.dashboard_display_tickers ??", self.js_source)
        self.assertIn("payload?.dashboard_tickers ??", self.js_source)
        self.assertIn("<dt>세션</dt>", self.index_source)
        self.assertNotIn("<dt>서비스</dt>", self.index_source)
        self.assertIn('"시간외"', self.js_source)
        self.assertIn("hour > 23 || minute > 59 || second > 59", self.js_source)
        self.assertIn("resolveSessionLabel", self.js_source)

    def test_monitor_backend_reads_live_memory_without_renewing_dashboard_lease(self):
        self.assertIn("def monitor_quotes(", self.quote_source)
        monitor_method = self.quote_source.split("def monitor_quotes(", 1)[1].split("def _market_context", 1)[0]
        self.assertNotIn("request_universe(", monitor_method)
        self.assertIn("krx_quote_service.monitor_quotes(", self.app_source)
        self.assertIn('"bridge_connected": bridge_connected', self.app_source)

    def test_monitor_timeout_is_error_but_lifecycle_abort_is_silent(self):
        source = self.js_source
        self.assertIn('monitorAbortReason = "timeout"', source)
        self.assertIn('monitorAbortReason = "lifecycle"', source)
        self.assertIn('setConnectionState(false, "API 지연")', source)


if __name__ == "__main__":
    unittest.main()
