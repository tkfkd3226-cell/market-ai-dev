from __future__ import annotations

import http.server
import hashlib

import csv
import ctypes
import json
from ctypes import wintypes
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


APP_TITLE = "Investment Local Suite"
DASHBOARD_PORT = 8000
MARKET_AI_PORT = 8001
REMOTE_GET_PROXY_PORT = 8002
TAILSCALE_SERVICE = "Tailscale"
TAILSCALE_REMOTE_BASE = "https://node.tail60a98e.ts.net"
TAILSCALE_CLI_NAME = "tailscale.exe"
EFRIEND_EXE = Path(r"C:\eFriend Expert\efriendexpert\efriendexpert.exe")
EFRIEND_BOOTSTRAP_PROCESS = "efriendexpert.exe"
EFRIEND_GATE_PROCESS = "xexpertgate.exe"
EFRIEND_READY_PROCESS = "efexpertmain.exe"
BRIDGE_PROCESS = "KisKospi200Bridge.exe"
BRIDGE_EXIT_MESSAGE = 0x8200
BRIDGE_VIEW_MESSAGE = 0x8201
LOCAL_SUITE_ICON = "InvestmentLocalSuite.ico"
EFRIEND_TRAY_EXIT_SCRIPT = Path("tools") / "close-efriend-tray.ps1"

# eFriend Expert UI contract verified on the current installation.  These are
# Win32 dialog/control IDs, not screen coordinates, so automation remains
# stable when the window moves or the display resolution changes.
EFRIEND_LOGIN_WINDOW_TITLE = "eFriend Expert 로그인"
EFRIEND_CERT_WINDOW_TOKEN = "인증서 선택"
EFRIEND_CTRL_CUSTOMER_ID = 1000
EFRIEND_CTRL_ID_PASSWORD = 1001
EFRIEND_CTRL_CERT_PASSWORD = 1002
EFRIEND_CTRL_LOGIN = 1003
EFRIEND_CTRL_CERT_CONFIRM = 1
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5
SMTO_ABORTIFHUNG = 0x0002
SINGLE_INSTANCE_MUTEX = r"Local\InvestmentLocalSuiteLauncher"
ERROR_ALREADY_EXISTS = 183

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


MARKET_AI_PROCESS = "MarketAI.exe"


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class StartupCancelled(Exception):
    """Internal control-flow signal for a user-requested shutdown during startup."""


def _is_admin() -> bool:
    """Return True when the current launcher already has an elevated token."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevation_python() -> Path:
    """Prefer pythonw.exe so the elevated launcher never opens a console window."""
    exe = Path(sys.executable).resolve()
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def _request_launcher_elevation() -> bool:
    if os.name != "nt":
        return False
    if _is_admin():
        return True

    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        parameters = subprocess.list2cmdline(sys.argv[1:])
        working_dir = _runtime_root()
    else:
        executable = _elevation_python()
        script = Path(__file__).resolve()
        parameters = subprocess.list2cmdline([str(script), *sys.argv[1:]])
        working_dir = script.parent

    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(
            None,
            "runas",
            str(executable),
            parameters,
            str(working_dir),
            1,
        )
        return int(result or 0) > 32
    except Exception:
        return False


def _show_elevation_error():
    root_dir = _runtime_root()
    log_path = root_dir / "start-local-server.log"
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n[FATAL] Launcher administrator approval was "
                "cancelled or failed.\n"
            )
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"{APP_TITLE}을 시작하려면 관리자 권한 승인이 필요합니다.\n\n"
            "처음 표시되는 사용자 계정 컨트롤에서 '예'를 눌러 주세요.",
            APP_TITLE,
            0x00000030,
        )
    except Exception:
        pass


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


class RemoteGetMarketAiProxyHandler(http.server.BaseHTTPRequestHandler):
    """Loopback-only reverse proxy for approved Market AI Dashboard GET surfaces.

    The backend keeps its full local API on :8001 for the KIS Bridge and
    maintenance tools. Tailscale Serve points only at this proxy on :8002.
    Remote quote client IDs are namespaced before forwarding. Lease admission
    itself is centralized inside the :8001 KrxQuoteService, where local clients
    have priority and remote clients can use only spare ticker capacity.
    """

    protocol_version = "HTTP/1.1"
    backend_base = f"http://127.0.0.1:{MARKET_AI_PORT}"
    allowed_methods = ("GET", "HEAD", "OPTIONS")
    hop_by_hop_headers = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
    remote_paths = frozenset({
        "/api/health",
        "/api/signal/latest",
        "/api/market-data/snapshot",
        "/api/market-data/krx-quotes",
        "/api/bridge/kis-efriend/status",
        "/api/bridge/kis-efriend/quote-universe",
        "/monitor/",
        "/monitor/index.html",
        "/monitor/monitor.css",
        "/monitor/monitor.js",
    })
    remote_query_params = {
        "/api/health": frozenset(),
        "/api/signal/latest": frozenset({"include_details"}),
        "/api/market-data/snapshot": frozenset(),
        "/api/market-data/krx-quotes": frozenset({"tickers", "client_id"}),
        "/api/bridge/kis-efriend/status": frozenset(),
        "/api/bridge/kis-efriend/quote-universe": frozenset(),
        "/monitor/": frozenset(),
        "/monitor/index.html": frozenset(),
        "/monitor/monitor.css": frozenset(),
        "/monitor/monitor.js": frozenset(),
    }
    remote_quote_max_tickers_per_client = 64

    def _proxy_logger(self, message: str):
        callback = getattr(self.server, "log_callback", None)
        if callable(callback):
            try:
                callback(message)
            except Exception:
                pass

    def log_message(self, format, *args):
        self._proxy_logger(
            f"remote-getonly {self.address_string()} - - "
            f"[{self.log_date_time_string()}] {format % args}"
        )

    def _send_json_error(self, status: int, detail: str):
        body = json.dumps({"detail": str(detail)}, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    @staticmethod
    def _normalize_dashboard_ticker(value: object) -> str:
        ticker = str(value or "").strip().upper()
        if len(ticker) != 6 or any(ch not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in ticker):
            return ""
        return ticker

    @staticmethod
    def _normalize_remote_client_id(value: object) -> str:
        client_id = str(value or "").strip()
        if not client_id:
            client_id = "legacy"
        if len(client_id) > 80 or any(ch not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._:-" for ch in client_id):
            return ""
        # Namespace and hash remote IDs so they cannot collide with local :8001
        # Dashboard leases while still preserving per-tab identity.
        return "remote-" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:32]

    def _prepare_remote_quote_query(self, query: dict[str, list[str]]) -> dict[str, list[str]] | None:
        requested = []
        seen = set()
        for raw in str((query.get("tickers") or [""])[0]).split(","):
            raw = raw.strip()
            if not raw:
                continue
            ticker = self._normalize_dashboard_ticker(raw)
            if not ticker:
                self._send_json_error(422, "invalid KRX ticker")
                return None
            if ticker not in seen:
                requested.append(ticker)
                seen.add(ticker)
        if len(requested) > self.remote_quote_max_tickers_per_client:
            self._send_json_error(422, "too many remote KRX tickers")
            return None

        forwarded_client_id = self._normalize_remote_client_id((query.get("client_id") or [""])[0])
        if not forwarded_client_id:
            self._send_json_error(422, "invalid remote quote client_id")
            return None

        # Do not mirror backend lease state here. The backend owns the only
        # authoritative lease table and applies remote-client count/capacity
        # admission atomically with local-priority reclaim under one service lock.
        return {
            "tickers": [",".join(requested)],
            "client_id": [forwarded_client_id],
        }

    def _prepare_forward_path(self) -> str | None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/monitor":
            self.send_response(308)
            self.send_header("Location", "/monitor/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path not in self.remote_paths:
            self._send_json_error(404, "remote Market AI endpoint is not exposed")
            return None

        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
        allowed_params = self.remote_query_params.get(path, frozenset())
        unexpected = sorted(set(query) - set(allowed_params))
        if unexpected:
            self._send_json_error(400, "remote Market AI query contains unsupported parameters")
            return None
        if any(len(values) != 1 for values in query.values()):
            self._send_json_error(400, "remote Market AI query parameter must be singular")
            return None

        if path == "/api/market-data/krx-quotes" and self.command != "OPTIONS":
            prepared = self._prepare_remote_quote_query(query)
            if prepared is None:
                return None
            query = prepared

        canonical_query = urllib.parse.urlencode(
            [(key, value) for key in sorted(query) for value in query[key]],
            doseq=True,
        )
        return urllib.parse.urlunsplit(("", "", path, canonical_query, ""))

    def _forward_get_request(self):
        forward_path = self._prepare_forward_path()
        if forward_path is None:
            return
        target = f"{self.backend_base}{forward_path}"
        forwarded_headers = {}
        for name in (
            "Accept",
            "Cache-Control",
            "If-Modified-Since",
            "If-None-Match",
            "Origin",
            "User-Agent",
            "Access-Control-Request-Method",
            "Access-Control-Request-Headers",
        ):
            value = self.headers.get(name)
            if value:
                forwarded_headers[name] = value

        request = urllib.request.Request(
            target,
            headers=forwarded_headers,
            method=self.command,
        )
        response = None
        try:
            response = urllib.request.urlopen(request, timeout=10.0)
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception as exc:
            self._proxy_logger(
                f"[WARN] GET-only Market AI proxy upstream failed: "
                f"{type(exc).__name__}: {exc}"
            )
            self.send_error(502, "Market AI upstream unavailable")
            return

        try:
            status = int(getattr(response, "status", response.getcode()))
            body = b"" if self.command == "HEAD" else response.read()
            self.send_response(status)
            for name, value in response.headers.items():
                if name.lower() in self.hop_by_hop_headers:
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _reject_write(self):
        # Do not keep a connection alive after rejecting a request body. Closing the
        # socket prevents unread body bytes from being parsed as a second request.
        body = b'{"detail":"remote Market AI is GET-only"}'
        self.send_response(405)
        self.send_header("Allow", ", ".join(self.allowed_methods))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self._forward_get_request()

    def do_HEAD(self):
        self._forward_get_request()

    def do_OPTIONS(self):
        self._forward_get_request()

    def do_POST(self):
        self._reject_write()

    def do_PUT(self):
        self._reject_write()

    def do_PATCH(self):
        self._reject_write()

    def do_DELETE(self):
        self._reject_write()


class RemoteGetMarketAiProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class SingleInstanceGuard:
    """Keep exactly one elevated Local Suite launcher alive per Windows session."""

    def __init__(self, name: str = SINGLE_INSTANCE_MUTEX):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def close(self):
        if os.name != "nt" or not self.handle:
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self.handle)
        finally:
            self.handle = None


def _show_already_running():
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"{APP_TITLE}가 이미 실행 중입니다.\n\n"
            "시스템 트레이의 Investment Local Suite 아이콘에서 상태/로그, Bridge 보기 또는 종료를 사용해 주세요.",
            APP_TITLE,
            0x00000040,
        )
    except Exception:
        pass


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Windows Credential Manager wrapper for eFriend auto-login secrets.

    Secrets are consumed only in memory by the local eFriend UI automation path
    and are never written to logs or repository files.
    """

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    TARGET = "InvestmentLocalSuite/eFriendExpert"

    def __init__(self, target: str | None = None):
        self.target = target or self.TARGET

    @staticmethod
    def _encode_payload(id_password: str, certificate_password: str) -> bytes:
        payload = {
            "version": 1,
            "id_password": id_password,
            "certificate_password": certificate_password,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _decode_payload(blob: bytes) -> dict[str, str]:
        payload = json.loads(blob.decode("utf-8"))
        if payload.get("version") != 1:
            raise ValueError("지원하지 않는 eFriend 자격 증명 형식입니다.")
        return {
            "id_password": str(payload.get("id_password", "")),
            "certificate_password": str(payload.get("certificate_password", "")),
        }

    def _api(self):
        if os.name != "nt":
            raise RuntimeError("Windows Credential Manager는 Windows에서만 사용할 수 있습니다.")
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        return advapi32

    def exists(self) -> bool:
        api = self._api()
        pointer = ctypes.POINTER(_CREDENTIALW)()
        ctypes.set_last_error(0)
        if not api.CredReadW(self.target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return False
            raise ctypes.WinError(error)
        try:
            return True
        finally:
            api.CredFree(pointer)

    def username(self) -> str:
        api = self._api()
        pointer = ctypes.POINTER(_CREDENTIALW)()
        ctypes.set_last_error(0)
        if not api.CredReadW(self.target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return ""
            raise ctypes.WinError(error)
        try:
            return pointer.contents.UserName or ""
        finally:
            api.CredFree(pointer)

    def read(self) -> dict[str, str] | None:
        api = self._api()
        pointer = ctypes.POINTER(_CREDENTIALW)()
        ctypes.set_last_error(0)
        if not api.CredReadW(self.target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            payload = self._decode_payload(blob)
            payload["customer_id"] = credential.UserName or ""
            return payload
        finally:
            api.CredFree(pointer)

    def write(self, customer_id: str, id_password: str, certificate_password: str):
        if not customer_id or not id_password or not certificate_password:
            raise ValueError("고객 ID와 두 비밀번호를 모두 입력해 주세요.")

        api = self._api()
        blob_bytes = self._encode_payload(id_password, certificate_password)
        blob = ctypes.create_string_buffer(blob_bytes, len(blob_bytes))
        credential = _CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.CredentialBlobSize = len(blob_bytes)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = customer_id
        credential.Comment = "Investment Local Suite eFriend auto-login"

        try:
            ctypes.set_last_error(0)
            if not api.CredWriteW(ctypes.byref(credential), 0):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            ctypes.memset(ctypes.addressof(blob), 0, ctypes.sizeof(blob))

    def delete(self) -> bool:
        api = self._api()
        ctypes.set_last_error(0)
        if api.CredDeleteW(self.target, self.CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == self.ERROR_NOT_FOUND:
            return False
        raise ctypes.WinError(error)


class TrayIcon:
    """Small Windows tray icon implemented with Win32 only (no extra packages)."""

    WM_APP = 0x8000
    WM_TRAY = WM_APP + 20
    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B

    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100

    ID_VIEW = 1001
    ID_BRIDGE_VIEW = 1002
    ID_CREDENTIALS = 1003
    ID_EXIT_KEEP_EFRIEND = 1004
    ID_EXIT_ALL = 1005
    ID_LOCAL_BROWSER = 1006
    IDI_APPLICATION = 32512
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    def __init__(self, action_queue: queue.Queue[str], shutdown_requested: threading.Event):
        self.action_queue = action_queue
        self.shutdown_requested = shutdown_requested
        self.hwnd = None
        self.nid = None
        self._thread = None
        self._wndproc_ref = None
        self._class_name = f"InvestmentLocalSuiteTray_{os.getpid()}"
        self._ready = threading.Event()
        self.available = False
        self.error = ""
        self.icon_warning = ""
        self.custom_icon_loaded = False
        self._icon_handle = None
        self._taskbar_created_message = 0

    def _request_exit(self, *, stop_efriend: bool):
        # Set shutdown intent immediately on the tray thread. The Tk main loop
        # may process the queued action slightly later, so this event is the
        # authoritative early gate for startup/browser completion races.
        self.shutdown_requested.set()
        self.action_queue.put("exit_all" if stop_efriend else "exit_keep_efriend")

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            self.error = "tray initialization timed out"
            return False
        return self.available

    def stop(self):
        if os.name != "nt" or not self.hwnd:
            return
        ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def _run(self):
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        try:
            # Explicit Win32 signatures are required on 64-bit Python.  ctypes
            # otherwise assumes c_int for unspecified parameters, which truncates
            # pointer-sized HWND/HMENU/HINSTANCE values (e.g. CreateWindowExW arg 11).
            LRESULT = ctypes.c_ssize_t
            UINT_PTR = ctypes.c_size_t
            LPVOID = ctypes.c_void_p

            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            user32.LoadIconW.restype = wintypes.HICON
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.DestroyIcon.argtypes = [wintypes.HICON]
            user32.DestroyIcon.restype = wintypes.BOOL
            user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
            user32.RegisterWindowMessageW.restype = wintypes.UINT
            user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.DefWindowProcW.restype = LRESULT
            user32.CreatePopupMenu.argtypes = []
            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.AppendMenuW.argtypes = [
                wintypes.HMENU,
                wintypes.UINT,
                UINT_PTR,
                wintypes.LPCWSTR,
            ]
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            user32.GetCursorPos.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = [
                wintypes.HMENU,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                ctypes.c_void_p,
            ]
            user32.TrackPopupMenu.restype = wintypes.UINT
            user32.DestroyMenu.argtypes = [wintypes.HMENU]
            user32.DestroyMenu.restype = wintypes.BOOL
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.PostQuitMessage.restype = None

            shell32.Shell_NotifyIconW.argtypes = [
                wintypes.DWORD,
                ctypes.POINTER(self.NOTIFYICONDATAW),
            ]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL

            WNDPROCTYPE = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROCTYPE),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.restype = LRESULT

            self._taskbar_created_message = int(user32.RegisterWindowMessageW("TaskbarCreated"))

            def add_tray_icon() -> bool:
                if self.nid is None:
                    return False
                return bool(shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self.nid)))

            def wndproc(hwnd, msg, wparam, lparam):
                if self._taskbar_created_message and msg == self._taskbar_created_message:
                    # Explorer can restart independently of the launcher. Re-register
                    # the icon so tray access does not silently disappear.
                    add_tray_icon()
                    return 0
                if msg == self.WM_TRAY:
                    event = int(lparam) & 0xFFFF
                    if event == self.WM_LBUTTONDBLCLK:
                        self.action_queue.put("view")
                        return 0
                    if event in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                        self._show_menu(hwnd)
                        return 0
                elif msg == self.WM_COMMAND:
                    command = int(wparam) & 0xFFFF
                    if command == self.ID_LOCAL_BROWSER:
                        self.action_queue.put("local_browser")
                        return 0
                    if command == self.ID_VIEW:
                        self.action_queue.put("view")
                        return 0
                    if command == self.ID_BRIDGE_VIEW:
                        self.action_queue.put("bridge_view")
                        return 0
                    if command == self.ID_CREDENTIALS:
                        self.action_queue.put("credentials")
                        return 0
                    if command == self.ID_EXIT_KEEP_EFRIEND:
                        self._request_exit(stop_efriend=False)
                        return 0
                    if command == self.ID_EXIT_ALL:
                        self._request_exit(stop_efriend=True)
                        return 0
                elif msg == self.WM_DESTROY:
                    if self.nid is not None:
                        shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.nid))
                    if self._icon_handle:
                        user32.DestroyIcon(self._icon_handle)
                        self._icon_handle = None
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc_ref = WNDPROCTYPE(wndproc)
            hinstance = kernel32.GetModuleHandleW(None)

            icon_path = Path(__file__).resolve().parent / LOCAL_SUITE_ICON
            hicon = None
            if icon_path.exists():
                loaded = user32.LoadImageW(
                    None,
                    str(icon_path),
                    self.IMAGE_ICON,
                    0,
                    0,
                    self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
                )
                if loaded:
                    hicon = wintypes.HICON(loaded)
                    self._icon_handle = hicon
                    self.custom_icon_loaded = True
                else:
                    self.icon_warning = f"custom icon load failed: {icon_path}"
            else:
                self.icon_warning = f"custom icon not found: {icon_path}"

            if not hicon:
                hicon = user32.LoadIconW(None, ctypes.c_void_p(self.IDI_APPLICATION))

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = self._class_name
            wc.hIcon = hicon
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                raise ctypes.WinError()

            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                APP_TITLE,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self.hwnd = hwnd

            nid = self.NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(self.NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            nid.uCallbackMessage = self.WM_TRAY
            nid.hIcon = hicon
            nid.szTip = APP_TITLE
            self.nid = nid
            if not add_tray_icon():
                raise RuntimeError("Shell_NotifyIconW(NIM_ADD) failed")

            self.available = True
            self._ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.available = False
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()

    def _show_menu(self, hwnd):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, self.MF_STRING, self.ID_LOCAL_BROWSER, "로컬 브라우저 열기")
            user32.AppendMenuW(menu, self.MF_STRING, self.ID_VIEW, "Local Suite 상태/로그")
            user32.AppendMenuW(menu, self.MF_STRING, self.ID_BRIDGE_VIEW, "보유종목 실시간 시세")
            user32.AppendMenuW(menu, self.MF_STRING, self.ID_CREDENTIALS, "eFriend 자동 로그인 설정")
            user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT_KEEP_EFRIEND, "서버·Bridge 종료")
            user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT_ALL, "서버·Bridge·eFriend 종료")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            command = user32.TrackPopupMenu(
                menu,
                self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            if command == self.ID_LOCAL_BROWSER:
                self.action_queue.put("local_browser")
            elif command == self.ID_VIEW:
                self.action_queue.put("view")
            elif command == self.ID_BRIDGE_VIEW:
                self.action_queue.put("bridge_view")
            elif command == self.ID_CREDENTIALS:
                self.action_queue.put("credentials")
            elif command == self.ID_EXIT_KEEP_EFRIEND:
                self._request_exit(stop_efriend=False)
            elif command == self.ID_EXIT_ALL:
                self._request_exit(stop_efriend=True)
        finally:
            user32.DestroyMenu(menu)


class LocalSuiteLauncher:
    def __init__(self):
        self.launcher_dir = _runtime_root()
        self.market_ai_dir = self.launcher_dir
        self.dashboard_dir = self.launcher_dir.parent / "investment-dashboard"
        self.root_dir = self.launcher_dir
        self.log_path = self.launcher_dir / "start-local-server.log"

        self.action_queue: queue.Queue[str] = queue.Queue()
        self.shutdown_requested = threading.Event()
        self.tray = TrayIcon(self.action_queue, self.shutdown_requested)
        self.tray_available = False
        self.credential_store = WindowsCredentialStore()
        self.stop_event = threading.Event()
        self.lifecycle_lock = threading.RLock()
        self.started_processes: dict[str, subprocess.Popen] = {}
        self.startup_thread = None
        self.log_handle = None
        self.log_lock = threading.Lock()
        self.status = "시작 준비"
        self.status_lock = threading.Lock()

        self.dashboard_httpd = None
        self.dashboard_thread = None
        self.remote_proxy_httpd = None
        self.remote_proxy_thread = None

        self.progress_lock = threading.Lock()
        self.progress_percent = 0
        self.progress_message = "초기화 중"
        self.progress_failed = False
        self.step_states = {
            "efriend": "대기",
            "bridge": "대기",
            "market_ai": "대기",
            "remote": "대기",
            "dashboard": "대기",
        }

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._hide_loading)

        self.view_window = None
        self.credential_window = None
        self.status_var = tk.StringVar(value=self.status)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_percent_var = tk.StringVar(value="0%")
        self.progress_message_var = tk.StringVar(value=self.progress_message)
        self.step_vars = {key: tk.StringVar(value="○ 대기") for key in self.step_states}
        self.log_text = None
        self.last_log_snapshot = ""
        self._build_loading_ui()

    def _build_loading_ui(self):
        outer = ttk.Frame(self.root, padding=(24, 22, 24, 20))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(outer, text="로컬 투자 환경을 준비하고 있습니다.").pack(anchor="w", pady=(4, 18))

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x")
        ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100, length=390).pack(side="left", fill="x", expand=True)
        ttk.Label(progress_row, textvariable=self.progress_percent_var, width=5, anchor="e").pack(side="right", padx=(12, 0))

        ttk.Label(outer, textvariable=self.progress_message_var, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 18))

        steps = ttk.Frame(outer)
        steps.pack(fill="x")
        labels = (
            ("efriend", "eFriend Expert"),
            ("bridge", "KIS eFriend Market Bridge"),
            ("market_ai", "Market AI API"),
            ("remote", "Tailscale Remote"),
            ("dashboard", "Investment Dashboard"),
        )
        for row, (key, label) in enumerate(labels):
            ttk.Label(steps, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Label(steps, textvariable=self.step_vars[key], width=10, anchor="e").grid(row=row, column=1, sticky="e", pady=3, padx=(24, 0))
        steps.columnconfigure(0, weight=1)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(18, 0))
        ttk.Button(footer, text="eFriend 자동 로그인 설정", command=self.show_credential_setup).pack(side="left")
        ttk.Button(footer, text="로그 보기", command=self.show_view).pack(side="right")

        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 470)
        height = max(self.root.winfo_reqheight(), 330)
        x = max((self.root.winfo_screenwidth() - width) // 2, 0)
        y = max((self.root.winfo_screenheight() - height) // 2, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()

    def _hide_loading(self):
        # Never make the launcher inaccessible when tray registration failed.
        try:
            if self.tray_available:
                self.root.withdraw()
            else:
                self.show_view()
                self.root.withdraw()
        except Exception:
            pass

    def set_progress(self, percent: int, message: str, *, active: str | None = None, complete: tuple[str, ...] = (), failed: bool = False):
        with self.progress_lock:
            requested_percent = max(0, min(100, int(percent)))
            # Progress represents completed startup work, so transient eFriend
            # process hand-offs must never make the UI move backwards.
            self.progress_percent = max(self.progress_percent, requested_percent)
            self.progress_message = message
            self.progress_failed = bool(failed)
            for key in complete:
                if key in self.step_states:
                    self.step_states[key] = "완료"
            if active in self.step_states and self.step_states[active] != "완료":
                self.step_states[active] = "진행"

    def _refresh_loading_state(self):
        with self.progress_lock:
            percent = self.progress_percent
            message = self.progress_message
            failed = self.progress_failed
            states = dict(self.step_states)

        self.progress_var.set(percent)
        self.progress_percent_var.set(f"{percent}%")
        self.progress_message_var.set(("실패: " if failed else "") + message)
        symbols = {
            "대기": "○ 대기",
            "진행": "● 진행",
            "완료": "✓ 완료",
            "경고": "⚠ 경고",
        }
        for key, state in states.items():
            value = symbols.get(state, state)
            if self.step_vars[key].get() != value:
                self.step_vars[key].set(value)

    def _queue_startup_complete(self):
        # Give the main thread enough time to paint the real 100% state before
        # hiding the loader and opening the dashboard.
        self.root.after(650, self._finish_startup_ui)

    def _finish_startup_ui(self):
        try:
            with self.lifecycle_lock:
                self._cancel_gate()
                if self.tray_available:
                    self.root.withdraw()
                else:
                    # Keep a visible control surface when Windows rejected the
                    # tray icon so View/Credentials/Shutdown are still reachable.
                    self.root.deiconify()
                    self.root.lift()
                webbrowser.open(f"http://localhost:{DASHBOARD_PORT}/")
        except StartupCancelled:
            return
        except Exception as exc:
            self.log(f"[WARN] Browser open failed: {exc}")

    def run(self):
        self._reset_log()
        self.tray_available = self.tray.start()
        if self.tray_available:
            self.log("[OK]    Investment Local Suite tray icon registered.")
            if self.tray.custom_icon_loaded:
                self.log(f"[OK]    Local Suite tray image loaded: {LOCAL_SUITE_ICON}")
            elif self.tray.icon_warning:
                self.log(f"[WARN] Local Suite tray image unavailable; default icon used: {self.tray.icon_warning}")
        else:
            self.log(f"[WARN] Local Suite tray icon registration failed: {self.tray.error or 'unknown error'}")
            self.log("       Progress/View UI will remain available as a fallback.")
        self.root.after(150, self._process_actions)
        self.root.after(800, self._refresh_view)
        self.startup_thread = threading.Thread(target=self._startup, name="local-suite-startup", daemon=True)
        self.startup_thread.start()
        self.root.mainloop()

    def _reset_log(self):
        self.log_path.write_text("", encoding="utf-8")
        self.log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
        self.log("=" * 58)
        self.log("  Investment Dashboard + Market AI Local Suite")
        self.log("=" * 58)
        self.log(f"Started   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log(f"Log file  : {self.log_path}")

    def log(self, message: str = ""):
        line = message.rstrip("\r\n")
        with self.log_lock:
            handle = self.log_handle
            if handle is None or handle.closed:
                return
            try:
                handle.write(line + "\n")
                handle.flush()
            except (OSError, ValueError):
                # Shutdown may close the log while the daemon startup thread is
                # returning from a long build/dependency check. Never let that
                # race surface as a second launcher error.
                return

    def _cancel_gate(self):
        """Abort startup cleanly once tray Exit has requested shutdown."""
        if self.shutdown_requested.is_set() or self.stop_event.is_set():
            raise StartupCancelled()

    def set_status(self, value: str):
        # Worker threads only update Python state. Tk state is refreshed on the
        # main thread from _process_actions() to avoid cross-thread Tcl calls.
        with self.status_lock:
            self.status = value

    def set_step_state(self, key: str, state: str):
        """Update one startup step without treating optional warnings as fatal."""
        with self.progress_lock:
            if key in self.step_states:
                self.step_states[key] = state

    def _startup(self):
        try:
            if os.name != "nt":
                raise RuntimeError("이 런처는 Windows 전용입니다.")
            if not _is_admin():
                raise RuntimeError("런처 관리자 권한이 확인되지 않았습니다.")
            self._cancel_gate()

            self.log("[OK]    Launcher administrator token confirmed (single UAC mode).")

            market_ai_dir = self.market_ai_dir
            dashboard_dir = self.dashboard_dir
            market_ai_exe = market_ai_dir / MARKET_AI_PROCESS

            if not market_ai_exe.exists():
                raise RuntimeError(f"MarketAI.exe를 찾지 못했습니다: {market_ai_exe}")
            if not (market_ai_dir / "_internal").is_dir():
                raise RuntimeError(
                    f"Market AI _internal 폴더를 찾지 못했습니다: {market_ai_dir / '_internal'}"
                )
            if not (dashboard_dir / "index.html").exists():
                raise RuntimeError(
                    "investment-dashboard 폴더를 찾지 못했습니다. "
                    "market-ai와 같은 상위 폴더에 배치해 주세요."
                )
            self._cancel_gate()

            self.log(f"Dashboard : http://localhost:{DASHBOARD_PORT}/")
            self.log(f"Dashboard Dir: {dashboard_dir}")
            self.log(f"Market AI : http://127.0.0.1:{MARKET_AI_PORT}/")
            self.log(f"Remote GET: http://127.0.0.1:{REMOTE_GET_PROXY_PORT}/ -> Market AI :{MARKET_AI_PORT}")
            self.log(f"Market AI Dir: {market_ai_dir}")
            self.log(
                "Runtime   : MarketAI.exe + embedded Dashboard HTTP "
                "(external Python not used)"
            )
            self.log(f"eFriend   : {EFRIEND_EXE}")
            try:
                credential_state = (
                    "configured" if self.credential_store.exists() else "not configured"
                )
                self.log(
                    f"Credential: eFriend auto-login {credential_state} "
                    "(Windows Credential Manager)"
                )
            except Exception as exc:
                self.log(
                    f"[WARN] eFriend credential status check failed: "
                    f"{type(exc).__name__}"
                )
            self.log()

            self.set_status("기존 로컬 프로세스 정리 중")
            self.set_progress(5, "기존 로컬 프로세스를 정리하고 있습니다.")

            if not self._stop_bridge_gracefully(
                reason="새 시작 순서 적용",
                refuse_external=True,
            ):
                raise RuntimeError("기존 KIS Bridge 상태를 안전하게 정리하지 못해 시작을 중단했습니다.")

            if not self._stop_image(MARKET_AI_PROCESS, reason="새 시작 순서 적용"):
                raise RuntimeError("기존 Market AI Runtime을 안전하게 종료하지 못해 시작을 중단했습니다.")
            self._stop_remote_proxy_server()
            if not self._stop_port_listener(REMOTE_GET_PROXY_PORT):
                raise RuntimeError(f"포트 {REMOTE_GET_PROXY_PORT}을 다른 프로세스가 사용 중입니다.")
            if not self._stop_port_listener(MARKET_AI_PORT):
                raise RuntimeError(f"포트 {MARKET_AI_PORT}을 다른 프로세스가 사용 중입니다.")
            self._stop_dashboard_server()
            if not self._stop_port_listener(DASHBOARD_PORT):
                raise RuntimeError(f"포트 {DASHBOARD_PORT}을 다른 프로세스가 사용 중입니다.")
            self._cancel_gate()

            self.set_status("eFriend Expert 확인 중")
            self.set_progress(
                15,
                "eFriend Expert 실행 상태를 확인하고 있습니다.",
                active="efriend",
            )
            if not self._ensure_efriend():
                if self.stop_event.is_set():
                    raise StartupCancelled()
                raise RuntimeError(
                    "eFriend Expert 실행을 확인하지 못해 시작을 중단했습니다."
                )
            self._cancel_gate()

            self.set_status("KIS Bridge 실행 파일 확인 중")
            self.set_progress(
                62,
                "KIS Bridge 실행 파일을 확인하고 있습니다.",
                active="bridge",
                complete=("efriend",),
            )
            bridge_exe = market_ai_dir / BRIDGE_PROCESS
            if not bridge_exe.exists():
                self.log(f"[ERROR] KIS Bridge executable not found: {bridge_exe}")
                raise RuntimeError("KIS Bridge 실행 파일이 없어 시작을 중단했습니다.")
            self.log("[OK]    KIS Bridge prebuilt Release/x86 runtime ready.")
            self._cancel_gate()

            self.set_status("Market AI 실행 파일 확인 중")
            self.set_progress(
                67,
                "MarketAI.exe 실행 환경을 확인하고 있습니다.",
                active="bridge",
                complete=("efriend",),
            )
            if not self._check_market_ai_runtime(market_ai_dir):
                raise RuntimeError(
                    "MarketAI.exe 실행 환경 확인에 실패해 시작을 중단했습니다."
                )
            self._cancel_gate()

            self.set_status("KIS Bridge 시작 중")
            self.set_progress(
                70,
                "KIS eFriend Market Bridge를 시작하고 있습니다.",
                active="bridge",
                complete=("efriend",),
            )
            if not self._start_bridge(market_ai_dir):
                if self.stop_event.is_set():
                    raise StartupCancelled()
                raise RuntimeError(
                    "KIS Bridge 프로세스 실행을 확인하지 못해 시작을 중단했습니다."
                )
            self._cancel_gate()

            self.set_status("Market AI API 시작 중")
            self.set_progress(
                80,
                "MarketAI.exe를 시작하고 있습니다.",
                active="market_ai",
                complete=("efriend", "bridge"),
            )
            if not self._start_market_ai_api(market_ai_dir):
                if self.stop_event.is_set():
                    raise StartupCancelled()
                raise RuntimeError(
                    "Market AI API가 준비되지 않아 대시보드를 시작하지 않았습니다."
                )
            self._cancel_gate()

            self.set_status("원격 읽기 전용 API 준비 중")
            self.set_progress(
                91,
                "Market AI 원격 읽기 전용 프록시를 준비하고 있습니다.",
                active="remote",
                complete=("efriend", "bridge", "market_ai"),
            )
            if not self._start_remote_proxy_server():
                self.log("[WARN] Remote GET-only proxy could not start; remote mode will stay unavailable.")
                # Tailscale Serve --bg persists across restarts.  If this PC still
                # has the old direct :8001 mapping, explicitly turn the canonical
                # Serve root off so a proxy failure can never reopen write APIs.
                self._disable_direct_market_ai_serve(
                    reason="GET-only proxy launch failed",
                    force=True,
                )
                remote_ready = False
            else:
                self.set_status("Tailscale 원격 연결 확인 중")
                remote_ready = self._ensure_tailscale_remote()
            if remote_ready:
                self.set_progress(
                    94,
                    "Tailscale 원격 Market AI 연결이 준비되었습니다.",
                    complete=("efriend", "bridge", "market_ai", "remote"),
                )
            else:
                self.set_step_state("remote", "경고")
                self.set_progress(
                    94,
                    "Tailscale 원격 연결을 사용할 수 없어 로컬 모드로 계속합니다.",
                    complete=("efriend", "bridge", "market_ai"),
                )

            self.set_status("대시보드 시작 중")
            self.set_progress(
                96,
                "Investment Dashboard를 시작하고 있습니다.",
                active="dashboard",
                complete=("efriend", "bridge", "market_ai"),
            )
            if not self._start_dashboard():
                if self.stop_event.is_set():
                    raise StartupCancelled()
                raise RuntimeError("대시보드 HTTP 서버가 준비되지 않았습니다.")
            self._cancel_gate()

            with self.lifecycle_lock:
                self._cancel_gate()
                self.set_status("실행 중")
                self.set_progress(
                    100,
                    "모든 서비스가 준비되었습니다.",
                    complete=("efriend", "bridge", "market_ai", "dashboard"),
                )
                self.log()
                self.log("[OK] Startup sequence complete.")
                remote_note = (
                    "Tailscale Remote ready"
                    if remote_ready
                    else "Tailscale Remote unavailable (local-only fallback)"
                )
                self.log(
                    "     eFriend Expert -> KIS Bridge -> MarketAI.exe "
                    "-> Tailscale Remote check -> Embedded Dashboard HTTP"
                )
                self.log(f"     Remote   : {remote_note}")
                self.log(
                    "     Tray icon: right-click Local Suite 상태/로그 / KIS eFriend Market Bridge / 종료"
                )
                self.action_queue.put("startup_complete")

        except StartupCancelled:
            return
        except Exception as exc:
            self.set_status("시작 실패")
            with self.progress_lock:
                current_percent = self.progress_percent
            self.set_progress(current_percent, str(exc), failed=True)
            self.log()
            self.log(f"[ERROR] {exc}")
            self.log(
                "        시스템 트레이 아이콘 우클릭 > Local Suite 상태/로그에서 로그를 확인하세요."
            )


    def _find_market_ai_dir(self) -> Path | None:
        candidate = self.market_ai_dir
        if (
            (candidate / MARKET_AI_PROCESS).exists()
            and (candidate / "_internal").is_dir()
        ):
            return candidate
        return None

    def _run_hidden(self, args, cwd=None, timeout=None):
        return subprocess.run(
            [str(x) for x in args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )

    def _find_tailscale_cli(self) -> Path | None:
        """Locate the installed Tailscale CLI without depending on shell PATH."""
        resolved = shutil.which(TAILSCALE_CLI_NAME) or shutil.which("tailscale")
        if resolved:
            return Path(resolved)

        candidates = []
        for env_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Tailscale" / TAILSCALE_CLI_NAME)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _tailscale_backend_state(self, cli: Path) -> str:
        """Return Tailscale BackendState, or an empty string when the daemon is unavailable."""
        try:
            result = self._run_hidden([cli, "status", "--json"], timeout=6)
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return ""
        return str(payload.get("BackendState", "")).strip()

    def _start_tailscale_service(self) -> bool:
        """Best-effort start of the Windows Tailscale service; remote access stays optional."""
        if os.name != "nt":
            return False
        self.log(f"[START] Windows service: {TAILSCALE_SERVICE}")
        try:
            result = self._run_hidden(["sc.exe", "start", TAILSCALE_SERVICE], timeout=10)
        except Exception as exc:
            self.log(f"[WARN] Tailscale service start failed: {type(exc).__name__}: {exc}")
            return False

        # sc.exe returns a non-zero code when an already-running service is started.
        # Treat the actual backend state, not that command result, as authoritative.
        if result.returncode not in (0, 1056):
            detail = (result.stdout or result.stderr or "").strip().replace("\r", " ").replace("\n", " ")
            if detail:
                self.log(f"[INFO]  Tailscale service start result: {detail[:240]}")
        return True

    def _wait_tailscale_running(self, cli: Path, seconds: float = 10.0) -> str:
        deadline = time.monotonic() + seconds
        last_state = ""
        while time.monotonic() < deadline and not self.stop_event.is_set():
            self._cancel_gate()
            last_state = self._tailscale_backend_state(cli)
            if last_state == "Running":
                return last_state
            if last_state in {"NeedsLogin", "NoState"}:
                return last_state
            time.sleep(0.5)
        return last_state

    def _tailscale_serve_status(self, cli: Path) -> tuple[bool, str]:
        try:
            result = self._run_hidden([cli, "serve", "status"], timeout=8)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        expected_proxy = f"proxy http://127.0.0.1:{REMOTE_GET_PROXY_PORT}"
        expected_host = TAILSCALE_REMOTE_BASE.removeprefix("https://").rstrip("/")
        ready = (
            result.returncode == 0
            and expected_proxy.lower() in output.lower()
            and expected_host.lower() in output.lower()
            and not self._serve_exposes_backend_directly(output)
        )
        return ready, output

    @staticmethod
    def _serve_exposes_backend_directly(serve_output: str) -> bool:
        unsafe_proxy = f"proxy http://127.0.0.1:{MARKET_AI_PORT}"
        return unsafe_proxy.lower() in (serve_output or "").lower()

    def _disable_direct_market_ai_serve(
        self,
        *,
        cli: Path | None = None,
        serve_output: str | None = None,
        reason: str = "GET-only proxy unavailable",
        force: bool = False,
    ) -> bool:
        """Fail closed when the legacy Serve mapping could expose FastAPI :8001.

        The project owns the canonical root Serve mapping.  If the GET-only
        :8002 proxy is unavailable, remote access must not silently fall back to
        a persistent legacy `tailscale serve --bg 8001` configuration.
        """
        if cli is None:
            cli = self._find_tailscale_cli()
        if cli is None:
            self.log(
                "[WARN] Tailscale CLI not found; could not clear a persisted "
                "legacy direct Serve mapping."
            )
            return False

        if serve_output is None:
            _, serve_output = self._tailscale_serve_status(cli)

        unsafe = self._serve_exposes_backend_directly(serve_output or "")
        if not unsafe and not force:
            return True

        self.log(
            f"[WARN] Fail-closed remote boundary: disabling canonical Tailscale "
            f"Serve ({reason})."
        )
        try:
            disabled = self._run_hidden([cli, "serve", "off"], timeout=12)
        except Exception as exc:
            self.log(
                f"[ERROR] Tailscale Serve fail-closed disable failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

        if disabled.returncode != 0:
            detail = (disabled.stdout or disabled.stderr or "").strip()
            self.log(
                f"[ERROR] Tailscale Serve fail-closed disable failed "
                f"({disabled.returncode}): {detail[:300]}"
            )
            return False

        _, verify_output = self._tailscale_serve_status(cli)
        if self._serve_exposes_backend_directly(verify_output):
            self.log(
                "[ERROR] Legacy direct Market AI Serve mapping is still present "
                "after fail-closed disable."
            )
            return False

        self.log(
            "[FIX]   Canonical Tailscale Serve disabled; local Market AI remains "
            "available on loopback only."
        )
        return True

    def _remote_health_ready(self, seconds: float = 8.0) -> bool:
        url = f"{TAILSCALE_REMOTE_BASE.rstrip('/')}/api/health"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            self._cancel_gate()
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if 200 <= response.status < 300:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _ensure_tailscale_remote(self) -> bool:
        """Repair the optional tailnet proxy without making local startup depend on it."""
        cli = self._find_tailscale_cli()
        if cli is None:
            self.log("[WARN] Tailscale CLI not found; remote Market AI is unavailable.")
            return False

        state = self._tailscale_backend_state(cli)
        if state == "NeedsLogin":
            self.log("[WARN] Tailscale requires login; remote Market AI remains unavailable.")
            return False

        if state != "Running":
            if state:
                self.log(f"[INFO]  Tailscale backend state: {state}; attempting service start.")
            else:
                self.log("[INFO]  Tailscale backend is not responding; attempting service start.")
            self._start_tailscale_service()
            state = self._wait_tailscale_running(cli)

        if state != "Running":
            if state == "NeedsLogin":
                self.log("[WARN] Tailscale requires login; remote Market AI remains unavailable.")
            else:
                self.log(f"[WARN] Tailscale is not connected (state={state or 'unavailable'}); local mode continues.")
            return False

        serve_ready, serve_output = self._tailscale_serve_status(cli)

        # A canonical :8002 mapping does not make the configuration safe if a
        # second handler still exposes the full :8001 backend. Clear the owned
        # Serve root first, then rebuild only the GET-only mapping.
        if self._serve_exposes_backend_directly(serve_output):
            self.log(
                "[WARN] Tailscale Serve contains a direct Market AI :8001 mapping; "
                "rebuilding the canonical GET-only boundary."
            )
            if not self._disable_direct_market_ai_serve(
                cli=cli,
                serve_output=serve_output,
                reason="direct :8001 mapping detected alongside remote configuration",
                force=True,
            ):
                return False
            serve_ready = False
            serve_output = ""

        if not serve_ready:
            if serve_output:
                first_line = serve_output.splitlines()[0].strip()
                self.log(f"[INFO]  Tailscale Serve repair required: {first_line[:240]}")
            else:
                self.log("[INFO]  Tailscale Serve configuration is missing; repairing it.")
            try:
                repair = self._run_hidden(
                    [cli, "serve", "--bg", str(REMOTE_GET_PROXY_PORT)],
                    timeout=15,
                )
            except Exception as exc:
                self.log(f"[WARN] Tailscale Serve repair failed: {type(exc).__name__}: {exc}")
                self._disable_direct_market_ai_serve(
                    cli=cli,
                    serve_output=serve_output,
                    reason="GET-only Serve repair raised an exception",
                    force=True,
                )
                return False
            if repair.returncode != 0:
                detail = (repair.stdout or repair.stderr or "").strip()
                self.log(f"[WARN] Tailscale Serve repair failed ({repair.returncode}): {detail[:300]}")
                self._disable_direct_market_ai_serve(
                    cli=cli,
                    serve_output=serve_output,
                    reason="GET-only Serve repair returned an error",
                    force=True,
                )
                return False

            serve_ready, serve_output = self._tailscale_serve_status(cli)
            if not serve_ready:
                detail = serve_output.splitlines()[0].strip() if serve_output else "status unavailable"
                self.log(f"[WARN] Tailscale Serve verification failed after repair: {detail[:240]}")
                self._disable_direct_market_ai_serve(
                    cli=cli,
                    serve_output=serve_output,
                    reason="GET-only Serve verification failed",
                    force=True,
                )
                return False
            self.log(f"[FIX]   Tailscale Serve restored: {TAILSCALE_REMOTE_BASE} -> http://127.0.0.1:{REMOTE_GET_PROXY_PORT} (GET-only)")
        else:
            self.log(f"[OK]    Tailscale Serve already configured for GET-only Market AI proxy :{REMOTE_GET_PROXY_PORT}.")

        if not self._remote_health_ready():
            self.log(f"[WARN] Tailscale Serve exists but remote health did not answer: {TAILSCALE_REMOTE_BASE}/api/health")
            return False

        self.log(f"[OK]    Tailscale remote Market AI ready: {TAILSCALE_REMOTE_BASE}")
        return True

    def _process_running(self, image_name: str) -> bool:
        try:
            result = self._run_hidden(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                timeout=5,
            )
            for row in csv.reader(result.stdout.splitlines()):
                if row and row[0].strip().lower() == image_name.lower():
                    return True
        except Exception:
            pass
        return False

    def _window_title_contains(self, needle: str) -> bool:
        """Return True when any visible top-level Windows window contains needle."""
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            needle_lower = needle.lower()
            found = ctypes.c_bool(False)

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def enum_proc(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if needle_lower in buffer.value.lower():
                    found.value = True
                    return False
                return True

            callback = WNDENUMPROC(enum_proc)
            user32.EnumWindows(callback, 0)
            return bool(found.value)
        except Exception:
            return False

    def _window_process_name(self, hwnd: int) -> str:
        """Return the executable name that owns hwnd, or an empty string."""
        if os.name != "nt" or not hwnd:
            return ""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            if not pid.value:
                return ""

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not handle:
                return ""
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                kernel32.QueryFullProcessImageNameW.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.LPWSTR,
                    ctypes.POINTER(wintypes.DWORD),
                ]
                kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
                if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return ""
                return Path(buffer.value).name.lower()
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""

    def _control_class_matches(self, dialog_hwnd: int, control_id: int, expected_class: str) -> bool:
        """Verify that one dialog child has the expected Win32 class."""
        if os.name != "nt" or not dialog_hwnd:
            return False
        try:
            user32 = ctypes.windll.user32
            user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetDlgItem.restype = wintypes.HWND
            user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetClassNameW.restype = ctypes.c_int
            child = user32.GetDlgItem(wintypes.HWND(dialog_hwnd), int(control_id))
            if not child:
                return False
            class_buffer = ctypes.create_unicode_buffer(128)
            if not user32.GetClassNameW(child, class_buffer, len(class_buffer)):
                return False
            return class_buffer.value.lower() == expected_class.lower()
        except Exception:
            return False

    def _find_efriend_dialog(
        self,
        *,
        exact_title: str | None = None,
        title_contains: str | None = None,
        required_controls: tuple[int, ...] = (),
        required_control_classes: tuple[tuple[int, str], ...] = (),
    ) -> int:
        """Find a verified visible dialog owned by efriendexpert.exe.

        Automation is intentionally fail-closed: title, process ownership, required
        CtrlIds and expected child classes must all match before a window is used.
        """
        if os.name != "nt":
            return 0

        user32 = ctypes.windll.user32
        user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetDlgItem.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        found = [0]
        exact_lower = exact_title.lower() if exact_title else None
        contains_lower = title_contains.lower() if title_contains else None
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_proc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            class_buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            if class_buffer.value != "#32770":
                return True

            # Never automate a similarly named dialog from another application.
            if self._window_process_name(int(hwnd)) != EFRIEND_BOOTSTRAP_PROCESS.lower():
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(max(length + 1, 2))
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            title = title_buffer.value
            title_lower = title.lower()
            if exact_lower is not None and title_lower != exact_lower:
                return True
            if contains_lower is not None and contains_lower not in title_lower:
                return True

            for control_id in required_controls:
                if not user32.GetDlgItem(hwnd, int(control_id)):
                    return True
            for control_id, expected_class in required_control_classes:
                if not self._control_class_matches(int(hwnd), int(control_id), expected_class):
                    return True

            found[0] = int(hwnd)
            return False

        callback = WNDENUMPROC(enum_proc)
        user32.EnumWindows(callback, 0)
        return found[0]

    def _wait_efriend_dialog(
        self,
        *,
        exact_title: str | None = None,
        title_contains: str | None = None,
        required_controls: tuple[int, ...] = (),
        required_control_classes: tuple[tuple[int, str], ...] = (),
        timeout_seconds: float = 20.0,
    ) -> int:
        """Wait for one of the verified eFriend dialogs without blocking shutdown."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._cancel_gate()
            if self._efriend_ready():
                return 0
            hwnd = self._find_efriend_dialog(
                exact_title=exact_title,
                title_contains=title_contains,
                required_controls=required_controls,
                required_control_classes=required_control_classes,
            )
            if hwnd:
                return hwnd
            time.sleep(0.25)
        return 0

    def _set_dialog_edit(self, dialog_hwnd: int, control_id: int, value: str) -> bool:
        """Set a verified Win32 Edit control through WM_SETTEXT without reading it back."""
        if os.name != "nt" or not dialog_hwnd:
            return False
        if self._window_process_name(dialog_hwnd) != EFRIEND_BOOTSTRAP_PROCESS.lower():
            return False
        if not self._control_class_matches(dialog_hwnd, control_id, "Edit"):
            return False
        user32 = ctypes.windll.user32
        user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetDlgItem.restype = wintypes.HWND
        user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        user32.IsWindowEnabled.restype = wintypes.BOOL
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        child = user32.GetDlgItem(wintypes.HWND(dialog_hwnd), int(control_id))
        if not child or not user32.IsWindowEnabled(child):
            return False

        # Keep the secret in a temporary writable buffer only for the synchronous
        # cross-process WM_SETTEXT call.  We deliberately never read control text.
        buffer = ctypes.create_unicode_buffer(value)
        message_result = ctypes.c_size_t(0)
        try:
            sent = user32.SendMessageTimeoutW(
                child,
                WM_SETTEXT,
                0,
                ctypes.cast(buffer, ctypes.c_void_p).value,
                SMTO_ABORTIFHUNG,
                2000,
                ctypes.byref(message_result),
            )
            return bool(sent) and bool(message_result.value)
        finally:
            ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))

    def _click_dialog_button(self, dialog_hwnd: int, control_id: int) -> bool:
        """Invoke a verified eFriend Button by CtrlId; no mouse coordinates are used."""
        if os.name != "nt" or not dialog_hwnd:
            return False
        if self._window_process_name(dialog_hwnd) != EFRIEND_BOOTSTRAP_PROCESS.lower():
            return False
        if not self._control_class_matches(dialog_hwnd, control_id, "Button"):
            return False
        user32 = ctypes.windll.user32
        user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetDlgItem.restype = wintypes.HWND
        user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        user32.IsWindowEnabled.restype = wintypes.BOOL
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        child = user32.GetDlgItem(wintypes.HWND(dialog_hwnd), int(control_id))
        if not child or not user32.IsWindowEnabled(child):
            return False
        message_result = ctypes.c_size_t(0)
        sent = user32.SendMessageTimeoutW(
            child,
            BM_CLICK,
            0,
            0,
            SMTO_ABORTIFHUNG,
            2000,
            ctypes.byref(message_result),
        )
        return bool(sent)

    def _read_efriend_auto_login_credentials(self) -> dict[str, str] | None:
        """Read the three local secrets once; never log their values."""
        try:
            credentials = self.credential_store.read()
        except Exception as exc:
            self.log(f"[WARN] eFriend auto-login credential read failed: {type(exc).__name__}")
            return None
        if not credentials:
            self.log("[INFO]  eFriend auto-login credentials are not configured; manual login mode.")
            return None

        required = ("customer_id", "id_password", "certificate_password")
        if not all(str(credentials.get(key, "")) for key in required):
            self.log("[WARN] eFriend auto-login credentials are incomplete; manual login mode.")
            for key in required:
                credentials[key] = ""
            return None
        return credentials

    def _attempt_efriend_auto_login(self) -> bool:
        """Attempt one eFriend login/certificate approval cycle, then fall back safely.

        No credential is retried automatically.  A failed/changed eFriend UI simply
        returns False so the existing human-login readiness loop can continue.
        """
        credentials = self._read_efriend_auto_login_credentials()
        if credentials is None:
            return False

        try:
            self._cancel_gate()
            self.log("[AUTO]  eFriend auto-login started (stored credential values are not logged).")
            self.set_progress(25, "eFriend Expert 자동 로그인을 준비하고 있습니다.", active="efriend")

            # If the certificate dialog is already open (for example after the
            # user manually pressed Login), continue from that stage only.
            cert_hwnd = self._find_efriend_dialog(
                title_contains=EFRIEND_CERT_WINDOW_TOKEN,
                required_controls=(EFRIEND_CTRL_CERT_CONFIRM,),
                required_control_classes=((EFRIEND_CTRL_CERT_CONFIRM, "Button"),),
            )
            if not cert_hwnd:
                login_hwnd = self._wait_efriend_dialog(
                    exact_title=EFRIEND_LOGIN_WINDOW_TITLE,
                    required_controls=(
                        EFRIEND_CTRL_CUSTOMER_ID,
                        EFRIEND_CTRL_ID_PASSWORD,
                        EFRIEND_CTRL_CERT_PASSWORD,
                        EFRIEND_CTRL_LOGIN,
                    ),
                    required_control_classes=(
                        (EFRIEND_CTRL_CUSTOMER_ID, "Edit"),
                        (EFRIEND_CTRL_ID_PASSWORD, "Edit"),
                        (EFRIEND_CTRL_CERT_PASSWORD, "Edit"),
                        (EFRIEND_CTRL_LOGIN, "Button"),
                    ),
                    timeout_seconds=20.0,
                )
                if self._efriend_ready():
                    return True
                if not login_hwnd:
                    self.log("[WARN] eFriend login dialog was not detected; switching to manual login.")
                    return False

                self.set_progress(28, "저장된 eFriend 로그인 정보를 입력하고 있습니다.", active="efriend")
                fields = (
                    (EFRIEND_CTRL_CUSTOMER_ID, credentials["customer_id"]),
                    (EFRIEND_CTRL_ID_PASSWORD, credentials["id_password"]),
                    (EFRIEND_CTRL_CERT_PASSWORD, credentials["certificate_password"]),
                )
                for control_id, value in fields:
                    if not self._set_dialog_edit(login_hwnd, control_id, value):
                        self.log(f"[WARN] eFriend login CtrlId {control_id} 입력에 실패해 수동 로그인으로 전환합니다.")
                        return False

                if not self._click_dialog_button(login_hwnd, EFRIEND_CTRL_LOGIN):
                    self.log("[WARN] eFriend 로그인 버튼 실행에 실패해 수동 로그인으로 전환합니다.")
                    return False
                self.log("[AUTO]  eFriend login form submitted.")
                self.set_progress(32, "eFriend 로그인 요청을 처리하고 있습니다.", active="efriend")

                # One submission only.  If the certificate dialog never appears,
                # do not retry passwords; hand control back to the user.
                cert_hwnd = self._wait_efriend_dialog(
                    title_contains=EFRIEND_CERT_WINDOW_TOKEN,
                    required_controls=(EFRIEND_CTRL_CERT_CONFIRM,),
                    required_control_classes=((EFRIEND_CTRL_CERT_CONFIRM, "Button"),),
                    timeout_seconds=45.0,
                )
                if self._efriend_ready():
                    return True
                if not cert_hwnd:
                    self.log("[WARN] eFriend certificate dialog was not detected after one auto-login attempt; manual login fallback.")
                    return False

            self.set_progress(42, "eFriend 인증서 선택을 자동 승인하고 있습니다.", active="efriend")
            if not self._click_dialog_button(cert_hwnd, EFRIEND_CTRL_CERT_CONFIRM):
                self.log("[WARN] eFriend 인증서 선택(확인) 실행에 실패해 수동 처리로 전환합니다.")
                return False
            self.log("[AUTO]  eFriend certificate selection confirmed.")
            self.set_progress(48, "eFriend 로그인/인증 완료를 확인하고 있습니다.", active="efriend")

            if self._wait_process(EFRIEND_READY_PROCESS, 30, stable_seconds=1.5):
                return True

            self.log("[WARN] eFriend main process was not ready after auto approval; manual login fallback.")
            return False
        except StartupCancelled:
            raise
        except Exception as exc:
            self.log(f"[WARN] eFriend auto-login automation failed ({type(exc).__name__}); manual login fallback.")
            return False
        finally:
            # Drop references as soon as the single attempt ends.  Python strings
            # cannot be reliably zeroed, but they are never persisted/logged here.
            for key in ("customer_id", "id_password", "certificate_password"):
                if key in credentials:
                    credentials[key] = ""
            del credentials

    def _efriend_ready(self) -> bool:
        """Return True only when the logged-in eFriend Expert main process is ready."""
        return self._process_running(EFRIEND_READY_PROCESS)

    def _efriend_login_in_progress(self) -> bool:
        """Return True when eFriend has already been launched but login is not ready yet."""
        return (
            self._process_running(EFRIEND_GATE_PROCESS)
            or self._process_running(EFRIEND_BOOTSTRAP_PROCESS)
        )

    def _wait_efriend_ready(self, launch_grace_seconds: int = 15) -> bool:
        """Wait for the real logged-in Expert runtime, not merely the login launcher.

        Observed eFriend state transition on this installation:
          efriendexpert.exe                  -> ID/password login
          efriendexpert.exe + xexpertgate.exe -> certificate approval flow
          efexpertmain.exe                  -> login/certificate approval complete

        Human login has no arbitrary timeout. If the login flow is closed before
        efexpertmain.exe appears, abort after a short disappearance grace period.
        """
        started_at = time.monotonic()
        ready_seen_at = None
        login_process_seen = self._efriend_login_in_progress()
        login_gone_at = None
        last_login_ui_stage = None

        while not self.stop_event.is_set():
            if self._process_running(EFRIEND_GATE_PROCESS):
                if last_login_ui_stage != "certificate":
                    self.set_progress(35, "eFriend 공동인증서 승인을 완료해 주세요.", active="efriend")
                    last_login_ui_stage = "certificate"
            elif self._process_running(EFRIEND_BOOTSTRAP_PROCESS):
                if last_login_ui_stage != "login":
                    self.set_progress(25, "eFriend Expert 로그인을 완료해 주세요.", active="efriend")
                    last_login_ui_stage = "login"

            if self._efriend_ready():
                if ready_seen_at is None:
                    ready_seen_at = time.monotonic()
                if time.monotonic() - ready_seen_at >= 1.5:
                    return True
            else:
                ready_seen_at = None

            if self._efriend_login_in_progress():
                login_process_seen = True
                login_gone_at = None
            elif login_process_seen:
                # A very short hand-off gap can occur between the certificate
                # helper and efexpertmain.exe, so do not fail immediately.
                if login_gone_at is None:
                    login_gone_at = time.monotonic()
                elif time.monotonic() - login_gone_at >= 8.0:
                    return False
            elif time.monotonic() - started_at >= launch_grace_seconds:
                # ShellExecute succeeded but no eFriend login process appeared.
                return False

            time.sleep(0.5)

        return False

    def _wait_process(self, image_name: str, timeout_seconds: int, stable_seconds: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout_seconds
        first_seen = None
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if self._process_running(image_name):
                if first_seen is None:
                    first_seen = time.monotonic()
                if time.monotonic() - first_seen >= stable_seconds:
                    return True
            else:
                first_seen = None
            time.sleep(0.5)
        return False

    def _wait_owned_runtime_process(
        self, image_name: str, pid: int, timeout_seconds: int, stable_seconds: float = 1.5
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        first_seen = None
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if pid in self._owned_runtime_pids(image_name):
                if first_seen is None:
                    first_seen = time.monotonic()
                if time.monotonic() - first_seen >= stable_seconds:
                    return True
            else:
                first_seen = None
            time.sleep(0.5)
        return False

    def _ensure_efriend(self) -> bool:
        if not EFRIEND_EXE.exists():
            self.log(f"[WARN] eFriend Expert executable not found: {EFRIEND_EXE}")
            return False

        # The actual logged-in runtime is efexpertmain.exe.  If it is already
        # present, auto-login is never entered and the user's existing session is
        # left untouched.
        if self._efriend_ready():
            self.log(f"[OK]    eFriend Expert already logged in ({EFRIEND_READY_PROCESS}); launch skipped.")
            self.set_progress(55, "eFriend Expert 로그인/인증이 완료되었습니다.", complete=("efriend",))
            return True

        # Never start a duplicate login launcher.  If a login/certificate flow is
        # already open, attempt the configured automation once from its current
        # stage; otherwise fall back to the existing manual wait loop.
        if self._efriend_login_in_progress():
            if self._attempt_efriend_auto_login():
                self.log(f"[OK]    eFriend Expert auto-login ready ({EFRIEND_READY_PROCESS}).")
                self.set_progress(55, "eFriend Expert 자동 로그인/인증이 완료되었습니다.", complete=("efriend",))
                return True

            if self._process_running(EFRIEND_GATE_PROCESS):
                self.log("[WAIT]  eFriend Expert 인증서 선택/승인을 수동으로 완료해 주세요.")
                self.set_progress(35, "eFriend 공동인증서 승인을 완료해 주세요.", active="efriend")
            else:
                self.log("[WAIT]  eFriend Expert 아이디/비밀번호 로그인을 수동으로 완료해 주세요.")
                self.set_progress(25, "eFriend Expert 로그인을 완료해 주세요.", active="efriend")
            self.log(f"        최종 로그인 완료 프로세스 대기: {EFRIEND_READY_PROCESS}")
            if self._wait_efriend_ready():
                self.log(f"[OK]    eFriend Expert login/certificate ready ({EFRIEND_READY_PROCESS}).")
                self.set_progress(55, "eFriend Expert 로그인/인증이 완료되었습니다.", complete=("efriend",))
                return True
            self.log("[WARN] eFriend Expert 로그인 흐름이 완료 전에 종료되었습니다.")
            return False

        self.log("[START] eFriend Expert")
        self.log("        관리자 권한 런처에서 실행하므로 추가 UAC는 표시되지 않습니다.")
        self.set_progress(20, "eFriend Expert를 실행하고 있습니다.", active="efriend")
        try:
            with self.lifecycle_lock:
                self._cancel_gate()
                subprocess.Popen(
                    [str(EFRIEND_EXE)],
                    cwd=str(EFRIEND_EXE.parent),
                    creationflags=CREATE_NEW_PROCESS_GROUP,
                )
        except StartupCancelled:
            return False
        except Exception as exc:
            self.log(f"[WARN] eFriend Expert launch failed: {exc}")
            return False

        if self._attempt_efriend_auto_login():
            self.log(f"[OK]    eFriend Expert auto-login ready ({EFRIEND_READY_PROCESS}).")
            self.set_progress(55, "eFriend Expert 자동 로그인/인증이 완료되었습니다.", complete=("efriend",))
            return True

        self.log("[WAIT]  eFriend Expert 수동 로그인 + 공동인증서 승인을 기다립니다.")
        self.log(f"        최종 Ready 기준: {EFRIEND_READY_PROCESS}")
        self.set_progress(25, "eFriend 로그인과 공동인증서 승인을 완료해 주세요.", active="efriend")
        if self._wait_efriend_ready():
            self.log(f"[OK]    eFriend Expert login/certificate ready ({EFRIEND_READY_PROCESS}).")
            self.set_progress(55, "eFriend Expert 로그인/인증이 완료되었습니다.", complete=("efriend",))
            return True

        self.log("[WARN] eFriend Expert 로그인 흐름이 완료 전에 종료되었습니다.")
        return False

    def _process_pids(self, image_name: str) -> set[int]:
        """Return exact PIDs for an image name using tasklist CSV output."""
        pids: set[int] = set()
        try:
            result = self._run_hidden(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                timeout=5,
            )
            for row in csv.reader(result.stdout.splitlines()):
                if len(row) >= 2 and row[0].strip().lower() == image_name.lower():
                    pid_text = row[1].strip()
                    if pid_text.isdigit():
                        pids.add(int(pid_text))
        except Exception:
            pass
        return pids

    def _process_executable_path(self, pid: int) -> Path | None:
        """Resolve a Windows process image path without trusting its image name."""
        if os.name != "nt" or pid <= 0:
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            query_path = kernel32.QueryFullProcessImageNameW
            close_handle = kernel32.CloseHandle

            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            query_path.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            query_path.restype = wintypes.BOOL
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                capacity = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(capacity.value)
                if not query_path(handle, 0, buffer, ctypes.byref(capacity)):
                    return None
                value = buffer.value.strip()
                return Path(value) if value else None
            finally:
                close_handle(handle)
        except Exception:
            return None

    @staticmethod
    def _same_executable_path(actual: Path | None, expected: Path) -> bool:
        if actual is None:
            return False
        try:
            return os.path.normcase(os.path.abspath(str(actual))) == os.path.normcase(
                os.path.abspath(str(expected))
            )
        except Exception:
            return False

    def _runtime_executable(self, image_name: str) -> Path | None:
        if image_name.lower() == MARKET_AI_PROCESS.lower():
            return self.market_ai_dir / MARKET_AI_PROCESS
        if image_name.lower() == BRIDGE_PROCESS.lower():
            return self.market_ai_dir / BRIDGE_PROCESS
        return None

    def _runtime_pid_groups(self, image_name: str) -> tuple[set[int], set[int], set[int]]:
        """Split same-name PIDs into owned, verified-external, and unresolved groups."""
        expected = self._runtime_executable(image_name)
        if expected is None:
            return set(), set(), set(self._process_pids(image_name))

        owned: set[int] = set()
        external: set[int] = set()
        unresolved: set[int] = set()
        for pid in self._process_pids(image_name):
            actual = self._process_executable_path(pid)
            if actual is None:
                unresolved.add(pid)
            elif self._same_executable_path(actual, expected):
                owned.add(pid)
            else:
                external.add(pid)
        return owned, external, unresolved

    def _owned_runtime_pids(self, image_name: str) -> set[int]:
        return self._runtime_pid_groups(image_name)[0]

    def _unowned_runtime_pids(self, image_name: str) -> set[int]:
        return self._runtime_pid_groups(image_name)[1]

    def _unresolved_runtime_pids(self, image_name: str) -> set[int]:
        return self._runtime_pid_groups(image_name)[2]

    def _post_wm_close_to_pids(self, pids: set[int]) -> int:
        """Ask visible top-level windows owned by pids to close normally."""
        if os.name != "nt" or not pids:
            return 0
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            enum_windows = user32.EnumWindows
            get_window_pid = user32.GetWindowThreadProcessId
            is_visible = user32.IsWindowVisible
            post_message = user32.PostMessageW

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            enum_windows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
            enum_windows.restype = wintypes.BOOL
            get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            get_window_pid.restype = wintypes.DWORD
            is_visible.argtypes = [wintypes.HWND]
            is_visible.restype = wintypes.BOOL
            post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            post_message.restype = wintypes.BOOL

            WM_CLOSE = 0x0010
            posted = 0

            def enum_proc(hwnd, _lparam):
                nonlocal posted
                pid = wintypes.DWORD()
                get_window_pid(hwnd, ctypes.byref(pid))
                if pid.value in pids and is_visible(hwnd):
                    if post_message(hwnd, WM_CLOSE, 0, 0):
                        posted += 1
                return True

            callback = WNDENUMPROC(enum_proc)
            enum_windows(callback, 0)
            return posted
        except Exception as exc:
            self.log(f"[WARN] eFriend graceful window-close request failed: {type(exc).__name__}: {exc}")
            return 0

    def _efriend_running_entries(self) -> list[tuple[str, int]]:
        entries: list[tuple[str, int]] = []
        for image_name in (EFRIEND_READY_PROCESS, EFRIEND_GATE_PROCESS, EFRIEND_BOOTSTRAP_PROCESS):
            for pid in sorted(self._process_pids(image_name)):
                entries.append((image_name, pid))
        return entries

    def _invoke_efriend_tray_exit(self) -> bool:
        """Use eFriend's own tray menu (e-Friend Expert > 종료) for normal shutdown."""
        if os.name != "nt":
            return False

        script = self.root_dir / EFRIEND_TRAY_EXIT_SCRIPT
        if not script.exists():
            self.log(f"[WARN] eFriend tray-exit helper not found: {script}")
            return False

        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-STA",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-TrayName",
                    "e-Friend Expert",
                    "-ExitName",
                    "종료",
                    "-TimeoutSeconds",
                    "6",
                ],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=_hidden_startupinfo(),
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
        except Exception as exc:
            self.log(f"[WARN] eFriend tray 종료 자동화 실행 실패: {type(exc).__name__}: {exc}")
            return False

        detail = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0:
            if detail:
                self.log(f"[WARN] eFriend tray 종료 자동화 실패 ({result.returncode}): {detail}")
            else:
                self.log(f"[WARN] eFriend tray 종료 자동화 실패 ({result.returncode}).")
            return False

        self.log("        e-Friend Expert tray > 종료 invoked.")
        return True

    def _stop_efriend_chain(self, reason: str = "") -> bool:
        """Close eFriend through its verified tray Exit command, then verify zero processes.

        On this installation the main-window X/WM_CLOSE and `파일 > 끝내기` do not
        terminate the eFriend runtime.  The verified normal exit path is the
        `e-Friend Expert` system-tray icon's `종료` command.  Use that path first;
        only if tray automation fails do we retain the old force-stop fallback.
        """
        initial = self._efriend_running_entries()
        if not initial:
            return True

        suffix = f" ({reason})" if reason else ""
        self.log(f"[STOP]  eFriend Expert{suffix}")

        if self._invoke_efriend_tray_exit():
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if not self._efriend_running_entries():
                    self.log("[OK]    eFriend Expert stopped via tray Exit.")
                    return True
                time.sleep(0.25)
            self.log("[WARN] eFriend tray 종료 명령 후 프로세스가 남아 있어 fallback 종료를 시도합니다.")

        # Fallback only: eFriend's own tray Exit is the primary verified path.
        kill_order = (EFRIEND_GATE_PROCESS, EFRIEND_READY_PROCESS, EFRIEND_BOOTSTRAP_PROCESS)
        for _round_no in range(1, 3):
            if not self._efriend_running_entries():
                self.log("[OK]    eFriend Expert stopped.")
                return True

            for image_name in kill_order:
                for pid in sorted(self._process_pids(image_name)):
                    try:
                        self._run_hidden(["taskkill", "/F", "/PID", str(pid)], timeout=8)
                    except Exception as exc:
                        self.log(f"[WARN] eFriend fallback stop PID {pid} ({image_name}) failed: {type(exc).__name__}: {exc}")
            time.sleep(0.5)

        remaining = self._efriend_running_entries()
        if remaining:
            detail = ", ".join(f"{name}(PID {pid})" for name, pid in remaining)
            self.log(f"[ERROR] eFriend Expert processes are still running: {detail}")
            return False

        self.log("[OK]    eFriend Expert stopped.")
        return True

    def _post_bridge_message(self, message: int, pids: set[int] | None = None) -> int:
        """Post a Local Suite private message only to Bridge windows owned by this runtime."""
        if os.name != "nt":
            return 0
        pids = set(pids) if pids is not None else self._owned_runtime_pids(BRIDGE_PROCESS)
        if not pids:
            return 0

        posted = 0
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            enum_windows = user32.EnumWindows
            get_window_pid = user32.GetWindowThreadProcessId
            post_message = user32.PostMessageW

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            enum_windows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
            enum_windows.restype = wintypes.BOOL
            get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            get_window_pid.restype = wintypes.DWORD
            post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            post_message.restype = wintypes.BOOL

            def enum_proc(hwnd, _lparam):
                nonlocal posted
                owner_pid = wintypes.DWORD()
                get_window_pid(hwnd, ctypes.byref(owner_pid))
                if owner_pid.value in pids:
                    if post_message(hwnd, message, 0, 0):
                        posted += 1
                return True

            callback = WNDENUMPROC(enum_proc)
            enum_windows(callback, 0)
        except Exception as exc:
            self.log(f"[WARN] KIS Bridge message 0x{message:04X} failed: {type(exc).__name__}: {exc}")
        return posted

    def show_bridge_view(self):
        """Open the hidden Bridge monitor from the single Local Suite tray icon."""
        owned_pids = self._owned_runtime_pids(BRIDGE_PROCESS)
        if not owned_pids:
            self.log(f"[WARN] Owned {BRIDGE_PROCESS} is not running; Bridge view cannot be opened.")
            return
        posted = self._post_bridge_message(BRIDGE_VIEW_MESSAGE, owned_pids)
        if posted:
            self.log("[VIEW]  KIS eFriend Market Bridge window requested from Local Suite tray.")
        else:
            self.log("[WARN] KIS eFriend Market Bridge view message could not be delivered.")

    def _stop_bridge_gracefully(
        self,
        reason: str = "",
        *,
        refuse_external: bool = False,
    ) -> bool:
        """Stop only this runtime's Bridge; optionally refuse startup beside another runtime."""
        owned_pids, external_pids, unresolved_pids = self._runtime_pid_groups(BRIDGE_PROCESS)
        if external_pids:
            if refuse_external:
                self.log(
                    f"[ERROR] Another runtime owns {BRIDGE_PROCESS}; refusing to start a second Bridge beside "
                    f"PID {', '.join(map(str, sorted(external_pids)))}."
                )
                return False
            self.log(
                f"[KEEP]  Unrelated {BRIDGE_PROCESS} process(es) are outside this runtime: "
                f"PID {', '.join(map(str, sorted(external_pids)))}"
            )
        if unresolved_pids:
            self.log(
                f"[ERROR] Cannot verify {BRIDGE_PROCESS} ownership; refusing to stop or start beside "
                f"PID {', '.join(map(str, sorted(unresolved_pids)))}."
            )
            return False
        if not owned_pids:
            return True

        suffix = f" ({reason})" if reason else ""
        self.log(f"[STOP]  {BRIDGE_PROCESS}{suffix}")
        posted = self._post_bridge_message(BRIDGE_EXIT_MESSAGE, owned_pids)

        if posted:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                remaining = owned_pids & self._owned_runtime_pids(BRIDGE_PROCESS)
                if not remaining:
                    self.log(f"[OK]    {BRIDGE_PROCESS} stopped cleanly.")
                    return True
                time.sleep(0.1)
        else:
            self.log("[WARN] KIS Bridge graceful exit message could not be delivered.")

        remaining = owned_pids & self._owned_runtime_pids(BRIDGE_PROCESS)
        if remaining:
            self.log(f"[WARN] Graceful exit did not remove {BRIDGE_PROCESS}; forcing owned PID termination.")
        for pid in sorted(remaining):
            try:
                self._run_hidden(["taskkill", "/F", "/PID", str(pid), "/T"], timeout=8)
            except Exception as exc:
                self.log(f"[WARN] Forced stop failed for {BRIDGE_PROCESS} PID {pid}: {exc}")

        time.sleep(0.4)
        remaining = owned_pids & self._owned_runtime_pids(BRIDGE_PROCESS)
        if remaining:
            self.log(
                f"[ERROR] Owned {BRIDGE_PROCESS} is still running "
                f"(PID {', '.join(map(str, sorted(remaining)))})."
            )
            return False

        self.log(f"[OK]    {BRIDGE_PROCESS} stopped.")
        return True

    def _stop_image(self, image_name: str, reason: str = "") -> bool:
        """Stop only runtime-owned processes; never kill a same-name process elsewhere."""
        owned_pids, unowned_pids, unresolved_pids = self._runtime_pid_groups(image_name)
        if unowned_pids:
            self.log(
                f"[KEEP]  Unrelated {image_name} process(es) are outside this runtime and will not be stopped: "
                f"PID {', '.join(map(str, sorted(unowned_pids)))}"
            )
        if unresolved_pids:
            self.log(
                f"[KEEP]  {image_name} ownership could not be verified, so it will not be stopped: "
                f"PID {', '.join(map(str, sorted(unresolved_pids)))}"
            )
        if not owned_pids:
            return True

        suffix = f" ({reason})" if reason else ""
        self.log(f"[STOP]  {image_name}{suffix}")
        for pid in sorted(owned_pids):
            try:
                result = self._run_hidden(["taskkill", "/PID", str(pid), "/T"], timeout=8)
                time.sleep(0.2)
                if result.returncode != 0 and pid in self._owned_runtime_pids(image_name):
                    self._run_hidden(["taskkill", "/F", "/PID", str(pid), "/T"], timeout=8)
            except Exception as exc:
                self.log(f"[WARN] Normal stop failed for {image_name} PID {pid}: {exc}")

        remaining = owned_pids & self._owned_runtime_pids(image_name)
        if remaining:
            self.log(f"[WARN] Normal stop did not remove {image_name}; forcing owned PID termination.")
            for pid in sorted(remaining):
                try:
                    self._run_hidden(["taskkill", "/F", "/PID", str(pid), "/T"], timeout=8)
                except Exception as exc:
                    self.log(f"[WARN] Forced stop failed for {image_name} PID {pid}: {exc}")

        time.sleep(0.5)
        remaining = owned_pids & self._owned_runtime_pids(image_name)
        if remaining:
            self.log(
                f"[ERROR] Owned {image_name} is still running "
                f"(PID {', '.join(map(str, sorted(remaining)))})."
            )
            return False

        self.log(f"[OK]    {image_name} stopped.")
        return True

    def _pids_listening_on_port(self, port: int) -> set[int]:
        pids: set[int] = set()
        try:
            result = self._run_hidden(["netstat", "-ano", "-p", "tcp"], timeout=8)
            target = f":{port}"
            for raw in result.stdout.splitlines():
                parts = raw.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                local_addr, state, pid_text = parts[1], parts[3].upper(), parts[4]
                if state == "LISTENING" and local_addr.endswith(target) and pid_text.isdigit():
                    pids.add(int(pid_text))
        except Exception as exc:
            self.log(f"[WARN] Could not inspect port {port}: {exc}")
        return pids

    def _stop_port_listener(self, port: int) -> bool:
        """Fail closed on an occupied runtime port; ownership cleanup happens by PID/path first."""
        remaining = {pid for pid in self._pids_listening_on_port(port) if pid != os.getpid()}
        if not remaining:
            return True

        details = []
        for pid in sorted(remaining):
            path = self._process_executable_path(pid)
            details.append(f"PID {pid} ({path if path is not None else 'path unavailable'})")
        self.log(
            f"[ERROR] Port {port} is owned by another process; Local Suite will not terminate it: "
            + ", ".join(details)
        )
        return False

    def _check_market_ai_runtime(self, market_ai_dir: Path) -> bool:
        exe = market_ai_dir / MARKET_AI_PROCESS
        internal = market_ai_dir / "_internal"

        if not exe.exists():
            self.log(f"[ERROR] Market AI standalone executable not found: {exe}")
            return False
        if not internal.is_dir():
            self.log(
                f"[ERROR] Market AI _internal runtime directory not found: "
                f"{internal}"
            )
            return False

        self.log(
            "[OK]    Market AI standalone runtime ready "
            "(external Python not used)."
        )
        return True

    def _stop_dashboard_server(self) -> bool:
        httpd = self.dashboard_httpd
        thread = self.dashboard_thread

        if httpd is None:
            return True

        try:
            httpd.shutdown()
        except Exception as exc:
            self.log(
                f"[WARN] Dashboard embedded server shutdown failed: {exc}"
            )
        try:
            httpd.server_close()
        except Exception as exc:
            self.log(
                f"[WARN] Dashboard embedded server close failed: {exc}"
            )

        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=5)
            except Exception:
                pass

        self.dashboard_httpd = None
        self.dashboard_thread = None

        if thread is not None and thread.is_alive():
            self.log(
                "[ERROR] Dashboard embedded HTTP thread is still running."
            )
            return False

        self.log(
            "[OK]    Investment Dashboard embedded HTTP server stopped."
        )
        return True

    def _stop_remote_proxy_server(self) -> bool:
        httpd = self.remote_proxy_httpd
        thread = self.remote_proxy_thread
        if httpd is None:
            return True

        try:
            httpd.shutdown()
        except Exception as exc:
            self.log(f"[WARN] Remote GET-only proxy shutdown failed: {exc}")
        try:
            httpd.server_close()
        except Exception as exc:
            self.log(f"[WARN] Remote GET-only proxy close failed: {exc}")

        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=5)
            except Exception:
                pass

        self.remote_proxy_httpd = None
        self.remote_proxy_thread = None
        if thread is not None and thread.is_alive():
            self.log("[ERROR] Remote GET-only proxy thread is still running.")
            return False

        self.log("[OK]    Remote GET-only Market AI proxy stopped.")
        return True

    def _start_remote_proxy_server(self) -> bool:
        health = f"http://127.0.0.1:{REMOTE_GET_PROXY_PORT}/api/health"
        self.log(
            f"[START] Remote GET-only Market AI proxy :{REMOTE_GET_PROXY_PORT} "
            f"-> 127.0.0.1:{MARKET_AI_PORT}"
        )
        try:
            with self.lifecycle_lock:
                self._cancel_gate()
                httpd = RemoteGetMarketAiProxyServer(
                    ("127.0.0.1", REMOTE_GET_PROXY_PORT),
                    RemoteGetMarketAiProxyHandler,
                )
                httpd.log_callback = self.log
                thread = threading.Thread(
                    target=httpd.serve_forever,
                    kwargs={"poll_interval": 0.2},
                    name="market-ai-getonly-proxy",
                    daemon=True,
                )
                self.remote_proxy_httpd = httpd
                self.remote_proxy_thread = thread
                thread.start()
        except StartupCancelled:
            return False
        except Exception as exc:
            self.log(f"[WARN] Remote GET-only proxy launch failed: {exc}")
            self._stop_remote_proxy_server()
            return False

        if not self._wait_url(health, 10):
            self.log("[WARN] Remote GET-only proxy health did not become ready.")
            self._stop_remote_proxy_server()
            return False

        self.log("[OK]    Remote GET-only Market AI proxy ready; write methods return 405.")
        return True

    def _start_bridge(self, market_ai_dir: Path) -> bool:
        bridge_exe = market_ai_dir / BRIDGE_PROCESS
        if not bridge_exe.exists():
            self.log(f"[WARN] Bridge executable not found: {bridge_exe}")
            return False

        self.log("[START] KIS eFriend Market Bridge")
        self.log("        관리자 권한 런처에서 실행하므로 추가 UAC는 표시되지 않습니다.")
        try:
            with self.lifecycle_lock:
                self._cancel_gate()
                proc = subprocess.Popen(
                    [str(bridge_exe)],
                    cwd=str(market_ai_dir),
                    creationflags=CREATE_NEW_PROCESS_GROUP,
                )
                self.started_processes["bridge"] = proc
        except StartupCancelled:
            return False
        except Exception as exc:
            self.log(f"[WARN] KIS Bridge launch failed: {exc}")
            return False

        # Verify the actual Bridge process by image name. The WinForms app itself
        # starts hidden and exposes View / 종료 through its system-tray icon.
        if not self._wait_owned_runtime_process(BRIDGE_PROCESS, proc.pid, 20, stable_seconds=2.0):
            self.log("[WARN] Owned KIS Bridge process did not become stable within 20 seconds.")
            return False

        self.log("[OK]    KIS eFriend Market Bridge process ready.")
        self.log("        Market AI API will start next; Bridge AUTO route retries until API is ready.")
        self.set_progress(75, "KIS eFriend Market Bridge가 준비되었습니다.", complete=("efriend", "bridge"))
        return True

    def _url_ready(self, url: str, timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return 200 <= response.status < 500
        except Exception:
            return False

    def _wait_url(self, url: str, seconds: int) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if self._url_ready(url):
                return True
            time.sleep(1)
        return False

    def _spawn_server(self, name: str, args, cwd: Path):
        with self.lifecycle_lock:
            self._cancel_gate()
            if self.log_handle is None or self.log_handle.closed:
                raise RuntimeError("log file is not open")
            proc = subprocess.Popen(
                [str(x) for x in args],
                cwd=str(cwd),
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                startupinfo=_hidden_startupinfo(),
            )
            self.started_processes[name] = proc
            return proc

    def _start_market_ai_api(self, market_ai_dir: Path) -> bool:
        health = f"http://127.0.0.1:{MARKET_AI_PORT}/api/health"
        market_ai_exe = market_ai_dir / MARKET_AI_PROCESS

        self.log(
            f"[START] Market AI API :{MARKET_AI_PORT} "
            f"({MARKET_AI_PROCESS})"
        )
        try:
            self._spawn_server(
                "market_ai",
                [market_ai_exe],
                market_ai_dir,
            )
        except StartupCancelled:
            return False
        except Exception as exc:
            self.log(f"[WARN] Market AI EXE launch failed: {exc}")
            return False

        if not self._wait_url(health, 35):
            self.log(
                "[WARN] Market AI API did not become ready within 35 seconds."
            )
            return False

        self.log("[OK]    Market AI API ready (standalone EXE).")
        self.set_progress(
            90,
            "Market AI API가 준비되었습니다.",
            complete=("efriend", "bridge", "market_ai"),
        )
        return True

    def _start_dashboard(self) -> bool:
        url = f"http://127.0.0.1:{DASHBOARD_PORT}/"
        dashboard_dir = self.dashboard_dir

        self.log(
            f"[START] Investment Dashboard :{DASHBOARD_PORT} "
            "(embedded HTTP)"
        )

        launcher = self

        class DashboardHttpServer(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(
                    *args,
                    directory=str(dashboard_dir),
                    **kwargs,
                )

            def log_message(self, format, *args):
                try:
                    launcher.log(
                        f"{self.address_string()} - - "
                        f"[{self.log_date_time_string()}] {format % args}"
                    )
                except Exception:
                    pass

        try:
            with self.lifecycle_lock:
                self._cancel_gate()
                httpd = DashboardHttpServer(
                    ("127.0.0.1", DASHBOARD_PORT),
                    Handler,
                )
                thread = threading.Thread(
                    target=httpd.serve_forever,
                    kwargs={"poll_interval": 0.2},
                    name="investment-dashboard-http",
                    daemon=True,
                )
                self.dashboard_httpd = httpd
                self.dashboard_thread = thread
                thread.start()

        except StartupCancelled:
            return False
        except Exception as exc:
            self.log(
                f"[WARN] Dashboard embedded HTTP launch failed: {exc}"
            )
            try:
                self._stop_dashboard_server()
            except Exception:
                pass
            return False

        if not self._wait_url(url, 15):
            self.log(
                "[WARN] Dashboard embedded HTTP server did not become "
                "ready within 15 seconds."
            )
            self._stop_dashboard_server()
            return False

        self.log("[OK]    Investment Dashboard ready (embedded HTTP).")
        self.set_progress(
            100,
            "Investment Dashboard가 준비되었습니다.",
            complete=("efriend", "bridge", "market_ai", "dashboard"),
        )
        return True

    def _process_actions(self):
        with self.status_lock:
            current_status = self.status
        if self.status_var.get() != current_status:
            self.status_var.set(current_status)
        self._refresh_loading_state()

        try:
            while True:
                action = self.action_queue.get_nowait()
                if action == "local_browser":
                    webbrowser.open(f"http://localhost:{DASHBOARD_PORT}/")
                elif action == "view":
                    self.show_view()
                elif action == "bridge_view":
                    self.show_bridge_view()
                elif action == "credentials":
                    self.show_credential_setup()
                elif action == "startup_complete":
                    self._queue_startup_complete()
                elif action == "exit_keep_efriend":
                    self.shutdown(stop_efriend=False)
                    return
                elif action == "exit_all":
                    self.shutdown(stop_efriend=True)
                    return
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(150, self._process_actions)

    def show_credential_setup(self):
        """Local-only credential setup; values never enter logs or repository files."""
        if self.credential_window is not None and self.credential_window.winfo_exists():
            self.credential_window.deiconify()
            self.credential_window.lift()
            self.credential_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("eFriend 자동 로그인 설정")
        win.resizable(False, False)

        # Do not make the credential dialog transient to the hidden launcher
        # root. After startup the root is withdrawn to the tray, and on Windows
        # a transient child of that hidden owner can be created without ever
        # becoming visible. Use the visible status/log window as owner when it
        # exists; otherwise keep this dialog as an independent top-level window.
        visible_owner = None
        if self.view_window is not None and self.view_window.winfo_exists():
            try:
                if self.view_window.state() != "withdrawn":
                    visible_owner = self.view_window
            except tk.TclError:
                visible_owner = None
        if visible_owner is not None:
            win.transient(visible_owner)
        self.credential_window = win

        def close_credential_window():
            self.credential_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_credential_window)

        outer = ttk.Frame(win, padding=(20, 18, 20, 18))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="eFriend 자동 로그인 자격 증명", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            outer,
            text="Windows 자격 증명 관리자에 저장되며 다음 실행부터 eFriend 자동 로그인에 사용됩니다.\n코드·로그·Git 파일에는 기록하지 않습니다.",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 14))

        customer_var = tk.StringVar()
        id_password_var = tk.StringVar()
        cert_password_var = tk.StringVar()
        status_var = tk.StringVar(value="")

        try:
            customer_var.set(self.credential_store.username())
            configured = self.credential_store.exists()
            status_var.set("현재 상태: 저장됨" if configured else "현재 상태: 저장되지 않음")
        except Exception as exc:
            status_var.set(f"현재 상태 확인 실패: {type(exc).__name__}")

        fields = (
            ("고객 ID", customer_var, False),
            ("ID 비밀번호", id_password_var, True),
            ("공동인증 비밀번호", cert_password_var, True),
        )
        first_entry = None
        for row, (label, variable, secret) in enumerate(fields, start=2):
            ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(outer, textvariable=variable, width=34, show="●" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=5)
            if first_entry is None:
                first_entry = entry

        ttk.Label(outer, textvariable=status_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def clear_secret_entries():
            id_password_var.set("")
            cert_password_var.set("")

        def save_credentials():
            try:
                self.credential_store.write(customer_var.get().strip(), id_password_var.get(), cert_password_var.get())
                clear_secret_entries()
                status_var.set("현재 상태: 저장됨")
                messagebox.showinfo(APP_TITLE, "eFriend 자격 증명을 Windows 자격 증명 관리자에 저장했습니다.", parent=win)
            except Exception as exc:
                clear_secret_entries()
                messagebox.showerror(APP_TITLE, f"자격 증명을 저장하지 못했습니다.\n\n{type(exc).__name__}: {exc}", parent=win)

        def delete_credentials():
            if not messagebox.askyesno(APP_TITLE, "저장된 eFriend 자동 로그인 자격 증명을 삭제할까요?", parent=win):
                return
            try:
                self.credential_store.delete()
                customer_var.set("")
                clear_secret_entries()
                status_var.set("현재 상태: 저장되지 않음")
            except Exception as exc:
                messagebox.showerror(APP_TITLE, f"자격 증명을 삭제하지 못했습니다.\n\n{type(exc).__name__}: {exc}", parent=win)

        buttons = ttk.Frame(outer)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="저장 정보 삭제", command=delete_credentials).pack(side="left")
        ttk.Button(buttons, text="닫기", command=close_credential_window).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="저장", command=save_credentials).pack(side="left", padx=(8, 0))
        outer.columnconfigure(1, weight=1)

        win.update_idletasks()
        width = max(win.winfo_reqwidth(), 500)
        height = max(win.winfo_reqheight(), 290)
        x = max((win.winfo_screenwidth() - width) // 2, 0)
        y = max((win.winfo_screenheight() - height) // 2, 0)
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.deiconify()
        win.lift()
        # Force the tray-opened dialog to the foreground once, then immediately
        # release topmost so it behaves like a normal settings window.
        try:
            win.attributes("-topmost", True)
            win.after(180, lambda: win.winfo_exists() and win.attributes("-topmost", False))
        except tk.TclError:
            pass
        if first_entry is not None:
            first_entry.focus_force()

    def show_view(self):
        if self.view_window is None or not self.view_window.winfo_exists():
            win = tk.Toplevel(self.root)
            win.title(APP_TITLE)
            win.geometry("820x540")
            win.minsize(680, 420)
            win.protocol("WM_DELETE_WINDOW", self.hide_view)

            header = ttk.Frame(win, padding=(12, 10))
            header.pack(fill="x")
            ttk.Label(header, text="상태:").pack(side="left")
            ttk.Label(header, textvariable=self.status_var).pack(side="left", padx=(6, 0))

            frame = ttk.Frame(win, padding=(12, 0, 12, 12))
            frame.pack(fill="both", expand=True)
            text = tk.Text(frame, wrap="none", state="disabled", font=("Consolas", 9))
            yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
            xscroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
            text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            text.grid(row=0, column=0, sticky="nsew")
            yscroll.grid(row=0, column=1, sticky="ns")
            xscroll.grid(row=1, column=0, sticky="ew")
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)

            self.view_window = win
            self.log_text = text
            self.last_log_snapshot = ""
        else:
            self.view_window.deiconify()
            self.view_window.lift()
            self.view_window.focus_force()
        self._refresh_view(force=True)

    def hide_view(self):
        if self.view_window is not None and self.view_window.winfo_exists():
            self.view_window.withdraw()

    def _refresh_view(self, force: bool = False):
        if self.log_text is not None and self.log_text.winfo_exists():
            try:
                snapshot = self.log_path.read_text(encoding="utf-8", errors="replace")
                if force or snapshot != self.last_log_snapshot:
                    self.log_text.configure(state="normal")
                    self.log_text.delete("1.0", "end")
                    self.log_text.insert("1.0", snapshot)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                    self.last_log_snapshot = snapshot
            except Exception:
                pass
        if not self.stop_event.is_set():
            self.root.after(800, self._refresh_view)

    def shutdown(self, *, stop_efriend: bool):
        self.shutdown_requested.set()

        with self.lifecycle_lock:
            if self.stop_event.is_set():
                return
            self.stop_event.set()

        self.set_status("종료 중")
        self.log()
        mode = "서버·Bridge·eFriend" if stop_efriend else "서버·Bridge (eFriend 유지)"
        self.log(f"[STOP] Local Suite shutdown requested: {mode}.")

        dashboard_stopped = self._stop_dashboard_server()
        remote_proxy_stopped = self._stop_remote_proxy_server()

        proc = self.started_processes.get("market_ai")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        market_ai_image_stopped = self._stop_image(
            MARKET_AI_PROCESS,
            reason="Local Suite 종료",
        )
        dashboard_port_stopped = self._stop_port_listener(DASHBOARD_PORT)
        remote_proxy_port_stopped = self._stop_port_listener(REMOTE_GET_PROXY_PORT)
        market_ai_port_stopped = self._stop_port_listener(MARKET_AI_PORT)
        bridge_stopped = self._stop_bridge_gracefully(
            reason="Local Suite 종료"
        )
        efriend_stopped = True
        if stop_efriend:
            efriend_stopped = self._stop_efriend_chain(
                reason="Local Suite 종료"
            )
        else:
            self.log("[KEEP]  eFriend Expert는 종료하지 않고 유지합니다.")

        if (
            dashboard_stopped
            and remote_proxy_stopped
            and dashboard_port_stopped
            and remote_proxy_port_stopped
            and market_ai_port_stopped
            and market_ai_image_stopped
            and bridge_stopped
            and efriend_stopped
        ):
            if stop_efriend:
                self.log("[OK]    서버 -> Bridge -> eFriend 순서로 종료되었습니다.")
            else:
                self.log("[OK]    서버 -> Bridge 순서로 종료되었습니다. eFriend Expert는 유지 중입니다.")
        else:
            self.log(
                "[WARN] 종료 후 일부 대상 프로세스가 남아 있습니다. "
                "위 ERROR 로그를 확인해 주세요."
            )

        try:
            with self.log_lock:
                if self.log_handle is not None:
                    self.log_handle.flush()
                    self.log_handle.close()
                    self.log_handle = None
        except Exception:
            pass

        self.tray.stop()
        self.root.after(50, self.root.destroy)


def _report_fatal_error(exc: BaseException):
    root_dir = _runtime_root()
    log_path = root_dir / "start-local-server.log"
    detail = f"{type(exc).__name__}: {exc}"
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n[FATAL] Launcher initialization failed.\n")
            handle.write(f"        {detail}\n")
    except Exception:
        pass

    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"{APP_TITLE}을 시작하지 못했습니다.\n\n"
                f"{detail}\n\n로그: {log_path}",
                APP_TITLE,
                0x00000010,
            )
        except Exception:
            pass


if __name__ == "__main__":
    instance_guard = None
    try:
        if os.name == "nt" and not _is_admin():
            if _request_launcher_elevation():
                # The elevated pythonw.exe instance continues startup. This
                # original non-elevated process must exit immediately.
                sys.exit(0)
            _show_elevation_error()
            sys.exit(1)

        # The launcher intentionally remains resident after startup to own its
        # tray menu. A named mutex prevents repeated double-clicks from leaving
        # multiple hidden pythonw.exe launcher instances behind.
        instance_guard = SingleInstanceGuard()
        if not instance_guard.acquire():
            _show_already_running()
            sys.exit(0)

        launcher = LocalSuiteLauncher()
        launcher.run()
    except Exception as exc:
        _report_fatal_error(exc)
        sys.exit(1)
    finally:
        if instance_guard is not None:
            instance_guard.close()
