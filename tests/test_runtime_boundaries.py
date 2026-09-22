from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request

import pytest

from bridges.krx_quotes import KrxQuoteService


ROOT = Path(__file__).resolve().parents[1]
RUN_PATH = ROOT / "run_market_ai.py"
SUITE_PATH = ROOT / "start-local-server.pyw"
BUILD_MARKET_PATH = ROOT / "build-market-ai.ps1"
BUILD_SUITE_PATH = ROOT / "build-investment-local-suite.ps1"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_suite_module():
    loader = importlib.machinery.SourceFileLoader("investment_local_suite_boundary", str(SUITE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _BackendHandler(http.server.BaseHTTPRequestHandler):
    post_count = 0
    get_count = 0
    last_path = ""

    def log_message(self, *_args):
        return

    def _send_json(self, status: int, payload: dict[str, object]):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "https://example.test")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        type(self).get_count += 1
        type(self).last_path = self.path
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        tickers = str((query.get("tickers") or [""])[0]).split(",")
        if parsed.path == "/api/market-data/krx-quotes" and "999998" in tickers:
            self._send_json(422, {"detail": "backend rejected ticker"})
            return
        self._send_json(200, {"ok": True, "path": self.path})

    def do_HEAD(self):
        type(self).get_count += 1
        type(self).last_path = self.path
        self._send_json(200, {"ok": True})

    def do_OPTIONS(self):
        type(self).last_path = self.path
        self.send_response(200)
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Origin", "https://example.test")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        type(self).post_count += 1
        self._send_json(200, {"mutated": True})


class _ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _proxy_pair(suite):
    backend = _ThreadedServer(("127.0.0.1", 0), _BackendHandler)
    backend_thread = _start_server(backend)

    class ProxyHandler(suite.RemoteGetMarketAiProxyHandler):
        backend_base = f"http://127.0.0.1:{backend.server_address[1]}"

    proxy = suite.RemoteGetMarketAiProxyServer(("127.0.0.1", 0), ProxyHandler)
    proxy.log_callback = lambda _message: None
    proxy_thread = _start_server(proxy)
    return backend, backend_thread, proxy, proxy_thread


def _stop_pair(backend, backend_thread, proxy, proxy_thread):
    proxy.shutdown()
    proxy.server_close()
    backend.shutdown()
    backend.server_close()
    proxy_thread.join(timeout=2)
    backend_thread.join(timeout=2)


# Remote GET-only proxy boundary
def test_remote_proxy_forwards_whitelisted_get_and_preserves_cors_header():
    suite = _load_suite_module()
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_address[1]}/api/health",
            headers={"Origin": "https://example.test"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.test"
            assert payload == {"ok": True, "path": "/api/health"}
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)



def test_remote_proxy_exposes_monitor_assets_and_readonly_quote_universe():
    suite = _load_suite_module()
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        for path in (
            "/monitor/",
            "/monitor/index.html",
            "/monitor/monitor.css",
            "/monitor/monitor.js",
            "/api/bridge/kis-efriend/quote-universe",
        ):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{proxy.server_address[1]}{path}",
                timeout=3,
            ) as response:
                assert response.status == 200
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_remote_monitor_without_trailing_slash_redirects_to_monitor_directory():
    suite = _load_suite_module()
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        try:
            opener.open(
                f"http://127.0.0.1:{proxy.server_address[1]}/monitor",
                timeout=3,
            )
            raise AssertionError("/monitor unexpectedly skipped canonical redirect")
        except urllib.error.HTTPError as exc:
            assert exc.code == 308
            assert exc.headers.get("Location") == "/monitor/"
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)

def test_remote_proxy_blocks_non_dashboard_get_before_backend():
    suite = _load_suite_module()
    _BackendHandler.get_count = 0
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{proxy.server_address[1]}/api/backtest/dataset",
                timeout=3,
            )
            raise AssertionError("non-dashboard GET unexpectedly reached backend")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert b"not exposed" in exc.read()
        assert _BackendHandler.get_count == 0
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_remote_krx_quotes_preserve_per_tab_identity_but_namespace_client_id():
    suite = _load_suite_module()
    _BackendHandler.last_path = ""
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        first = (
            f"http://127.0.0.1:{proxy.server_address[1]}/api/market-data/krx-quotes"
            "?tickers=005930,000660&client_id=tab-one"
        )
        with urllib.request.urlopen(first, timeout=3) as response:
            assert response.status == 200
            response.read()
        parsed = urllib.parse.urlsplit(_BackendHandler.last_path)
        query = urllib.parse.parse_qs(parsed.query)
        first_forwarded = query["client_id"][0]
        assert parsed.path == "/api/market-data/krx-quotes"
        assert query["tickers"] == ["005930,000660"]
        assert first_forwarded.startswith("remote-")
        assert first_forwarded != "tab-one"

        second = (
            f"http://127.0.0.1:{proxy.server_address[1]}/api/market-data/krx-quotes"
            "?tickers=0163Y0&client_id=tab-two"
        )
        with urllib.request.urlopen(second, timeout=3) as response:
            assert response.status == 200
            response.read()
        second_query = urllib.parse.parse_qs(urllib.parse.urlsplit(_BackendHandler.last_path).query)
        assert second_query["client_id"][0].startswith("remote-")
        assert second_query["client_id"][0] != first_forwarded
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_remote_krx_quotes_reject_invalid_or_oversized_request_before_backend():
    suite = _load_suite_module()
    _BackendHandler.get_count = 0
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        invalid = (
            f"http://127.0.0.1:{proxy.server_address[1]}/api/market-data/krx-quotes"
            "?tickers=005930,BAD!&client_id=x"
        )
        try:
            urllib.request.urlopen(invalid, timeout=3)
            raise AssertionError("invalid ticker unexpectedly reached backend")
        except urllib.error.HTTPError as exc:
            assert exc.code == 422

        tickers = ",".join(f"{index:06d}" for index in range(proxy.RequestHandlerClass.remote_quote_max_tickers_per_client + 1))
        oversized = (
            f"http://127.0.0.1:{proxy.server_address[1]}/api/market-data/krx-quotes"
            f"?tickers={tickers}&client_id=x"
        )
        try:
            urllib.request.urlopen(oversized, timeout=3)
            raise AssertionError("oversized ticker request unexpectedly reached backend")
        except urllib.error.HTTPError as exc:
            assert exc.code == 422
        assert _BackendHandler.get_count == 0
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_remote_proxy_delegates_quote_capacity_admission_to_backend_source_of_truth():
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert "remote_quote_max_tickers_per_client = 64" in source
    assert "remote_quote_leases" not in source
    assert "remote_quote_lock" not in source
    assert "_reserve_remote_quote_lease" not in source
    assert 'return "remote-" + hashlib.sha256' in source


def test_remote_proxy_and_backend_local_priority_admission_integration():
    suite = _load_suite_module()
    quote_service = KrxQuoteService(max_tickers=64)

    class AdmissionBackendHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/api/market-data/krx-quotes":
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            requested = quote_service.parse_ticker_query((query.get("tickers") or [""])[0])
            client_id = (query.get("client_id") or [""])[0]
            try:
                payload = quote_service.request_universe(requested, client_id=client_id)
                status = 200
            except ValueError as exc:
                payload = {"detail": str(exc)}
                status = 422
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    backend = _ThreadedServer(("127.0.0.1", 0), AdmissionBackendHandler)
    backend_thread = _start_server(backend)

    class ProxyHandler(suite.RemoteGetMarketAiProxyHandler):
        backend_base = f"http://127.0.0.1:{backend.server_address[1]}"

    proxy = suite.RemoteGetMarketAiProxyServer(("127.0.0.1", 0), ProxyHandler)
    proxy.log_callback = lambda _message: None
    proxy_thread = _start_server(proxy)
    try:
        remote = ",".join(f"{100000 + index:06d}" for index in range(62))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{proxy.server_address[1]}/api/market-data/krx-quotes?tickers={remote}&client_id=phone",
            timeout=3,
        ) as response:
            assert response.status == 200
            response.read()

        local = [f"{200000 + index:06d}" for index in range(10)]
        local_state = quote_service.request_universe(local, client_id="desktop-local")
        assert local_state["evicted_remote_client_count"] == 1
        assert local_state["remote_client_count"] == 0
        assert set(local).issubset(local_state["dashboard_tickers"])

        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{proxy.server_address[1]}/api/market-data/krx-quotes?tickers={remote}&client_id=phone",
                timeout=3,
            )
            raise AssertionError("remote request unexpectedly displaced local capacity")
        except urllib.error.HTTPError as exc:
            assert exc.code == 422
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_remote_quote_requests_cannot_persist_restart_bootstrap_state():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    quote_source = (ROOT / "bridges" / "krx_quotes.py").read_text(encoding="utf-8")
    holdings_source = (ROOT / "bridges" / "dashboard_holdings.py").read_text(encoding="utf-8")
    assert "if not krx_quote_service.is_remote_client_id(client_id):" in app_source
    assert "with dashboard_universe_persist_lock:" in app_source
    assert "krx_quote_service.local_dashboard_tickers()" in app_source
    assert "def local_dashboard_tickers" in quote_source
    assert "_STATE_WRITE_LOCK = Lock()" in holdings_source
    assert "with _STATE_WRITE_LOCK:" in holdings_source


def test_remote_proxy_rejects_query_pollution_before_backend():
    suite = _load_suite_module()
    _BackendHandler.get_count = 0
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        urls = [
            f"http://127.0.0.1:{proxy.server_address[1]}/api/health?unexpected=1",
            f"http://127.0.0.1:{proxy.server_address[1]}/api/signal/latest?include_details=true&include_details=false",
        ]
        for url in urls:
            try:
                urllib.request.urlopen(url, timeout=3)
                raise AssertionError("unsupported query unexpectedly reached backend")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
        assert _BackendHandler.get_count == 0
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_remote_proxy_blocks_write_before_backend_and_closes_connection():
    suite = _load_suite_module()
    _BackendHandler.post_count = 0
    backend, backend_thread, proxy, proxy_thread = _proxy_pair(suite)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_address[1]}/api/signal/run-once",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
            raise AssertionError("remote write unexpectedly succeeded")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
            assert exc.headers.get("Allow") == "GET, HEAD, OPTIONS"
            assert exc.headers.get("Connection") == "close"
            assert b"GET-only" in exc.read()
        assert _BackendHandler.post_count == 0
    finally:
        _stop_pair(backend, backend_thread, proxy, proxy_thread)


def test_fail_closed_disables_persisted_direct_backend_serve():
    suite = _load_suite_module()
    launcher = object.__new__(suite.LocalSuiteLauncher)
    calls = []
    logs = []
    state = {"disabled": False}

    launcher.log = logs.append
    launcher._find_tailscale_cli = lambda: Path("tailscale.exe")

    def fake_status(_cli):
        if state["disabled"]:
            return False, "No serve config"
        return (
            False,
            f"https://node.tail60a98e.ts.net\n|-- proxy http://127.0.0.1:{suite.MARKET_AI_PORT}",
        )

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, cwd=None, timeout=None):
        calls.append([str(arg) for arg in args])
        if len(args) >= 3 and str(args[1]) == "serve" and str(args[2]) == "off":
            state["disabled"] = True
        return Result()

    launcher._tailscale_serve_status = fake_status
    launcher._run_hidden = fake_run

    assert launcher._disable_direct_market_ai_serve(reason="test") is True
    assert state["disabled"] is True
    assert any(call[-2:] == ["serve", "off"] for call in calls)
    assert any("Fail-closed remote boundary" in line for line in logs)


def test_proxy_launch_failure_path_is_fail_closed_in_source():
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert "force=True" in source
    assert '[cli, "serve", "off"]' in source
    assert 'reason="GET-only proxy launch failed"' in source


# Local runtime / Tailscale / process boundary
def test_market_ai_full_backend_binding_ignores_host_port_environment(monkeypatch):
    module = _load_module(RUN_PATH, "run_market_ai_boundary")
    calls: dict[str, object] = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls.update(kwargs)

    fake_uvicorn.run = fake_run
    fake_app = types.ModuleType("app")
    fake_app.app = object()

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setenv("MARKET_AI_HOST", "0.0.0.0")
    monkeypatch.setenv("MARKET_AI_PORT", "9999")
    monkeypatch.setattr(module, "prepare_runtime_environment", lambda: ROOT)

    module.main()

    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8001


def test_tailscale_serve_ready_never_accepts_direct_8001_mapping():
    source = SUITE_PATH.read_text(encoding="utf-8")
    assert "and not self._serve_exposes_backend_directly(output)" in source


def test_mixed_tailscale_serve_mapping_is_removed_before_canonical_repair():
    suite = _load_suite_module()
    launcher = object.__new__(suite.LocalSuiteLauncher)
    state = {"mode": "mixed"}
    calls: list[list[str]] = []
    logs: list[str] = []

    launcher.log = logs.append
    launcher._find_tailscale_cli = lambda: Path("tailscale.exe")
    launcher._tailscale_backend_state = lambda _cli: "Running"
    launcher._remote_health_ready = lambda seconds=8.0: True

    def fake_status(_cli):
        if state["mode"] == "mixed":
            return True, (
                "https://node.tail60a98e.ts.net\n"
                "|-- proxy http://127.0.0.1:8002\n"
                "|-- /legacy proxy http://127.0.0.1:8001"
            )
        if state["mode"] == "off":
            return False, "No serve config"
        return True, "https://node.tail60a98e.ts.net\n|-- proxy http://127.0.0.1:8002"

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, cwd=None, timeout=None):
        normalized = [str(arg) for arg in args]
        calls.append(normalized)
        if normalized[-2:] == ["serve", "off"]:
            state["mode"] = "off"
        elif len(normalized) >= 4 and normalized[1:3] == ["serve", "--bg"]:
            assert normalized[3] == "8002"
            state["mode"] = "safe"
        return Result()

    launcher._tailscale_serve_status = fake_status
    launcher._run_hidden = fake_run

    assert launcher._ensure_tailscale_remote() is True
    assert state["mode"] == "safe"
    assert any(call[-2:] == ["serve", "off"] for call in calls)
    assert any(len(call) >= 4 and call[1:4] == ["serve", "--bg", "8002"] for call in calls)
    assert any("direct Market AI :8001 mapping" in line for line in logs)



def test_unverifiable_serve_repair_failure_attempts_fail_closed_serve_off():
    suite = _load_suite_module()
    launcher = object.__new__(suite.LocalSuiteLauncher)
    calls: list[list[str]] = []
    launcher.log = lambda _line: None
    launcher._find_tailscale_cli = lambda: Path("tailscale.exe")
    launcher._tailscale_backend_state = lambda _cli: "Running"
    launcher._tailscale_serve_status = lambda _cli: (False, "status unavailable")

    class Result:
        stdout = ""
        stderr = "failed"

        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(args, cwd=None, timeout=None):
        normalized = [str(arg) for arg in args]
        calls.append(normalized)
        if len(normalized) >= 4 and normalized[1:3] == ["serve", "--bg"]:
            return Result(1)
        if normalized[-2:] == ["serve", "off"]:
            return Result(0)
        return Result(0)

    launcher._run_hidden = fake_run

    assert launcher._ensure_tailscale_remote() is False
    assert any(call[-2:] == ["serve", "off"] for call in calls)

def test_local_quote_persistence_failure_is_best_effort_after_admission():
    source = (ROOT / "app.py").read_text(encoding="utf-8-sig")
    assert "try:\n                    persist_dashboard_universe(" in source
    assert "except OSError as exc:" in source
    assert "continuing with in-memory state" in source
    assert "return snapshot" in source


def test_market_ai_build_smoke_is_isolated_from_external_runtime_state():
    source = BUILD_MARKET_PATH.read_text(encoding="utf-8-sig")
    assert '$SmokeRuntimeRoot = Join-Path $BuildBase "smoke-runtime"' in source
    assert '$env:MARKET_AI_DB_PATH = $SmokeDbPath' in source
    assert '$env:MARKET_AI_COLLECTOR_ENABLED = "false"' in source
    assert '$env:MARKET_AI_NEWS_ENABLED = "false"' in source
    assert '$env:MARKET_AI_AI_ENABLED = "false"' in source
    assert '$env:MARKET_AI_SIGNAL_ENABLED = "false"' in source
    assert "Existing market_signal.db not found. Build aborted" not in source
    assert "isolated smoke DB used" in source


def test_local_suite_build_gate_protects_mixed_serve_boundary():
    source = BUILD_SUITE_PATH.read_text(encoding="utf-8-sig")
    assert 'if self._serve_exposes_backend_directly(serve_output):' in source
    assert 'direct :8001 mapping detected alongside remote configuration' in source


def test_local_suite_runtime_port_conflict_is_fail_closed_without_taskkill():
    suite = _load_suite_module()
    launcher = object.__new__(suite.LocalSuiteLauncher)
    logs: list[str] = []
    launcher.log = logs.append
    launcher._pids_listening_on_port = lambda _port: {4321}
    launcher._process_executable_path = lambda _pid: Path(r"C:/other/tool.exe")
    launcher._run_hidden = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("port conflict must never invoke taskkill")
    )

    assert launcher._stop_port_listener(8001) is False
    assert any("will not terminate it" in line for line in logs)


def test_local_suite_process_cleanup_targets_only_runtime_owned_pid():
    suite = _load_suite_module()
    launcher = object.__new__(suite.LocalSuiteLauncher)
    launcher.market_ai_dir = Path("/runtime")
    launcher.log = lambda _line: None
    alive = {101, 202}
    launcher._process_pids = lambda _image: set(alive)
    launcher._process_executable_path = lambda pid: (
        Path("/runtime/MarketAI.exe") if pid == 101 else Path("/other/MarketAI.exe")
    )
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(args, timeout=None):
        normalized = [str(value) for value in args]
        calls.append(normalized)
        if "/PID" in normalized:
            alive.discard(int(normalized[normalized.index("/PID") + 1]))
        return Result()

    launcher._run_hidden = fake_run
    assert launcher._stop_image(suite.MARKET_AI_PROCESS) is True
    assert any("101" in call for call in calls)
    assert all("202" not in call for call in calls)
    assert all("/IM" not in call for call in calls)
    assert 202 in alive



def test_local_suite_refuses_startup_beside_external_bridge_runtime():
    suite = _load_suite_module()
    launcher = object.__new__(suite.LocalSuiteLauncher)
    launcher.market_ai_dir = Path("/runtime")
    logs: list[str] = []
    launcher.log = logs.append
    launcher._process_pids = lambda _image: {202}
    launcher._process_executable_path = lambda _pid: Path("/other/KisKospi200Bridge.exe")
    launcher._post_bridge_message = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("external Bridge must block startup before any stop message")
    )

    assert launcher._stop_bridge_gracefully(
        reason="새 시작 순서 적용",
        refuse_external=True,
    ) is False
    assert any("refusing to start a second Bridge" in line for line in logs)
