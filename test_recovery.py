"""Regression tests for Windows browser recovery and OneBSS auth detection."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


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
                    localStorage.setItem('auth', encode({alg:'none'}) + '.' +
                        encode({exp:expiry,client_id:'onebss',scope:['read']}) + '.signature');
                }"""
                page.evaluate(put_token, int(time.time()) - 60)
                self.assertTrue(app.ATSApp._onebss_session_expired(None, page))
                page.evaluate(put_token, int(time.time()) + 3600)
                self.assertFalse(app.ATSApp._onebss_session_expired(None, page))
            finally:
                browser.close()

    def test_windows_uses_bundled_chromium_and_separate_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = Mock()
            state = SimpleNamespace(browser_backend="")
            profile = Path(temporary) / "chrome-profile"
            with patch.object(app.sys, "platform", "win32"), patch.dict(app.os.environ, {"ATS_BROWSER_CHANNEL": ""}):
                app.ATSApp._launch_browser_context(state, browser, profile=profile)
                default_profile = browser.chromium.launch_persistent_context.call_args.args[0]
                default_options = browser.chromium.launch_persistent_context.call_args.kwargs
                self.assertEqual(state.browser_backend, "playwright-chromium")
                self.assertEqual(Path(default_profile).name, "chrome-profile-playwright-chromium")
                self.assertNotIn("channel", default_options)

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
            write_log=Mock(),
            _launch_browser_context=Mock(return_value=new_context),
            _ensure_onebss_session_active=Mock(),
            _navigate_onebss=Mock(),
        )
        result = app.ATSApp._recover_closed_browser(state, Mock(), old_context, 2, 1)
        self.assertEqual(result, (new_context, new_page, 1))
        self.assertIsNone(state._launch_browser_context.call_args.kwargs["browser_channel"])
        old_context.close.assert_called_once()

    def test_recovery_does_not_retry_when_otp_is_needed(self):
        old_context = Mock()
        new_context = Mock(pages=[Mock()])
        state = SimpleNamespace(
            browser_backend="playwright-chromium",
            write_log=Mock(),
            _launch_browser_context=Mock(return_value=new_context),
            _ensure_onebss_session_active=Mock(side_effect=app.OneBSSSessionExpiredError("expired")),
            _navigate_onebss=Mock(),
        )
        with self.assertRaises(app.OneBSSSessionExpiredError):
            app.ATSApp._recover_closed_browser(state, Mock(), old_context, 2, 1)
        state._launch_browser_context.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
