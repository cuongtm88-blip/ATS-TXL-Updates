"""Regression tests for Windows browser recovery and OneBSS auth detection."""

import io
import base64
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import diagnostics_upload
import updater


class RecoveryTests(unittest.TestCase):
    def test_diagnostic_url_redacts_queries_and_identifiers(self):
        url = "https://onebss.vnpt.vn/api/exportExcel/12345678?access_token=secret"
        safe = app.ATSApp._safe_diagnostic_url(url)
        self.assertNotIn("secret", safe)
        self.assertNotIn("12345678", safe)
        self.assertIn("[QUERY_REDACTED]", safe)
        self.assertIn("exportExcel", safe)

    def test_test_a_only_flags_explicit_export_candidates(self):
        export = SimpleNamespace(url="https://onebss.vnpt.vn/api/exportExcel")
        ambiguous = SimpleNamespace(url="https://onebss.vnpt.vn/api/queryRecords")
        self.assertTrue(app.ATSApp._is_explicit_export_candidate(export))
        self.assertFalse(app.ATSApp._is_explicit_export_candidate(ambiguous))

    def test_test_c_finds_root_browser_pid_for_ats_profile(self):
        snapshot = [
            {
                "ProcessId": 10,
                "Name": "chrome.exe",
                "CommandLine": 'chrome.exe --user-data-dir="C:\\ATS\\profile"',
            },
            {
                "ProcessId": 11,
                "Name": "chrome.exe",
                "CommandLine": 'chrome.exe --type=renderer --user-data-dir="C:\\ATS\\profile"',
            },
            {
                "ProcessId": 12,
                "Name": "msedge.exe",
                "CommandLine": 'msedge.exe --user-data-dir="C:\\Edge\\profile"',
            },
        ]
        root = app.ATSApp._find_main_browser_process(
            json.dumps(snapshot), r"C:\ATS\profile"
        )
        self.assertEqual(root["pid"], 10)

    def test_test_c_observation_waits_without_closing_browser_targets(self):
        context = object()
        active = {
            "context": context,
            "network_armed": True,
            "main_browser_process": {"pid": 10},
            "profile_path": "",
            "events": [],
            "network_events": [],
        }
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state.write_log = Mock()
        page = Mock()
        page.is_closed.return_value = False
        page.wait_for_timeout.side_effect = lambda timeout: time.sleep(timeout / 1000)
        with (
            patch.object(app.ATSApp, "_open_main_process_watch", return_value=object()),
            patch.object(app.ATSApp, "_poll_main_process_watch", return_value={"alive": True}),
            patch.object(app.ATSApp, "_close_main_process_watch"),
            patch.object(app.ATSApp, "_snapshot_browser_dumps", return_value={}),
        ):
            result = state._observe_test_c(
                page, context, active, duration_seconds=0.02
            )
        self.assertIn("Chrome remained alive", result)
        page.close.assert_not_called()
        self.assertFalse(active["test_c_observing"])
        self.assertEqual(active["close_reason"], "application-cleanup")

    def test_test_c_records_main_pid_exit_and_exit_code(self):
        context = object()
        active = {
            "context": context,
            "network_armed": True,
            "main_browser_process": {"pid": 10},
            "profile_path": "",
            "events": [],
            "network_events": [],
        }
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state.write_log = Mock()
        page = Mock()
        page.is_closed.return_value = False
        page.wait_for_timeout.side_effect = lambda timeout: time.sleep(timeout / 1000)
        with (
            patch.object(app.ATSApp, "_open_main_process_watch", return_value=object()),
            patch.object(app.ATSApp, "_poll_main_process_watch", return_value={"alive": False, "exit_code": 7}),
            patch.object(app.ATSApp, "_close_main_process_watch"),
            patch.object(app.ATSApp, "_snapshot_browser_dumps", return_value={}),
        ):
            result = state._observe_test_c(
                page, context, active, duration_seconds=0.02
            )
        self.assertIn("terminated independently", result)
        self.assertEqual(active["close_reason"], "unexpected-browser-exit")
        exited = [
            event for event in active["network_events"]
            if event["event_type"] == "MAIN CHROME PROCESS EXITED"
        ]
        self.assertEqual(exited[0]["exit_code"], 7)

    def test_test_c_classifies_playwright_close_as_unexpected_even_if_main_pid_lives(self):
        context = object()
        active = {
            "context": context,
            "network_armed": True,
            "main_browser_process": {"pid": 10},
            "profile_path": "",
            "events": [],
            "network_events": [],
        }
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state.write_log = Mock()
        page = Mock()
        page.is_closed.return_value = False

        def close_context_during_observation(_timeout):
            state._on_diagnostic_context_closed(context)
            time.sleep(0.001)

        page.wait_for_timeout.side_effect = close_context_during_observation
        with (
            patch.object(app.ATSApp, "_open_main_process_watch", return_value=object()),
            patch.object(app.ATSApp, "_poll_main_process_watch", return_value={"alive": True}),
            patch.object(app.ATSApp, "_close_main_process_watch"),
            patch.object(app.ATSApp, "_snapshot_browser_dumps", return_value={}),
        ):
            result = state._observe_test_c(
                page, context, active, duration_seconds=0.02
            )
        self.assertIn("observed browser/page/context closure", result)
        self.assertEqual(active["close_reason"], "unexpected-browser-exit")

    def test_test_c_closes_context_only_after_observation_and_cleanup_marker(self):
        context = Mock()
        sequence = []
        context.close.side_effect = lambda: sequence.append("context.close")
        active = {
            "context": context,
            "network_armed": True,
            "download_event_seen": True,
            "events": [],
            "network_events": [],
            "page_ids": {},
            "main_browser_process": {"pid": 10},
            "close_reason": "application-cleanup",
        }
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state._diagnostic_upload_config = None
        state._error_recipient_ids = []
        state.write_log = Mock(side_effect=lambda line: sequence.append(line))
        state._finish_export_diagnostic = Mock(
            side_effect=lambda *args: sequence.append("finish diagnostics")
        )
        page = Mock()
        page.is_closed.return_value = False
        page.get_by_text.return_value.click.return_value = None
        with (
            patch.object(
                app.ATSApp,
                "_observe_test_c",
                return_value="TEST C RESULT: Chrome remained alive for 30 seconds after Blob download.",
            ) as observe,
            self.assertRaises(app.DiagnosticTestCompleted),
        ):
            state._run_export_test_a(page, context)
        observe.assert_called_once_with(page, context, active, duration_seconds=30)
        self.assertLess(
            sequence.index("DIAGNOSTIC CLEANUP STARTED"),
            sequence.index("context.close"),
        )
        self.assertLess(
            sequence.index("context.close"),
            sequence.index("DIAGNOSTIC CLEANUP FINISHED"),
        )
        self.assertLess(
            sequence.index("DIAGNOSTIC CLEANUP FINISHED"),
            sequence.index("finish diagnostics"),
        )

    def test_xlsx_response_is_recognized_by_ooxml_structure(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as workbook:
            workbook.writestr("[Content_Types].xml", "<Types/>")
            workbook.writestr("xl/workbook.xml", "<workbook/>")
        self.assertTrue(app.ATSApp._is_xlsx_payload(stream.getvalue()))

    def test_arbitrary_zip_is_not_mistaken_for_xlsx(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("readme.txt", "not a workbook")
        self.assertFalse(app.ATSApp._is_xlsx_payload(stream.getvalue()))
        self.assertFalse(app.ATSApp._is_xlsx_payload(b"not an xlsx"))

    def test_expired_jwt_is_detected_even_on_inventory_page(self):
        page = Mock()
        page.is_closed.return_value = False
        page.url = app.INCIDENT_INVENTORY_URL
        page.evaluate.return_value = time.time() - 60
        self.assertTrue(app.ATSApp._onebss_session_expired(None, page))
        page.locator.assert_not_called()

    def test_valid_jwt_does_not_signal_expiry(self):
        page = Mock()
        page.is_closed.return_value = False
        page.url = app.INCIDENT_INVENTORY_URL
        page.evaluate.return_value = time.time() + 3600
        page.locator.return_value.first.count.return_value = 0
        self.assertFalse(app.ATSApp._onebss_session_expired(None, page))

    def test_browser_storage_jwt_detection_end_to_end(self):
        # No network or OneBSS account is involved; exercise the real JS in
        # the same Chromium engine used by the release build.
        with app.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(body="<html></html>"))
                page.goto("https://example.test/")
                put_token = """expiry => {
                    const encode = value => btoa(JSON.stringify(value))
                        .replace(/=+$/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
                    const access = encode({alg:'none'}) + '.' +
                        encode({exp:expiry,client_id:'onebss',scope:['read']}) + '.signature';
                    const refresh = encode({alg:'none'}) + '.' +
                        encode({exp:expiry+86400,client_id:'onebss',scope:['read']}) + '.signature';
                    localStorage.setItem('OneBSS-Token', JSON.stringify({access_token:access}));
                    localStorage.setItem('refresh_token', refresh);
                }"""
                page.evaluate(put_token, int(time.time()) - 60)
                self.assertTrue(app.ATSApp._onebss_session_expired(None, page))
                page.evaluate(put_token, int(time.time()) + 3600)
                self.assertFalse(app.ATSApp._onebss_session_expired(None, page))
                page.evaluate("() => localStorage.removeItem('OneBSS-Token')")
                self.assertTrue(app.ATSApp._onebss_session_expired(None, page))
            finally:
                browser.close()

    def test_windows_uses_selected_browser_and_separate_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = Mock()
            state = SimpleNamespace(
                browser_backend="",
                browser_choice=SimpleNamespace(get=lambda: "Google Chrome (mặc định)"),
            )
            profile = Path(temporary) / "chrome-profile"
            with patch.object(app.sys, "platform", "win32"), patch.dict(app.os.environ, {"ATS_BROWSER_CHANNEL": ""}):
                app.ATSApp._launch_browser_context(state, browser, profile=profile)
                default_profile = browser.chromium.launch_persistent_context.call_args.args[0]
                default_options = browser.chromium.launch_persistent_context.call_args.kwargs
                self.assertEqual(state.browser_backend, "chrome")
                self.assertEqual(Path(default_profile).name, "chrome-profile-chrome")
                self.assertEqual(default_options["channel"], "chrome")

                app.ATSApp._launch_browser_context(state, browser, profile=profile, browser_channel="msedge")
                edge_profile = browser.chromium.launch_persistent_context.call_args.args[0]
                self.assertEqual(Path(edge_profile).name, "chrome-profile-msedge")
                self.assertNotEqual(default_profile, edge_profile)

    def test_recovery_restarts_same_backend_only(self):
        old_context = Mock()
        new_page = Mock()
        new_context = Mock(pages=[new_page])
        state = SimpleNamespace(
            browser_backend="playwright-chromium",
            current_stage="Chu kỳ 2: tìm kiếm và xuất Excel",
            _last_diagnostic_path=Path("export_20260922_134341_1dfafa0945"),
            write_log=Mock(),
            _send_workflow_error_alert=Mock(),
            _launch_browser_context=Mock(return_value=new_context),
            _ensure_onebss_session_active=Mock(),
            _navigate_onebss=Mock(),
            _set_onebss_token_status=Mock(),
            _onebss_token_expires_at=0,
            _send_onebss_session_alert=Mock(),
        )
        result = app.ATSApp._recover_closed_browser(state, Mock(), old_context, 2, 1)
        self.assertEqual(result, (new_context, new_page, 1))
        self.assertIsNone(state._launch_browser_context.call_args.kwargs["browser_channel"])
        alert = state._send_workflow_error_alert.call_args.args[0]
        self.assertIn("đang tự phục hồi lần 1/3", alert)
        self.assertIn("export_20260922_134341_1dfafa0945", alert)
        old_context.close.assert_called_once()

    def test_recovery_does_not_retry_when_otp_is_needed(self):
        old_context = Mock()
        new_context = Mock(pages=[Mock()])
        state = SimpleNamespace(
            browser_backend="playwright-chromium",
            current_stage="Chu kỳ 2: tìm kiếm và xuất Excel",
            write_log=Mock(),
            _send_workflow_error_alert=Mock(),
            _launch_browser_context=Mock(return_value=new_context),
            _ensure_onebss_session_active=Mock(side_effect=app.OneBSSSessionExpiredError("expired")),
            _navigate_onebss=Mock(),
        )
        with self.assertRaises(app.OneBSSSessionExpiredError):
            app.ATSApp._recover_closed_browser(state, Mock(), old_context, 2, 1)
        state._launch_browser_context.assert_called_once()

    def test_telegram_failure_does_not_block_browser_recovery(self):
        old_context = Mock()
        new_page = Mock()
        new_context = Mock(pages=[new_page])
        state = SimpleNamespace(
            browser_backend="playwright-chromium",
            current_stage="Chu kỳ 1: tìm kiếm và xuất Excel",
            write_log=Mock(),
            _send_workflow_error_alert=Mock(side_effect=RuntimeError("Telegram offline")),
            _launch_browser_context=Mock(return_value=new_context),
            _ensure_onebss_session_active=Mock(),
            _navigate_onebss=Mock(),
        )
        result = app.ATSApp._recover_closed_browser(state, Mock(), old_context, 1, 1)
        self.assertEqual(result, (new_context, new_page, 1))
        state._send_workflow_error_alert.assert_called_once()
        state.write_log.assert_any_call(
            "Không gửi được cảnh báo browser bị đóng: Telegram offline"
        )

    def test_search_timeout_retries_without_closing_context(self):
        context = Mock()
        page = Mock()
        page.is_closed.return_value = False
        state = SimpleNamespace(
            current_stage="",
            _ensure_onebss_session_active=Mock(),
            _refresh_cycle_dates=Mock(),
            _export_excel=Mock(side_effect=[app.OneBSSSearchTimeoutError("timeout"), "report.xlsx"]),
            _send_workflow_error_alert=Mock(),
            _recover_onebss_after_search_timeout=Mock(),
        )
        result = app.ATSApp._export_excel_with_recovery(state, Mock(), context, page, 3)
        self.assertEqual(result, (context, page, "report.xlsx"))
        state._recover_onebss_after_search_timeout.assert_called_once_with(page, 3, 1)
        state._send_workflow_error_alert.assert_called_once()
        context.close.assert_not_called()

    def test_repeated_search_timeouts_continue_until_success(self):
        context = Mock()
        page = Mock()
        state = SimpleNamespace(
            current_stage="",
            _ensure_onebss_session_active=Mock(),
            _refresh_cycle_dates=Mock(),
            _export_excel=Mock(side_effect=[
                app.OneBSSSearchTimeoutError("timeout 1"),
                app.OneBSSSearchTimeoutError("timeout 2"),
                "report.xlsx",
            ]),
            _send_workflow_error_alert=Mock(),
            _recover_onebss_after_search_timeout=Mock(),
        )
        result = app.ATSApp._export_excel_with_recovery(state, Mock(), context, page, 3)
        self.assertEqual(result, (context, page, "report.xlsx"))
        self.assertEqual(
            [call.args[2] for call in state._recover_onebss_after_search_timeout.call_args_list],
            [1, 2],
        )
        state._send_workflow_error_alert.assert_called_once()
        context.close.assert_not_called()

    def test_search_timeout_refreshes_same_page_and_reconfigures(self):
        page = Mock()
        state = SimpleNamespace(
            current_stage="",
            stop_requested=False,
            write_log=Mock(),
            _wait_before_search_retry=Mock(),
            _ensure_onebss_session_active=Mock(),
            _navigate_onebss=Mock(),
        )
        app.ATSApp._recover_onebss_after_search_timeout(state, page, 3, 1)
        page.reload.assert_called_once()
        state._navigate_onebss.assert_called_once_with(page)

    def test_refresh_failure_is_retried_without_closing_page(self):
        page = Mock()
        page.is_closed.return_value = False
        page.reload.side_effect = [RuntimeError("temporary network failure"), None]
        state = SimpleNamespace(
            current_stage="",
            stop_requested=False,
            write_log=Mock(),
            _wait_before_search_retry=Mock(),
            _ensure_onebss_session_active=Mock(),
            _navigate_onebss=Mock(),
            _is_browser_closed_error=app.ATSApp._is_browser_closed_error,
        )
        app.ATSApp._recover_onebss_after_search_timeout(state, page, 3, 1)
        self.assertEqual(page.reload.call_count, 2)
        state._navigate_onebss.assert_called_once_with(page)
        page.close.assert_not_called()

    def test_manual_stop_interrupts_search_retry_without_closing_page(self):
        page = Mock()
        state = SimpleNamespace(stop_requested=True)
        with self.assertRaisesRegex(RuntimeError, "Đã dừng bởi người dùng"):
            app.ATSApp._wait_before_search_retry(state, page, 300)
        page.close.assert_not_called()

    def test_expired_session_interrupts_search_retry_without_closing_page(self):
        page = Mock()
        page.is_closed.return_value = False
        state = SimpleNamespace(
            stop_requested=False,
            _ensure_onebss_session_active=Mock(
                side_effect=app.OneBSSSessionExpiredError("expired")
            ),
        )
        with self.assertRaises(app.OneBSSSessionExpiredError):
            app.ATSApp._wait_before_search_retry(state, page, 300)
        page.close.assert_not_called()

    def test_expired_session_pauses_instead_of_closing_browser(self):
        page = Mock()
        page.is_closed.return_value = False
        event = Mock()
        event.wait.return_value = True
        state = SimpleNamespace(
            current_stage="",
            stop_requested=False,
            start_event=event,
            write_log=Mock(),
            _send_workflow_error_alert=Mock(),
            _set_onebss_token_status=Mock(),
            _onebss_token_expires_at=0,
            _send_onebss_session_alert=Mock(),
            _ensure_onebss_session_active=Mock(
                side_effect=[app.OneBSSSessionExpiredError("expired"), None, None]
            ),
            _navigate_onebss=Mock(),
        )
        app.ATSApp._wait_for_reauthentication(state, page, "expired")
        self.assertEqual(event.wait.call_count, 2)
        state._navigate_onebss.assert_called_once_with(page)
        page.close.assert_not_called()

    def test_workflow_keeps_playwright_open_during_reauthentication(self):
        context = Mock()
        page = Mock()
        context.pages = [page]
        state = SimpleNamespace(
            current_stage="",
            browser_context=None,
            browser_page=None,
            stop_requested=False,
            start_event=Mock(),
            auto_repeat=False,
            progress=Mock(),
            _last_diagnostic_path=None,
            _launch_browser_context=Mock(return_value=context),
            _start_process_exit_monitor=Mock(),
            _stop_process_exit_monitor=Mock(),
            _onebss_session_expired=Mock(return_value=False),
            _onebss_token_expiry=Mock(return_value=1_800_000_000),
            _set_onebss_token_status=Mock(),
            _maybe_warn_onebss_session_expiring=Mock(),
            _ensure_onebss_session_active=Mock(),
            _acquire_keep_awake=Mock(),
            _release_keep_awake=Mock(),
            _navigate_onebss=Mock(),
            _export_excel_with_recovery=Mock(side_effect=[
                app.OneBSSSessionExpiredError("expired"),
                (context, page, "report.xlsx"),
            ]),
            _process_and_send=Mock(),
            _wait_for_reauthentication=Mock(),
            _send_workflow_error_alert=Mock(),
            write_log=Mock(),
            after=Mock(),
            browser_choice=SimpleNamespace(get=lambda: "Google Chrome (mặc định)"),
        )
        state._wait_for_reauthentication.side_effect = (
            lambda *_: context.close.assert_not_called()
        )
        state.start_event.wait.side_effect = [False, True]
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "DOWNLOADS", Path(temporary)
        ), patch.object(app, "sync_playwright") as playwright:
            app.ATSApp._session_workflow(state)
        self.assertEqual(state._export_excel_with_recovery.call_count, 2)
        self.assertEqual(state.start_event.wait.call_count, 2)
        self.assertEqual(state._onebss_token_expiry.call_count, 2)
        state._set_onebss_token_status.assert_called_with(1_800_000_000)
        state._maybe_warn_onebss_session_expiring.assert_called_with(1_800_000_000)
        state._wait_for_reauthentication.assert_called_once()
        state._process_and_send.assert_called_once_with("report.xlsx")
        state._start_process_exit_monitor.assert_called_once()
        state._stop_process_exit_monitor.assert_called_once()
        context.close.assert_called_once()

    def test_deep_diagnostic_stops_on_first_browser_error(self):
        context = Mock()
        page = Mock()
        state = SimpleNamespace(
            current_stage="",
            deep_diagnostic_mode=True,
            _ensure_onebss_session_active=Mock(),
            _refresh_cycle_dates=Mock(),
            _export_excel=Mock(side_effect=RuntimeError("Target page, context or browser has been closed")),
            write_log=Mock(),
        )
        with self.assertRaisesRegex(RuntimeError, "Target page"):
            app.ATSApp._export_excel_with_recovery(state, Mock(), context, page, 1)
        state.write_log.assert_called_once()
        context.close.assert_not_called()

    def test_windows_diagnostics_do_not_open_console(self):
        completed = SimpleNamespace(stdout="", stderr="")
        with patch.object(app.sys, "platform", "win32"), patch.object(
            app.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
        ), patch.object(app.subprocess, "run", return_value=completed) as run:
            app.ATSApp._windows_browser_processes()
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
            app.ATSApp._windows_crash_events()
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_diagnostic_event_redacts_bearer(self):
        context = object()
        state = SimpleNamespace(_active_export_diagnostic={"context": context, "events": []})
        app.ATSApp._record_diagnostic_event(
            state, context, "console-warning", message="Authorization:Bearer header.payload.signature"
        )
        message = state._active_export_diagnostic["events"][0]["message"]
        self.assertNotIn("header.payload.signature", message)
        self.assertIn("[REDACTED]", message)

    def test_diagnostic_upload_never_includes_native_chromium_log(self):
        self.assertNotIn("chromium-native.log", diagnostics_upload.DEFAULT_FILES)
        self.assertIn("export-network-events.jsonl", diagnostics_upload.DEFAULT_FILES)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "export"
            folder.mkdir()
            (folder / "chromium-native.log").write_text(
                "raw console payload must stay local", encoding="utf-8"
            )
            archive_path, _ = diagnostics_upload._archive(folder)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertNotIn("chromium-native.log", archive.namelist())

    def test_diagnostic_console_events_do_not_store_message_text(self):
        context = object()
        secret = "Authorization: Bearer do-not-log-this"
        active = {"context": context, "events": []}
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state._diagnostic_page_ids = set()
        page = Mock()
        page.url = "https://onebss.vnpt.vn/"
        state._attach_page_diagnostics(context, page)
        console_handler = next(
            call.args[1] for call in page.on.call_args_list
            if call.args[0] == "console"
        )
        console_handler(SimpleNamespace(type="warning", text=secret))
        self.assertNotIn(secret, json.dumps(active["events"]))
        self.assertNotIn("message", active["events"][0])

    def test_token_expiry_warning_is_sent_once_per_token(self):
        expiry = time.time() + 10 * 60
        state = SimpleNamespace(
            _onebss_expiry_warning_for=None,
        )
        def send_alert(expires_at, *, expiring):
            self.assertTrue(expiring)
            state._onebss_expiry_warning_for = int(expires_at)
        state._send_onebss_session_alert = Mock(side_effect=send_alert)
        app.ATSApp._maybe_warn_onebss_session_expiring(state, expiry)
        app.ATSApp._maybe_warn_onebss_session_expiring(state, expiry)
        state._send_onebss_session_alert.assert_called_once_with(expiry, expiring=True)

    def test_installer_launcher_waits_for_ats_process_before_starting_setup(self):
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "ATS-TXL-Setup.exe"
            installer.touch()
            with patch.object(updater, "can_self_update", return_value=True), patch.object(
                updater.os, "getpid", return_value=24680
            ), patch.object(updater.subprocess, "Popen") as popen:
                launcher = updater.launch_windows_installer(installer)
            script = Path(temporary) / "launch-install-24680.ps1"
            content = script.read_text(encoding="utf-8-sig")
            self.assertIn("Wait-Process -Id $targetProcessId", content)
            self.assertLess(content.index("Wait-Process"), content.index("Start-Process"))
            self.assertIn("24680", content)
            self.assertEqual(launcher, popen.return_value)
            self.assertEqual(popen.call_args.args[0][0], "powershell.exe")

    def test_update_ui_exits_immediately_after_starting_installer(self):
        state = SimpleNamespace(
            progress=SimpleNamespace(stop=Mock()),
            destroy=Mock(),
            write_log=Mock(),
            _finish_update_download_error=Mock(),
        )
        update = SimpleNamespace(version="1.1.13")
        with patch.object(app.updater, "launch_windows_installer") as launch, patch.object(
            app.messagebox, "showinfo"
        ) as showinfo:
            app.ATSApp._install_downloaded_update(state, update, Path("installer.exe"))
        launch.assert_called_once_with(Path("installer.exe"))
        state.destroy.assert_called_once()
        showinfo.assert_not_called()

    def test_deep_diagnostic_can_select_installed_chrome_for_ab_test(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = Mock()
            state = SimpleNamespace(
                browser_backend="",
                deep_diagnostic_mode=True,
                browser_choice=SimpleNamespace(get=lambda: "Google Chrome (mặc định)"),
                write_log=Mock(),
                _browser_launches=[],
                _windows_browser_processes=Mock(return_value=[]),
            )
            with patch.object(app.sys, "platform", "win32"):
                app.ATSApp._launch_browser_context(state, browser, profile=Path(temporary) / "profile")
            options = browser.chromium.launch_persistent_context.call_args.kwargs
            self.assertEqual(options["channel"], "chrome")

    def test_sandbox_ab_switch_is_limited_to_deep_diagnostic_build(self):
        for deep_mode, selected, expected in (
            (True, True, True),
            (True, False, False),
            (False, True, None),
        ):
            with self.subTest(deep_mode=deep_mode, selected=selected), tempfile.TemporaryDirectory() as temporary:
                browser = Mock()
                state = SimpleNamespace(
                    browser_backend="",
                    browser_choice=SimpleNamespace(get=lambda: "Google Chrome (mặc định)"),
                    deep_diagnostic_mode=deep_mode,
                    chromium_sandbox_enabled=SimpleNamespace(get=lambda: selected),
                    write_log=Mock(),
                    _browser_launches=[],
                    _windows_browser_processes=Mock(return_value=[]),
                )
                with patch.object(app.sys, "platform", "win32"):
                    app.ATSApp._launch_browser_context(
                        state, browser, profile=Path(temporary) / "profile"
                    )
                options = browser.chromium.launch_persistent_context.call_args.kwargs
                if expected is None:
                    self.assertNotIn("chromium_sandbox", options)
                else:
                    self.assertIs(options["chromium_sandbox"], expected)


class TestDBlobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = app.sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def _page(self, install=True):
        context = self.browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html><body>Test D</body></html>"
        ))
        page.goto("https://onebss.vnpt.vn/test-d")
        if install:
            self.assertTrue(page.evaluate(app.TEST_D_HOOK_SCRIPT))
            self.assertTrue(page.evaluate("() => window.__atsTxlTestD.arm()"))
            self.assertTrue(page.evaluate("() => window.__atsTxlTestD.markExportClick()"))
        self.addCleanup(context.close)
        return page

    @staticmethod
    def _click_blob(page, mime, filename, mode="programmatic"):
        return page.evaluate("""([mime, filename, mode]) => {
            const blob = new Blob(['test'], {type: mime});
            const anchor = document.createElement('a');
            anchor.href = URL.createObjectURL(blob);
            anchor.download = filename;
            document.body.append(anchor);
            if (mode === 'event') {
                anchor.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            } else {
                anchor.click();
            }
            return window.__atsTxlTestD.metadata();
        }""", [mime, filename, mode])

    def test_test_d_accepts_xlsx_with_excel_octet_stream_or_empty_mime(self):
        for mime in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream", "",
        ):
            with self.subTest(mime=mime):
                page = self._page()
                downloads = []
                page.on("download", lambda item: downloads.append(item))
                result = self._click_blob(page, mime, "export.xlsx")
                page.wait_for_timeout(100)
                self.assertEqual(result["state"], "captured")
                self.assertEqual(result["suppressed_count"], 1)
                self.assertFalse(downloads)

    def test_test_d_does_not_suppress_unrelated_blob(self):
        page = self._page()
        downloads = []
        page.on("download", lambda item: downloads.append(item))
        result = self._click_blob(page, "application/pdf", "other.pdf")
        page.wait_for_timeout(200)
        self.assertEqual(result["state"], "armed")
        self.assertEqual(result["suppressed_count"], 0)
        self.assertEqual(len(downloads), 1)

    def test_test_d_does_not_suppress_contradictory_pdf_mime(self):
        page = self._page()
        downloads = []
        page.on("download", lambda item: downloads.append(item))
        result = self._click_blob(page, "application/pdf", "misnamed.xlsx")
        page.wait_for_timeout(200)
        self.assertEqual(result["state"], "armed")
        self.assertEqual(result["suppressed_count"], 0)
        self.assertEqual(len(downloads), 1)

    def test_test_d_capture_click_event_and_duplicate_click(self):
        page = self._page()
        downloads = []
        page.on("download", lambda item: downloads.append(item))
        result = self._click_blob(page, "", "export.xlsx", mode="event")
        self.assertEqual(result["suppressed_count"], 1)
        page.evaluate("() => document.querySelector('a').click()")
        page.wait_for_timeout(100)
        result = page.evaluate("() => window.__atsTxlTestD.metadata()")
        self.assertEqual(result["suppressed_count"], 2)
        self.assertFalse(result["multiple_candidates"])
        self.assertFalse(downloads)

    def test_test_d_keeps_blob_after_onebss_revokes_url(self):
        page = self._page()
        self._click_blob(page, "", "export.xlsx")
        result = page.evaluate("""async () => {
            URL.revokeObjectURL(document.querySelector('a').href);
            return window.__atsTxlTestD.readChunk(0, 4);
        }""")
        self.assertEqual(base64.b64decode(result), b"test")

    def test_test_d_cleanup_restores_original_browser_functions(self):
        page = self._page(install=False)
        page.evaluate("""() => {
            window.originalCreate = URL.createObjectURL;
            window.originalClick = HTMLAnchorElement.prototype.click;
        }""")
        self.assertTrue(page.evaluate(app.TEST_D_HOOK_SCRIPT))
        page.evaluate("() => window.__atsTxlTestD.cleanup()")
        restored = page.evaluate("""() => ({
            create: URL.createObjectURL === window.originalCreate,
            click: HTMLAnchorElement.prototype.click === window.originalClick,
            stateReleased: window.__atsTxlTestD === undefined
        })""")
        self.assertEqual(restored, {"create": True, "click": True, "stateReleased": True})

    def test_test_d_reconstructs_multiple_chunks_and_validates_workbook(self):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(["value", 123])
        source = io.BytesIO()
        workbook.save(source)
        workbook.close()
        original = source.getvalue()
        page = self._page()
        page.evaluate("""encoded => {
            const binary = atob(encoded);
            const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
            const anchor = document.createElement('a');
            anchor.href = URL.createObjectURL(new Blob([bytes], {type: 'application/octet-stream'}));
            anchor.download = 'report.xlsx';
            document.body.append(anchor);
            anchor.click();
        }""", base64.b64encode(original).decode("ascii"))
        metadata = page.evaluate("() => window.__atsTxlTestD.metadata()")
        state = app.ATSApp.__new__(app.ATSApp)
        state.write_log = Mock()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "DOWNLOADS", Path(temporary)
        ), patch.object(app, "TEST_D_CHUNK_BYTES", 128):
            result = state._transfer_test_d_blob(page, metadata)
            saved = list(Path(temporary).glob("*.xlsx"))
            self.assertEqual(result["state"], "saved")
            self.assertEqual(result["bytes_transferred"], len(original))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), original)
            self.assertFalse(list(Path(temporary).glob("*.partial.xlsx")))

    def test_test_d_size_limit_and_corrupt_xlsx(self):
        state = app.ATSApp.__new__(app.ATSApp)
        page = Mock()
        result = state._transfer_test_d_blob(
            page, {"blob_size": app.MAX_EXCEL_CAPTURE_BYTES + 1}
        )
        self.assertEqual(result["state"], "size-limit")
        page.evaluate.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            bad = Path(temporary) / "bad.xlsx"
            bad.write_bytes(b"PK not a zip")
            with self.assertRaises(zipfile.BadZipFile):
                state._validate_test_d_xlsx(bad)

    def test_test_d_result_conditions(self):
        meta = {"state": "captured", "suppressed_count": 1,
                "multiple_candidates": False, "blob_size": 123}
        transfer = {"state": "saved", "bytes_transferred": 123,
                    "pk": True, "zip": True, "xlsx_structure": True, "openpyxl": True}
        alive = "TEST C RESULT: Chrome remained alive for 30 seconds after Blob download."
        classify = app.ATSApp._classify_test_d
        self.assertIn("PASS", classify(meta, transfer, alive, {}))
        self.assertIn("FAIL", classify(meta, transfer, alive, {"download_event_seen": True}))
        self.assertIn("FAIL", classify(meta, transfer, alive,
                                      {"close_reason": "unexpected-browser-exit"}))
        self.assertIn("FAIL", classify(meta, {"state": "corrupt"}, alive, {}))
        self.assertIn("INCONCLUSIVE", classify({"state": "armed"}, transfer, alive, {}))
        self.assertIn("INCONCLUSIVE", classify(meta, {"state": "size-limit"}, alive, {}))
        self.assertIn("FAIL", classify(meta, {**transfer, "bytes_transferred": 122}, alive, {}))

    def test_test_d_download_listener_does_not_read_download_object(self):
        context = Mock()
        page = Mock()
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = {
            "context": context, "network_armed": True, "test_d_active": True,
            "events": [], "network_events": [], "page_ids": {},
        }
        state._diagnostic_page_ids = set()
        state._attach_page_diagnostics(context, page)
        handler = next(call.args[1] for call in page.on.call_args_list
                       if call.args[0] == "download")
        class ForbiddenDownload:
            @property
            def suggested_filename(self):
                raise AssertionError("Download object accessed")
            @property
            def url(self):
                raise AssertionError("Download object accessed")
        handler(ForbiddenDownload())
        active = state._active_export_diagnostic
        self.assertTrue(active["download_event_seen"])
        self.assertEqual(active["network_events"][-1]["decision"], "fail")

    def test_test_d_full_synthetic_export_bypasses_download_event_and_stays_alive_30s(self):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(["ticket", "test"])
        source = io.BytesIO()
        workbook.save(source)
        workbook.close()
        page = self._page(install=False)
        context = page.context
        page.evaluate("""encoded => {
            const button = document.createElement('button');
            button.textContent = 'Xuất Excel';
            button.onclick = () => {
                const binary = atob(encoded);
                const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
                const anchor = document.createElement('a');
                anchor.href = URL.createObjectURL(new Blob([bytes], {type: 'application/octet-stream'}));
                anchor.download = 'onebss.xlsx';
                anchor.click();
                URL.revokeObjectURL(anchor.href);
            };
            document.body.append(button);
        }""", base64.b64encode(source.getvalue()).decode("ascii"))
        state = app.ATSApp.__new__(app.ATSApp)
        active = {"context": context, "events": [], "network_events": [],
                  "page_ids": {}, "main_browser_process": {"pid": 1}}
        state._active_export_diagnostic = active
        state._diagnostic_page_ids = set()
        state.write_log = Mock()
        state._finish_export_diagnostic = Mock()
        state._attach_page_diagnostics(context, page)
        def observe(_page, _context, observed_active, duration_seconds):
            self.assertEqual(duration_seconds, 30)
            started = time.monotonic()
            while time.monotonic() - started < duration_seconds:
                self.assertTrue(self.browser.is_connected())
                self.assertFalse(page.is_closed())
                self.assertEqual(page.evaluate("() => document.title"), "")
                self.assertFalse(observed_active.get("download_event_seen"))
                page.wait_for_timeout(1000)
            observed_active["close_reason"] = "application-cleanup"
            return "TEST C RESULT: Chrome remained alive for 30 seconds after Blob download."
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "DOWNLOADS", Path(temporary)
        ), patch.object(app.ATSApp, "_observe_test_c", side_effect=observe):
            with self.assertRaises(app.DiagnosticTestCompleted) as completed:
                state._run_export_test_d(page, context)
            self.assertEqual(len(list(Path(temporary).glob("*.xlsx"))), 1)
        self.assertIn("TEST D PASS", str(completed.exception))
        self.assertFalse(active.get("download_event_seen"))
        self.assertEqual(active["test_d"]["validation"], "saved")

    def test_test_d_cleanup_starts_after_observation(self):
        context = Mock()
        page = Mock(url="https://onebss.vnpt.vn/test-d")
        page.is_closed.return_value = False
        sequence = []
        active = {"context": context, "events": [], "network_events": [],
                  "download_event_seen": False, "close_reason": "application-cleanup"}
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state.write_log = Mock(side_effect=lambda message: sequence.append(message))
        state._finish_export_diagnostic = Mock()
        captured = {"state": "captured", "filename": "export.xlsx", "mime_type": "",
                    "blob_size": 123, "suppressed_count": 1, "multiple_candidates": False}
        def evaluate(script):
            if script == app.TEST_D_HOOK_SCRIPT or ".arm()" in script or ".markExportClick()" in script:
                return True
            if ".metadata()" in script:
                return captured
            return None
        page.evaluate.side_effect = evaluate
        with patch.object(app.ATSApp, "_transfer_test_d_blob", return_value={
            "state": "saved", "bytes_transferred": 123,
            "pk": True, "zip": True, "xlsx_structure": True, "openpyxl": True,
        }), patch.object(app.ATSApp, "_observe_test_c", side_effect=lambda *args, **kwargs: (
            sequence.append("observe") or
            "TEST C RESULT: Chrome remained alive for 30 seconds after Blob download."
        )), self.assertRaises(app.DiagnosticTestCompleted):
            context.close.side_effect = lambda: sequence.append("context.close")
            state._run_export_test_d(page, context)
        self.assertLess(sequence.index("observe"), sequence.index("DIAGNOSTIC CLEANUP STARTED"))
        self.assertLess(sequence.index("DIAGNOSTIC CLEANUP STARTED"), sequence.index("context.close"))
        self.assertEqual(active["test_d"]["validation"], "saved")


if __name__ == "__main__":
    unittest.main()
