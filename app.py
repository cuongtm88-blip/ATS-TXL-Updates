"""ATS TXL - automated OneBSS export and Telegram reporting."""
from __future__ import annotations

import base64
import html
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import shutil
import zipfile
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import requests

try:
    import keyring
except ImportError:  # Source-only fallback; release builds include keyring.
    keyring = None

try:
    from playwright._impl._errors import TargetClosedError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - friendly message at runtime
    sync_playwright = None
    PlaywrightTimeoutError = RuntimeError
    TargetClosedError = RuntimeError

import TXL_Monitor_Tele_Group_All_Over10 as txl
import diagnostics_upload
import updater
from version import APP_VERSION, UPDATE_CHECK_INTERVAL_SECONDS


ROOT = Path(__file__).resolve().parent


def _app_data_dir():
    override = os.getenv("ATS_TXL_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ATS-TXL"


APP_DATA = _app_data_dir()
SETTINGS_PATH = APP_DATA / "settings.json"
APPLICATION_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
# This marker is shipped only in the dedicated diagnostic installer. Keeping
# it beside the executable avoids changing any settings on a normal install.
DEEP_DIAGNOSTIC_MODE = (
    os.getenv("ATS_TXL_DEEP_DIAGNOSTIC", "").strip() == "1"
    or (APPLICATION_DIR / "ATS-TXL.deep-diagnostic").is_file()
)
if getattr(sys, "frozen", False):
    PROFILE = APP_DATA / "chrome-profile"
    DOWNLOADS = Path.home() / "Downloads" / "ATS-TXL"
else:
    PROFILE = ROOT / "chrome-profile"
    DOWNLOADS = ROOT / "downloads"
ONEBSS_URL = "https://onebss.vnpt.vn/"
INCIDENT_INVENTORY_URL = ONEBSS_URL + "#/htkh/ManagementIncidentInventory?tag=2"
# A browser can close again after the first restart. Retry the same backend a
# bounded number of times; never rotate Chromium/Chrome/Edge mid-session.
MAX_EXCEL_RECOVERY_ATTEMPTS = 3
DIAGNOSTICS_DIRNAME = "diagnostics"
DIAGNOSTICS_SECRET_SERVICE = "ATS-TXL"
DIAGNOSTICS_SECRET_NAME = "github-diagnostics-token"
DEFAULT_DIAGNOSTICS_REPOSITORY = "cuongtm88-blip/ATS-TXL-Diagnostics"
BROWSER_CHOICES = {
    "Google Chrome (mặc định)": "chrome",
    "Microsoft Edge": "msedge",
    "Chromium tích hợp (Playwright)": None,
}
LEGACY_BROWSER_CHOICES = {
    "Google Chrome cài sẵn": "Google Chrome (mặc định)",
}
ONEBSS_EXPIRY_WARNING_SECONDS = 15 * 60
MAX_EXCEL_CAPTURE_BYTES = 100 * 1024 * 1024
TEST_D_CHUNK_BYTES = 512 * 1024
TEST_D_HOOK_VERSION = '1.1.20-diagnostic'
TEST_D_HOOK_SCRIPT = r"""(options) => {
    const {frameId = 'frame-unassigned', observeOnly = false} = options || {};
    if (location.origin !== 'https://onebss.vnpt.vn' || window.__atsTxlTestD) return false;
    const nativeCreate = URL.createObjectURL;
    const nativeRevoke = URL.revokeObjectURL;
    const nativeClick = HTMLAnchorElement.prototype.click;
    const nativeOpen = window.open;
    const documentId = 'document-' + Math.random().toString(36).slice(2);
    const state = {
        armed: false, clickedAt: 0, blobs: new Map(), candidate: null,
        suppressedCount: 0, multipleCandidates: false, listenerInstalled: false,
        nextBlobId: 1
    };
    const emit = (eventType, fields = {}) => {
        // The binding receives only fixed metadata; never send URLs or Blob bytes.
        try { void window.__atsTxlTestDEvent?.({event_type: eventType, frame_id: frameId,
            document_id: documentId, js_timestamp: new Date().toISOString(), ...fields}); } catch (_) {}
    };
    const mimeCategory = blob => {
        const mime = (blob?.type || '').toLowerCase();
        if (!mime) return 'empty';
        if (mime === 'application/octet-stream') return 'octet-stream';
        if (mime === 'application/zip') return 'zip';
        if (/(excel|spreadsheet|sheet)/.test(mime)) return 'excel';
        return 'other';
    };
    const verify = () => ({
        version: '1.1.20-diagnostic', frame_id: frameId, document_id: documentId,
        origin: location.origin, observe_only: observeOnly,
        create_wrapped: URL.createObjectURL === wrappedCreate,
        click_wrapped: HTMLAnchorElement.prototype.click === wrappedClick,
        listener_installed: state.listenerInstalled, blob_map_exists: state.blobs instanceof Map,
        armed: state.armed
    });
    const metadata = () => ({
        state: state.candidate ? 'captured' : state.armed ? 'armed' : 'installed',
        filename_extension: state.candidate ? '.xlsx' : '',
        mime_category: mimeCategory(state.candidate?.blob),
        blob_size: state.candidate?.blob.size || 0,
        captured_at: state.candidate?.capturedAt || '',
        suppressed_count: state.suppressedCount,
        multiple_candidates: state.multipleCandidates
    });
    const candidateFor = (anchor, path) => {
        const isAnchor = anchor instanceof HTMLAnchorElement;
        const filename = isAnchor ? anchor.download || '' : '';
        const url = isAnchor ? anchor.href : '';
        const entry = state.blobs.get(url);
        const ageMs = entry ? Date.now() - entry.createdAt : null;
        const signals = {
            path, armed: state.armed, href_blob: url.startsWith('blob:'),
            blob_in_map: !!entry, blob_age_ms: ageMs,
            filename_extension: filename.toLowerCase().endsWith('.xlsx') ? '.xlsx' :
                /\.(xls|csv|pdf|zip)$/i.test(filename) ? '.' + filename.split('.').pop().toLowerCase() : '.other',
            filename_valid: /^[^\\/\x00-\x1f]{1,160}\.xlsx$/i.test(filename),
            mime_category: mimeCategory(entry?.blob), blob_size: entry?.blob.size || 0,
            onebss_origin: location.origin === 'https://onebss.vnpt.vn',
            duplicate: !!state.candidate && state.candidate.url === url,
            export_timing_window: !!state.clickedAt && Date.now() - state.clickedAt <= 60000,
            blob_id: entry?.id || ''
        };
        let reason = '';
        if (!state.armed || !state.clickedAt) reason = 'NOT_ARMED';
        else if (!isAnchor || !url.startsWith('blob:')) reason = 'NOT_BLOB_URL';
        else if (!signals.onebss_origin || !url.startsWith('blob:' + location.origin + '/')) reason = 'WRONG_ORIGIN';
        else if (!signals.filename_valid) reason = 'FILENAME_NOT_XLSX';
        else if (!entry) reason = 'BLOB_NOT_IN_MAP';
        else if (entry.createdAt < state.clickedAt) reason = 'BLOB_TOO_OLD';
        else if (!signals.export_timing_window) reason = 'EXPORT_WINDOW_EXPIRED';
        else if (entry.blob.size < 1) reason = 'SIZE_INVALID';
        else if (signals.mime_category === 'other') reason = 'MIME_REJECTED';
        emit('CANDIDATE EVALUATED', {...signals, decision: reason ? 'ALLOW' :
            observeOnly ? 'ALLOW' : 'SUPPRESS', reason_code: reason ||
            (observeOnly ? 'OBSERVE_ONLY_FRAME' : state.candidate && state.candidate.url !== url ?
                'MULTIPLE_CANDIDATES' : signals.duplicate ? 'DUPLICATE' : 'MATCH')});
        return reason ? null : {url, blob: entry.blob};
    };
    const capture = (anchor, path) => {
        const candidate = candidateFor(anchor, path);
        if (!candidate || observeOnly) return false;
        if (state.candidate && state.candidate.url !== candidate.url) {
            state.multipleCandidates = true;
        } else if (!state.candidate) {
            state.candidate = {...candidate, capturedAt: new Date().toISOString()};
        }
        state.suppressedCount += 1;
        return true;
    };
    const wrappedCreate = function(...args) {
        const url = nativeCreate.apply(this, args);
        if (state.armed && args[0] instanceof Blob) {
            const id = 'blob-' + state.nextBlobId++;
            state.blobs.set(url, {blob: args[0], createdAt: Date.now(), id});
            emit('CREATE_OBJECT_URL', {blob_id: id, blob_size: args[0].size,
                mime_category: mimeCategory(args[0]), armed: state.armed});
        }
        return url;
    };
    const wrappedRevoke = function(url) {
        const entry = state.blobs.get(url);
        if (entry) emit('REVOKE_OBJECT_URL', {blob_id: entry.id, armed: state.armed});
        return nativeRevoke.apply(this, arguments);
    };
    const wrappedClick = function(...args) {
        emit('ANCHOR_CLICK', {href_blob: this.href.startsWith('blob:'),
            blob_id: state.blobs.get(this.href)?.id || '', armed: state.armed});
        if (capture(this, 'anchor.click')) return;
        return nativeClick.apply(this, args);
    };
    const onClick = event => {
        const anchor = event.target instanceof Element ? event.target.closest('a') : null;
        if (!anchor) return;
        emit('CAPTURE_CLICK_EVENT', {href_blob: anchor.href.startsWith('blob:'),
            blob_id: state.blobs.get(anchor.href)?.id || '', armed: state.armed});
        if (capture(anchor, 'capture-click-event')) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
    };
    const wrappedOpen = function(url, ...args) {
        if (typeof url === 'string' && url.startsWith('blob:')) {
            emit('WINDOW_OPEN_BLOB', {blob_id: state.blobs.get(url)?.id || '', armed: state.armed});
        }
        return nativeOpen.call(this, url, ...args);
    };
    URL.createObjectURL = wrappedCreate;
    URL.revokeObjectURL = wrappedRevoke;
    HTMLAnchorElement.prototype.click = wrappedClick;
    window.open = wrappedOpen;
    document.addEventListener('click', onClick, true);
    state.listenerInstalled = true;
    window.__atsTxlTestD = {
        verify,
        arm() { state.armed = true; return true; },
        markExportClick() { state.clickedAt = Date.now(); return true; },
        metadata,
        async readChunk(offset, length) {
            const blob = state.candidate?.blob;
            if (!blob || !Number.isSafeInteger(offset) || !Number.isSafeInteger(length) ||
                offset < 0 || length < 1 || length > 512 * 1024 ||
                offset + length > blob.size) throw new Error('invalid-test-d-chunk');
            // Only one bounded slice crosses Playwright's JSON transport at a time.
            const bytes = new Uint8Array(await blob.slice(offset, offset + length).arrayBuffer());
            let binary = '';
            for (let i = 0; i < bytes.length; i += 0x8000) {
                binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
            }
            return btoa(binary);
        },
        cleanup() {
            state.armed = false;
            document.removeEventListener('click', onClick, true);
            state.listenerInstalled = false;
            if (URL.createObjectURL === wrappedCreate) URL.createObjectURL = nativeCreate;
            if (URL.revokeObjectURL === wrappedRevoke) URL.revokeObjectURL = nativeRevoke;
            if (HTMLAnchorElement.prototype.click === wrappedClick) {
                HTMLAnchorElement.prototype.click = nativeClick;
            }
            if (window.open === wrappedOpen) window.open = nativeOpen;
            state.blobs.clear();
            state.candidate = null;
            delete window.__atsTxlTestD;
        }
    };
    return true;
}"""


def _normalise_browser_choice(value, default):
    value = LEGACY_BROWSER_CHOICES.get(str(value or ""), str(value or ""))
    return value if value in BROWSER_CHOICES else default


class OneBSSSessionExpiredError(RuntimeError):
    """OneBSS needs a fresh interactive login, including OTP if requested."""


class OneBSSSearchTimeoutError(RuntimeError):
    """OneBSS did not finish a search; never export a partial result."""


class DiagnosticTestCompleted(RuntimeError):
    """A deliberate end of the isolated export-network diagnostic test."""


def _load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_settings(data):
    APP_DATA.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, SETTINGS_PATH)


def _load_diagnostics_token():
    if keyring is None:
        return ""
    try:
        return keyring.get_password(DIAGNOSTICS_SECRET_SERVICE, DIAGNOSTICS_SECRET_NAME) or ""
    except Exception:
        return ""


def _save_diagnostics_token(token):
    if keyring is None:
        raise RuntimeError("Thiếu thư viện keyring để lưu GitHub token an toàn")
    try:
        keyring.set_password(DIAGNOSTICS_SECRET_SERVICE, DIAGNOSTICS_SECRET_NAME, token)
    except Exception as exc:
        raise RuntimeError(f"Không thể lưu GitHub token vào kho bảo mật hệ điều hành: {exc}") from exc
    try:
        SETTINGS_PATH.chmod(0o600)
    except OSError:
        pass


def _parse_recipient_chat_ids(value):
    """Return unique Telegram chat IDs from comma/space/newline-separated text."""
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value]
    else:
        parts = re.split(r"[,;\s]+", str(value or "").strip())

    recipients = []
    for part in parts:
        if not part:
            continue
        if not re.fullmatch(r"-?\d+", part):
            raise ValueError(f'Chat ID không hợp lệ: "{part}"')
        if part not in recipients:
            recipients.append(part)
    return recipients


def _load_recipient_chat_ids(value):
    try:
        return _parse_recipient_chat_ids(value)
    except ValueError:
        return []


class ATSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        saved = _load_settings()
        self.title("ATS TXL - OneBSS → Telegram")
        self.geometry("980x760")
        self.minsize(900, 650)
        self.worker = None
        self.stop_requested = False
        self.auto_repeat = bool(saved.get("schedule_enabled", True))
        self.repeat_seconds = 30 * 60
        self.schedule_enabled = tk.BooleanVar(value=self.auto_repeat)
        self.repeat_minutes = tk.StringVar(value=str(saved.get("repeat_minutes", 30)))
        self.keep_awake_enabled = tk.BooleanVar(
            value=bool(saved.get("keep_awake_enabled", False))
        )
        self.keep_awake_active = False
        self.keep_awake_process = None
        self.telegram_token = tk.StringVar(
            value=os.getenv("TXL_TELEGRAM_BOT_TOKEN", "") or saved.get("telegram_token", "")
        )
        self.telegram_chat_id = tk.StringVar(
            value=os.getenv("TXL_TELEGRAM_GROUP_CHAT_ID", "") or saved.get("telegram_chat_id", "")
        )
        saved_recipients = _load_recipient_chat_ids(saved.get("error_recipient_chat_ids", []))
        self.error_recipient_chat_ids = tk.StringVar(value=", ".join(saved_recipients))
        self._error_recipient_ids = saved_recipients
        self._telegram_token_for_alerts = self.telegram_token.get().strip()
        self.github_diagnostics_enabled = tk.BooleanVar(
            value=bool(saved.get("github_diagnostics_enabled", False))
        )
        self.github_diagnostics_repository = tk.StringVar(
            value=saved.get("github_diagnostics_repository", DEFAULT_DIAGNOSTICS_REPOSITORY)
        )
        self.github_diagnostics_token = tk.StringVar(value=_load_diagnostics_token())
        self.github_include_screenshot = tk.BooleanVar(
            value=bool(saved.get("github_include_screenshot", False))
        )
        self.github_include_trace = tk.BooleanVar(
            value=bool(saved.get("github_include_trace", False))
        )
        self.diagnostics_machine_id = str(saved.get("diagnostics_machine_id") or uuid.uuid4())
        self._diagnostics_upload_config = None
        self.current_stage = "Khởi tạo ứng dụng"
        self.browser_context = None
        self.browser_page = None
        self.browser_backend = "playwright-chromium"
        self.deep_diagnostic_mode = DEEP_DIAGNOSTIC_MODE
        default_browser = (
            "Chromium tích hợp (Playwright)"
            if self.deep_diagnostic_mode
            else "Google Chrome (mặc định)"
        )
        saved_browser = saved.get("browser_choice", saved.get("diagnostic_browser_choice", default_browser))
        self.browser_choice = tk.StringVar(
            value=_normalise_browser_choice(saved_browser, default_browser)
        )
        self.chromium_sandbox_enabled = tk.BooleanVar(
            value=bool(saved.get("diagnostic_chromium_sandbox", False))
        )
        self._onebss_token_expires_at = None
        self._onebss_expiry_warning_for = None
        self._onebss_expired_alert_for = None
        self._browser_launches = []
        self._browser_profile_path = None
        self._browser_native_log_path = None
        self._process_exit_monitor = None
        self._process_exit_monitor_paths = {}
        self._active_export_diagnostic = None
        self._diagnostic_context_ids = set()
        self._diagnostic_page_ids = set()
        self._last_diagnostic_path = None
        self.start_event = None
        self.update_in_progress = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60000, self._tick_onebss_token_status)
        try:
            self._apply_diagnostics_config()
        except Exception as exc:
            self.write_log(f"Chẩn đoán GitHub chưa sẵn sàng: {exc}")
        self.after(5000, self._retry_pending_diagnostics)
        if self.deep_diagnostic_mode:
            self.update_btn.configure(state="disabled", text="Bản test chẩn đoán")
            self.write_log(
                "BẢN TEST CHẨN ĐOÁN: chọn một browser để kiểm chứng; dừng tại lỗi "
                "đầu tiên, không tự phục hồi hoặc tự cập nhật."
            )
        elif updater.can_self_update():
            self.after(2500, self._automatic_update_tick)

    def _build_ui(self):
        pad = {"padx": 12, "pady": 8}
        header = ttk.Frame(self)
        header.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(header, text="ATS TXL", font=("Arial", 18, "bold")).pack(side="left")
        self.update_btn = ttk.Button(
            header,
            text="Kiểm tra cập nhật",
            command=lambda: self._start_update_check(silent=False),
        )
        self.update_btn.pack(side="right")
        ttk.Label(header, text=f"Phiên bản {APP_VERSION}").pack(side="right", padx=(0, 10))
        ttk.Label(self, text="Tự động xuất phiếu từ OneBSS và chuyển sang MonitorTXL").pack(anchor="w", padx=12)

        box = ttk.LabelFrame(self, text="Thiết lập")
        box.pack(fill="x", **pad)
        ttk.Label(box, text="Ngày từ (dd/mm/yyyy)").grid(row=0, column=0, sticky="w", **pad)
        self.from_date = tk.StringVar(value=time.strftime("%d/%m/%Y"))
        ttk.Entry(box, textvariable=self.from_date, width=16).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(box, text="Đến ngày").grid(row=0, column=2, sticky="w", **pad)
        self.to_date = tk.StringVar(value=time.strftime("%d/%m/%Y"))
        ttk.Entry(box, textvariable=self.to_date, width=16).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(box, text="Telegram Bot token").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.telegram_token, show="*", width=28).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Label(box, text="Group chat ID").grid(row=1, column=2, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.telegram_chat_id, width=20).grid(row=1, column=3, sticky="ew", **pad)
        ttk.Label(box, text="Chat ID nhận cảnh báo lỗi").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.error_recipient_chat_ids).grid(
            row=2, column=1, sticky="ew", **pad
        )
        self.fetch_chat_btn = ttk.Button(
            box,
            text="Lấy Chat ID",
            command=self._fetch_private_chats,
        )
        self.fetch_chat_btn.grid(row=2, column=2, sticky="ew", **pad)
        ttk.Button(box, text="Gửi thử", command=self._test_error_recipients).grid(
            row=2, column=3, sticky="ew", **pad
        )
        ttk.Label(
            box,
            text="Có thể nhập nhiều Chat ID, cách nhau bằng dấu phẩy. Để trống nếu không nhận cảnh báo lỗi.",
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 4))
        ttk.Label(
            box,
            text=f"Cấu hình Telegram được lưu riêng trên máy này: {SETTINGS_PATH}",
        ).grid(row=4, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 8))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(2, weight=1)

        diagnostics_box = ttk.LabelFrame(self, text="Chẩn đoán GitHub private (tùy chọn)")
        diagnostics_box.pack(fill="x", **pad)
        ttk.Checkbutton(
            diagnostics_box,
            text="Tự gửi gói chẩn đoán khi có lỗi",
            variable=self.github_diagnostics_enabled,
        ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(diagnostics_box, text="Repository").grid(row=0, column=1, sticky="w", **pad)
        ttk.Entry(
            diagnostics_box,
            textvariable=self.github_diagnostics_repository,
            width=34,
        ).grid(row=0, column=2, sticky="ew", **pad)
        ttk.Label(diagnostics_box, text="GitHub token").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(
            diagnostics_box,
            textvariable=self.github_diagnostics_token,
            show="*",
            width=34,
        ).grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(
            diagnostics_box,
            text="Kiểm tra & gửi thử",
            command=self._test_diagnostics_upload,
        ).grid(row=1, column=3, sticky="ew", **pad)
        ttk.Checkbutton(
            diagnostics_box,
            text="Kèm ảnh màn hình lỗi",
            variable=self.github_include_screenshot,
        ).grid(row=2, column=0, sticky="w", padx=12, pady=(0, 4))
        ttk.Checkbutton(
            diagnostics_box,
            text="Kèm Playwright trace (có thể chứa token và dữ liệu OneBSS)",
            variable=self.github_include_trace,
        ).grid(row=2, column=1, columnspan=2, sticky="w", padx=12, pady=(0, 4))
        if self.deep_diagnostic_mode:
            ttk.Checkbutton(
                diagnostics_box,
                text="Thử bật Chromium sandbox (A/B; mặc định tắt)",
                variable=self.chromium_sandbox_enabled,
            ).grid(row=3, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 4))
        ttk.Label(
            diagnostics_box,
            text="Token chỉ lưu trong Windows Credential Manager/Keychain; không ghi vào settings.json hay GitHub public.",
        ).grid(
            row=4 if self.deep_diagnostic_mode else 3,
            column=0,
            columnspan=4,
            sticky="w",
            padx=12,
            pady=(0, 8),
        )
        diagnostics_box.columnconfigure(2, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="1. Mở OneBSS / đăng nhập", command=self.open_browser)
        self.start_btn.pack(side="left", padx=4)
        self.run_btn = ttk.Button(actions, text="2. Cấu hình & chạy", command=self.run_workflow)
        self.run_btn.pack(side="left", padx=4)
        ttk.Button(actions, text="Dừng", command=self.request_stop).pack(side="left", padx=4)
        ttk.Checkbutton(actions, text="Tự động lặp sau", variable=self.schedule_enabled).pack(side="left", padx=(12, 4))
        ttk.Spinbox(actions, from_=1, to=10080, textvariable=self.repeat_minutes, width=6).pack(side="left")
        ttk.Label(actions, text="phút").pack(side="left", padx=(4, 0))
        ttk.Checkbutton(
            actions,
            text="Giữ máy thức khi chạy",
            variable=self.keep_awake_enabled,
            command=self._on_keep_awake_changed,
        ).pack(side="left", padx=(12, 0))
        ttk.Label(actions, text="Trình duyệt OneBSS:").pack(side="left", padx=(14, 4))
        ttk.Combobox(
            actions,
            textvariable=self.browser_choice,
            values=tuple(BROWSER_CHOICES),
            state="readonly",
            width=27,
        ).pack(side="left")
        self.onebss_token_status = tk.StringVar(value="Phiên OneBSS: chưa kiểm tra")

        session_status = ttk.Frame(self)
        session_status.pack(fill="x", padx=16, pady=(0, 5))
        ttk.Label(session_status, textvariable=self.onebss_token_status).pack(side="left")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=12, pady=(0, 8))
        log_frame = ttk.LabelFrame(self, text="Nhật ký")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(log_frame, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.write_log("Bước 1: mở OneBSS và tự đăng nhập/nhập OTP. Bước 2: cấu hình và chạy quy trình.")

    def write_log(self, text):
        self.after(0, self._append_log, text)

    def report_callback_exception(self, exc_type, exc_value, traceback):
        """Report otherwise-unhandled Tk callback failures to private recipients."""
        error_text = f"{exc_type.__name__}: {exc_value}"
        self.current_stage = "Xử lý giao diện ứng dụng"
        self.write_log(f"LỖI GIAO DIỆN: {error_text}")
        if self._error_recipient_ids and self._telegram_token_for_alerts:
            threading.Thread(
                target=self._send_workflow_error_alert,
                args=(error_text,),
                daemon=True,
            ).start()
        messagebox.showerror("ATS TXL", error_text)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self):
        """Release the temporary no-sleep request before the UI exits."""
        self.request_stop()
        self._stop_process_exit_monitor()
        self._release_keep_awake()
        self.destroy()

    def _on_keep_awake_changed(self):
        """Persist the choice and apply it immediately during an active session."""
        enabled = bool(self.keep_awake_enabled.get())
        try:
            settings = _load_settings()
            settings["keep_awake_enabled"] = enabled
            _save_settings(settings)
        except (OSError, RuntimeError, diagnostics_upload.DiagnosticUploadError) as exc:
            self.write_log(f"Không lưu được lựa chọn giữ máy thức: {exc}")

        if self.worker and self.worker.is_alive():
            if enabled:
                self._acquire_keep_awake()
            else:
                self._release_keep_awake()

    def _acquire_keep_awake(self):
        """Keep macOS/Windows awake only while the OneBSS session is active."""
        if not self.keep_awake_enabled.get() or self.keep_awake_active:
            return

        try:
            if sys.platform == "darwin":
                command = ["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())]
                self.keep_awake_process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            elif sys.platform == "win32":
                import ctypes

                continuous = 0x80000000
                system_required = 0x00000001
                if not ctypes.windll.kernel32.SetThreadExecutionState(
                    continuous | system_required
                ):
                    raise OSError("Windows không chấp nhận yêu cầu giữ máy thức")
            else:
                self.write_log("Chức năng giữ máy thức chưa hỗ trợ hệ điều hành này.")
                return
        except (OSError, subprocess.SubprocessError) as exc:
            self.write_log(f"Không bật được chế độ giữ máy thức: {exc}")
            return

        self.keep_awake_active = True
        self.write_log("Đã bật giữ máy thức trong lúc phiên OneBSS đang chạy.")

    def _release_keep_awake(self):
        """Release the request when the session stops or the checkbox is cleared."""
        if not self.keep_awake_active:
            return

        if sys.platform == "darwin":
            process = self.keep_awake_process
            self.keep_awake_process = None
            if process and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        elif sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except OSError:
                pass

        self.keep_awake_active = False
        self.write_log("Đã tắt giữ máy thức.")

    def _automatic_update_tick(self):
        """Check the public release channel at startup and every six hours."""
        self._start_update_check(silent=True)
        self.after(UPDATE_CHECK_INTERVAL_SECONDS * 1000, self._automatic_update_tick)

    def _start_update_check(self, silent=False):
        if self.update_in_progress:
            if not silent:
                self.write_log("Đang kiểm tra hoặc tải bản cập nhật; vui lòng chờ.")
            return
        self.update_in_progress = True
        self.update_btn.configure(state="disabled")
        if not silent:
            self.write_log("Đang kiểm tra bản cập nhật trên GitHub Releases...")
        threading.Thread(
            target=self._update_check_worker,
            args=(silent,),
            daemon=True,
        ).start()

    def _update_check_worker(self, silent):
        try:
            info = updater.get_available_update(APP_VERSION)
            self.after(
                0,
                lambda info=info, silent=silent: self._finish_update_check(
                    info,
                    None,
                    silent,
                ),
            )
        except Exception as exc:
            error_text = str(exc)
            self.after(
                0,
                lambda error_text=error_text, silent=silent: self._finish_update_check(
                    None,
                    error_text,
                    silent,
                ),
            )

    def _finish_update_check(self, info, error_text, silent):
        self.update_in_progress = False
        self.update_btn.configure(state="normal")
        if error_text:
            self.write_log(f"Không kiểm tra được cập nhật: {error_text}")
            if not silent:
                messagebox.showerror("Cập nhật ATS TXL", error_text)
            return
        if info is None:
            if not silent:
                messagebox.showinfo(
                    "Cập nhật ATS TXL",
                    f"Bạn đang dùng phiên bản mới nhất ({APP_VERSION}).",
                )
            return

        if self.worker and self.worker.is_alive():
            self.write_log(
                f"Có phiên bản {info.version}. Hãy dừng quy trình rồi bấm Kiểm tra cập nhật."
            )
            if not silent:
                messagebox.showinfo(
                    "Có bản cập nhật",
                    f"Phiên bản {info.version} đã sẵn sàng.\n"
                    "Hãy dừng quy trình đang chạy rồi kiểm tra lại để cập nhật an toàn.",
                )
            return

        if not updater.can_self_update():
            self.write_log(
                f"Có phiên bản Windows {info.version}; tự thay EXE chỉ hoạt động trong bản Windows đóng gói."
            )
            if not silent:
                messagebox.showinfo(
                    "Có bản cập nhật Windows",
                    f"Phiên bản {info.version} đã có trên GitHub Releases.\n"
                    "Tính năng tự thay EXE chỉ hoạt động khi chạy ATS-TXL.exe trên Windows.",
                )
            return

        size_text = self._format_size(info.size)
        notes = info.notes.strip()
        if len(notes) > 700:
            notes = notes[:697] + "..."
        prompt = (
            f"Có phiên bản ATS TXL {info.version} ({size_text}).\n\n"
            "Ứng dụng sẽ tải bản mới, kiểm tra SHA-256 rồi tự khởi động lại. "
            "Token và các thiết lập hiện tại được giữ nguyên."
        )
        if notes:
            prompt += f"\n\nNội dung cập nhật:\n{notes}"
        if messagebox.askyesno("Có bản cập nhật", prompt):
            self._start_update_download(info)

    @staticmethod
    def _format_size(size):
        if not size:
            return "không rõ dung lượng"
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    def _start_update_download(self, info):
        self.update_in_progress = True
        self.update_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self.write_log(f"Đang tải ATS TXL {info.version} và xác minh SHA-256...")
        threading.Thread(
            target=self._update_download_worker,
            args=(info,),
            daemon=True,
        ).start()

    def _update_download_worker(self, info):
        try:
            target = updater.download_update(info, APP_DATA / "updates")
            self.after(
                0,
                lambda info=info, target=target: self._install_downloaded_update(
                    info,
                    target,
                ),
            )
        except Exception as exc:
            error_text = str(exc)
            self.after(
                0,
                lambda error_text=error_text: self._finish_update_download_error(
                    error_text
                ),
            )

    def _finish_update_download_error(self, error_text):
        self.update_in_progress = False
        self.progress.stop()
        self.update_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.run_btn.configure(state="normal")
        self.write_log(f"Cập nhật thất bại: {error_text}")
        messagebox.showerror("Cập nhật ATS TXL", error_text)

    def _install_downloaded_update(self, info, target):
        self.progress.stop()
        try:
            updater.launch_windows_installer(target)
        except Exception as exc:
            self._finish_update_download_error(str(exc))
            return
        self.write_log(
            f"Đã xác minh bộ cài ATS TXL {info.version}; ứng dụng sẽ thoát trước khi cài đặt."
        )
        self.destroy()

    def open_browser(self):
        if self.update_in_progress:
            self.write_log("Hãy chờ quá trình cập nhật hoàn tất.")
            return
        if sync_playwright is None:
            messagebox.showerror("Thiếu thư viện", "Chạy: pip install -r requirements.txt && playwright install chromium")
            return
        if self.worker and self.worker.is_alive():
            self.write_log("Trình duyệt OneBSS đã mở. Hãy đăng nhập rồi bấm nút 2.")
            return
        self.stop_requested = False
        self.start_event = threading.Event()
        self.worker = threading.Thread(target=self._session_workflow, daemon=True)
        self.worker.start()

    def request_stop(self):
        self.stop_requested = True
        if self.start_event:
            self.start_event.set()
        self.write_log("Đã yêu cầu dừng.")

    def run_workflow(self):
        if self.worker and self.worker.is_alive():
            if self.start_event:
                if not self._apply_telegram_config():
                    return
                self.write_log("Đã nhận bước 2. Đang kiểm tra đăng nhập, cấu hình OneBSS và chạy quy trình...")
                self.start_event.set()
            return
        self.write_log("Hãy bấm nút 1 để mở OneBSS trước.")

    def _apply_telegram_config(self):
        token = self.telegram_token.get().strip()
        chat_id = self.telegram_chat_id.get().strip()
        if not token or not chat_id:
            messagebox.showerror(
                "Thiếu cấu hình Telegram",
                "Hãy nhập Telegram Bot token và Group chat ID trước khi chạy.",
            )
            return False
        try:
            error_recipient_ids = _parse_recipient_chat_ids(
                self.error_recipient_chat_ids.get()
            )
        except ValueError as exc:
            messagebox.showerror("Chat ID nhận cảnh báo lỗi", str(exc))
            return False
        try:
            interval_minutes = int(self.repeat_minutes.get().strip())
            if not 1 <= interval_minutes <= 10080:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Chu kỳ không hợp lệ",
                "Số phút lặp lại phải là số nguyên từ 1 đến 10080.",
            )
            return False

        self.auto_repeat = bool(self.schedule_enabled.get())
        self.repeat_seconds = interval_minutes * 60
        self._telegram_token_for_alerts = token
        self._error_recipient_ids = error_recipient_ids
        os.environ["TXL_TELEGRAM_BOT_TOKEN"] = token
        os.environ["TXL_TELEGRAM_GROUP_CHAT_ID"] = chat_id
        try:
            settings = _load_settings()
            settings.update({
                "telegram_token": token,
                "telegram_chat_id": chat_id,
                "error_recipient_chat_ids": error_recipient_ids,
                "schedule_enabled": self.auto_repeat,
                "repeat_minutes": interval_minutes,
                "keep_awake_enabled": bool(self.keep_awake_enabled.get()),
                "browser_choice": self.browser_choice.get(),
            })
            self._apply_diagnostics_config(settings)
            _save_settings(settings)
        except OSError as exc:
            messagebox.showerror(
                "Không lưu được cấu hình",
                f"Không thể lưu cấu hình trên máy này: {exc}",
            )
            return False
        self.write_log(
            f"Đã lưu cấu hình; chu kỳ lặp là {interval_minutes} phút; "
            f"có {len(error_recipient_ids)} người nhận cảnh báo lỗi."
        )
        return True

    def _apply_diagnostics_config(self, settings=None):
        """Persist non-secret diagnostics settings and snapshot upload options."""
        enabled = bool(self.github_diagnostics_enabled.get())
        repository = self.github_diagnostics_repository.get().strip()
        token = self.github_diagnostics_token.get().strip() or _load_diagnostics_token()
        if enabled:
            repository = diagnostics_upload.validate_repository(repository)
            if not token:
                raise OSError(
                    "Đã bật gửi chẩn đoán GitHub nhưng chưa có GitHub token. "
                    "Nhập token rồi bấm bước 2 hoặc Kiểm tra & gửi thử."
                )
            if self.github_diagnostics_token.get().strip():
                _save_diagnostics_token(token)
        if settings is None:
            settings = _load_settings()
        settings.update({
            "github_diagnostics_enabled": enabled,
            "github_diagnostics_repository": repository,
            "github_include_screenshot": bool(self.github_include_screenshot.get()),
            "github_include_trace": bool(self.github_include_trace.get()),
            "diagnostics_machine_id": self.diagnostics_machine_id,
        })
        if getattr(self, "deep_diagnostic_mode", False):
            settings["diagnostic_chromium_sandbox"] = bool(
                self.chromium_sandbox_enabled.get()
            )
        self._diagnostics_upload_config = (
            {
                "repository": repository,
                "token": token,
                "include_screenshot": bool(self.github_include_screenshot.get()),
                "include_trace": bool(self.github_include_trace.get()),
                "machine_id": self.diagnostics_machine_id,
            }
            if enabled else None
        )
        return settings

    def _test_diagnostics_upload(self):
        try:
            settings = self._apply_diagnostics_config()
            _save_settings(settings)
        except Exception as exc:
            messagebox.showerror("Chẩn đoán GitHub", str(exc))
            return
        config = self._diagnostics_upload_config
        if not config:
            messagebox.showwarning(
                "Chẩn đoán GitHub",
                "Hãy bật “Tự gửi gói chẩn đoán khi có lỗi” trước khi kiểm tra.",
            )
            return
        self.write_log("Đang kiểm tra quyền gửi chẩn đoán lên GitHub private...")
        threading.Thread(
            target=self._test_diagnostics_upload_worker,
            args=(config,),
            daemon=True,
        ).start()

    def _test_diagnostics_upload_worker(self, config):
        try:
            result = diagnostics_upload.upload_connection_test(
                config["repository"], config["token"], config["machine_id"]
            )
            text = f"Đã gửi kiểm tra GitHub thành công: {result['remote_path']}"
            self.write_log(text)
            self.after(0, lambda: messagebox.showinfo("Chẩn đoán GitHub", text))
        except Exception as exc:
            text = f"Kiểm tra gửi chẩn đoán thất bại: {exc}"
            self.write_log(text)
            self.after(0, lambda: messagebox.showerror("Chẩn đoán GitHub", text))

    def _send_private_message(self, message):
        """Best-effort delivery to every configured private error recipient."""
        recipients = list(self._error_recipient_ids)
        token = self._telegram_token_for_alerts
        successes = []
        failures = []
        if not token:
            return successes, [(chat_id, "Thiếu Telegram Bot token") for chat_id in recipients]

        for chat_id in recipients:
            try:
                txl.send_telegram_message(
                    message,
                    chat_id=chat_id,
                    bot_token=token,
                )
                successes.append(chat_id)
            except Exception as exc:
                failures.append((chat_id, str(exc)))
        return successes, failures

    def _fetch_private_chats(self):
        """Fetch recent private bot conversations without requiring Terminal."""
        token = self.telegram_token.get().strip()
        if not token:
            messagebox.showerror(
                "Thiếu Telegram Bot token",
                "Hãy nhập Telegram Bot token trước khi lấy Chat ID.",
            )
            return
        self.fetch_chat_btn.configure(state="disabled")
        self.write_log("Đang lấy danh sách người đã nhắn tin riêng cho bot...")
        threading.Thread(
            target=self._fetch_private_chats_worker,
            args=(token,),
            daemon=True,
        ).start()

    def _fetch_private_chats_worker(self, token):
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                timeout=15,
            )
            try:
                result = response.json()
            except ValueError:
                result = {}
            if not response.ok or not result.get("ok"):
                description = result.get("description") or f"HTTP {response.status_code}"
                raise RuntimeError(f"Không lấy được Chat ID: {description}")

            chats_by_id = {}
            # Most recent conversations are shown first. Only private chats are
            # eligible; group and channel updates are deliberately ignored.
            for update in reversed(result.get("result", [])):
                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                if chat.get("type") != "private" or "id" not in chat:
                    continue
                chat_id = str(chat["id"])
                if chat_id in chats_by_id:
                    continue
                full_name = " ".join(
                    str(chat.get(key, "")).strip()
                    for key in ("first_name", "last_name")
                    if str(chat.get(key, "")).strip()
                )
                username = str(chat.get("username", "")).strip()
                display_name = full_name or (f"@{username}" if username else "Người dùng Telegram")
                if username and full_name:
                    display_name += f" (@{username})"
                chats_by_id[chat_id] = {
                    "id": chat_id,
                    "name": display_name,
                }

            chats = list(chats_by_id.values())
            self.after(0, lambda chats=chats: self._show_private_chat_picker(chats))
        except Exception as exc:
            error_text = str(exc)
            self.write_log(error_text)
            self.after(
                0,
                lambda error_text=error_text: messagebox.showerror(
                    "Lấy Chat ID",
                    error_text,
                ),
            )
        finally:
            self.after(0, lambda: self.fetch_chat_btn.configure(state="normal"))

    def _show_private_chat_picker(self, chats):
        if not chats:
            messagebox.showinfo(
                "Chưa tìm thấy người nhận",
                "Hãy mở Telegram, vào đúng bot và gửi /start hoặc một tin nhắn mới. "
                "Sau đó quay lại bấm Lấy Chat ID lần nữa.",
            )
            return

        dialog = tk.Toplevel(self)
        dialog.title("Chọn người nhận cảnh báo")
        dialog.geometry("560x360")
        dialog.minsize(480, 300)
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="Chọn một hoặc nhiều người rồi bấm Thêm người đã chọn.",
        ).pack(anchor="w", padx=14, pady=(14, 8))
        listbox = tk.Listbox(dialog, selectmode="extended", exportselection=False)
        listbox.pack(fill="both", expand=True, padx=14, pady=8)
        for chat in chats:
            listbox.insert("end", f'{chat["name"]} — Chat ID: {chat["id"]}')
        listbox.selection_set(0)

        def add_selected():
            selected = listbox.curselection()
            if not selected:
                messagebox.showwarning(
                    "Chưa chọn người nhận",
                    "Hãy chọn ít nhất một người nhận trong danh sách.",
                    parent=dialog,
                )
                return
            try:
                combined = _parse_recipient_chat_ids(
                    self.error_recipient_chat_ids.get()
                )
            except ValueError:
                combined = []
            for index in selected:
                chat_id = chats[index]["id"]
                if chat_id not in combined:
                    combined.append(chat_id)
            self.error_recipient_chat_ids.set(", ".join(combined))
            dialog.destroy()
            self.write_log(
                f"Đã thêm {len(selected)} người nhận cảnh báo lỗi. Hãy bấm Gửi thử để kiểm tra và lưu."
            )

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Button(buttons, text="Hủy", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(
            buttons,
            text="Thêm người đã chọn",
            command=add_selected,
        ).pack(side="right")

    def _test_error_recipients(self):
        if not self._apply_telegram_config():
            return
        if not self._error_recipient_ids:
            messagebox.showwarning(
                "Chưa có người nhận",
                "Hãy nhập ít nhất một Chat ID nhận cảnh báo lỗi.",
            )
            return

        message = "\n".join([
            "<b>✅ KIỂM TRA CẢNH BÁO RIÊNG ATS TXL</b>",
            f"<i>{time.strftime('%d/%m/%Y %H:%M:%S')}</i>",
            "",
            "Bot đã gửi thành công tin nhắn kiểm tra đến người nhận này.",
        ])
        successes, failures = self._send_private_message(message)
        if failures:
            details = "\n".join(f"{chat_id}: {error}" for chat_id, error in failures)
            messagebox.showwarning(
                "Kết quả gửi thử",
                f"Gửi thành công: {len(successes)}\nGửi lỗi: {len(failures)}\n\n{details}",
            )
        else:
            messagebox.showinfo(
                "Kết quả gửi thử",
                f"Đã gửi thành công đến {len(successes)} người nhận.",
            )

    def _send_workflow_error_alert(self, error_text):
        if not self._error_recipient_ids:
            self.write_log("Chưa cấu hình người nhận cảnh báo lỗi riêng.")
            return

        escaped_error = html.escape(str(error_text)[:2500])
        escaped_stage = html.escape(self.current_stage)
        escaped_host = html.escape(socket.gethostname())
        escaped_os = html.escape(f"{platform.system()} {platform.release()}")
        message = "\n".join([
            "<b>⚠️ ATS TXL GẶP SỰ CỐ</b>",
            f"<i>{time.strftime('%d/%m/%Y %H:%M:%S')}</i>",
            "",
            f"<b>Máy:</b> {escaped_host}",
            f"<b>Hệ điều hành:</b> {escaped_os}",
            f"<b>Giai đoạn:</b> {escaped_stage}",
            f"<b>Lỗi:</b> {escaped_error}",
            "",
            "Vui lòng kiểm tra ứng dụng, kết nối mạng và phiên đăng nhập OneBSS.",
        ])
        successes, failures = self._send_private_message(message)
        self.write_log(
            f"Cảnh báo lỗi riêng: gửi thành công {len(successes)}/{len(self._error_recipient_ids)} người nhận."
        )
        if failures:
            self.write_log(f"Có {len(failures)} người nhận cảnh báo lỗi không thành công.")

    @staticmethod
    def _safe_page_url(page):
        try:
            return page.url
        except Exception:
            return "<không đọc được URL>"

    def _record_diagnostic_event(self, context, event_name, **details):
        """Keep a small in-memory event timeline for the current Export only."""
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context:
            return
        def redact(value, key=""):
            value = str(value)
            if key.lower().endswith("url") or key.lower() == "url":
                return self._safe_diagnostic_url(value)
            return re.sub(
                r"(?i)(authorization\s*:\s*bearer\s+)([A-Za-z0-9._~-]+)",
                r"\1[REDACTED]",
                value,
            )[:2000]
        active["events"].append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": event_name,
                **{key: redact(value, key) for key, value in details.items()},
            }
        )

    @staticmethod
    def _safe_diagnostic_url(url):
        """Retain route shape while dropping credentials, query and identifiers."""
        try:
            parts = urlsplit(str(url))
            safe_path = re.sub(
                r"(?i)([0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{20,}|[a-z0-9_-]{32,}|\d{6,})",
                "[REDACTED]",
                parts.path,
            )
            return urlunsplit((parts.scheme, parts.hostname or "", safe_path, "[QUERY_REDACTED]" if parts.query else "", ""))[:500]
        except Exception:
            return "[URL_REDACTED]"

    @staticmethod
    def _is_explicit_export_candidate(request):
        """Flag a high-confidence export candidate when its URL path names an export route."""
        try:
            path = urlsplit(request.url).path.lower()
        except Exception:
            return False
        return bool(
            re.search(
                r"(?:^|[/_.-])(export(?:excel|xlsx|xls|data|file)?|excel(?:export|download)?|download(?:excel|xlsx|xls|file)?|xlsx|xls|spreadsheet)(?:$|[/_.-])",
                path,
            )
            or re.search(r"\.(?:xlsx?|xlsm|csv)$", path)
        )

    def _attach_page_diagnostics(self, context, page):
        page_id = id(page)
        if page_id in self._diagnostic_page_ids:
            return
        self._diagnostic_page_ids.add(page_id)
        page.on(
            "crash",
            lambda: self._on_diagnostic_page_crashed(context, page),
        )
        page.on(
            "close",
            lambda: self._on_diagnostic_page_closed(context, page),
        )
        page.on(
            "pageerror",
            lambda error: self._record_diagnostic_event(
                context, "page-error", error_type=type(error).__name__,
                url=self._safe_page_url(page),
            ),
        )

        def record_console(message):
            if message.type in ("error", "warning"):
                self._record_diagnostic_event(
                    context,
                    "console-" + message.type,
                    url=self._safe_page_url(page),
                )

        page.on("console", record_console)

        def record_download(download):
            active = self._active_export_diagnostic
            if active and active.get("context") is context:
                active["download_event_seen"] = True
                active["download_event_monotonic"] = time.monotonic()
                if active.get("test_d_active"):
                    # Test D uses this event solely as a failure signal; do
                    # not read even metadata from Playwright's Download object.
                    self._record_network_event(
                        context, event_type="TEST D DOWNLOAD EVENT DETECTED",
                        page_id=self._diagnostic_page_id(context, page),
                        decision="fail",
                    )
                    return
            self._record_diagnostic_event(
                context,
                "DOWNLOAD EVENT DETECTED",
                file_extension=Path(download.suggested_filename).suffix.lower()[:12],
                url=self._safe_diagnostic_url(download.url),
                page_id=self._diagnostic_page_id(context, page),
            )
            self._record_network_event(
                context,
                event_type="DOWNLOAD EVENT DETECTED",
                file_extension=Path(download.suggested_filename).suffix.lower()[:12],
                url=self._safe_diagnostic_url(download.url),
                page_id=self._diagnostic_page_id(context, page),
                decision="observed",
            )

        page.on("download", record_download)

    def _diagnostic_page_id(self, context, page):
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context:
            return ""
        ids = active.setdefault("page_ids", {})
        key = id(page)
        if key not in ids:
            ids[key] = f"page-{len(ids) + 1}"
        return ids[key]

    def _attach_context_diagnostics(self, context, page):
        """Subscribe once to browser events needed to identify an Export failure."""
        context_id = id(context)
        if context_id not in self._diagnostic_context_ids:
            self._diagnostic_context_ids.add(context_id)
            context.on(
                "close",
                lambda: self._on_diagnostic_context_closed(context),
            )
            context.on(
                "page",
                lambda new_page: self._on_diagnostic_new_page(context, new_page),
            )
            context.on(
                "request",
                lambda request: self._record_network_request(context, request),
            )
            context.on(
                "response",
                lambda response: self._record_network_response(context, response),
            )
            try:
                context.on(
                    "serviceworker",
                    lambda worker: self._record_network_event(
                        context,
                        event_type="service-worker-detected",
                        url=self._safe_diagnostic_url(worker.url),
                        decision="observed",
                    ),
                )
            except Exception:
                pass
            context.on(
                "requestfailed",
                lambda request: self._record_diagnostic_event(
                    context,
                    "request-failed",
                    url=request.url,
                    failure_observed=bool(request.failure),
                ),
            )
            context.on(
                "weberror",
                lambda error: self._record_diagnostic_event(
                    context, "web-error", error_type=type(error.error).__name__
                ),
            )
            try:
                browser = context.browser
                if browser:
                    browser.on(
                        "disconnected",
                        lambda: self._on_diagnostic_browser_disconnected(context),
                    )
            except Exception:
                pass
        self._attach_page_diagnostics(context, page)

    def _on_diagnostic_new_page(self, context, page):
        page_id = self._diagnostic_page_id(context, page)
        self._record_network_event(
            context, event_type="NEW PAGE DETECTED", page_id=page_id,
            url=self._safe_diagnostic_url(self._safe_page_url(page)),
            decision="observed",
        )
        self._attach_page_diagnostics(context, page)

    def _on_diagnostic_browser_disconnected(self, context):
        active = self._active_export_diagnostic
        if active and active.get("context") is context:
            active["browser_disconnected"] = True
            if active.get("test_c_observing"):
                self._record_test_c_timeline(context, "BROWSER DISCONNECTED")
            if not active.get("download_event_seen"):
                self._record_network_event(
                    context,
                    event_type="browser-crash-before-download-event",
                    note="Browser crashed before Playwright download event.",
                    decision="observed",
                )
        self._record_diagnostic_event(context, "browser-disconnected")

    def _on_diagnostic_page_crashed(self, context, page):
        active = self._active_export_diagnostic
        if active and active.get("context") is context:
            active["page_crashed"] = True
            if active.get("test_c_observing"):
                self._record_test_c_timeline(
                    context, "PAGE CRASH", page_id=self._diagnostic_page_id(context, page)
                )
            if not active.get("download_event_seen"):
                self._record_network_event(
                    context,
                    event_type="browser-crash-before-download-event",
                    note="Browser crashed before Playwright download event.",
                    page_id=self._diagnostic_page_id(context, page),
                    decision="observed",
                )
        self._record_diagnostic_event(context, "page-crash", url=self._safe_page_url(page))

    def _on_diagnostic_page_closed(self, context, page):
        active = self._active_export_diagnostic
        if active and active.get("context") is context and active.get("test_c_observing"):
            active["page_closed"] = True
            self._record_test_c_timeline(
                context, "PAGE CLOSE", page_id=self._diagnostic_page_id(context, page)
            )
        self._record_diagnostic_event(context, "page-close", url=self._safe_page_url(page))

    def _on_diagnostic_context_closed(self, context):
        active = self._active_export_diagnostic
        if active and active.get("context") is context:
            active["context_closed"] = True
            if active.get("test_c_observing"):
                self._record_test_c_timeline(context, "CONTEXT CLOSE")
            if not active.get("download_event_seen"):
                self._record_network_event(
                    context,
                    event_type="browser-crash-before-download-event",
                    note="Browser crashed before Playwright download event.",
                    decision="observed",
                )
        self._record_diagnostic_event(context, "context-close")

    def _record_test_c_timeline(self, context, event_type, **details):
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context:
            return
        self._record_network_event(
            context,
            event_type=event_type,
            decision="observed",
            **details,
        )
        self._record_diagnostic_event(context, event_type, **details)

    def _record_network_event(self, context, **event):
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context or not active.get("network_armed"):
            return
        event.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        active.setdefault("network_events", []).append(event)
        evidence = active.get("test_d_evidence_dir")
        if evidence:
            with (evidence / "export-network-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def _record_network_request(self, context, request):
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context or not active.get("network_armed"):
            return
        service_worker_request = False
        try:
            service_worker_request = request.service_worker is not None
        except Exception:
            pass
        page = None
        try:
            frame = request.frame
            page = frame.page if frame else None
        except Exception:
            page = None
        resource_type = getattr(request, "resource_type", "")
        if resource_type not in ("document", "fetch", "xhr", "other"):
            return
        self._record_network_event(
            context,
            event_type=("service-worker-request" if service_worker_request else "request-observed"),
            method=getattr(request, "method", ""),
            resource_type=resource_type,
            url=self._safe_diagnostic_url(getattr(request, "url", "")),
            page_id=self._diagnostic_page_id(context, page) if page else "unknown",
            navigation=bool(getattr(request, "is_navigation_request", lambda: False)()),
            service_worker_controlled=service_worker_request,
            decision="observed",
        )

    def _record_network_response(self, context, response):
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context or not active.get("network_armed"):
            return
        request = response.request
        service_worker_request = False
        try:
            service_worker_request = request.service_worker is not None
        except Exception:
            pass
        try:
            from_service_worker = bool(response.from_service_worker)
        except Exception:
            from_service_worker = False
        resource_type = getattr(request, "resource_type", "")
        if resource_type not in ("document", "fetch", "xhr", "other"):
            return
        self._record_network_event(
            context,
            event_type=("service-worker-response" if from_service_worker else "response-observed"),
            method=getattr(request, "method", ""),
            resource_type=resource_type,
            url=self._safe_diagnostic_url(response.url),
            status=response.status,
            service_worker_controlled=service_worker_request,
            from_service_worker=from_service_worker,
            decision="observed",
        )

    @staticmethod
    def _find_main_browser_process(snapshot, profile_path):
        """Find the browser root PID by its dedicated ATS-TXL profile, not process name alone."""
        try:
            processes = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
            if isinstance(processes, dict):
                processes = [processes]
            profile = os.path.normcase(str(profile_path or "").replace("/", "\\")).rstrip("\\")
            if not profile or not isinstance(processes, list):
                return None
            for process in processes:
                if not isinstance(process, dict):
                    continue
                command_line = str(process.get("CommandLine") or "").replace("/", "\\")
                if profile not in os.path.normcase(command_line):
                    continue
                if re.search(r"(?:^|\s)--type=", command_line, re.IGNORECASE):
                    continue
                return {
                    "pid": int(process["ProcessId"]),
                    "process_name": str(process.get("Name") or ""),
                    "created_at": str(process.get("CreationDate") or ""),
                }
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        return None

    @staticmethod
    def _open_main_process_watch(pid):
        """Hold a Windows process handle so exit and exit code are checked for the exact root PID."""
        if sys.platform != "win32" or not pid:
            return None
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle_type = ctypes.c_void_p
            dword_type = ctypes.c_ulong
            kernel32.OpenProcess.argtypes = [dword_type, ctypes.c_int, dword_type]
            kernel32.OpenProcess.restype = handle_type
            kernel32.WaitForSingleObject.argtypes = [handle_type, dword_type]
            kernel32.WaitForSingleObject.restype = dword_type
            kernel32.GetExitCodeProcess.argtypes = [handle_type, ctypes.POINTER(dword_type)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [handle_type]
            kernel32.CloseHandle.restype = ctypes.c_int
            access = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            handle = kernel32.OpenProcess(access, False, int(pid))
            if not handle:
                return None
            return {"kernel32": kernel32, "handle": handle, "pid": int(pid)}
        except Exception:
            return None

    @staticmethod
    def _poll_main_process_watch(watch):
        if not watch:
            return None
        try:
            import ctypes

            kernel32 = watch["kernel32"]
            result = int(kernel32.WaitForSingleObject(watch["handle"], 0))
            if result == 0x00000102:  # WAIT_TIMEOUT: process is still running.
                return {"alive": True, "exit_code": None}
            if result != 0:  # WAIT_FAILED or an unexpected wait result.
                return None
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(watch["handle"], ctypes.byref(exit_code)):
                return {"alive": False, "exit_code": None}
            return {"alive": False, "exit_code": int(exit_code.value)}
        except Exception:
            return None

    @staticmethod
    def _close_main_process_watch(watch):
        if not watch:
            return
        try:
            watch["kernel32"].CloseHandle(watch["handle"])
        except Exception:
            pass

    @staticmethod
    def _snapshot_browser_dumps(profile_path):
        """Return metadata only for new local-dump, Crashpad, and Chrome WER artifacts."""
        if sys.platform != "win32":
            return {}
        roots = [
            (Path(r"C:\ATS-TXL-Dumps"), "Windows LocalDumps", False),
            (Path(profile_path) / "Crashpad", "Crashpad", True),
        ]
        found = {}
        for root, source, recursive in roots:
            try:
                if not root.is_dir():
                    continue
                files = root.rglob("*.dmp") if recursive else root.glob("*.dmp")
                for path in files:
                    try:
                        stat = path.stat()
                        found[str(path).casefold()] = {
                            "source": source,
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    except OSError:
                        continue
            except OSError:
                continue
        program_data = os.environ.get("ProgramData")
        if program_data:
            for wer_name in ("ReportArchive", "ReportQueue"):
                wer_root = (
                    Path(program_data) / "Microsoft" / "Windows" / "WER" / wer_name
                )
                try:
                    reports = tuple(wer_root.iterdir())
                except OSError:
                    continue
                for report in reports:
                    if not report.is_dir() or not re.search(
                        r"appcrash.*chrome\.exe", report.name, re.IGNORECASE
                    ):
                        continue
                    try:
                        stat = report.stat()
                        found[str(report).casefold()] = {
                            "source": "Windows WER",
                            "size": 0,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                        for path in report.rglob("*.dmp"):
                            try:
                                stat = path.stat()
                                found[str(path).casefold()] = {
                                    "source": "Windows WER",
                                    "size": stat.st_size,
                                    "mtime_ns": stat.st_mtime_ns,
                                }
                            except OSError:
                                continue
                    except OSError:
                        continue
        return found

    def _observe_test_c(self, page, context, active, duration_seconds=30):
        """Observe the browser after a Blob download without touching Download or closing targets."""
        pid_info = active.get("main_browser_process") or {}
        pid = pid_info.get("pid")
        watch = self._open_main_process_watch(pid)
        profile_path = active.get("profile_path") or ""
        dumps_before = self._snapshot_browser_dumps(profile_path)
        seen_dump_keys = set(dumps_before)
        started = time.monotonic()
        active["test_c_observing"] = True
        self._record_test_c_timeline(
            context,
            "OBSERVATION WINDOW STARTED",
            duration_seconds=duration_seconds,
            main_chrome_pid=pid,
            process_watch_available=bool(watch),
        )
        self.write_log("OBSERVATION WINDOW STARTED")
        if not pid or not watch:
            self._record_test_c_timeline(
                context,
                "MAIN CHROME PID UNAVAILABLE",
                main_chrome_pid=pid,
            )

        process_exit = None
        last_status = None
        try:
            while time.monotonic() - started < duration_seconds:
                sample_started = time.monotonic()
                try:
                    if page.is_closed():
                        raise RuntimeError("page closed")
                    remaining = max(
                        0.001,
                        min(1.0, duration_seconds - (time.monotonic() - started)),
                    )
                    page.wait_for_timeout(max(1, int(remaining * 1000)))
                except Exception:
                    remaining = 1.0 - (time.monotonic() - sample_started)
                    if remaining > 0:
                        time.sleep(remaining)

                last_status = self._poll_main_process_watch(watch)
                elapsed = min(duration_seconds, int(time.monotonic() - started))
                self._record_test_c_timeline(
                    context,
                    "MAIN CHROME PROCESS STATUS",
                    main_chrome_pid=pid,
                    alive=last_status.get("alive") if last_status else None,
                    elapsed_seconds=elapsed,
                )
                if last_status and not last_status["alive"] and process_exit is None:
                    process_exit = last_status
                    active["close_reason"] = "unexpected-browser-exit"
                    exit_time = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    self._record_test_c_timeline(
                        context,
                        "MAIN CHROME PROCESS EXITED",
                        main_chrome_pid=pid,
                        timestamp=exit_time,
                        exit_code=last_status.get("exit_code"),
                    )
                    self.write_log(
                        "MAIN CHROME PROCESS EXITED "
                        f"{exit_time}; exit code={last_status.get('exit_code')}"
                    )

                dumps_now = self._snapshot_browser_dumps(profile_path)
                new_dumps = [
                    (key, item) for key, item in dumps_now.items()
                    if key not in seen_dump_keys or item != dumps_before.get(key)
                ]
                for _, dump in new_dumps:
                    self._record_test_c_timeline(
                        context,
                        "NEW CRASHPAD/WER DUMP DETECTED",
                        source=dump["source"],
                        size=dump["size"],
                    )
                seen_dump_keys.update(key for key, _ in new_dumps)
                dumps_before = dumps_now

            if process_exit:
                result = "TEST C RESULT: Chrome terminated independently before ATS-TXL cleanup."
                active["close_reason"] = "unexpected-browser-exit"
            elif any(
                active.get(key)
                for key in (
                    "page_closed",
                    "page_crashed",
                    "context_closed",
                    "browser_disconnected",
                )
            ):
                result = (
                    "TEST C RESULT: Playwright observed browser/page/context closure "
                    "before ATS-TXL cleanup."
                )
                active["close_reason"] = "unexpected-browser-exit"
            elif watch and last_status and last_status.get("alive"):
                result = (
                    "TEST C RESULT: Chrome remained alive for "
                    f"{int(duration_seconds)} seconds after Blob download."
                )
                active["close_reason"] = "application-cleanup"
            else:
                result = "TEST C RESULT: INCONCLUSIVE; main Chrome PID/exit state could not be verified."
                active["close_reason"] = "application-cleanup"
            self._record_test_c_timeline(context, result)
            self.write_log(result)
            return result
        finally:
            active["test_c_observing"] = False
            self._close_main_process_watch(watch)

    def _begin_export_diagnostic(self, context, page):
        profile_path = str(self._browser_profile_path or "")
        browser_processes_before = self._windows_browser_processes(
            include_command_line=getattr(self, "deep_diagnostic_mode", False)
        )
        process_snapshot_for_pid = browser_processes_before
        if not getattr(self, "deep_diagnostic_mode", False):
            # Command lines are used only in memory to match the dedicated profile;
            # never persist this potentially sensitive snapshot in the bundle.
            process_snapshot_for_pid = self._windows_browser_processes(
                include_command_line=True
            )
        main_browser_process = self._find_main_browser_process(
            process_snapshot_for_pid, profile_path
        )
        self._active_export_diagnostic = {
            "id": uuid.uuid4().hex[:10],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_epoch": time.time(),
            "context": context,
            "backend": self.browser_backend,
            "page_url": self._safe_diagnostic_url(self._safe_page_url(page)),
            "events": [],
            "network_events": [],
            "page_ids": {},
            "network_armed": False,
            "download_event_seen": False,
            "download_event_monotonic": None,
            "browser_disconnected": False,
            "context_closed": False,
            "page_crashed": False,
            "test_c_observing": False,
            "test_c_result": "",
            "close_reason": "",
            "trace_started": False,
            "browser_processes_before": browser_processes_before,
            "main_browser_process": main_browser_process,
            "windows_extended_before": self._windows_extended_diagnostics(),
            "browser_launches": list(self._browser_launches),
            "profile_path": profile_path,
            "native_log_path": str(self._browser_native_log_path or ""),
            "chromium_sandbox": (
                self.chromium_sandbox_enabled.get()
                if getattr(self, "deep_diagnostic_mode", False)
                and getattr(self, "chromium_sandbox_enabled", None) is not None
                else None
            ),
            "process_exit_monitor": dict(self._process_exit_monitor_paths),
        }
        self._attach_context_diagnostics(context, page)
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self._active_export_diagnostic["trace_started"] = True
        except Exception as exc:
            self._record_diagnostic_event(
                context, "trace-start-failed", error_type=type(exc).__name__
            )

    @staticmethod
    def _windows_crash_events():
        if sys.platform != "win32":
            return "Không áp dụng: không phải Windows."
        command = (
            "$since=(Get-Date).AddMinutes(-10);"
            "$events=Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$since} "
            "-ErrorAction SilentlyContinue | Where-Object {"
            "$_.ProviderName -match 'Application Error|Windows Error Reporting' -or "
            "$_.Message -match 'chrome|chromium|msedge|ATS-TXL'"
            "} | Select-Object TimeCreated,ProviderName,Id,LevelDisplayName,Message;"
            "$events | ConvertTo-Json -Depth 3"
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.stdout.strip() or result.stderr.strip() or "Không có sự kiện phù hợp."
        except Exception as exc:
            return f"Không đọc được Windows Event Log: {exc}"

    @staticmethod
    def _windows_browser_processes(include_command_line=False):
        """Snapshot browser processes around an Export.

        Command lines are included only in the dedicated diagnostic build.
        They identify the exact bundled Chromium and profile but are kept in
        the private diagnostic repository only.
        """
        if sys.platform != "win32":
            return "Không áp dụng: không phải Windows."
        fields = "ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate"
        if include_command_line:
            fields += ",CommandLine,WorkingSetSize,HandleCount"
        command = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe' OR Name='msedge.exe'\" "
            "-ErrorAction SilentlyContinue | "
            f"Select-Object {fields} | "
            "ConvertTo-Json -Depth 3"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.stdout.strip() or result.stderr.strip() or "Không có browser process phù hợp."
        except Exception as exc:
            return f"Không đọc được browser process: {exc}"

    @staticmethod
    def _read_file_tail(path, limit=1_000_000):
        """Return the last part of a diagnostic log without exhausting disk/RAM."""
        try:
            path = Path(path)
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - limit))
                return stream.read().decode("utf-8", errors="replace")
        except OSError as exc:
            return f"Không đọc được log Chromium: {exc}"

    def _start_process_exit_monitor(self):
        """Watch Windows process exits while a dedicated test is running.

        Win32_ProcessStopTrace records a process ExitStatus even when Windows
        Error Reporting does not create a dump. Events are later correlated
        with the Chromium PIDs saved at launch.
        """
        if (
            not getattr(self, "deep_diagnostic_mode", False)
            or sys.platform != "win32"
            or self._process_exit_monitor is not None
        ):
            return
        runtime = APP_DATA / DIAGNOSTICS_DIRNAME / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        events_path = runtime / f"process-exit_{stamp}.jsonl"
        script_path = runtime / f"watch-process-exit_{stamp}.ps1"
        script_path.write_text(
            r'''param([string]$OutputPath)
$query = "SELECT * FROM Win32_ProcessStopTrace WHERE ProcessName='chrome.exe' OR ProcessName='chromium.exe' OR ProcessName='msedge.exe'"
$watcher = New-Object System.Management.ManagementEventWatcher $query
try {
  while ($true) {
    $event = $watcher.WaitForNextEvent()
    [pscustomobject]@{
      collected_at = [DateTime]::UtcNow.ToString('o')
      process_id = [int]$event.ProcessID
      parent_process_id = [int]$event.ParentProcessID
      process_name = [string]$event.ProcessName
      exit_status = [uint32]$event.ExitStatus
      session_id = [uint32]$event.SessionID
      event_time_raw = [string]$event.TIME_CREATED
    } | ConvertTo-Json -Compress | Add-Content -LiteralPath $OutputPath -Encoding utf8
  }
} finally {
  if ($watcher) { $watcher.Stop(); $watcher.Dispose() }
}
''',
            encoding="utf-8",
        )
        try:
            process = subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                    "-OutputPath", str(events_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._process_exit_monitor = process
            self._process_exit_monitor_paths = {
                "events": str(events_path),
                "script": str(script_path),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "pid": process.pid,
            }
        except Exception as exc:
            self._process_exit_monitor_paths = {"error": str(exc)}

    def _stop_process_exit_monitor(self):
        process = self._process_exit_monitor
        self._process_exit_monitor = None
        if not process:
            return
        try:
            if process.poll() is None:
                process.terminate()
            _, stderr = process.communicate(timeout=5)
            if stderr:
                self._process_exit_monitor_paths["stderr"] = stderr[-4000:]
        except Exception as exc:
            self._process_exit_monitor_paths["stop_error"] = str(exc)

    @staticmethod
    def _windows_extended_diagnostics():
        """Collect Windows evidence that may explain an unexpected browser exit."""
        if sys.platform != "win32":
            return {"platform": "non-Windows", "note": "Không áp dụng."}
        command = r'''
$since=(Get-Date).AddMinutes(-15)
$terms='chrome|chromium|playwright|ATS-TXL|Application Error|Windows Error Reporting|WerFault|Defender|AppLocker|blocked|terminated|crash'
$logs=@('Application','System','Security','Microsoft-Windows-WER-Diag/Operational','Microsoft-Windows-Windows Defender/Operational','Microsoft-Windows-AppLocker/EXE and DLL')
$events=@()
foreach($log in $logs) {
  try { $events += @(Get-WinEvent -FilterHashtable @{LogName=$log;StartTime=$since} -MaxEvents 300 -ErrorAction Stop | Where-Object { $_.ProviderName -match $terms -or $_.Message -match $terms } | Select-Object TimeCreated,LogName,ProviderName,Id,LevelDisplayName,Message) } catch { }
}
$reliability=@()
try { $reliability=@(Get-CimInstance Win32_ReliabilityRecords -ErrorAction Stop | Where-Object { $_.TimeGenerated -and ([Management.ManagementDateTimeConverter]::ToDateTime($_.TimeGenerated) -ge $since) -and ($_.ProductName -match $terms -or $_.Message -match $terms) } | Select-Object TimeGenerated,SourceName,ProductName,EventIdentifier,Message) } catch { }
$wer=@()
foreach($root in @("$env:ProgramData\Microsoft\Windows\WER\ReportArchive","$env:ProgramData\Microsoft\Windows\WER\ReportQueue")) { if(Test-Path $root) { $wer += @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $since -and $_.Name -match $terms } | Select-Object FullName,Name,LastWriteTime) } }
[pscustomobject]@{collected_at=(Get-Date).ToString('o'); since=$since; events=$events; reliability=$reliability; wer_reports=$wer} | ConvertTo-Json -Depth 6
'''
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=45, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output = result.stdout.strip()
            if not output:
                return {"error": result.stderr.strip() or "Không có dữ liệu."}
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return {"error": "Windows diagnostics không trả về JSON hợp lệ.", "raw_tail": output[-4000:]}
        except Exception as exc:
            return {"error": f"Không đọc được chẩn đoán Windows mở rộng: {exc}"}

    def _queue_diagnostic_upload(self, folder):
        config = self._diagnostics_upload_config
        if not config:
            return
        threading.Thread(
            target=self._diagnostic_upload_worker,
            args=(Path(folder), dict(config)),
            daemon=True,
        ).start()

    def _diagnostic_upload_worker(self, folder, config):
        try:
            receipt = diagnostics_upload.upload_folder(folder, **config)
            self.write_log(
                f"Đã gửi gói chẩn đoán GitHub private: {receipt['remote_path']}"
            )
        except Exception as exc:
            try:
                (folder / "github-upload-error.txt").write_text(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} {exc}\n", encoding="utf-8"
                )
            except OSError:
                pass
            self.write_log(f"Chưa gửi được gói chẩn đoán GitHub: {exc}")

    def _retry_pending_diagnostics(self):
        config = self._diagnostics_upload_config
        root = APP_DATA / DIAGNOSTICS_DIRNAME
        if not config or not root.is_dir():
            return
        pending = [
            folder for folder in sorted(root.iterdir())
            if folder.is_dir() and (folder / "summary.json").is_file()
            and not (folder / "github-upload.json").is_file()
        ][:3]
        for folder in pending:
            self._queue_diagnostic_upload(folder)

    def _finish_export_diagnostic(self, context, page, exc=None):
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context:
            return None
        self._active_export_diagnostic = None

        if exc is None:
            try:
                if active["trace_started"]:
                    context.tracing.stop()
            except Exception:
                pass
            return None

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        folder = active.get("test_d_evidence_dir") or (
            APP_DATA / DIAGNOSTICS_DIRNAME / f"export_{timestamp}_{active['id']}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        dump_paths = []
        if sys.platform == "win32":
            try:
                dump_root = Path(r"C:\ATS-TXL-Dumps")
                dump_folder = folder / "windows-dumps"
                for dump in dump_root.glob("*.dmp"):
                    if dump.is_file() and dump.stat().st_mtime >= active.get("started_epoch", 0):
                        dump_folder.mkdir(parents=True, exist_ok=True)
                        copied = dump_folder / dump.name
                        shutil.copy2(dump, copied)
                        dump_paths.append(str(copied))
            except Exception as dump_exc:
                (folder / "dump-copy-error.txt").write_text(str(dump_exc), encoding="utf-8")
        crashpad_index = []
        if getattr(self, "deep_diagnostic_mode", False):
            profile_path = active.get("profile_path")
            if profile_path:
                try:
                    source_root = Path(profile_path) / "Crashpad"
                    if source_root.is_dir():
                        destination_root = folder / "crashpad"
                        for source in source_root.rglob("*"):
                            if not source.is_file() or source.stat().st_mtime < active["started_epoch"] - 60:
                                continue
                            relative = source.relative_to(source_root)
                            crashpad_index.append(
                                {
                                    "source": str(source),
                                    "relative_path": str(relative),
                                    "size": source.stat().st_size,
                                    "modified_at": time.strftime(
                                        "%Y-%m-%d %H:%M:%S", time.localtime(source.stat().st_mtime)
                                    ),
                                }
                            )
                            # Crashpad dumps can contain OneBSS data. Keep a
                            # local copy for investigation but never auto-upload it.
                            target = destination_root / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target)
                except Exception as crashpad_exc:
                    (folder / "crashpad-copy-error.txt").write_text(
                        str(crashpad_exc), encoding="utf-8"
                    )
        screenshot_path = folder / "onebss-error.png"
        screenshot_error = ""
        try:
            if not page.is_closed():
                page.screenshot(path=str(screenshot_path), full_page=True, timeout=10000)
        except Exception as image_exc:
            screenshot_error = str(image_exc)
        trace_path = folder / "playwright-trace.zip"
        trace_error = ""
        if active["trace_started"]:
            try:
                context.tracing.stop(path=str(trace_path))
            except Exception as trace_exc:
                trace_error = str(trace_exc)

        summary = {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
            "stage": self.current_stage,
            "browser_backend": active["backend"],
            "export_started_at": active["started_at"],
            "page_url_before_export": active["page_url"],
            "page_url_after_error": self._safe_diagnostic_url(self._safe_page_url(page)),
            "trace_path": str(trace_path) if trace_path.exists() else "",
            "trace_error": trace_error,
            "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
            "screenshot_error": screenshot_error,
            "windows_dump_count": len(dump_paths),
            "windows_dumps": dump_paths,
            "test_c_result": active.get("test_c_result", ""),
            "test_d": active.get("test_d", {}),
            "main_browser_process": active.get("main_browser_process"),
            "close_reason": active.get("close_reason", ""),
            "deep_diagnostic_mode": getattr(self, "deep_diagnostic_mode", False),
            "chromium_sandbox_enabled": active.get("chromium_sandbox"),
            "crashpad_file_count": len(crashpad_index),
        }
        (folder / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "browser-events.json").write_text(
            json.dumps(active["events"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not active.get("test_d_evidence_dir"):
            with (folder / "export-network-events.jsonl").open("w", encoding="utf-8") as stream:
                for event in active.get("network_events", []):
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        (folder / "windows-events.json").write_text(
            self._windows_crash_events(), encoding="utf-8"
        )
        (folder / "browser-processes.json").write_text(
            json.dumps(
                {
                    "before_export": active.get("browser_processes_before", ""),
                    "after_error": self._windows_browser_processes(
                        include_command_line=getattr(self, "deep_diagnostic_mode", False)
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if getattr(self, "deep_diagnostic_mode", False):
            (folder / "browser-launches.json").write_text(
                json.dumps(active.get("browser_launches", []), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (folder / "crashpad-report-index.json").write_text(
                json.dumps(crashpad_index, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            monitor = dict(self._process_exit_monitor_paths)
            events_path = Path(monitor.get("events", "")) if monitor.get("events") else None
            if events_path and events_path.is_file():
                shutil.copy2(events_path, folder / "browser-exit-events.jsonl")
            (folder / "process-exit-monitor.json").write_text(
                json.dumps(monitor, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            native_path = active.get("native_log_path")
            if native_path and Path(native_path).is_file():
                (folder / "chromium-native.log").write_text(
                    self._read_file_tail(native_path), encoding="utf-8"
                )
        (folder / "windows-extended-diagnostics.json").write_text(
            json.dumps(
                {
                    "before_export": active.get("windows_extended_before", {}),
                    "after_error": self._windows_extended_diagnostics(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._last_diagnostic_path = folder
        self.write_log(f"Đã lưu gói chẩn đoán Export: {folder}")
        self._queue_diagnostic_upload(folder)
        return folder

    def _session_workflow(self):
        try:
            self.current_stage = "Khởi tạo Playwright và Chromium"
            if sync_playwright is None:
                raise RuntimeError("Thiếu Playwright. Hãy cài requirements.txt trước.")
            DOWNLOADS.mkdir(exist_ok=True)
            with sync_playwright() as p:
                self._start_process_exit_monitor()
                selected_browser = self.browser_choice.get()
                self.write_log(f"Đang mở OneBSS bằng {selected_browser}...")
                ctx = self._launch_browser_context(p)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                self.browser_context, self.browser_page = ctx, page
                self.current_stage = "Mở OneBSS"
                page.goto(ONEBSS_URL, wait_until="domcontentloaded")
                self.write_log(
                    "Bước 1 hoàn tất: OneBSS đã mở. Hãy tự đăng nhập và nhập OTP; "
                    "chương trình sẽ không tự làm mới hoặc chuyển trang ở bước này."
                )
                self.current_stage = "Chờ người dùng hoàn tất đăng nhập và bấm bước 2"
                while not self.stop_requested:
                    signaled = self.start_event.wait(timeout=5)
                    if self.stop_requested:
                        return
                    expires_at = self._onebss_token_expiry(page)
                    if expires_at:
                        self._set_onebss_token_status(expires_at)
                        self._maybe_warn_onebss_session_expiring(expires_at)
                    if not signaled:
                        continue
                    if not self._onebss_session_expired(page, expires_at or None):
                        break
                    self.write_log(
                        "OneBSS chưa đăng nhập xong. Hãy hoàn tất OTP, rồi bấm lại bước 2; "
                        "trang hiện tại được giữ nguyên."
                    )
                    self.start_event.clear()
                if self.stop_requested:
                    return
                self.current_stage = "Bước 2: kiểm tra phiên đăng nhập OneBSS"
                self._ensure_onebss_session_active(page)
                self._acquire_keep_awake()
                self.current_stage = "Bước 2: mở màn hình và cấu hình bộ lọc OneBSS"
                self._navigate_onebss(page)
                self.write_log("Đã vào trang Kiểm soát tồn báo hỏng CNTT và cấu hình bộ lọc. Bắt đầu chạy.")
                self.after(0, lambda: self.progress.start(10))
                cycle = 1
                while not self.stop_requested:
                    if cycle > 1:
                        self.write_log(f"Bắt đầu chu kỳ tự động lần {cycle}.")
                    try:
                        ctx, page, excel = self._export_excel_with_recovery(
                            p, ctx, page, cycle
                        )
                    except OneBSSSessionExpiredError as exc:
                        # Keep this Playwright session and browser open while
                        # the user logs in and completes OTP in the same tab.
                        ctx = self.browser_context or ctx
                        page = self.browser_page or page
                        self._wait_for_reauthentication(page, exc)
                        continue
                    self.browser_context, self.browser_page = ctx, page
                    self.current_stage = f"Chu kỳ {cycle}: xử lý dữ liệu và gửi Telegram"
                    self._process_and_send(excel)
                    self.write_log("Hoàn tất quy trình.")
                    if not self.auto_repeat:
                        break
                    interval_minutes = self.repeat_seconds // 60
                    self.write_log(
                        f"Đã bật tự động: sẽ chạy lại sau {interval_minutes} phút, không cần bấm thêm nút."
                    )
                    for _ in range(self.repeat_seconds):
                        if self.stop_requested:
                            break
                        time.sleep(1)
                    cycle += 1
                ctx.close()
                self.browser_context, self.browser_page = None, None
        except Exception as exc:
            if self.stop_requested:
                self.write_log("Đã dừng quy trình theo yêu cầu.")
            elif isinstance(exc, DiagnosticTestCompleted):
                self.write_log(str(exc))
            else:
                self.write_log(f"LỖI: {exc}")
                error_text = str(exc)
                if self._last_diagnostic_path:
                    error_text += f"\nGói chẩn đoán: {self._last_diagnostic_path}"
                self._send_workflow_error_alert(error_text)
                self.after(0, lambda error_text=error_text: messagebox.showerror("ATS TXL", error_text))
        finally:
            self._stop_process_exit_monitor()
            self.current_stage = "Đã dừng"
            self.after(0, self.progress.stop)
            self.after(0, self._release_keep_awake)

    @staticmethod
    def _onebss_token_expiry(page):
        """Read only the JWT expiry claim; never return the token itself."""
        try:
            expires_at = page.evaluate("""() => {
                try {
                    const stored = JSON.parse(localStorage.getItem('OneBSS-Token') || 'null');
                    const token = stored && stored.access_token;
                    if (!token || typeof token !== 'string') return 0;
                    const payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
                    const claims = JSON.parse(atob(payload));
                    return typeof claims.exp === 'number' ? claims.exp : 0;
                } catch (_) { return 0; }
            }""")
            return float(expires_at or 0)
        except Exception:
            return 0

    def _set_onebss_token_status(self, expires_at):
        self._onebss_token_expires_at = expires_at or None
        if not expires_at:
            text = "Phiên OneBSS: chưa xác định/chưa đăng nhập"
        else:
            remaining = int(expires_at - time.time())
            if remaining <= 0:
                text = "Phiên OneBSS: đã hết hạn — cần đăng nhập lại"
            else:
                hours, remainder = divmod(remaining, 3600)
                minutes = remainder // 60
                expires_text = time.strftime("%H:%M", time.localtime(expires_at))
                text = f"Phiên OneBSS: còn {hours} giờ {minutes} phút (đến {expires_text})"
        self.after(0, lambda text=text: self.onebss_token_status.set(text))

    def _tick_onebss_token_status(self):
        """Keep the token countdown and expiry alert active between cycles."""
        expires_at = self._onebss_token_expires_at
        if expires_at:
            self._set_onebss_token_status(expires_at)
            worker = getattr(self, "worker", None)
            if worker and worker.is_alive():
                remaining = expires_at - time.time()
                if remaining <= 30:
                    threading.Thread(
                        target=self._send_onebss_session_alert,
                        args=(expires_at,),
                        daemon=True,
                    ).start()
                elif remaining <= ONEBSS_EXPIRY_WARNING_SECONDS:
                    threading.Thread(
                        target=self._send_onebss_session_alert,
                        args=(expires_at,),
                        kwargs={"expiring": True},
                        daemon=True,
                    ).start()
        self.after(60000, self._tick_onebss_token_status)

    def _send_onebss_session_alert(self, expires_at, *, expiring=False):
        key = int(expires_at or 0)
        attribute = "_onebss_expiry_warning_for" if expiring else "_onebss_expired_alert_for"
        if getattr(self, attribute, None) == key:
            return
        setattr(self, attribute, key)
        if not self._error_recipient_ids:
            self.write_log("Chưa cấu hình người nhận cảnh báo phiên OneBSS.")
            return
        remaining = max(0, int((expires_at or time.time()) - time.time()))
        expires_text = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(expires_at or time.time()))
        if expiring:
            title = "⚠️ PHIÊN ONEBSS SẮP HẾT HẠN"
            instruction = (
                f"Phiên sẽ hết hạn sau khoảng {max(1, remaining // 60)} phút. "
                "Hãy chuẩn bị đăng nhập và nhập OTP khi OneBSS yêu cầu."
            )
        else:
            title = "🔐 PHIÊN ONEBSS ĐÃ HẾT HẠN"
            instruction = (
                "ATS TXL đã giữ trình duyệt mở và tạm dừng xuất Excel. "
                "Hãy đăng nhập OneBSS, nhập OTP trong trình duyệt đang mở, rồi bấm bước 2 để tiếp tục."
            )
        message = "\n".join([
            f"<b>{title}</b>",
            f"<i>{time.strftime('%d/%m/%Y %H:%M:%S')}</i>",
            "",
            f"<b>Máy:</b> {html.escape(socket.gethostname())}",
            f"<b>Browser:</b> {html.escape(self.browser_backend)}",
            f"<b>Token hết hạn lúc:</b> {expires_text}",
            "",
            html.escape(instruction),
        ])
        successes, _ = self._send_private_message(message)
        self.write_log(
            f"Cảnh báo phiên OneBSS: gửi thành công {len(successes)}/{len(self._error_recipient_ids)} người nhận."
        )

    def _maybe_warn_onebss_session_expiring(self, expires_at):
        remaining = (expires_at or 0) - time.time()
        if (
            30 < remaining <= ONEBSS_EXPIRY_WARNING_SECONDS
            and self._onebss_expiry_warning_for != int(expires_at or 0)
        ):
            self._send_onebss_session_alert(expires_at, expiring=True)

    def _onebss_session_expired(self, page, expires_at=None):
        if page.is_closed():
            return False
        url = page.url.lower()
        if any(marker in url for marker in ("login", "signin", "auth")):
            return True
        # OneBSS stores its access JWT in localStorage['OneBSS-Token'] and can
        # leave the inventory screen visible after expiry. Return only its exp;
        # never copy the token into Python, logs, or diagnostic uploads.
        if expires_at is None:
            expires_at = ATSApp._onebss_token_expiry(page)
        if expires_at <= time.time() + 30:
            return True
        try:
            password = page.locator('input[type="password"]').first
            return password.count() > 0 and password.is_visible(timeout=300)
        except Exception:
            return False

    def _ensure_onebss_session_active(self, page):
        if page.is_closed():
            raise RuntimeError(
                "Trình duyệt OneBSS đã bị đóng. Hãy mở lại ứng dụng để tiếp tục."
            )
        expires_at = self._onebss_token_expiry(page)
        self._set_onebss_token_status(expires_at)
        self._maybe_warn_onebss_session_expiring(expires_at)
        if self._onebss_session_expired(page, expires_at):
            raise OneBSSSessionExpiredError(
                "Phiên đăng nhập OneBSS đã hết hạn. Hãy đăng nhập và nhập OTP lại "
                "trong trình duyệt đang mở, sau đó bấm bước 2. Chương trình không tự xuất Excel "
                "khi phiên xác thực không còn hiệu lực."
            )

    def _wait_for_reauthentication(self, page, reason):
        """Pause without closing Playwright until OTP is completed in this tab."""
        self.current_stage = "Chờ đăng nhập lại OneBSS"
        self.start_event.clear()
        self.write_log(
            "Phiên OneBSS đã hết hạn; giữ trình duyệt mở. "
            "Nếu trang vẫn hiện danh sách, hãy đăng xuất OneBSS, đăng nhập/nhập OTP "
            "trong trình duyệt rồi bấm nút 2 để tiếp tục."
        )
        self._set_onebss_token_status(self._onebss_token_expires_at or 0)
        self._send_onebss_session_alert(self._onebss_token_expires_at or time.time())
        while not self.stop_requested:
            if not self.start_event.wait(timeout=1):
                continue
            self.start_event.clear()
            if page.is_closed():
                raise RuntimeError("Trình duyệt đã bị đóng trong lúc chờ đăng nhập lại.")
            try:
                self._ensure_onebss_session_active(page)
                self.current_stage = "Cấu hình lại OneBSS sau đăng nhập"
                self._navigate_onebss(page)
                self._ensure_onebss_session_active(page)
                self.write_log("Đăng nhập lại thành công; tiếp tục chu kỳ đang dở.")
                return
            except OneBSSSessionExpiredError:
                self.write_log("OneBSS vẫn chưa xác thực xong; hãy hoàn tất OTP rồi bấm lại nút 2.")
            except Exception as exc:
                self.write_log(f"Chưa cấu hình lại được OneBSS: {exc}. Hãy bấm lại nút 2 sau khi trang sẵn sàng.")

    def _launch_browser_context(
        self,
        playwright,
        *,
        headless=False,
        profile=PROFILE,
        browser_channel=None,
        use_configured_channel=True,
    ):
        """Use Playwright's version-matched Chromium unless explicitly overridden."""
        options = {
            "headless": headless,
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 900},
        }
        if getattr(self, "deep_diagnostic_mode", False):
            sandbox_enabled = bool(
                getattr(self, "chromium_sandbox_enabled", None)
                and self.chromium_sandbox_enabled.get()
            )
            # Playwright otherwise adds --no-sandbox to Chromium. This
            # diagnostic-only switch allows a controlled A/B without changing
            # the production browser launch configuration.
            options["chromium_sandbox"] = sandbox_enabled
            self.write_log(
                "Thử nghiệm sandbox Chromium: "
                + ("đã bật (A/B)" if sandbox_enabled else "tắt theo mặc định Playwright")
            )
        if browser_channel is None and use_configured_channel:
            browser_channel = BROWSER_CHOICES.get(self.browser_choice.get(), "chrome")
        if browser_channel:
            options["channel"] = browser_channel
        self.browser_backend = browser_channel or "playwright-chromium"
        # Previous releases opened every browser channel against the same
        # profile. Use fresh, separate Windows directories, retaining the old
        # directory untouched in case the user needs to roll back.
        selected_profile = Path(profile)
        if sys.platform == "win32":
            selected_profile = selected_profile.with_name(
                f"{selected_profile.name}-{self.browser_backend}"
            )
        selected_profile.mkdir(parents=True, exist_ok=True)
        self._browser_profile_path = selected_profile
        self._browser_native_log_path = None
        if getattr(self, "deep_diagnostic_mode", False) and sys.platform == "win32":
            runtime = APP_DATA / DIAGNOSTICS_DIRNAME / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            native_log = runtime / (
                f"chromium_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.log"
            )
            self._browser_native_log_path = native_log
            options["args"] = [
                "--enable-logging",
                "--v=1",
                f"--log-file={native_log}",
            ]
        try:
            context = playwright.chromium.launch_persistent_context(str(selected_profile), **options)
        except Exception as exc:
            if browser_channel:
                raise RuntimeError(
                    f"Không mở được {self.browser_choice.get()}. Hãy kiểm tra browser đã được cài "
                    "hoặc chọn browser khác trong ATS TXL."
                ) from exc
            raise
        if getattr(self, "deep_diagnostic_mode", False):
            self._browser_launches.append(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "backend": self.browser_backend,
                    "chromium_sandbox_enabled": options.get("chromium_sandbox"),
                    "sandbox_experiment": (
                        "enabled"
                        if options.get("chromium_sandbox") is True
                        else "disabled-default-no-sandbox"
                        if "chromium_sandbox" in options
                        else "not-applicable"
                    ),
                    "profile_path": str(selected_profile),
                    "native_log_path": str(self._browser_native_log_path or ""),
                    "processes": self._windows_browser_processes(include_command_line=True),
                }
            )
        return context

    def _navigate_onebss(self, page):
        menu_name = "Kiểm soát viên - Kiểm soát tồn báo hỏng CNTT"
        self.write_log(f'Mở mục Chăm sóc khách hàng → "{menu_name}"...')
        last_error = None
        for attempt in range(1, 4):
            try:
                page.goto(INCIDENT_INVENTORY_URL, wait_until="domcontentloaded", timeout=30000)
                page.locator('select[name="statusId"]').wait_for(state="attached", timeout=30000)
                page.wait_for_timeout(1500)
                self.write_log(f"Đã mở đúng trang {menu_name}.")
                self._configure_filters(page)
                return
            except Exception as exc:
                last_error = exc
                self.write_log(f"Lần cấu hình {attempt}/3 chưa thành công: {exc}")
                if attempt < 3:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
        raise RuntimeError(f"Không cấu hình được trang {menu_name}: {last_error}")

    def _configure_filters(self, page):
        self.write_log("Đang cấu hình ngày và bộ lọc theo ảnh mẫu...")
        self._refresh_cycle_dates(page)

        self._ensure_all_statuses(page)

        # Remove previous/persisted tree selections before applying the exact
        # requested set. Unit tree is first, province tree is second.
        self._clear_tree_selections(page, 0)
        self._clear_tree_selections(page, 1)

        self._expand_tree_parent(page, "Đài HTDV CNTT&DVS", 0)
        for label in ("Phòng HTKH Miền Bắc (VIP 1)", "Phòng HTKH Miền Nam (VIP 2)", "Phòng HTKH Miền Trung (VIP 3)"):
            self._ensure_tree_checked(page, label, 0)
        for label in ("Tập trung", "Miền Bắc", "Miền Trung", "Miền Nam"):
            self._ensure_tree_checked(page, label, 1)
        self.write_log("Đã cấu hình ngày, trạng thái, đơn vị và tỉnh theo ảnh mẫu.")

    def _ensure_all_statuses(self, page):
        status_wrapper = page.locator('select[name="statusId"]').locator("xpath=..").first
        status_wrapper.wait_for(state="attached", timeout=30000)
        if status_wrapper.inner_text().strip().lower().startswith("4 selected"):
            return

        popup = page.locator("#statusId_popup")
        status_input = page.locator('input[placeholder="Chọn trạng thái"]').first
        for attempt in range(3):
            try:
                if attempt % 2 == 0:
                    status_input.click(force=True)
                else:
                    status_wrapper.click(force=True)
                popup.wait_for(state="visible", timeout=3000)
                break
            except Exception:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
        else:
            raise RuntimeError("Không mở được danh sách Trạng thái")

        select_all = popup.locator(".e-selectall-parent").first
        select_all.wait_for(state="visible", timeout=5000)
        frame_class = select_all.locator(".e-frame").get_attribute("class") or ""
        if "e-check" not in frame_class:
            select_all.click(force=True)
            page.wait_for_timeout(500)
        page.keyboard.press("Escape")
        if not status_wrapper.inner_text().strip().lower().startswith("4 selected"):
            raise RuntimeError("Trạng thái chưa chọn đủ 4 mục")

    def _clear_tree_selections(self, page, index):
        tree = page.locator(".vue-treeselect").nth(index)
        tree.wait_for(state="attached", timeout=10000)
        clear = tree.locator(".vue-treeselect__x-container")
        try:
            if clear.count():
                clear.click(force=True)
                page.wait_for_timeout(500)
        except Exception:
            pass

    def _expand_tree_parent(self, page, label, tree_index=0):
        tree = page.locator(".vue-treeselect").nth(tree_index)
        tree_label = tree.locator("label.vue-treeselect__label").filter(has_text=label).first
        row = tree_label.locator("xpath=ancestor::div[contains(@class,'vue-treeselect__option')]").first
        row.wait_for(state="visible", timeout=10000)
        arrow = row.locator(".vue-treeselect__option-arrow-container")
        if arrow.count():
            svg_class = arrow.locator("svg").get_attribute("class") or ""
            if "--rotated" not in svg_class:
                arrow.click(force=True)
                page.wait_for_timeout(300)

    def _ensure_tree_checked(self, page, label, tree_index):
        tree = page.locator(".vue-treeselect").nth(tree_index)
        text = tree.locator("label.vue-treeselect__label").filter(has_text=label).first
        text.wait_for(state="visible", timeout=10000)
        # The fixed OneBSS footer can cover the last item (Miền Nam). Center
        # the item inside its scrollable tree before clicking.
        text.evaluate('(element) => element.scrollIntoView({block: "center"})')
        page.wait_for_timeout(150)
        row = text.locator("xpath=ancestor::div[contains(@class,'vue-treeselect__option')]").first
        checkbox = row.locator(".vue-treeselect__checkbox").first
        classes = checkbox.get_attribute("class") or ""
        if "--checked" not in classes:
            row.locator(".vue-treeselect__label-container").click()
            page.wait_for_timeout(200)
            classes = checkbox.get_attribute("class") or ""
            if "--checked" not in classes:
                checkbox.click(force=True)
                page.wait_for_timeout(300)
        classes = checkbox.get_attribute("class") or ""
        if "--checked" not in classes:
            raise RuntimeError(f'Không chọn được mục "{label}"')

    def _click_page_text(self, page, text):
        candidates = (
            page.get_by_text(text, exact=False).first,
            page.locator("a").filter(has_text=text).first,
            page.locator("button").filter(has_text=text).first,
            page.locator("li").filter(has_text=text).first,
        )
        for item in candidates:
            try:
                item.wait_for(state="visible", timeout=5000)
                item.click(timeout=5000)
                return
            except Exception:
                continue
        # Save a diagnostic screenshot next to the downloaded files.
        try:
            page.screenshot(path=str(DOWNLOADS / "onebss-navigation-error.png"), full_page=True)
        except Exception:
            pass
        raise RuntimeError(f'Không tìm thấy mục OneBSS: "{text}". Hãy kiểm tra đã đăng nhập và trang đã tải xong.')

    def _refresh_cycle_dates(self, page):
        """Apply today's date to OneBSS and keep the desktop UI in sync."""
        today = time.strftime("%d/%m/%Y")
        self.after(0, lambda: (self.from_date.set(today), self.to_date.set(today)))
        self._fill_date(page, today, 0)
        self._fill_date(page, today, 1)
        return today

    def _fill_date(self, page, value, index):
        inputs = page.locator('input.mx-input, input[type="date"], input[placeholder*="ngày"], input[placeholder*="Ngày"]')
        if inputs.count() <= index:
            raise RuntimeError("Không tìm thấy ô ngày trên màn hình OneBSS")
        date_input = inputs.nth(index)
        date_input.fill(value)
        # OneBSS uses a reactive date widget. Blur commits the changed value
        # before the next search, especially when the calendar date rolls over.
        date_input.press("Tab")
        page.wait_for_timeout(150)

    def _check_text(self, page, label):
        loc = page.get_by_text(label, exact=True)
        if loc.count():
            try:
                loc.first.scroll_into_view_if_needed()
                loc.first.click(force=True)
                return
            except Exception:
                pass
        # Tree controls often attach the click handler to the row/container,
        # not to the text node itself.
        for selector in ("label", "li", "tr", "div"):
            try:
                row = page.locator(selector).filter(has_text=label).first
                if row.count() and row.is_visible(timeout=300):
                    row.click(force=True)
                    return
            except Exception:
                continue

    def _export_excel(self, page, context):
        if page.is_closed():
            raise RuntimeError("Trình duyệt đã đóng. Hãy bấm nút 1 để mở lại OneBSS.")
        self._ensure_onebss_session_active(page)
        self._last_diagnostic_path = None
        self._begin_export_diagnostic(context, page)
        target = DOWNLOADS / ("Bao_hong_ton_" + time.strftime("%Y%m%d%H%M%S") + ".xlsx")
        capture = {"path": None, "error": None, "source": None}

        def intercept_export_request(route):
            """Fetch export-triggered responses outside Chrome's download manager."""
            try:
                response = route.fetch(timeout=120000)
                headers = {key.lower(): value for key, value in response.headers.items()}
                content_type = headers.get("content-type", "").lower()
                disposition = headers.get("content-disposition", "").lower()
                request_type = route.request.resource_type
                candidate = (
                    "attachment" in disposition
                    or "spreadsheet" in content_type
                    or "excel" in content_type
                    or "application/zip" in content_type
                    or (
                        request_type in ("xhr", "fetch", "document", "other")
                        and "application/octet-stream" in content_type
                    )
                )
                if not candidate:
                    route.fulfill(response=response)
                    return
                try:
                    response_size = int(headers.get("content-length", "0"))
                except ValueError:
                    response_size = 0
                if response_size > MAX_EXCEL_CAPTURE_BYTES:
                    capture["error"] = "response-too-large"
                    route.abort(error_code="blockedbyclient")
                    return
                body = response.body()
                if self._is_xlsx_payload(body):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    capture.update(path=target, source="api-response")
                    route.abort(error_code="blockedbyclient")
                    return
                if (
                    "attachment" in disposition
                    or "spreadsheet" in content_type
                    or "excel" in content_type
                    or "application/zip" in content_type
                    or "application/octet-stream" in content_type
                ):
                    capture["error"] = "unsupported-attachment"
                    route.abort(error_code="blockedbyclient")
                    return
                route.fulfill(response=response)
            except Exception as exc:
                # Do not retry a request after route.fetch(): it may already
                # have reached OneBSS. Abort rather than risk duplicate work.
                capture["error"] = type(exc).__name__
                try:
                    route.abort(error_code="failed")
                except Exception:
                    pass

        try:
            self.write_log("Bấm Tìm kiếm và chờ tải hết phiếu...")
            page.get_by_text("Tìm kiếm", exact=True).click(timeout=15000)
            self._wait_for_search_complete(page)
            if getattr(self, "deep_diagnostic_mode", False):
                self._run_export_test_d(page, context)
            self.write_log("Bấm Xuất Excel...")
            page.evaluate("""() => {
                window.__atsTxlExportCapture = {armed: true, blobUrl: null};
                if (!window.__atsTxlOriginalAnchorClick) {
                    window.__atsTxlOriginalAnchorClick = HTMLAnchorElement.prototype.click;
                    HTMLAnchorElement.prototype.click = function(...args) {
                        const capture = window.__atsTxlExportCapture;
                        if (capture?.armed && this.href.startsWith('blob:')) {
                            capture.blobUrl = this.href;
                            capture.filename = this.download || '';
                            return;
                        }
                        return window.__atsTxlOriginalAnchorClick.apply(this, args);
                    };
                }
                if (!window.__atsTxlExportCaptureListener) {
                    document.addEventListener('click', event => {
                        if (!window.__atsTxlExportCapture?.armed) return;
                        const anchor = event.target instanceof Element
                            ? event.target.closest('a[href^="blob:"]') : null;
                        if (!anchor) return;
                        event.preventDefault();
                        window.__atsTxlExportCapture.blobUrl = anchor.href;
                        window.__atsTxlExportCapture.filename = anchor.download || '';
                    }, true);
                    window.__atsTxlExportCaptureListener = true;
                }
            }""")
            page.route("**/*", intercept_export_request)
            click_error = None
            try:
                page.get_by_text("Xuất Excel", exact=True).click(timeout=15000)
            except Exception as exc:
                # The intercepted file response is intentionally aborted. Some
                # OneBSS export buttons surface that as a failed navigation.
                click_error = exc
            finally:
                try:
                    page.unroute("**/*", intercept_export_request)
                except Exception:
                    pass

            if capture["path"] is None and not page.is_closed():
                try:
                    page.wait_for_function(
                        "() => Boolean(window.__atsTxlExportCapture?.blobUrl)",
                        timeout=10000,
                    )
                    blob_result = page.evaluate("""async () => {
                        const capture = window.__atsTxlExportCapture;
                        const blob = await fetch(capture.blobUrl).then(response => response.blob());
                        const bytes = new Uint8Array(await blob.arrayBuffer());
                        if (bytes.length > 100 * 1024 * 1024) {
                            throw new Error('export-file-too-large');
                        }
                        let binary = '';
                        const chunkSize = 0x8000;
                        for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                            binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
                        }
                        URL.revokeObjectURL(capture.blobUrl);
                        capture.armed = false;
                        return {base64: btoa(binary), size: bytes.length};
                    }""")
                    blob_bytes = base64.b64decode(blob_result["base64"], validate=True)
                    if len(blob_bytes) > MAX_EXCEL_CAPTURE_BYTES:
                        raise RuntimeError("Tệp Excel vượt quá giới hạn an toàn 100 MiB.")
                    if not self._is_xlsx_payload(blob_bytes):
                        raise RuntimeError("OneBSS tạo Blob nhưng nội dung không phải tệp XLSX hợp lệ.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob_bytes)
                    capture.update(path=target, source="page-blob")
                except Exception as exc:
                    if capture["error"]:
                        raise RuntimeError(
                            "Không lấy được response Excel trực tiếp từ OneBSS; "
                            "chi tiết kỹ thuật đã ghi trong gói chẩn đoán."
                        ) from exc
                    if click_error is not None:
                        raise click_error
                    raise RuntimeError(
                        "OneBSS không trả response XLSX hoặc Blob Excel; "
                        "ATS TXL không khởi chạy tải xuống qua Chrome."
                    ) from exc
            elif capture["path"] is None:
                raise RuntimeError(
                    "Chrome đã đóng trước khi nhận được response Excel từ OneBSS."
                ) from click_error

            if capture["source"] == "api-response":
                self.write_log("Đã nhận và lưu response XLSX trực tiếp; không chuyển file qua Chrome Download Manager.")
            else:
                self.write_log("Đã lấy Excel từ Blob OneBSS và lưu trực tiếp; không chuyển file qua Chrome Download Manager.")
            self._finish_export_diagnostic(context, page)
            self.write_log(f"Đã lưu Excel: {target}")
            return target
        except Exception as exc:
            self._finish_export_diagnostic(context, page, exc)
            raise

    @staticmethod
    def _validate_test_d_xlsx(path):
        """Validate the streamed file without loading the whole workbook into RAM."""
        with Path(path).open("rb") as stream:
            if stream.read(2) != b"PK":
                raise ValueError("missing-pk-magic")
        with zipfile.ZipFile(path) as workbook:
            entries = set(workbook.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= entries:
                raise ValueError("missing-xlsx-entries")
            if sum(item.file_size for item in workbook.infolist()) > 512 * 1024 * 1024:
                raise ValueError("zip-expanded-too-large")
            if workbook.testzip() is not None:
                raise ValueError("zip-crc-failed")
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            if not workbook.sheetnames:
                raise ValueError("workbook-has-no-sheets")
        finally:
            workbook.close()

    def _transfer_test_d_blob(self, page, metadata):
        size = metadata["blob_size"]
        if size > MAX_EXCEL_CAPTURE_BYTES:
            return {"state": "size-limit", "bytes_transferred": 0}
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        unique = uuid.uuid4().hex[:10]
        target = DOWNLOADS / f"Bao_hong_ton_TestD_{stamp}_{unique}.xlsx"
        temporary = target.with_name(target.stem + ".partial.xlsx")
        transferred = 0
        try:
            with temporary.open("xb") as stream:
                while transferred < size:
                    length = min(TEST_D_CHUNK_BYTES, size - transferred)
                    encoded = page.evaluate(
                        "([offset, length]) => window.__atsTxlTestD.readChunk(offset, length)",
                        [transferred, length],
                    )
                    chunk = base64.b64decode(encoded, validate=True)
                    if len(chunk) != length:
                        raise ValueError("chunk-length-mismatch")
                    stream.write(chunk)
                    transferred += length
            self._validate_test_d_xlsx(temporary)
            # A hard link claims the final name atomically and never overwrites.
            os.link(temporary, target)
            temporary.unlink()
            self.write_log(f"Test D đã lưu XLSX: {target}")
            return {"state": "saved", "bytes_transferred": transferred,
                    "pk": True, "zip": True, "xlsx_structure": True,
                    "openpyxl": True}
        except (ValueError, zipfile.BadZipFile) as exc:
            return {"state": "corrupt", "bytes_transferred": transferred,
                    "validation_error": str(exc)[:60]}
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _test_d_hook_verified(verification, frame_id, armed, observe_only=False):
        return bool(
            isinstance(verification, dict)
            and verification.get("version") == TEST_D_HOOK_VERSION
            and verification.get("frame_id") == frame_id
            and str(verification.get("document_id", "")).startswith("document-")
            and verification.get("origin") == ONEBSS_URL.rstrip("/")
            and verification.get("observe_only") is observe_only
            and verification.get("create_wrapped") is True
            and verification.get("click_wrapped") is True
            and verification.get("listener_installed") is True
            and verification.get("blob_map_exists") is True
            and verification.get("armed") is armed
        )

    def _test_d_js_event(self, context, source, payload):
        """Accept only bounded, allowlisted telemetry; never persist a page-supplied URL."""
        active = self._active_export_diagnostic
        if not active or active.get("context") is not context or not isinstance(payload, dict):
            return
        event_type = payload.get("event_type")
        if event_type not in {
            "CREATE_OBJECT_URL", "REVOKE_OBJECT_URL", "ANCHOR_CLICK",
            "CAPTURE_CLICK_EVENT", "WINDOW_OPEN_BLOB", "CANDIDATE EVALUATED",
        }:
            return
        frame_id = active.get("test_d_frame_ids", {}).get(id(source.get("frame")), "unknown")
        item = next((item for item in active.get("test_d_frame_inventory", [])
                     if item.get("frame_id") == frame_id), {})
        fields = {"frame_id": frame_id, "document_id": item.get("document_id", "")}
        allowed = {
            "path": {"anchor.click", "capture-click-event"},
            "filename_extension": {"", ".xlsx", ".xls", ".csv", ".pdf", ".zip", ".other"},
            "mime_category": {"empty", "octet-stream", "zip", "excel", "other"},
            "reason_code": {"NOT_ARMED", "NOT_BLOB_URL", "BLOB_NOT_IN_MAP", "BLOB_TOO_OLD",
                            "FILENAME_NOT_XLSX", "MIME_REJECTED", "SIZE_INVALID", "WRONG_ORIGIN",
                            "DUPLICATE", "MULTIPLE_CANDIDATES", "EXPORT_WINDOW_EXPIRED",
                            "OBSERVE_ONLY_FRAME", "MATCH", "OTHER"},
        }
        for key, choices in allowed.items():
            if isinstance(payload.get(key), str) and payload[key] in choices:
                fields[key] = payload[key]
        blob_id = payload.get("blob_id")
        if isinstance(blob_id, str) and re.fullmatch(r"blob-\d{1,10}", blob_id):
            fields["blob_id"] = blob_id
        if payload.get("decision") in ("ALLOW", "SUPPRESS"):
            fields["candidate_decision"] = payload["decision"]
        stamp = payload.get("js_timestamp")
        if isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp):
            fields["js_timestamp"] = stamp
        for key in ("armed", "href_blob", "blob_in_map", "filename_valid",
                    "onebss_origin", "duplicate", "export_timing_window"):
            if type(payload.get(key)) is bool:
                fields[key] = payload[key]
        for key in ("blob_size", "blob_age_ms"):
            value = payload.get(key)
            if type(value) is int and 0 <= value <= 2**40:
                fields[key] = value
        self._record_network_event(context, event_type=event_type, decision="observed", **fields)

    def _test_d_inventory_frames(self, page, context, active):
        frames = list(page.frames)
        ids = {id(frame): f"frame-{index + 1}" for index, frame in enumerate(frames)}
        active["test_d_frame_ids"] = ids
        inventory = []
        for frame in frames:
            frame_id = ids[id(frame)]
            parent = frame.parent_frame
            url = frame.url
            parts = urlsplit(url)
            frame_url = (
                f"{parts.scheme}://{parts.hostname}/[PATH_REDACTED]"
                if parts.scheme in ("http", "https") and parts.hostname
                else f"{parts.scheme}:[REDACTED]"
            )
            item = {
                "frame_id": frame_id,
                "parent_frame_id": ids.get(id(parent), "") if parent else "",
                "main_frame": frame is page.main_frame,
                "origin": "",
                "url": frame_url,
                "attached": not frame.is_detached(),
                "instrumented": False,
            }
            try:
                item["origin"] = frame.evaluate("() => location.origin")
            except Exception:
                item["not_instrumented_reason"] = "ORIGIN_UNREADABLE"
            inventory.append(item)
            self._record_network_event(context, event_type="FRAME INVENTORY",
                                       frame_id=frame_id, main_frame=item["main_frame"],
                                       origin=item["origin"] if item["origin"] == ONEBSS_URL.rstrip("/") else "other",
                                       attached=item["attached"], decision="observed")
        return frames, inventory

    @staticmethod
    def _test_d_persist_snapshot(active):
        folder = active["test_d_evidence_dir"]
        temporary = folder / "summary.pending"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"test_d": active["test_d"]}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / "summary.json")

    @staticmethod
    def _test_d_path(active):
        events = active.get("network_events", [])
        created = [event for event in events if event.get("event_type") == "CREATE_OBJECT_URL"]
        created_ids = {(event.get("frame_id"), event.get("blob_id")) for event in created}
        def matches(event_type):
            return any(event.get("event_type") == event_type
                       and (event.get("frame_id"), event.get("blob_id")) in created_ids
                       for event in events)
        if created and any(event.get("frame_id") != active.get("test_d_main_frame_id") for event in created):
            return "PATH D"
        if matches("ANCHOR_CLICK"):
            return "PATH A"
        if matches("CAPTURE_CLICK_EVENT"):
            return "PATH B"
        if matches("WINDOW_OPEN_BLOB"):
            return "PATH C"
        return "PATH E" if not created else "PATH F"

    @staticmethod
    def _classify_test_d(metadata, transfer, observation, active):
        if not active.get("test_d_hook_verified", True):
            return "TEST D INCONCLUSIVE: Blob interception hook was not installed/verified."
        path = ATSApp._test_d_path(active)
        frame_unobserved = (
            any(not frame.get("instrumented") and frame.get("origin") == ONEBSS_URL.rstrip("/")
                for frame in active.get("test_d_frame_inventory", []))
            or any(event.get("reason_code") in ("ATTACHED_AFTER_SNAPSHOT", "NAVIGATED_AFTER_SNAPSHOT")
                   for event in active.get("network_events", []))
        )
        if active.get("download_event_seen"):
            reasons = [event.get("reason_code") for event in active.get("network_events", [])
                       if event.get("event_type") == "CANDIDATE EVALUATED"
                       and event.get("candidate_decision") == "ALLOW"]
            if path == "PATH E":
                if frame_unobserved:
                    return "TEST D INCONCLUSIVE: Required frame could not be instrumented."
                return "TEST D INCONCLUSIVE: Export path was not observable by installed hooks."
            suffix = f" {path}; reason={reasons[-1] if reasons else 'UNKNOWN'}."
            return "TEST D FAIL: Native download occurred despite verified interception." + suffix
        if (
            active.get("close_reason") == "unexpected-browser-exit"
            or active.get("page_crashed") or active.get("page_closed")
            or active.get("context_closed") or active.get("browser_disconnected")
        ):
            if path == "PATH E":
                return ("TEST D INCONCLUSIVE: Required frame could not be instrumented."
                        if frame_unobserved else
                        "TEST D INCONCLUSIVE: Export path was not observable by installed hooks.")
            return "TEST D FAIL: Chrome or its page/context exited unexpectedly."
        if transfer.get("state") in ("corrupt", "transfer-failed"):
            return "TEST D FAIL: Captured XLSX is corrupt or chunk transfer failed."
        if metadata.get("state") != "captured" or metadata.get("multiple_candidates"):
            return "TEST D INCONCLUSIVE: Excel Blob was not identified unambiguously."
        if transfer.get("state") == "size-limit":
            return "TEST D INCONCLUSIVE: Excel Blob exceeds the 100 MiB safety limit."
        if transfer.get("state") != "saved" or metadata.get("suppressed_count", 0) < 1:
            return "TEST D INCONCLUSIVE: Excel Blob capture or suppression was not verified."
        if (
            metadata.get("blob_size", 0) < 1
            or transfer.get("bytes_transferred") != metadata["blob_size"]
            or not all(transfer.get(key) is True for key in
                       ("pk", "zip", "xlsx_structure", "openpyxl"))
        ):
            return "TEST D FAIL: Saved XLSX did not pass all byte and workbook checks."
        if observation != "TEST C RESULT: Chrome remained alive for 30 seconds after Blob download.":
            return "TEST D INCONCLUSIVE: Main Chrome PID could not be verified for 30 seconds."
        return (
            "TEST D PASS: Excel Blob captured and saved without Chrome Download Manager. "
            "Chrome remained alive for 30 seconds."
        )

    def _run_export_test_d(self, page, context):
        """Capture one high-confidence XLSX Blob before Chrome receives a download."""
        active = self._active_export_diagnostic
        evidence = APP_DATA / DIAGNOSTICS_DIRNAME / (
            f"export_{time.strftime('%Y%m%d_%H%M%S')}_{active['id']}"
        )
        evidence.mkdir(parents=True, exist_ok=True)
        active["test_d_evidence_dir"] = evidence
        active["network_armed"] = True
        active["test_d_active"] = True
        active["test_d_hook_verified"] = False
        metadata = {"state": "not-installed", "filename_extension": "", "mime_category": "empty",
                    "blob_size": 0, "suppressed_count": 0, "multiple_candidates": False}
        transfer = {"state": "not-started", "bytes_transferred": 0}
        observation = ""
        clicked = False
        observation_started = False
        try:
            self._record_network_event(context, event_type="INSTALL REQUESTED", decision="observed")
            context.expose_binding(
                "__atsTxlTestDEvent",
                lambda source, payload: self._test_d_js_event(context, source, payload),
            )
            frames, inventory = self._test_d_inventory_frames(page, context, active)
            active["test_d_frame_inventory"] = inventory
            main_frame = page.main_frame
            main_id = active["test_d_frame_ids"].get(id(main_frame), "")
            active["test_d_main_frame_id"] = main_id
            try:
                owner_count = page.get_by_text("Xuất Excel", exact=True).count()
            except Exception:
                owner_count = 0
            active["test_d_button_owner"] = (
                {"frame_id": main_id, "locator_count": owner_count}
                if owner_count else {"frame_id": "unresolved", "locator_count": 0}
            )
            for frame, item in zip(frames, inventory):
                frame_id = item["frame_id"]
                if item["origin"] != ONEBSS_URL.rstrip("/") or not item["attached"]:
                    item["not_instrumented_reason"] = (
                        "CROSS_ORIGIN_OR_OPAQUE" if item["origin"] != ONEBSS_URL.rstrip("/")
                        else "DETACHED"
                    )
                    self._record_network_event(context, event_type="FRAME NOT INSTRUMENTED",
                                               frame_id=frame_id,
                                               reason_code=item["not_instrumented_reason"],
                                               decision="observed")
                    continue
                try:
                    if not frame.evaluate(TEST_D_HOOK_SCRIPT,
                                          {"frameId": frame_id, "observeOnly": frame is not main_frame}):
                        raise RuntimeError("hook-install-returned-false")
                    item["instrumented"] = True
                    self._record_network_event(context, event_type="HOOK INSTALLED",
                                               frame_id=frame_id, decision="observed")
                    check = frame.evaluate("() => window.__atsTxlTestD.verify()")
                    if not self._test_d_hook_verified(check, frame_id, False,
                                                       frame is not main_frame):
                        raise RuntimeError("hook-verification-failed")
                    self._record_network_event(context, event_type="HOOK VERIFIED",
                                               frame_id=frame_id, decision="observed")
                    if not frame.evaluate("() => window.__atsTxlTestD.arm()"):
                        raise RuntimeError("hook-arm-failed")
                    check = frame.evaluate("() => window.__atsTxlTestD.verify()")
                    if not self._test_d_hook_verified(check, frame_id, True,
                                                       frame is not main_frame):
                        raise RuntimeError("armed-verification-failed")
                    item["document_id"] = check["document_id"]
                    self._record_network_event(context, event_type="TEST D ARMED",
                                               frame_id=frame_id, decision="observed")
                except Exception as exc:
                    item["instrumented"] = False
                    item["not_instrumented_reason"] = type(exc).__name__
                    self._record_network_event(context, event_type="FRAME NOT INSTRUMENTED",
                                               frame_id=frame_id, reason_code=type(exc).__name__,
                                               decision="observed")
            main_item = next((item for item in inventory if item["main_frame"]), {})
            active["test_d_hook_verified"] = bool(
                main_item.get("instrumented") and main_item.get("document_id")
                and all(item.get("instrumented") for item in inventory
                        if item.get("origin") == ONEBSS_URL.rstrip("/"))
            )
            if active["test_d_hook_verified"]:
                metadata = main_frame.evaluate("() => window.__atsTxlTestD.metadata()")
                if metadata.get("state") != "armed":
                    active["test_d_hook_verified"] = False
            if active["test_d_hook_verified"]:
                for frame, item in zip(frames, inventory):
                    if item.get("instrumented"):
                        if not frame.evaluate("() => window.__atsTxlTestD.markExportClick()"):
                            active["test_d_hook_verified"] = False
                            break
                if active["test_d_hook_verified"]:
                    # A final main-document handshake catches navigation/replacement
                    # between installation and the click.
                    check = main_frame.evaluate("() => window.__atsTxlTestD.verify()")
                    active["test_d_hook_verified"] = self._test_d_hook_verified(check, main_id, True)
            active["test_d"] = {
                "pre_click_snapshot": {
                    "hook_verified": active["test_d_hook_verified"],
                    "main_frame_id": main_id,
                    "document_id": main_item.get("document_id", ""),
                    "armed": metadata.get("state") == "armed",
                    "frame_inventory": inventory,
                    "button_owner": active["test_d_button_owner"],
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            }
            self._test_d_persist_snapshot(active)
            self._record_network_event(context, event_type="PRE-CLICK SNAPSHOT PERSISTED",
                                       frame_id=main_id, decision="observed")
            if active["test_d_hook_verified"]:
                page.on("frameattached", lambda frame: self._record_network_event(
                    context, event_type="FRAME NOT INSTRUMENTED",
                    frame_id="dynamic", reason_code="ATTACHED_AFTER_SNAPSHOT", decision="observed",
                ))
                page.on("framenavigated", lambda frame: self._record_network_event(
                    context, event_type="FRAME NOT INSTRUMENTED",
                    frame_id=active["test_d_frame_ids"].get(id(frame), "dynamic"),
                    reason_code="NAVIGATED_AFTER_SNAPSHOT", decision="observed",
                ))
                self._record_network_event(context, event_type="CLICK EXPORT", frame_id=main_id,
                                           decision="observed")
                self.write_log("Bấm Xuất Excel... (Test D: chặn Blob trước Chrome Download Manager)")
                clicked = True
                try:
                    page.get_by_text("Xuất Excel", exact=True).click(timeout=15000)
                except Exception as exc:
                    self._record_network_event(
                        context, event_type="test-d-export-click-error",
                        error_type=type(exc).__name__, decision="observed",
                    )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if active.get("download_event_seen") or page.is_closed() or active.get("browser_disconnected"):
                        break
                    metadata = main_frame.evaluate("() => window.__atsTxlTestD.metadata()")
                    if metadata["state"] == "captured":
                        break
                    page.wait_for_timeout(100)
                if metadata["state"] == "captured" and not active.get("download_event_seen"):
                    try:
                        transfer = self._transfer_test_d_blob(page, metadata)
                    except Exception as exc:
                        transfer = {"state": "transfer-failed", "bytes_transferred": 0,
                                    "error_type": type(exc).__name__}
                observation_started = True
                observation = self._observe_test_c(page, context, active, duration_seconds=30)
                if not page.is_closed():
                    try:
                        metadata = main_frame.evaluate("() => window.__atsTxlTestD.metadata()")
                    except Exception:
                        pass
        except Exception as exc:
            self._record_network_event(
                context, event_type="test-d-instrumentation-error",
                error_type=type(exc).__name__, decision="observed",
            )
            if clicked and not observation_started:
                try:
                    observation = self._observe_test_c(page, context, active, duration_seconds=30)
                except Exception:
                    observation = "TEST C RESULT: INCONCLUSIVE; process observation failed."
            elif not clicked:
                metadata["state"] = "instrumentation-failed"
        finally:
            try:
                if page.is_closed():
                    active["page_closed"] = True
            except Exception:
                pass
            active["test_d"] = {
                **active.get("test_d", {}),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                **metadata,
                "hook_verified": active.get("test_d_hook_verified", False),
                "export_path": self._test_d_path(active) if clicked else "not-clicked",
                "chunk_size": TEST_D_CHUNK_BYTES,
                "bytes_transferred": transfer.get("bytes_transferred", 0),
                "validation": transfer.get("state", "not-started"),
                "validation_results": {key: transfer.get(key, False) for key in
                    ("pk", "zip", "xlsx_structure", "openpyxl")},
                "download_event_seen": bool(active.get("download_event_seen")),
                "main_pid_state": (
                    "alive" if "remained alive for 30 seconds" in observation
                    else "exited" if "terminated independently" in observation
                    else "unverified"
                ),
                "observation_result": observation,
            }
            result = self._classify_test_d(metadata, transfer, observation, active)
            self.write_log(result)
            self.write_log("DIAGNOSTIC CLEANUP STARTED")
            self._record_test_c_timeline(context, "DIAGNOSTIC CLEANUP STARTED")
            try:
                if not page.is_closed():
                    page.evaluate("() => window.__atsTxlTestD?.cleanup()")
            except Exception:
                pass
            self.write_log("ATS-TXL is now intentionally closing the browser.")
            try:
                context.close()
            except Exception as exc:
                self._record_test_c_timeline(
                    context, "DIAGNOSTIC CLEANUP ERROR", error_type=type(exc).__name__
                )
            self.write_log("DIAGNOSTIC CLEANUP FINISHED")
            self._record_test_c_timeline(context, "DIAGNOSTIC CLEANUP FINISHED")
            active["network_armed"] = False
            active["test_d_active"] = False
            self._finish_export_diagnostic(context, page, RuntimeError(result))
        raise DiagnosticTestCompleted(result)

    def _run_export_test_a(self, page, context):
        """Observe a browser-context export request and block only a clearly named export route."""
        active = self._active_export_diagnostic
        state = {"export_aborted": False, "route_error": None}

        def intercept_test_request(route):
            request = route.request
            explicit_export_candidate = self._is_explicit_export_candidate(request)
            resource_type = getattr(request, "resource_type", "")
            request_method = getattr(request, "method", "")
            safe_url = self._safe_diagnostic_url(getattr(request, "url", ""))
            page_id = "unknown"
            try:
                request_page = request.frame.page
                page_id = self._diagnostic_page_id(context, request_page)
            except Exception:
                pass
            if explicit_export_candidate:
                export_event = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "event_type": "HIGH-CONFIDENCE EXPORT CANDIDATE",
                    "method": request_method,
                    "resource_type": resource_type,
                    "url": safe_url,
                    "page_id": page_id,
                    "navigation": bool(request.is_navigation_request()),
                    "decision": "abort-attempted",
                }
                active.setdefault("network_events", []).append(export_event)
                self._record_network_event(
                    context, event_type="export-interception-attempted",
                    method=request_method, resource_type=resource_type,
                    url=safe_url, page_id=page_id,
                    navigation=bool(request.is_navigation_request()),
                    decision="abort-attempted",
                )
                try:
                    route.abort(error_code="blockedbyclient")
                    state["export_aborted"] = True
                    export_event["decision"] = "aborted"
                except Exception as exc:
                    state["route_error"] = type(exc).__name__
                    export_event["decision"] = "abort-failed"
                return

            if resource_type in ("document", "fetch", "xhr", "other"):
                self._record_network_event(
                    context,
                event_type="request-candidate",
                    method=request_method,
                    resource_type=resource_type,
                    url=safe_url,
                    page_id=page_id,
                    navigation=bool(request.is_navigation_request()),
                    decision="candidate",
                )
            try:
                route.continue_()
            except Exception as exc:
                state["route_error"] = type(exc).__name__

        active["network_armed"] = True
        context.route("**/*", intercept_test_request)
        self.write_log("Bấm Xuất Excel... (Test A: theo dõi route ở cấp BrowserContext)")

        def pump_events():
            try:
                page.wait_for_timeout(100)
            except Exception:
                # A crashed renderer/browser is itself evidence; the registered
                # page/context listeners have already recorded its event.
                pass

        click_error = None
        try:
            page.get_by_text("Xuất Excel", exact=True).click(timeout=15000)
        except Exception as exc:
            click_error = exc

        # Let the export action reveal its actual network shape. If no request
        # can be safely classified, do not abort a guessed endpoint.
        identify_deadline = time.monotonic() + 10
        while (
            time.monotonic() < identify_deadline
            and not state["export_aborted"]
            and not active.get("download_event_seen")
        ):
            if page.is_closed() or active.get("page_crashed") or active.get("context_closed") or active.get("browser_disconnected"):
                break
            pump_events()

        if state["export_aborted"]:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if page.is_closed() or active.get("page_crashed") or active.get("context_closed") or active.get("browser_disconnected"):
                    break
                pump_events()
            if page.is_closed() or active.get("page_crashed") or active.get("context_closed") or active.get("browser_disconnected"):
                result = "TEST A FAIL: Chrome crashed despite export request interception."
            elif active.get("download_event_seen"):
                result = "TEST A INCONCLUSIVE: Could not safely identify the final export request."
            else:
                result = "TEST A PASS: Export request aborted before Chrome Download Manager. Chrome remained alive for 10 seconds."
        else:
            result = "TEST A INCONCLUSIVE: Could not safely identify the final export request."

        if active.get("download_event_seen"):
            try:
                context.unroute("**/*", intercept_test_request)
            except Exception:
                pass
            result = self._observe_test_c(page, context, active, duration_seconds=30)

        try:
            context.unroute("**/*", intercept_test_request)
        except Exception:
            pass
        if state["route_error"]:
            self._record_network_event(
                context, event_type="route-handler-error", decision="candidate"
            )
        if click_error:
            self._record_network_event(
                context, event_type="export-click-error", decision="candidate"
            )
        if not active.get("download_event_seen") and (
            page.is_closed() or active.get("page_crashed") or active.get("context_closed") or active.get("browser_disconnected")
        ):
            self._record_network_event(
                context,
                event_type="browser-crash-before-download-event",
                note="Browser crashed before Playwright download event.",
                decision="observed",
            )

        if not active.get("close_reason"):
            targets_closed = (
                page.is_closed()
                or active.get("page_crashed")
                or active.get("context_closed")
                or active.get("browser_disconnected")
            )
            active["close_reason"] = (
                "unexpected-browser-exit" if targets_closed else "application-cleanup"
            )
        active["test_c_result"] = result if result.startswith("TEST C RESULT:") else ""
        self.write_log("DIAGNOSTIC CLEANUP STARTED")
        self._record_test_c_timeline(context, "DIAGNOSTIC CLEANUP STARTED")
        self.write_log("ATS-TXL is now intentionally closing the browser.")
        self._record_test_c_timeline(
            context,
            "ATS-TXL is now intentionally closing the browser.",
            close_reason=active["close_reason"],
        )
        try:
            context.close()
        except Exception as exc:
            self._record_test_c_timeline(
                context, "DIAGNOSTIC CLEANUP ERROR", error_type=type(exc).__name__
            )
        self.write_log("DIAGNOSTIC CLEANUP FINISHED")
        self._record_test_c_timeline(context, "DIAGNOSTIC CLEANUP FINISHED")
        active["network_armed"] = False
        self._finish_export_diagnostic(context, page, RuntimeError(result))
        raise DiagnosticTestCompleted(result)

    @staticmethod
    def _is_xlsx_payload(body):
        """Recognize an OOXML workbook without logging or persisting response metadata."""
        if not body or not body.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as workbook:
                names = set(workbook.namelist())
            return "[Content_Types].xml" in names and "xl/workbook.xml" in names
        except (OSError, zipfile.BadZipFile):
            return False

    @staticmethod
    def _is_excel_download_timeout(exc):
        return 'event "download"' in str(exc).lower()

    def _recover_onebss_after_download_timeout(self, page, cycle, recovery_attempt):
        self.current_stage = f"Chu kỳ {cycle}: phục hồi OneBSS sau lỗi xuất Excel"
        self.write_log(
            "OneBSS không tạo file Excel sau 2 phút. "
            f"Đang làm mới trang và cấu hình lại (lần {recovery_attempt}/"
            f"{MAX_EXCEL_RECOVERY_ATTEMPTS})..."
        )
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        self._ensure_onebss_session_active(page)
        # Navigate again and configure every filter from a clean state. This
        # is deliberately not just a retry of the Export button.
        self._navigate_onebss(page)
        self._ensure_onebss_session_active(page)
        self.write_log("Đã phục hồi OneBSS; chạy lại tìm kiếm và xuất Excel.")

    def _wait_before_search_retry(self, page, seconds):
        """Back off without freezing Stop or ignoring an expiring OneBSS token."""
        deadline = time.monotonic() + seconds
        next_session_check = 0
        while time.monotonic() < deadline:
            if self.stop_requested:
                raise RuntimeError("Đã dừng bởi người dùng")
            if page.is_closed():
                raise RuntimeError("Trình duyệt đã đóng trong lúc chờ thử lại.")
            if time.monotonic() >= next_session_check:
                self._ensure_onebss_session_active(page)
                next_session_check = time.monotonic() + 5
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def _recover_onebss_after_search_timeout(self, page, cycle, attempt):
        """Reload and reconfigure in the same browser until OneBSS is ready."""
        self.current_stage = f"Chu kỳ {cycle}: thử lại sau khi OneBSS tìm kiếm quá lâu"
        while not self.stop_requested:
            delay = min(30 * 2 ** min(attempt - 1, 4), 300)
            self.write_log(
                f"OneBSS chưa hoàn tất tìm kiếm; giữ trình duyệt mở, "
                f"thử làm mới và cấu hình lại sau {delay} giây (lần {attempt})."
            )
            self._wait_before_search_retry(page, delay)
            try:
                self._ensure_onebss_session_active(page)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                self._ensure_onebss_session_active(page)
                self._navigate_onebss(page)
                self._ensure_onebss_session_active(page)
                self.write_log("Đã làm mới OneBSS và cấu hình lại; tìm kiếm từ đầu.")
                return
            except OneBSSSessionExpiredError:
                raise
            except Exception as exc:
                if page.is_closed() or self._is_browser_closed_error(exc):
                    raise
                self.write_log(f"OneBSS chưa sẵn sàng sau khi làm mới: {exc}")
                attempt += 1
        raise RuntimeError("Đã dừng bởi người dùng")

    @staticmethod
    def _is_browser_closed_error(exc):
        return isinstance(exc, TargetClosedError) or (
            "target page, context or browser has been closed" in str(exc).lower()
        )

    def _recover_closed_browser(
        self, playwright, context, cycle, recovery_attempt
    ):
        """Restart only the current browser with its own persistent profile."""
        failed_stage = self.current_stage
        self.current_stage = f"Chu kỳ {cycle}: phục hồi Chromium sau khi bị đóng"
        self.write_log(
            "Browser/OneBSS đã bị đóng trong chu kỳ. "
            f"Đang mở lại và cấu hình lại (lần {recovery_attempt}/"
            f"{MAX_EXCEL_RECOVERY_ATTEMPTS})..."
        )
        diagnostic = getattr(self, "_last_diagnostic_path", None)
        diagnostic_note = f" Gói chẩn đoán: {Path(diagnostic).name}." if diagnostic else ""
        try:
            self._send_workflow_error_alert(
                f"Trình duyệt OneBSS đóng ngoài dự kiến tại {failed_stage}; "
                f"đang tự phục hồi lần {recovery_attempt}/{MAX_EXCEL_RECOVERY_ATTEMPTS}."
                f"{diagnostic_note}"
            )
        except Exception as alert_exc:
            # A Telegram outage must not prevent browser recovery.
            self.write_log(f"Không gửi được cảnh báo browser bị đóng: {alert_exc}")
        try:
            context.close()
        except Exception:
            pass

        # Switching Chromium/Chrome/Edge with one user-data directory can
        # corrupt browser state and cannot transfer an OTP-authenticated login.
        channel = None if self.browser_backend == "playwright-chromium" else self.browser_backend
        backend = self.browser_backend
        for attempt in range(recovery_attempt, MAX_EXCEL_RECOVERY_ATTEMPTS + 1):
            new_context = None
            try:
                self.write_log(
                    f"Đang mở lại OneBSS bằng {backend} "
                    f"(lần {attempt}/{MAX_EXCEL_RECOVERY_ATTEMPTS})..."
                )
                new_context = self._launch_browser_context(
                    playwright, browser_channel=channel, use_configured_channel=False
                )
                new_page = new_context.pages[0] if new_context.pages else new_context.new_page()
                self.browser_context, self.browser_page = new_context, new_page
                new_page.goto(ONEBSS_URL, wait_until="domcontentloaded", timeout=30000)
                self._ensure_onebss_session_active(new_page)
                self._navigate_onebss(new_page)
                self._ensure_onebss_session_active(new_page)
                self.write_log(f"Đã mở lại OneBSS bằng {backend} và cấu hình xong.")
                return new_context, new_page, attempt
            except OneBSSSessionExpiredError:
                raise
            except Exception as exc:
                self.write_log(f"Không mở lại được {backend}: {exc}")
                try:
                    if new_context:
                        new_context.close()
                except Exception:
                    pass
                if attempt == MAX_EXCEL_RECOVERY_ATTEMPTS:
                    raise
                time.sleep(2)

    def _export_excel_with_recovery(self, playwright, context, page, cycle):
        """Run a complete cycle and recover from a browser disappearing at any stage."""
        recovery_attempt = 0
        search_timeouts = 0
        while True:
            try:
                # Browser shutdowns can race with the start of a scheduled
                # cycle, including the date-filter update.  Keep every
                # browser-touching operation inside this recovery boundary.
                self.current_stage = f"Chu kỳ {cycle}: kiểm tra phiên OneBSS"
                self._ensure_onebss_session_active(page)
                self.current_stage = f"Chu kỳ {cycle}: cập nhật ngày và bộ lọc"
                self._refresh_cycle_dates(page)
                self.current_stage = f"Chu kỳ {cycle}: tìm kiếm và xuất Excel"
                return context, page, self._export_excel(page, context)
            except Exception as exc:
                if isinstance(exc, OneBSSSessionExpiredError):
                    raise
                if getattr(self, "deep_diagnostic_mode", False):
                    self.write_log(
                        "BẢN TEST CHẨN ĐOÁN dừng ở lỗi đầu tiên; không mở lại "
                        "trình duyệt để giữ nguyên bằng chứng."
                    )
                    raise
                if isinstance(exc, OneBSSSearchTimeoutError):
                    search_timeouts += 1
                    if search_timeouts == 1:
                        self._send_workflow_error_alert(
                            "OneBSS tìm kiếm quá 10 phút; ATS TXL đang giữ trình duyệt mở "
                            "và tự làm mới, cấu hình lại để thử tiếp. Chưa xuất Excel."
                        )
                    try:
                        self._recover_onebss_after_search_timeout(
                            page, cycle, search_timeouts
                        )
                    except OneBSSSessionExpiredError:
                        raise
                    except Exception as retry_exc:
                        if not (self._is_browser_closed_error(retry_exc) or page.is_closed()):
                            raise
                        if recovery_attempt >= MAX_EXCEL_RECOVERY_ATTEMPTS:
                            raise
                        recovery_attempt += 1
                        context, page, recovery_attempt = self._recover_closed_browser(
                            playwright, context, cycle, recovery_attempt
                        )
                    continue
                recover_download = isinstance(exc, PlaywrightTimeoutError) and (
                    self._is_excel_download_timeout(exc)
                )
                recover_browser = self._is_browser_closed_error(exc) or page.is_closed()
                if (
                    not (recover_download or recover_browser)
                    or recovery_attempt >= MAX_EXCEL_RECOVERY_ATTEMPTS
                ):
                    raise
                recovery_attempt += 1
                if recover_browser:
                    context, page, recovery_attempt = self._recover_closed_browser(
                        playwright,
                        context,
                        cycle,
                        recovery_attempt,
                    )
                else:
                    self._recover_onebss_after_download_timeout(
                        page, cycle, recovery_attempt
                    )

    def _wait_for_search_complete(self, page, timeout_seconds=600):
        """Wait until OneBSS finishes loading every record.

        OneBSS removes the ``disabled`` class from the "Dừng Xử lý" action
        while a search is running and restores it only after all pages have
        been loaded. Exporting before that transition produces a partial file.
        """
        stop_control = page.get_by_text("Dừng Xử lý", exact=True).first
        stop_control.wait_for(state="visible", timeout=15000)
        deadline = time.monotonic() + timeout_seconds
        processing_seen = False
        next_progress_log = time.monotonic() + 30
        next_session_check = time.monotonic()

        while time.monotonic() < deadline:
            if self.stop_requested:
                raise RuntimeError("Đã dừng bởi người dùng")
            if page.is_closed():
                raise RuntimeError("Trình duyệt đã đóng trong khi OneBSS đang tìm kiếm.")
            if time.monotonic() >= next_session_check:
                self._ensure_onebss_session_active(page)
                next_session_check = time.monotonic() + 5

            classes = (stop_control.get_attribute("class") or "").split()
            is_disabled = "disabled" in classes
            if not is_disabled:
                processing_seen = True
            elif processing_seen:
                page.wait_for_timeout(1000)
                totals = page.locator("text=/Tổng cộng.*bản ghi/").all_inner_texts()
                if totals:
                    self.write_log(f"OneBSS đã tải xong: {totals[-1].strip()}")
                else:
                    self.write_log("OneBSS đã tải xong toàn bộ kết quả.")
                return

            if time.monotonic() >= next_progress_log:
                self.write_log("OneBSS vẫn đang xử lý; tiếp tục chờ, chưa xuất Excel...")
                next_progress_log += 30
            page.wait_for_timeout(500)

        if not processing_seen:
            raise OneBSSSearchTimeoutError(
                "OneBSS không bắt đầu xử lý sau khi bấm Tìm kiếm; chưa xuất Excel."
            )
        raise OneBSSSearchTimeoutError(
            f"OneBSS chưa xử lý xong sau {timeout_seconds // 60} phút; chưa xuất Excel để tránh thiếu bản ghi."
        )

    def _process_and_send(self, excel):
        """Use the transferred MonitorTXL source directly on macOS/Windows."""
        self.write_log("Đang xử lý Excel bằng mã nguồn MonitorTXL...")
        frame = txl.process_bh_file(str(excel))
        alert_frame = txl.get_alert_dataframe_bh(frame)
        self.write_log(
            f"Đã lọc {len(frame)} phiếu phù hợp; "
            f"có {len(alert_frame)} phiếu từ 10 phút trở lên."
        )
        if len(frame):
            report = DOWNLOADS / ("Thong_ke_SLA_TXL_BH_" + time.strftime("%Y%m%d_%H%M%S") + ".xlsx")
            txl.FILE_DATA_TIME = txl.get_file_datetime(str(excel))
            txl.export_bh_excel(frame, str(report))
            self.write_log(f"Đã tạo báo cáo: {report}")

        if len(alert_frame) == 0:
            self.write_log(
                "Không có phiếu Tiền xử lý báo hỏng từ 10 phút trở lên; "
                "bỏ qua gửi Telegram."
            )
            return

        message = txl.build_bh_message(alert_frame)
        txl.send_telegram_message(message)
        self.write_log(
            f"Đã gửi Telegram cho {len(alert_frame)} phiếu từ 10 phút trở lên."
        )


if __name__ == "__main__":
    # Used only by the Windows build pipeline.  This verifies that a frozen
    # EXE can load Python, Playwright and the application's imports without
    # opening a GUI or requiring OneBSS/Telegram configuration.
    if "--self-test" in sys.argv:
        raise SystemExit(0)
    ATSApp().mainloop()
