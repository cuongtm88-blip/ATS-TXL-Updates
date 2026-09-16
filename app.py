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
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import requests

try:
    from playwright._impl._errors import TargetClosedError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - friendly message at runtime
    sync_playwright = None
    PlaywrightTimeoutError = RuntimeError
    TargetClosedError = RuntimeError

import TXL_Monitor_Tele_Group_All_Over10 as txl
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
MAX_EXCEL_RECOVERY_ATTEMPTS = 1


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
        self.geometry("900x650")
        self.minsize(820, 550)
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
        self.current_stage = "Khởi tạo ứng dụng"
        self.browser_context = None
        self.browser_page = None
        self.start_event = None
        self.update_in_progress = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
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

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="1. Mở Chrome / OneBSS", command=self.open_browser)
        self.start_btn.pack(side="left", padx=4)
        self.run_btn = ttk.Button(actions, text="2. Chạy quy trình", command=self.run_workflow)
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
        self.write_log("Sẵn sàng. Hãy mở Chrome và đăng nhập OneBSS.")

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
        except OSError as exc:
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
            updater.launch_windows_replacement(target, APP_DATA / "updates")
        except Exception as exc:
            self._finish_update_download_error(str(exc))
            return
        self.write_log(
            f"Đã xác minh ATS TXL {info.version}; ứng dụng sẽ tự khởi động lại."
        )
        messagebox.showinfo(
            "Cập nhật ATS TXL",
            f"Đã tải và xác minh phiên bản {info.version}.\n"
            "Ứng dụng sẽ đóng và tự mở lại bằng phiên bản mới.",
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
            self.write_log("Chrome đã mở. Hãy đăng nhập rồi bấm nút 2.")
            return
        try:
            self._error_recipient_ids = _parse_recipient_chat_ids(
                self.error_recipient_chat_ids.get()
            )
        except ValueError as exc:
            messagebox.showerror("Chat ID nhận cảnh báo lỗi", str(exc))
            return
        self._telegram_token_for_alerts = self.telegram_token.get().strip()
        if self._error_recipient_ids and not self._telegram_token_for_alerts:
            messagebox.showerror(
                "Thiếu Telegram Bot token",
                "Cần nhập Telegram Bot token để gửi cảnh báo lỗi riêng.",
            )
            return
        self.stop_requested = False
        self.start_event = threading.Event()
        self._acquire_keep_awake()
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
                self.write_log("Bắt đầu quy trình tự động...")
                self.start_event.set()
            return
        self.write_log("Hãy bấm nút 1 để mở Chrome trước.")

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
            _save_settings({
                "telegram_token": token,
                "telegram_chat_id": chat_id,
                "error_recipient_chat_ids": error_recipient_ids,
                "schedule_enabled": self.auto_repeat,
                "repeat_minutes": interval_minutes,
                "keep_awake_enabled": bool(self.keep_awake_enabled.get()),
            })
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
                self.write_log("Chrome đã mở. Hãy đăng nhập OneBSS; chương trình sẽ tự mở trang kiểm soát.")
                self.current_stage = "Chờ người dùng đăng nhập OneBSS"
                self._wait_for_login(page)
                self.current_stage = "Mở màn hình và cấu hình bộ lọc OneBSS"
                self._navigate_onebss(page)
                self.write_log("Đã vào trang Kiểm soát tồn báo hỏng CNTT. Kiểm tra cấu hình rồi bấm nút 2.")
                self.current_stage = "Chờ bắt đầu quy trình"
                self.start_event.wait()
                if self.stop_requested:
                    return
                self.after(0, lambda: self.progress.start(10))
                cycle = 1
                while not self.stop_requested:
                    self.current_stage = f"Chu kỳ {cycle}: kiểm tra phiên OneBSS"
                    self._ensure_onebss_session_active(page)
                    if cycle > 1:
                        self.write_log(f"Bắt đầu chu kỳ tự động lần {cycle}.")
                    ctx, page, excel = self._export_excel_with_recovery(
                        p, ctx, page, cycle
                    )
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
            self.write_log(f"LỖI: {exc}")
            error_text = str(exc)
            if not self.stop_requested:
                self._send_workflow_error_alert(error_text)
            self.after(0, lambda error_text=error_text: messagebox.showerror("ATS TXL", error_text))
        finally:
            self.current_stage = "Đã dừng"
            self.after(0, self.progress.stop)
            self.after(0, self._release_keep_awake)

    def _wait_for_login(self, page):
        for _ in range(180):
            if self.stop_requested:
                raise RuntimeError("Đã dừng bởi người dùng")
            url = page.url.lower()
            if "login" not in url and ("onebss" in url or page.locator("text=Trang chủ").count()):
                return
            time.sleep(1)
        raise RuntimeError("Hết thời gian chờ đăng nhập OneBSS")

    def _onebss_session_expired(self, page):
        if page.is_closed():
            return False
        url = page.url.lower()
        if any(marker in url for marker in ("login", "signin", "auth")):
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
        if self._onebss_session_expired(page):
            raise RuntimeError(
                "Phiên đăng nhập OneBSS đã hết hạn. Hãy mở ứng dụng và đăng nhập lại OneBSS."
            )

    def _launch_browser_context(self, playwright, *, headless=False, profile=PROFILE):
        """Launch the Playwright-matched browser instead of system Chrome.

        The installed Google Chrome channel can be newer than the Playwright
        driver and has crashed during OneBSS downloads on macOS. Playwright's
        bundled Chromium is version-matched and is therefore the safe default.
        Set ATS_BROWSER_CHANNEL=chrome only when explicitly needed.
        """
        options = {
            "headless": headless,
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 900},
        }
        Path(profile).mkdir(parents=True, exist_ok=True)
        browser_channel = os.getenv("ATS_BROWSER_CHANNEL", "").strip()
        if browser_channel:
            options["channel"] = browser_channel
        return playwright.chromium.launch_persistent_context(str(profile), **options)

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

    def _export_excel(self, page):
        if page.is_closed():
            raise RuntimeError("Trình duyệt đã đóng. Hãy bấm nút 1 để mở lại OneBSS.")
        self._ensure_onebss_session_active(page)
        self.write_log("Bấm Tìm kiếm và chờ tải hết phiếu...")
        page.get_by_text("Tìm kiếm", exact=True).click(timeout=15000)
        self._wait_for_search_complete(page)
        self.write_log("Bấm Xuất Excel...")
        with page.expect_download(timeout=120000) as download_info:
            page.get_by_text("Xuất Excel", exact=True).click(timeout=15000)
        download = download_info.value
        target = DOWNLOADS / ("Bao_hong_ton_" + time.strftime("%Y%m%d%H%M%S") + ".xlsx")
        download.save_as(str(target))
        self.write_log(f"Đã lưu Excel: {target}")
        return target

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

    @staticmethod
    def _is_browser_closed_error(exc):
        return isinstance(exc, TargetClosedError) or (
            "target page, context or browser has been closed" in str(exc).lower()
        )

    def _recover_closed_browser(self, playwright, context, cycle, recovery_attempt):
        """Replace a crashed/closed context and restore the OneBSS workspace."""
        self.current_stage = f"Chu kỳ {cycle}: phục hồi Chromium sau khi bị đóng"
        self.write_log(
            "Chromium/OneBSS đã bị đóng khi xuất Excel. "
            f"Đang mở lại và cấu hình lại (lần {recovery_attempt}/"
            f"{MAX_EXCEL_RECOVERY_ATTEMPTS})..."
        )
        try:
            context.close()
        except Exception:
            pass

        new_context = self._launch_browser_context(playwright)
        new_page = new_context.pages[0] if new_context.pages else new_context.new_page()
        self.browser_context, self.browser_page = new_context, new_page
        new_page.goto(ONEBSS_URL, wait_until="domcontentloaded", timeout=30000)
        self._ensure_onebss_session_active(new_page)
        self._navigate_onebss(new_page)
        self._ensure_onebss_session_active(new_page)
        self.write_log("Đã mở lại OneBSS và cấu hình xong; chạy lại tìm kiếm và xuất Excel.")
        return new_context, new_page

    def _export_excel_with_recovery(self, playwright, context, page, cycle):
        """Export once, then recover from an absent download or closed browser."""
        recovery_attempt = 0
        while True:
            self.current_stage = f"Chu kỳ {cycle}: cập nhật ngày và bộ lọc"
            self._refresh_cycle_dates(page)
            self.current_stage = f"Chu kỳ {cycle}: tìm kiếm và xuất Excel"
            try:
                return context, page, self._export_excel(page)
            except Exception as exc:
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
                    context, page = self._recover_closed_browser(
                        playwright, context, cycle, recovery_attempt
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

        while time.monotonic() < deadline:
            if self.stop_requested:
                raise RuntimeError("Đã dừng bởi người dùng")
            if page.is_closed():
                raise RuntimeError("Trình duyệt đã đóng trong khi OneBSS đang tìm kiếm.")
            self._ensure_onebss_session_active(page)

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
            raise RuntimeError("OneBSS không bắt đầu xử lý sau khi bấm Tìm kiếm.")
        raise RuntimeError(
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
