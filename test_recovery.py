"""Regression tests for Windows browser recovery and OneBSS auth detection."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import diagnostics_upload
import updater


class RecoveryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
