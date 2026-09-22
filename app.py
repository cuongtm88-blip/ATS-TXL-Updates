"""ATS TXL - automated OneBSS export and Telegram reporting."""
from __future__ import annotations

import json
import html
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
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


class OneBSSSessionExpiredError(RuntimeError):
    """OneBSS needs a fresh interactive login, including OTP if requested."""


class OneBSSSearchTimeoutError(RuntimeError):
    """OneBSS did not finish a search; never export a partial result."""


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
        self._active_export_diagnostic = None
        self._diagnostic_context_ids = set()
        self._diagnostic_page_ids = set()
        self._last_diagnostic_path = None
        self.start_event = None
        self.update_in_progress = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self._apply_diagnostics_config()
        except Exception as exc:
            self.write_log(f"Chẩn đoán GitHub chưa sẵn sàng: {exc}")
        self.after(5000, self._retry_pending_diagnostics)
        if updater.can_self_update():
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
        ttk.Label(
            diagnostics_box,
            text="Token chỉ lưu trong Windows Credential Manager/Keychain; không ghi vào settings.json hay GitHub public.",
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 8))
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
            f"Đã xác minh bộ cài ATS TXL {info.version}; đang mở bộ cài để cập nhật."
        )
        messagebox.showinfo(
            "Cập nhật ATS TXL",
            f"Đã tải và xác minh phiên bản {info.version}.\n"
            "Bộ cài sẽ đóng ứng dụng, cập nhật và mở lại phiên bản mới.",
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
        def redact(value):
            value = str(value)
            return re.sub(
                r"(?i)(authorization\s*:\s*bearer\s+)([A-Za-z0-9._~-]+)",
                r"\1[REDACTED]",
                value,
            )[:2000]
        active["events"].append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": event_name,
                **{key: redact(value) for key, value in details.items()},
            }
        )

    def _attach_page_diagnostics(self, context, page):
        page_id = id(page)
        if page_id in self._diagnostic_page_ids:
            return
        self._diagnostic_page_ids.add(page_id)
        page.on(
            "crash",
            lambda: self._record_diagnostic_event(
                context, "page-crash", url=self._safe_page_url(page)
            ),
        )
        page.on(
            "close",
            lambda: self._record_diagnostic_event(
                context, "page-close", url=self._safe_page_url(page)
            ),
        )
        page.on(
            "pageerror",
            lambda error: self._record_diagnostic_event(
                context, "page-error", message=error, url=self._safe_page_url(page)
            ),
        )

        def record_console(message):
            if message.type in ("error", "warning"):
                self._record_diagnostic_event(
                    context,
                    "console-" + message.type,
                    message=message.text,
                    url=self._safe_page_url(page),
                )

        page.on("console", record_console)

    def _attach_context_diagnostics(self, context, page):
        """Subscribe once to browser events needed to identify an Export failure."""
        context_id = id(context)
        if context_id not in self._diagnostic_context_ids:
            self._diagnostic_context_ids.add(context_id)
            context.on(
                "close",
                lambda: self._record_diagnostic_event(context, "context-close"),
            )
            context.on(
                "page",
                lambda new_page: self._attach_page_diagnostics(context, new_page),
            )
            context.on(
                "requestfailed",
                lambda request: self._record_diagnostic_event(
                    context,
                    "request-failed",
                    url=request.url,
                    failure=request.failure,
                ),
            )
            context.on(
                "weberror",
                lambda error: self._record_diagnostic_event(
                    context, "web-error", message=error.error
                ),
            )
            try:
                browser = context.browser
                if browser:
                    browser.on(
                        "disconnected",
                        lambda: self._record_diagnostic_event(
                            context, "browser-disconnected"
                        ),
                    )
            except Exception:
                pass
        self._attach_page_diagnostics(context, page)

    def _begin_export_diagnostic(self, context, page):
        self._active_export_diagnostic = {
            "id": uuid.uuid4().hex[:10],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "context": context,
            "backend": self.browser_backend,
            "page_url": self._safe_page_url(page),
            "events": [],
            "trace_started": False,
            "browser_processes_before": self._windows_browser_processes(),
        }
        self._attach_context_diagnostics(context, page)
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self._active_export_diagnostic["trace_started"] = True
        except Exception as exc:
            self._record_diagnostic_event(context, "trace-start-failed", message=exc)

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
    def _windows_browser_processes():
        """Snapshot browser processes around an Export without collecting command lines."""
        if sys.platform != "win32":
            return "Không áp dụng: không phải Windows."
        command = (
            "Get-Process chrome,msedge,chromium -ErrorAction SilentlyContinue | "
            "Select-Object Id,ProcessName,Path,StartTime,CPU,WorkingSet64 | "
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
        folder = APP_DATA / DIAGNOSTICS_DIRNAME / f"export_{timestamp}_{active['id']}"
        folder.mkdir(parents=True, exist_ok=True)
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
            "page_url_after_error": self._safe_page_url(page),
            "trace_path": str(trace_path) if trace_path.exists() else "",
            "trace_error": trace_error,
            "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
            "screenshot_error": screenshot_error,
        }
        (folder / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "browser-events.json").write_text(
            json.dumps(active["events"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "windows-events.json").write_text(
            self._windows_crash_events(), encoding="utf-8"
        )
        (folder / "browser-processes.json").write_text(
            json.dumps(
                {
                    "before_export": active.get("browser_processes_before", ""),
                    "after_error": self._windows_browser_processes(),
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
                    self.start_event.wait()
                    if self.stop_requested:
                        return
                    if not self._onebss_session_expired(page):
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
            else:
                self.write_log(f"LỖI: {exc}")
                error_text = str(exc)
                if self._last_diagnostic_path:
                    error_text += f"\nGói chẩn đoán: {self._last_diagnostic_path}"
                self._send_workflow_error_alert(error_text)
                self.after(0, lambda error_text=error_text: messagebox.showerror("ATS TXL", error_text))
        finally:
            self.current_stage = "Đã dừng"
            self.after(0, self.progress.stop)
            self.after(0, self._release_keep_awake)

    def _onebss_session_expired(self, page):
        if page.is_closed():
            return False
        url = page.url.lower()
        if any(marker in url for marker in ("login", "signin", "auth")):
            return True
        # OneBSS stores its access JWT in localStorage['OneBSS-Token'] and can
        # leave the inventory screen visible after expiry. Return only its exp;
        # never copy the token into Python, logs, or diagnostic uploads.
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
            if expires_at is not None and float(expires_at) <= time.time() + 30:
                return True
        except Exception:
            # Login-page detection below remains available when storage is
            # inaccessible (e.g. while navigation is in progress).
            pass
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
        if self._onebss_session_expired(page):
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
        self._send_workflow_error_alert(str(reason))
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
        if browser_channel is None and use_configured_channel:
            browser_channel = os.getenv("ATS_BROWSER_CHANNEL", "").strip() or None
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
        return playwright.chromium.launch_persistent_context(str(selected_profile), **options)

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
        try:
            self.write_log("Bấm Tìm kiếm và chờ tải hết phiếu...")
            page.get_by_text("Tìm kiếm", exact=True).click(timeout=15000)
            self._wait_for_search_complete(page)
            self.write_log("Bấm Xuất Excel...")
            with page.expect_download(timeout=120000) as download_info:
                page.get_by_text("Xuất Excel", exact=True).click(timeout=15000)
            download = download_info.value
            target = DOWNLOADS / ("Bao_hong_ton_" + time.strftime("%Y%m%d%H%M%S") + ".xlsx")
            download.save_as(str(target))
            self._finish_export_diagnostic(context, page)
            self.write_log(f"Đã lưu Excel: {target}")
            return target
        except Exception as exc:
            self._finish_export_diagnostic(context, page, exc)
            raise

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
        self.current_stage = f"Chu kỳ {cycle}: phục hồi Chromium sau khi bị đóng"
        self.write_log(
            "Browser/OneBSS đã bị đóng trong chu kỳ. "
            f"Đang mở lại và cấu hình lại (lần {recovery_attempt}/"
            f"{MAX_EXCEL_RECOVERY_ATTEMPTS})..."
        )
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
