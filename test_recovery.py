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


def fake_onebss_grid_snapshot(total_count=2, row_count=2, page_size=10):
    headers = [mapping[0] for mapping in app.ONEBSS_UI_COLUMN_MAPPING]
    rows = []
    for number in range(1, row_count + 1):
        row = {header: f"FAKE-{header}-{number}" for header in headers}
        row["Mã báo hỏng"] = f"FAKE-ID-{number}"
        row["Số ảo"] = f"FAKE-VIRTUAL-{number}"
        rows.append(row)
    return {
        "total_count": total_count,
        "detected_headers": headers,
        "current_page_size": page_size,
        "current_page": 1,
        "rows": rows,
    }


def fake_onebss_grid_rows(start, count):
    headers = [mapping[0] for mapping in app.ONEBSS_UI_COLUMN_MAPPING]
    rows = []
    for number in range(start, start + count):
        row = {header: f"FAKE-{header}-{number}" for header in headers}
        row["Mã báo hỏng"] = f"FAKE-ID-{number}"
        rows.append(row)
    return rows


class RecoveryTests(unittest.TestCase):
    def test_page_size_uses_grid_footer_range_not_unrelated_pager_number(self):
        footer = (
            "1 2 3 4 ... 12 bản ghi trên trang Tổng cộng 59 bản ghi. "
            "Đang hiển thị bản ghi số 1 đến 10."
        )
        self.assertEqual(app._onebss_page_size_from_footer("12", footer, 59), 10)

    def test_page_size_uses_control_value_when_footer_is_short_only_because_total_is_small(self):
        footer = "Tổng cộng 59 bản ghi. Đang hiển thị bản ghi số 1 đến 59."
        self.assertEqual(app._onebss_page_size_from_footer("100", footer, 59), 100)

    def test_page_size_timeout_metadata_preserves_initial_grid_snapshot(self):
        snapshot = fake_onebss_grid_snapshot(total_count=57, row_count=10)
        page = Mock()

        def evaluate(script, *args):
            if script == app.ONEBSS_GRID_READ_SCRIPT:
                return snapshot
            if script == app.ONEBSS_PAGE_SIZE_STATE_SCRIPT:
                return {
                    "page_size_control_found": True,
                    "page_size_control_value": "12",
                    "initial_page_size_detected": 12,
                    "footer": "Tổng cộng 57 bản ghi. Đang hiển thị bản ghi số 1 đến 10.",
                    "dropdown_opened": False,
                    "options": [],
                }
            if script == app.ONEBSS_GRID_MUTATION_OBSERVER_SCRIPT:
                return False
            self.fail("Unexpected page.evaluate script")

        page.evaluate.side_effect = evaluate
        pager = Mock()
        page_sizes = Mock()
        page_sizes.wait_for.side_effect = TimeoutError("simulated locator timeout")
        dropdown = Mock()
        page_size_collection = Mock()
        page_size_collection.first = page_sizes
        dropdown_collection = Mock()
        dropdown_collection.first = dropdown
        pager.locator.return_value = page_size_collection
        page_sizes.locator.return_value = dropdown_collection
        locator = Mock()
        locator.filter.return_value = locator
        locator.first = pager
        page.locator.return_value = locator
        state = app.ATSApp.__new__(app.ATSApp)

        with self.assertRaises(app.OneBSSGridReadError) as raised:
            state._read_onebss_grid(page, 57)

        metadata = raised.exception.metadata
        self.assertEqual(metadata["timeout_stage"], "wait-page-size-control-visible")
        self.assertEqual(metadata["initial_page_size"], 10)
        self.assertEqual(metadata["initial_page_size_detected"], 10)
        self.assertEqual(metadata["selected_page_size"], 100)
        self.assertEqual(metadata["row_count"], 10)
        self.assertEqual(len(metadata["detected_headers"]), 25)
        self.assertEqual(metadata["unique_ma_bh_count"], 10)
        self.assertNotIn("FAKE-ID-1", json.dumps(metadata, ensure_ascii=False))

    def test_onebss_grid_collects_all_required_total_sizes(self):
        cases = (
            (0, 10, 0), (1, 10, 1), (10, 10, 1), (11, 20, 1),
            (37, 50, 1), (67, 100, 1), (200, 200, 1), (201, 500, 1),
            (500, 500, 1), (501, 1000, 1), (1000, 1000, 1),
            (1001, 2000, 1), (2000, 2000, 1), (2001, 2000, 2),
            (4001, 2000, 3),
        )
        headers = [mapping[0] for mapping in app.ONEBSS_UI_COLUMN_MAPPING]

        for total_count, expected_size, expected_pages in cases:
            with self.subTest(total_count=total_count):
                state = {"page_size": 10, "page": 1}

                def snapshot():
                    start = (state["page"] - 1) * state["page_size"] + 1
                    count = min(state["page_size"], max(total_count - start + 1, 0))
                    return {
                        "detected_headers": headers,
                        "current_page_size": state["page_size"],
                        "current_page": state["page"],
                        "rows": fake_onebss_grid_rows(start, count),
                    }

                initial = snapshot()
                if total_count == 0:
                    initial["rows"] = []
                aggregate = app._collect_onebss_grid_pages(
                    total_count,
                    initial,
                    lambda size: state.update(page_size=size, page=1),
                    snapshot,
                    lambda page_number: state.update(page=page_number),
                )
                parsed = app._parse_onebss_grid_snapshot(aggregate)

                self.assertEqual(aggregate["selected_page_size"], expected_size)
                self.assertEqual(aggregate["pages_read"], expected_pages)
                self.assertEqual(parsed["metadata"]["row_count"], total_count)
                self.assertEqual(parsed["metadata"]["unique_ma_bh_count"], total_count)
                self.assertEqual(parsed["metadata"]["result"], "PASS")

    def test_onebss_total_count_parser_handles_grouping_and_zero(self):
        self.assertEqual(app._parse_onebss_total_count("Tổng cộng 67 bản ghi"), 67)
        self.assertEqual(app._parse_onebss_total_count("Tổng cộng 4.001 bản ghi"), 4001)
        self.assertEqual(app._parse_onebss_total_count("Tổng cộng 0 bản ghi"), 0)
        self.assertIsNone(app._parse_onebss_total_count("Không có thông tin tổng"))

    def test_onebss_grid_reader_maps_all_25_headers(self):
        parsed = app._parse_onebss_grid_snapshot(fake_onebss_grid_snapshot())

        self.assertEqual(len(parsed["ui_records"]), 2)
        self.assertEqual(len(parsed["ui_records"][0]), 25)
        self.assertEqual(len(parsed["canonical_records"]), 2)
        self.assertEqual(parsed["metadata"]["result"], "PASS")
        self.assertEqual(parsed["metadata"]["unique_ma_bh_count"], 2)
        self.assertEqual(parsed["canonical_records"][0]["ma_bh"], "FAKE-ID-1")
        self.assertEqual(
            set(parsed["canonical_records"][0]),
            {
                "tentinh", "ma_tb", "ten_tb", "loaihinh_tb", "ten_dv", "ten_dv_xl",
                "ten_dv_dang_th", "ngay_bh", "sla", "ma_nd", "dienthoai_bh",
                "dienthoai_lh", "trangthai_bh", "ten_nv", "ghichu_hong", "ma_bh",
                "kenh_tn", "diachi_ld", "may_cn", "ngay_cn", "nguoi_cn",
                "ten_quytrinh", "ten_trangthai",
            },
        )

    def test_onebss_grid_reader_resolves_header_aliases_after_reordering(self):
        snapshot = fake_onebss_grid_snapshot()
        aliases = {
            "Đơn vị nhận": "Đơn vị nhân",
            "Đơn vị xử lí": "Đơn vị xử lý",
            "Trạng thái bảo hỏng": "Trạng thái báo hỏng",
        }
        snapshot["detected_headers"] = [
            aliases.get(header, header) for header in reversed(snapshot["detected_headers"])
        ]
        snapshot["rows"] = [
            {aliases.get(header, header): value for header, value in reversed(row.items())}
            for row in snapshot["rows"]
        ]

        parsed = app._parse_onebss_grid_snapshot(snapshot)

        self.assertEqual(parsed["metadata"]["result"], "PASS")
        self.assertEqual(parsed["ui_records"][0]["Đơn vị nhận"], "FAKE-Đơn vị nhận-1")
        self.assertEqual(parsed["ui_records"][0]["Đơn vị xử lí"], "FAKE-Đơn vị xử lí-1")
        self.assertEqual(parsed["ui_records"][0]["Trạng thái bảo hỏng"], "FAKE-Trạng thái bảo hỏng-1")

    def test_onebss_grid_reader_fails_on_missing_header(self):
        snapshot = fake_onebss_grid_snapshot()
        missing = "Tỉnh"
        snapshot["detected_headers"].remove(missing)

        with self.assertRaises(app.OneBSSGridReadError) as raised:
            app._parse_onebss_grid_snapshot(snapshot)

        self.assertEqual(raised.exception.metadata["result"], "FAIL")
        self.assertIn(missing, raised.exception.metadata["missing_headers"])

    def test_onebss_grid_reader_fails_on_duplicate_ma_bh(self):
        snapshot = fake_onebss_grid_snapshot()
        snapshot["rows"][1]["Mã báo hỏng"] = snapshot["rows"][0]["Mã báo hỏng"]

        with self.assertRaises(app.OneBSSGridReadError) as raised:
            app._parse_onebss_grid_snapshot(snapshot)

        self.assertEqual(raised.exception.metadata["duplicate_ma_bh_count"], 1)
        self.assertFalse(raised.exception.metadata["invariants"]["duplicate_ma_bh_count_zero"])

    def test_onebss_grid_reader_fails_on_missing_ma_bh(self):
        snapshot = fake_onebss_grid_snapshot()
        snapshot["rows"][0]["Mã báo hỏng"] = "  "

        with self.assertRaises(app.OneBSSGridReadError) as raised:
            app._parse_onebss_grid_snapshot(snapshot)

        self.assertEqual(raised.exception.metadata["missing_ma_bh_count"], 1)
        self.assertFalse(raised.exception.metadata["invariants"]["missing_ma_bh_count_zero"])

    def test_onebss_grid_reader_fails_when_row_count_differs_from_total(self):
        snapshot = fake_onebss_grid_snapshot(total_count=3)

        with self.assertRaises(app.OneBSSGridReadError) as raised:
            app._parse_onebss_grid_snapshot(snapshot)

        self.assertFalse(raised.exception.metadata["invariants"]["row_count_matches_total"])

    def test_onebss_grid_reader_fails_when_unique_key_count_differs_from_total(self):
        snapshot = fake_onebss_grid_snapshot()
        snapshot["rows"][1]["Mã báo hỏng"] = snapshot["rows"][0]["Mã báo hỏng"]

        with self.assertRaises(app.OneBSSGridReadError) as raised:
            app._parse_onebss_grid_snapshot(snapshot)

        self.assertFalse(raised.exception.metadata["invariants"]["unique_ma_bh_matches_total"])

    def test_onebss_grid_reader_preserves_both_person_values_without_overwrite(self):
        snapshot = fake_onebss_grid_snapshot()
        snapshot["rows"][0]["Người báo hỏng"] = "FAKE-REPORTER"
        snapshot["rows"][0]["Người cập nhật"] = "FAKE-UPDATER"

        parsed = app._parse_onebss_grid_snapshot(snapshot)

        ui_record = parsed["ui_records"][0]
        canonical = parsed["canonical_records"][0]
        self.assertEqual(ui_record["Người báo hỏng"], "FAKE-REPORTER")
        self.assertEqual(ui_record["Người cập nhật"], "FAKE-UPDATER")
        self.assertEqual(canonical["nguoi_cn"], "FAKE-UPDATER")
        self.assertEqual(
            parsed["metadata"]["mapping_status"][10]["status"], "ambiguous"
        )

    def test_onebss_grid_reader_preserves_unknown_so_ao_only_in_ui_record(self):
        parsed = app._parse_onebss_grid_snapshot(fake_onebss_grid_snapshot())

        self.assertEqual(parsed["ui_records"][0]["Số ảo"], "FAKE-VIRTUAL-1")
        self.assertNotIn("Số ảo", parsed["canonical_records"][0])
        self.assertEqual(
            parsed["metadata"]["mapping_status"][16]["status"], "unknown"
        )

    def test_onebss_grid_diagnostic_metadata_never_contains_row_values(self):
        parsed = app._parse_onebss_grid_snapshot(fake_onebss_grid_snapshot())
        serialized = json.dumps(parsed["metadata"], ensure_ascii=False)

        self.assertNotIn("FAKE-ID-1", serialized)
        self.assertNotIn("FAKE-VIRTUAL-1", serialized)
        self.assertEqual(parsed["metadata"]["result"], "PASS")

    def test_onebss_grid_diagnostic_file_and_log_contain_metadata_only(self):
        state = app.ATSApp.__new__(app.ATSApp)
        state.write_log = Mock()
        state._last_diagnostic_path = None
        page = Mock()
        page.evaluate.return_value = fake_onebss_grid_snapshot()

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "APP_DATA", Path(temporary)
        ):
            with self.assertRaises(app.DiagnosticTestCompleted):
                state._run_onebss_grid_diagnostic(page, 2)
            report = state._last_diagnostic_path / "onebss-grid-diagnostic.json"
            serialized = report.read_text(encoding="utf-8")

        self.assertNotIn("FAKE-ID-1", serialized)
        self.assertNotIn("FAKE-VIRTUAL-1", serialized)
        self.assertNotIn("FAKE-Đơn vị nhận-1", serialized)
        log_text = " ".join(str(call.args[0]) for call in state.write_log.call_args_list)
        self.assertNotIn("FAKE-ID-1", log_text)
        self.assertNotIn("FAKE-VIRTUAL-1", log_text)
        self.assertIn('"result": "PASS"', serialized)

    def test_grid_probe_sanitizer_keeps_only_schema_metadata(self):
        customer_value = "CUSTOMER-ROW-VALUE-MUST-NOT-LEAK"
        raw = {
            "total_count": 60,
            "current_page_size": 500,
            "dom_row_count": 60,
            "headers": ["Tỉnh", "Mã thuê bao"],
            "selector_structure": {
                "grid_root": {"selector_hint": ".e-grid", "tag": "div", "classes": ["e-grid"]},
                "rows": {"selector_hint": "tbody tr", "tag": "table", "classes": [], "row_classes": ["e-row"]},
                "page_size": {"selector_hint": "pager input", "tag": "input", "classes": ["e-dropdownlist"]},
                "pagination": {"selector_hint": ".e-pager", "tag": "div", "classes": ["e-pager"]},
                "virtualization_markers": [],
            },
            "grid_component_type": "Grid",
            "column_field_header_mapping": [
                {"field": "ma_tb", "headerText": "Mã thuê bao"},
            ],
            "raw_first_row_keys": ["ma_tb", "ten_dv"],
            "ats_field_presence": {"ma_tb": True, "ten_dv": True},
            "virtualization_state": "inconclusive",
            "row_values": {"ma_tb": customer_value},
        }

        sanitized = app._sanitize_grid_probe_result(raw)
        serialized = json.dumps(sanitized, ensure_ascii=False)

        self.assertNotIn(customer_value, serialized)
        self.assertEqual(sanitized["headers"], ["Tỉnh", "Mã thuê bao"])
        self.assertEqual(sanitized["raw_first_row_keys"], ["ma_tb", "ten_dv"])
        self.assertEqual(sanitized["ats_field_presence"]["ten_dv"], True)
        self.assertEqual(
            set(sanitized),
            {
                "total_count", "current_page_size", "dom_row_count", "headers",
                "selector_structure", "grid_component_type", "column_field_header_mapping",
                "raw_first_row_keys", "ats_field_presence", "virtualization_state",
            },
        )

    def test_grid_probe_file_never_persists_row_values(self):
        customer_value = "PRIVATE-CUSTOMER-ROW-VALUE"
        state = app.ATSApp.__new__(app.ATSApp)
        state.write_log = Mock()
        state._last_diagnostic_path = None
        state.deep_diagnostic_mode = True
        state._active_export_diagnostic = None
        page = Mock()
        page.evaluate.return_value = {
            "total_count": 1,
            "current_page_size": 10,
            "dom_row_count": 1,
            "headers": ["Mã thuê bao"],
            "selector_structure": {},
            "grid_component_type": "Grid",
            "column_field_header_mapping": [],
            "raw_first_row_keys": ["ma_tb"],
            "ats_field_presence": {"ma_tb": True},
            "virtualization_state": "inconclusive",
            "row_values": {"ma_tb": customer_value},
        }

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "APP_DATA", Path(temporary)
        ):
            with self.assertRaises(app.DiagnosticTestCompleted):
                state._probe_result_grid(page)
            report = state._last_diagnostic_path / "grid-probe.json"
            serialized = report.read_text(encoding="utf-8")

        self.assertNotIn(customer_value, serialized)
        self.assertEqual(page.evaluate.call_args.args[0], app.GRID_PROBE_SCRIPT)
        self.assertTrue(all(field in serialized for field in app.GRID_PROBE_FIELDS))

    def test_deep_diagnostic_grid_reader_stops_before_old_probe_test_d_and_export(self):
        state = app.ATSApp.__new__(app.ATSApp)
        state.deep_diagnostic_mode = True
        state._last_diagnostic_path = None
        state._ensure_onebss_session_active = Mock()
        state._wait_for_search_complete = Mock()
        state._wait_for_search_complete.return_value = 2
        state._run_onebss_grid_diagnostic = Mock(
            side_effect=app.DiagnosticTestCompleted("probe complete")
        )
        state._probe_result_grid = Mock()
        state._run_export_test_d = Mock()
        state._finish_export_diagnostic = Mock()
        state.write_log = Mock()
        page = Mock()
        page.is_closed.return_value = False
        context = object()

        with self.assertRaises(app.DiagnosticTestCompleted):
            app.ATSApp._export_excel(state, page, context)

        page.get_by_text.assert_called_once_with("Tìm kiếm", exact=True)
        state._run_onebss_grid_diagnostic.assert_called_once_with(page, 2)
        state._probe_result_grid.assert_not_called()
        state._run_export_test_d.assert_not_called()
        state._finish_export_diagnostic.assert_not_called()

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

    def _diagnostic_state(self, page):
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = {
            "id": "test-d", "context": page.context, "events": [], "network_events": [],
            "page_ids": {}, "main_browser_process": {"pid": 1},
        }
        state._diagnostic_page_ids = set()
        state.write_log = Mock()
        state._finish_export_diagnostic = Mock()
        state._attach_page_diagnostics(page.context, page)
        return state

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
        self.assertIn("INCONCLUSIVE", classify(meta, transfer, alive, {"download_event_seen": True}))
        self.assertIn("FAIL", classify(meta, transfer, alive, {
            "download_event_seen": True, "test_d_main_frame_id": "frame-1",
            "network_events": [{"event_type": "CREATE_OBJECT_URL", "frame_id": "frame-1"},
                               {"event_type": "ANCHOR_CLICK", "frame_id": "frame-1"}],
        }))
        self.assertIn("INCONCLUSIVE", classify(meta, transfer, alive,
                                              {"close_reason": "unexpected-browser-exit"}))
        self.assertIn("FAIL", classify(meta, transfer, alive, {
            "close_reason": "unexpected-browser-exit",
            "network_events": [{"event_type": "CREATE_OBJECT_URL", "frame_id": "frame-1"}],
        }))
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
        active = {"id": "synthetic", "context": context, "events": [], "network_events": [],
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
        ), patch.object(app, "APP_DATA", Path(temporary)), patch.object(
            app.ATSApp, "_observe_test_c", side_effect=observe
        ):
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
        frame = Mock(url=page.url, parent_frame=None)
        frame.is_detached.return_value = False
        page.main_frame = frame
        page.frames = [frame]
        page.get_by_text.return_value.count.return_value = 1
        sequence = []
        active = {"id": "cleanup", "context": context, "events": [], "network_events": [],
                  "download_event_seen": False, "close_reason": "application-cleanup"}
        state = app.ATSApp.__new__(app.ATSApp)
        state._active_export_diagnostic = active
        state.write_log = Mock(side_effect=lambda message: sequence.append(message))
        state._finish_export_diagnostic = Mock()
        captured = {"state": "captured", "filename": "export.xlsx", "mime_type": "",
                    "blob_size": 123, "suppressed_count": 1, "multiple_candidates": False}
        hook_state = {"armed": False, "clicked": False}
        page.get_by_text.return_value.click.side_effect = lambda **kwargs: hook_state.update(clicked=True)
        def evaluate(script, *args):
            if script == "() => location.origin":
                return "https://onebss.vnpt.vn"
            if script == app.TEST_D_HOOK_SCRIPT:
                return True
            if ".arm()" in script:
                hook_state["armed"] = True
                return True
            if ".markExportClick()" in script:
                return True
            if ".verify()" in script:
                return {"version": app.TEST_D_HOOK_VERSION, "frame_id": "frame-1",
                        "document_id": "document-mock", "origin": "https://onebss.vnpt.vn",
                        "observe_only": False,
                        "create_wrapped": True, "click_wrapped": True,
                        "listener_installed": True, "blob_map_exists": True,
                        "armed": hook_state["armed"]}
            if ".metadata()" in script:
                return captured if hook_state["clicked"] else {**captured, "state": "armed"}
            return None
        frame.evaluate.side_effect = evaluate
        page.evaluate.side_effect = evaluate
        with patch.object(app.ATSApp, "_transfer_test_d_blob", return_value={
            "state": "saved", "bytes_transferred": 123,
            "pk": True, "zip": True, "xlsx_structure": True, "openpyxl": True,
        }), patch.object(app.ATSApp, "_observe_test_c", side_effect=lambda *args, **kwargs: (
            sequence.append("observe") or
            "TEST C RESULT: Chrome remained alive for 30 seconds after Blob download."
        )), tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "APP_DATA", Path(temporary)
        ), self.assertRaises(app.DiagnosticTestCompleted):
            context.close.side_effect = lambda: sequence.append("context.close")
            state._run_export_test_d(page, context)
        self.assertLess(sequence.index("observe"), sequence.index("DIAGNOSTIC CLEANUP STARTED"))
        self.assertLess(sequence.index("DIAGNOSTIC CLEANUP STARTED"), sequence.index("context.close"))
        self.assertEqual(active["test_d"]["validation"], "saved")

    def test_test_d_handshake_pass_and_fail_gate_export_click(self):
        for verified in (True, False):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as temporary:
                page = self._page(install=False)
                page.evaluate("""() => {
                    const button = document.createElement('button');
                    button.textContent = 'Xuất Excel';
                    button.onclick = () => {
                        console.log('test-d-button-clicked');
                        const a = document.createElement('a');
                        a.href = URL.createObjectURL(new Blob(['test'], {type: 'application/pdf'}));
                        a.download = 'other.pdf';
                        a.click();
                    };
                    document.body.append(button);
                }""")
                clicked = []
                page.on("console", lambda message: clicked.append(message.text)
                        if message.text == "test-d-button-clicked" else None)
                state = self._diagnostic_state(page)
                with patch.object(app, "APP_DATA", Path(temporary)), patch.object(
                    app, "TEST_D_HOOK_VERSION", app.TEST_D_HOOK_VERSION if verified else "wrong-version"
                ), patch.object(app.ATSApp, "_observe_test_c", return_value="observation-finished"):
                    with self.assertRaises(app.DiagnosticTestCompleted):
                        state._run_export_test_d(page, page.context)
                active = state._active_export_diagnostic
                snapshot = active["test_d"]["pre_click_snapshot"]
                self.assertEqual(snapshot["hook_verified"], verified)
                self.assertEqual(bool(clicked), verified)
                folder = active["test_d_evidence_dir"]
                self.assertTrue((folder / "summary.json").exists())
                self.assertIn("pre_click_snapshot", (folder / "summary.json").read_text())
                if not verified:
                    self.assertIn("INCONCLUSIVE", str(state.write_log.call_args_list))

    def test_test_d_reason_codes_and_trigger_events(self):
        page = self._page(install=False)
        received = []
        page.context.expose_binding("__atsTxlTestDEvent", lambda source, event: received.append(event))
        self.assertTrue(page.evaluate(app.TEST_D_HOOK_SCRIPT, {"frameId": "frame-1"}))
        self.assertTrue(page.evaluate("() => window.__atsTxlTestD.arm()"))
        self.assertTrue(page.evaluate("() => window.__atsTxlTestD.markExportClick()"))
        self._click_blob(page, "application/pdf", "misnamed.xlsx")
        self._click_blob(page, "application/octet-stream", "other.pdf")
        self._click_blob(page, "", "valid.xlsx")
        page.evaluate("""() => document.querySelectorAll('a')[2].dispatchEvent(
            new MouseEvent('click', {bubbles: true, cancelable: true}))""")
        page.evaluate("() => URL.revokeObjectURL(document.querySelectorAll('a')[2].href)")
        page.wait_for_timeout(200)
        types = [event["event_type"] for event in received]
        self.assertIn("CREATE_OBJECT_URL", types)
        self.assertIn("ANCHOR_CLICK", types)
        self.assertIn("CAPTURE_CLICK_EVENT", types)
        self.assertIn("REVOKE_OBJECT_URL", types)
        decisions = [event for event in received if event["event_type"] == "CANDIDATE EVALUATED"]
        self.assertIn("MIME_REJECTED", [event["reason_code"] for event in decisions])
        self.assertIn("FILENAME_NOT_XLSX", [event["reason_code"] for event in decisions])
        self.assertTrue(any(event["decision"] == "SUPPRESS" for event in decisions))
        self.assertTrue(any(event["decision"] == "SUPPRESS" and
                            event["mime_category"] == "empty" for event in decisions))
        self.assertFalse(any("url" in event or "filename" in event for event in received))

    def test_test_d_frame_inventory_and_secondary_observation(self):
        page = self._page(install=False)
        page.evaluate("""() => {
            const frame = document.createElement('iframe');
            frame.src = '/test-d-frame?access_token=never-log-this';
            document.body.append(frame);
        }""")
        page.frame_locator("iframe").locator("body").wait_for()
        state = self._diagnostic_state(page)
        active = state._active_export_diagnostic
        active["network_armed"] = True
        active["test_d_active"] = True
        with tempfile.TemporaryDirectory() as temporary:
            active["test_d_evidence_dir"] = Path(temporary)
            page.context.expose_binding(
                "__atsTxlTestDEvent",
                lambda source, event: state._test_d_js_event(page.context, source, event),
            )
            frames, inventory = state._test_d_inventory_frames(page, page.context, active)
            self.assertEqual(len(frames), 2)
            self.assertEqual(inventory[1]["parent_frame_id"], inventory[0]["frame_id"])
            self.assertFalse(inventory[1]["main_frame"])
            self.assertNotIn("never-log-this", str(inventory))
            self.assertTrue(frames[1].evaluate(app.TEST_D_HOOK_SCRIPT, {
                "frameId": inventory[1]["frame_id"], "observeOnly": True,
            }))
            frames[1].evaluate("() => window.__atsTxlTestD.arm()")
            frames[1].evaluate("() => window.__atsTxlTestD.markExportClick()")
            frames[1].evaluate("""() => {
                const a = document.createElement('a');
                a.href = URL.createObjectURL(new Blob(['test'], {type: 'application/octet-stream'}));
                a.download = 'test.xlsx';
                a.click();
            }""")
            page.wait_for_timeout(150)
            events = active["network_events"]
            self.assertTrue(any(event["event_type"] == "CREATE_OBJECT_URL"
                                and event["frame_id"] == inventory[1]["frame_id"] for event in events))
            self.assertTrue(any(event.get("reason_code") == "OBSERVE_ONLY_FRAME" for event in events))
            self.assertTrue((Path(temporary) / "export-network-events.jsonl").exists())

    def test_test_d_binding_rejects_sensitive_page_payload(self):
        state = app.ATSApp.__new__(app.ATSApp)
        context = Mock()
        frame = Mock()
        state._active_export_diagnostic = {
            "context": context, "network_armed": True, "network_events": [],
            "test_d_frame_ids": {id(frame): "frame-1"},
            "test_d_frame_inventory": [{"frame_id": "frame-1", "document_id": "document-safe"}],
        }
        state._test_d_js_event(context, {"frame": frame}, {
            "event_type": "CANDIDATE EVALUATED", "reason_code": "Bearer secret",
            "filename_extension": ".token-secret", "document_id": "access_token=secret",
            "url": "https://onebss.vnpt.vn/?Cookie=secret", "blob_content": "password=secret",
            "decision": "ALLOW", "blob_size": 12,
        })
        encoded = json.dumps(state._active_export_diagnostic["network_events"])
        self.assertNotIn("secret", encoded)
        self.assertIn("document-safe", encoded)

    def test_test_d_immediate_download_preserves_preclick_snapshot_and_events(self):
        page = self._page(install=False)
        page.evaluate("""() => {
            const button = document.createElement('button');
            button.textContent = 'Xuất Excel';
            button.onclick = () => {
                const a = document.createElement('a');
                a.href = URL.createObjectURL(new Blob(['test'], {type: 'application/pdf'}));
                a.download = 'other.pdf';
                a.click();
            };
            document.body.append(button);
        }""")
        state = self._diagnostic_state(page)
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "APP_DATA", Path(temporary)
        ), patch.object(app.ATSApp, "_observe_test_c", return_value="observation-finished"):
            with self.assertRaises(app.DiagnosticTestCompleted):
                state._run_export_test_d(page, page.context)
            active = state._active_export_diagnostic
            self.assertTrue(active["download_event_seen"])
            self.assertTrue(active["test_d"]["pre_click_snapshot"]["hook_verified"])
            self.assertNotEqual(active["test_d"]["state"], "not-installed")
            events = (active["test_d_evidence_dir"] / "export-network-events.jsonl").read_text()
            self.assertIn("PRE-CLICK SNAPSHOT PERSISTED", events)
            self.assertIn("TEST D DOWNLOAD EVENT DETECTED", events)
            self.assertIn("CREATE_OBJECT_URL", events)
            self.assertIn("FILENAME_NOT_XLSX", events)

    def test_test_d_immediate_page_close_preserves_preclick_snapshot(self):
        page = self._page(install=False)
        page.evaluate("""() => {
            const button = document.createElement('button');
            button.textContent = 'Xuất Excel';
            document.body.append(button);
        }""")
        state = self._diagnostic_state(page)
        real_locator = page.get_by_text("Xuất Excel", exact=True)
        locator = Mock()
        locator.count.return_value = 1
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app, "APP_DATA", Path(temporary)
        ), patch.object(app.ATSApp, "_observe_test_c", return_value="observation-finished"), patch.object(
            page, "get_by_text", return_value=locator
        ):
            def close_after_click(**kwargs):
                folder = state._active_export_diagnostic["test_d_evidence_dir"]
                self.assertTrue((folder / "summary.json").exists())
                real_locator.click(**kwargs)
                page.close()
            locator.click.side_effect = close_after_click
            with self.assertRaises(app.DiagnosticTestCompleted):
                state._run_export_test_d(page, page.context)
            active = state._active_export_diagnostic
            self.assertTrue(active["test_d"]["pre_click_snapshot"]["hook_verified"])
            self.assertTrue(active["page_closed"])
            self.assertIn("PRE-CLICK SNAPSHOT PERSISTED",
                          (active["test_d_evidence_dir"] / "export-network-events.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
