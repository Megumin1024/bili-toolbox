# -*- coding: utf-8 -*-
"""任务历史、隐私白名单和 TaskPage 状态接入测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.task_page import TaskPage
from core import diagnostics, task_history
from tools.collector.page import CollectorPage
from tools.comments.page import CommentsPage


class _Field:
    def __init__(self, value=""):
        self.value = value

    def setText(self, value):
        self.value = value

    def setPlainText(self, value):
        self.value = value

    def set_value(self, value):
        self.value = str(value)

    def setChecked(self, value):
        self.value = bool(value)

    def setFocus(self):
        pass


class _Progress:
    def __init__(self):
        self.label = ""

    def set_success(self, text):
        self.label = text

    def set_warning(self, text):
        self.label = text

    def set_error(self, text):
        self.label = text

    def set_busy(self, text):
        self.label = text


class _Button:
    def setEnabled(self, _value):
        pass


class _ResultCard:
    def hide(self):
        pass


class _Log:
    def __init__(self):
        self.entries = []

    def append(self, text, level=None):
        self.entries.append((text, level))

    def clear(self):
        self.entries.clear()


class _HistoryPage:
    tool_title = "测试任务"
    tool_module = "测试模块"
    history_enabled = True
    history_tool_id = "test"

    def __init__(self):
        self.runner = None
        self._history_record_id = "record-id"
        self._cancel_requested = False
        self.progress_block = _Progress()
        self.btn_start = _Button()
        self.btn_cancel = _Button()
        self.result_card = _ResultCard()
        self.log_panel = _Log()
        self.finished_result = None

    def history_output_paths(self, result):
        return result.get("outputs", ["result.xlsx"])

    def history_target_summary(self, _params):
        return "测试目标"

    def history_reusable_params(self, params):
        return {"out_dir": params.get("out_dir", "")}

    def on_finished(self, result):
        self.finished_result = result

    def _on_progress(self, _kw):
        pass

    def _on_finished_ok(self, result):
        TaskPage._on_finished_ok(self, result)

    def _on_thread_finished(self):
        pass

    def _on_cancelled(self, result=None):
        TaskPage._on_cancelled(self, result)

    def _on_interrupted(self, result=None):
        TaskPage._on_interrupted(self, result)

    def _on_failed(self, msg):
        TaskPage._on_failed(self, msg)


class TaskHistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.history_file = Path(self.temp.name) / "task_history.json"
        self.patch_file = patch.object(task_history, "HISTORY_FILE", self.history_file)
        self.patch_file.start()

    def tearDown(self):
        self.patch_file.stop()
        self.temp.cleanup()

    def test_create_update_read_and_limit(self):
        started = "2026-09-08T12:00:00+08:00"
        created = task_history.create_record(
            "comments", "评论抓取", started_at=started,
            target_summary="BV1Demo", output_dir="D:\\export",
            reusable_params={"url": "BV1Demo", "sleep": 0.2},
        )
        self.assertEqual(created.record["status"], "running")
        self.assertTrue(created.persisted)
        self.assertTrue(task_history.update_record(
            created.record["id"], "completed",
            outputs=["D:\\export\\report.xlsx"],
        ))
        loaded = task_history.load_history()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["status"], "completed")
        self.assertEqual(loaded[0]["outputs"], ["D:\\export\\report.xlsx"])
        self.assertIsNotNone(loaded[0]["finished_at"])

        for index in range(105):
            task_history.create_record(
                "collector", "视频采集",
                started_at=f"2026-09-08T12:{index // 60:02d}:{index % 60:02d}+08:00",
                target_summary=f"BV{index}", output_dir="D:\\export",
                reusable_params={"sources": [f"BV{index}"]},
            )
        loaded = task_history.load_history()
        self.assertEqual(len(loaded), 100)
        self.assertEqual(loaded[0]["target_summary"], "BV104")

    def test_all_terminal_statuses_and_incomplete_running_record(self):
        ids = {}
        for status in ("completed", "failed", "cancelled", "interrupted"):
            result = task_history.create_record(
                status, status, target_summary=status,
                output_dir="D:\\export", reusable_params={"mode": status},
            )
            ids[status] = result.record["id"]
            error = {"details": "error"} if status == "failed" else None
            self.assertTrue(task_history.update_record(ids[status], status, error=error))
        task_history.create_record("running", "running", target_summary="running")
        records = {record["status"]: record for record in task_history.load_history()}
        for status in ("completed", "failed", "cancelled", "interrupted"):
            self.assertEqual(records[status]["status"], status)
            self.assertIsNotNone(records[status]["finished_at"])
        self.assertEqual(records["failed"]["error"]["details"], "error")
        self.assertIsNone(records["cancelled"]["error"])
        self.assertIsNone(records["interrupted"]["error"])
        self.assertIsNone(records["running"]["finished_at"])

    def test_corrupt_or_incomplete_file_is_ignored(self):
        self.history_file.write_text("{broken", encoding="utf-8")
        self.assertEqual(task_history.load_history(), [])
        self.history_file.write_text(json.dumps({
            "schema_version": task_history.SCHEMA_VERSION,
            "records": [{"id": "missing-fields"}],
        }), encoding="utf-8")
        self.assertEqual(task_history.load_history(), [])
        self.history_file.write_text(json.dumps({
            "schema_version": 999, "records": [],
        }), encoding="utf-8")
        self.assertEqual(task_history.load_history(), [])

    def test_write_failure_is_reported_without_raising(self):
        with patch.object(task_history, "save_history", return_value=False):
            result = task_history.create_record(
                "comments", "评论抓取", target_summary="BV1"
            )
        self.assertFalse(result.persisted)
        self.assertEqual(result.record["status"], "running")

    def test_privacy_filter_rejects_credentials_and_disk_raw_has_no_secret(self):
        params = {
            "url": "https://user:proxy-pass@www.bilibili.com/video/BV1?token=url-token",
            "Cookie": "cookie-secret",
            "proxy_password": "proxy-secret",
        }
        safe, reusable = task_history.prepare_reusable_params(params)
        self.assertFalse(reusable)
        self.assertEqual(safe, {})
        result = task_history.create_record(
            "comments", "评论抓取",
            target_summary="https://user:proxy-pass@www.bilibili.com/video/BV1?token=url-token",
            reusable_params=params,
        )
        raw = self.history_file.read_text(encoding="utf-8")
        for secret in ("proxy-pass", "url-token", "cookie-secret", "proxy-secret"):
            self.assertNotIn(secret, raw)
        self.assertFalse(result.record["reusable"])
        self.assertEqual(result.record["reusable_params"], {})

    def test_disk_boundary_filters_nested_secrets_and_keeps_safe_paths(self):
        secrets = (
            "cookie-in-url", "bearer-in-source", "bare-access-token",
            "nested-password", "directory-secret", "output-secret",
        )
        params = {
            "url": "https://www.bilibili.com/video/BV1?Cookie=cookie-in-url",
            "sources": ["Authorization: Bearer bearer-in-source"],
            "nested": [{"value": "access_token=bare-access-token"},
                        {"password": "nested-password"}],
        }
        result = task_history.create_record(
            "collector", "视频采集", target_summary="BV1",
            output_dir="D:\\export\\access_token=directory-secret",
            reusable_params=params,
        )
        self.assertFalse(result.record["reusable"])
        self.assertEqual(result.record["reusable_params"], {})
        self.assertEqual(result.record["output_dir"], "")
        self.assertTrue(task_history.update_record(
            result.record["id"], "cancelled",
            outputs=["D:\\export\\report.xlsx",
                     "D:\\export\\secret=output-secret.jsonl"],
        ))
        raw = self.history_file.read_text(encoding="utf-8")
        for secret in secrets:
            self.assertNotIn(secret, raw)
        loaded = task_history.load_history()[0]
        self.assertEqual(loaded["outputs"], ["D:\\export\\report.xlsx"])

    def test_safe_urls_ids_and_windows_paths_remain_reusable(self):
        params = {
            "url": "https://www.bilibili.com/video/BV1Demo?fid=123&sid=456",
            "sources": [
                "BV1Demo", "av2", "https://www.bilibili.com/video/av3?fid=789",
                "D:\\lists\\sources.txt",
            ],
            "nested": {"path": "D:\\export\\normal.xlsx"},
        }
        safe, reusable = task_history.prepare_reusable_params(params)
        self.assertTrue(reusable)
        self.assertEqual(safe, params)
        result = task_history.create_record(
            "collector", "视频采集", target_summary="BV1Demo",
            output_dir="D:\\export", reusable_params=params,
        )
        self.assertTrue(result.record["reusable"])
        self.assertEqual(task_history.load_history()[0]["reusable_params"], params)

    def test_sensitive_text_detector_covers_supported_forms(self):
        values = (
            "Cookie=secret-cookie",
            '{"Cookie": "secret-json"}',
            "SESSDATA=secret-sessdata",
            "bili_jct=secret-jct",
            "Authorization: Bearer secret-auth",
            "token=secret-token",
            "access_token=secret-access",
            "refresh_token=secret-refresh",
            "csrf=secret-csrf",
            "password=secret-password",
            "passwd=secret-passwd",
            "secret=secret-value",
            "api_key=secret-api-key",
            "proxy_account=secret-account",
            "proxy_password=secret-proxy-password",
            "https://proxy-user:proxy-password@example.com/",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(task_history.contains_sensitive_text(value))

        self.assertTrue(task_history.contains_sensitive_text({
            "proxy": {"username": "secret-user"},
        }))

    def test_standalone_bearer_tokens_are_removed_from_every_disk_field(self):
        secrets = (
            "standalone-bearer-secret", "lowercase-bearer-secret",
            "tab-bearer-secret", "error-bearer-secret",
            "output-dir-bearer-secret", "output-path-bearer-secret",
            "bypass-bearer-secret",
        )
        params_record = task_history.create_record(
            "comments", "评论抓取", target_summary="BV1",
            reusable_params={"label": "Bearer standalone-bearer-secret"},
        )
        self.assertFalse(params_record.record["reusable"])
        nested_record = task_history.create_record(
            "collector", "视频采集", target_summary="BV2",
            reusable_params={"sources": [["bearer lowercase-bearer-secret"]]},
        )
        self.assertFalse(nested_record.record["reusable"])
        target = task_history.create_record(
            "comments", "评论抓取", target_summary="Bearer\ttab-bearer-secret",
            reusable_params={"url": "BV3"},
        )
        error = task_history.create_record(
            "comments", "评论抓取", target_summary="BV4",
            reusable_params={"url": "BV4"},
        )
        task_history.update_record(
            error.record["id"], "failed",
            error={"details": "Bearer error-bearer-secret"},
        )
        task_history.create_record(
            "collector", "视频采集", target_summary="BV5",
            output_dir="D:\\export\\bearer output-dir-bearer-secret",
            reusable_params={"sources": ["BV5"]},
        )
        output = task_history.create_record(
            "collector", "视频采集", target_summary="BV6",
            reusable_params={"sources": ["BV6"]},
        )
        task_history.update_record(
            output.record["id"], "completed",
            outputs=[
                "D:\\export\\Bearer output-path-bearer-secret.xlsx",
                "D:\\export\\safe.xlsx",
            ],
        )
        raw = self.history_file.read_text(encoding="utf-8")
        for secret in secrets[:-1]:
            self.assertNotIn(secret, raw)

        bypass = task_history.create_record(
            "comments", "评论抓取", target_summary="BV7",
            reusable_params={"url": "BV7"},
        )
        bypass_record = task_history.load_history()[0]
        bypass_record["id"] = bypass.record["id"]
        bypass_record["reusable"] = True
        bypass_record["reusable_params"] = {"value": "bearer bypass-bearer-secret"}
        bypass_record["target_summary"] = "Bearer bypass-bearer-secret"
        bypass_record["output_dir"] = "D:\\export\\Bearer bypass-bearer-secret"
        bypass_record["outputs"] = ["D:\\export\\Bearer bypass-bearer-secret.jsonl"]
        bypass_record["error"] = {"details": "Bearer bypass-bearer-secret"}
        self.assertTrue(task_history.save_history([bypass_record]))

        raw = self.history_file.read_text(encoding="utf-8")
        for secret in secrets:
            self.assertNotIn(secret, raw)
        self.assertEqual(task_history.load_history()[0]["outputs"], [])
        self.assertEqual(task_history.load_history()[0]["output_dir"], "")
        self.assertIn("[已脱敏]", task_history.load_history()[0]["target_summary"])

    def test_bearer_without_token_and_normal_paths_remain_safe(self):
        safe_values = (
            "bearer",
            "BV1Demo", "av2",
            "https://www.bilibili.com/video/BV1Demo?fid=123&sid=456",
            "D:\\exports\\normal-result.xlsx",
        )
        for value in safe_values:
            with self.subTest(value=value):
                safe, reusable = task_history.prepare_reusable_params({"value": value})
                self.assertTrue(reusable)
                self.assertEqual(safe, {"value": value})

    def test_final_write_boundary_rechecks_bypassed_fields(self):
        task_history.create_record(
            "comments", "评论抓取", target_summary="BV1",
            output_dir="D:\\export", reusable_params={"url": "BV1"},
        )
        record = task_history.load_history()[0]
        record["reusable"] = True
        record["reusable_params"] = {
            "nested": {"items": ["refresh_token=boundary-secret"]}
        }
        record["output_dir"] = "D:\\export\\password=boundary-dir"
        record["outputs"] = ["D:\\export\\safe.xlsx",
                              "D:\\export\\api_key=boundary-output.jsonl"]
        self.assertTrue(task_history.save_history([record]))
        raw = self.history_file.read_text(encoding="utf-8")
        for secret in ("boundary-secret", "boundary-dir", "boundary-output"):
            self.assertNotIn(secret, raw)
        loaded = task_history.load_history()[0]
        self.assertFalse(loaded["reusable"])
        self.assertEqual(loaded["output_dir"], "")
        self.assertEqual(loaded["outputs"], ["D:\\export\\safe.xlsx"])

    def test_error_snapshot_is_sanitized_and_not_recent_error_reference(self):
        result = task_history.create_record("comments", "评论抓取", target_summary="BV1")
        task_history.update_record(
            result.record["id"], "failed",
            error={
                "timestamp": "2026-09-08T12:00:00+08:00",
                "source": "评论任务",
                "summary": "Cookie=secret-cookie",
                "details": (
                    "Authorization: Bearer auth-secret; "
                    "https://example.com?access_token=url-secret"
                ),
            },
        )
        raw = self.history_file.read_text(encoding="utf-8")
        for secret in ("secret-cookie", "auth-secret", "example.com", "url-secret"):
            self.assertNotIn(secret, raw)
        error = task_history.load_history()[0]["error"]
        self.assertIn("已脱敏", error["details"])

    def test_clear_history_only_removes_history_file(self):
        result = task_history.create_record("comments", "评论抓取", target_summary="BV1")
        self.assertTrue(result.persisted)
        output = Path(self.temp.name) / "report.xlsx"
        output.write_text("keep", encoding="utf-8")
        config_file = Path(self.temp.name) / "config.json"
        config_file.write_text("keep", encoding="utf-8")
        self.assertTrue(task_history.clear_history())
        self.assertFalse(self.history_file.exists())
        self.assertTrue(output.exists())
        self.assertTrue(config_file.exists())

    def test_output_exists_is_recomputed_from_filesystem(self):
        output = Path(self.temp.name) / "result.xlsx"
        self.assertFalse(task_history.output_exists(output))
        output.write_text("result", encoding="utf-8")
        self.assertTrue(task_history.output_exists(output))
        output.unlink()
        self.assertFalse(task_history.output_exists(output))


class TaskPageHistoryTests(unittest.TestCase):
    def test_running_history_is_created_before_runner_starts(self):
        page = _HistoryPage()
        page.collect_params = lambda: {"out_dir": "D:\\export"}
        page.pipeline = lambda: None
        page._begin_task_history = lambda params: TaskPage._begin_task_history(page, params)
        events = []

        class _Signal:
            def connect(self, _handler):
                pass

        class _Runner:
            def __init__(self, _pipeline, _kwargs):
                events.append("runner")
                self.progress = _Signal()
                self.finished_ok = _Signal()
                self.failed = _Signal()
                self.finished = _Signal()

            def isRunning(self):
                return False

            def start(self):
                events.append("start")

        def create(tool_id, tool_name, **_kwargs):
            events.append("history")
            return task_history.HistoryWriteResult({"id": "record-id"}, True)

        with patch.object(task_history, "create_record", side_effect=create), \
                patch("app.task_page.TaskRunner", _Runner):
            TaskPage.on_start(page)
        self.assertEqual(events, ["history", "runner", "start"])

    def test_parameter_validation_does_not_create_history(self):
        page = _HistoryPage()

        def invalid_params():
            raise ValueError("参数不完整")

        page.collect_params = invalid_params
        with patch("app.task_page.QMessageBox.warning"), \
                patch.object(task_history, "create_record") as create:
            TaskPage.on_start(page)
        create.assert_not_called()

    def test_success_stays_success_when_history_update_fails(self):
        page = _HistoryPage()
        with patch.object(task_history, "update_record", return_value=False):
            TaskPage._on_finished_ok(page, {"xlsx": "result.xlsx"})
        self.assertEqual(page.progress_block.label, "任务完成")
        self.assertIsNotNone(page.finished_result)

    def test_failure_uses_one_sanitized_error_snapshot(self):
        page = _HistoryPage()
        snapshot = {"details": "safe", "source": "测试模块"}
        with patch.object(diagnostics, "record_error", return_value=snapshot) as recent, \
                patch.object(task_history, "update_record", return_value=True) as update:
            TaskPage._on_failed(page, "RuntimeError: failure")
        recent.assert_called_once_with(
            source="测试模块", summary="任务执行失败", details="RuntimeError: failure"
        )
        update.assert_called_once_with(
            "record-id", "failed", outputs=None, error=snapshot
        )

    def test_cancel_and_interrupt_update_without_error(self):
        for method, status in (
            (TaskPage._on_cancelled, "cancelled"),
            (TaskPage._on_interrupted, "interrupted"),
        ):
            page = _HistoryPage()
            with patch.object(diagnostics, "record_error") as recent, \
                    patch.object(task_history, "update_record", return_value=True) as update:
                method(page)
            recent.assert_not_called()
            update.assert_called_once_with(
                "record-id", status, outputs=None, error=None
            )

    def test_cancel_and_interrupt_keep_result_outputs_without_error(self):
        cases = (
            ("cancelled", {"stats": {"cancelled": True}}),
            ("interrupted", {"stats": {"status": "interrupted", "aborted": True}}),
        )
        for status, result in cases:
            result["outputs"] = ["D:\\export\\result.xlsx", "D:\\export\\data.jsonl"]
            page = _HistoryPage()
            with patch.object(diagnostics, "record_error") as recent, \
                    patch.object(task_history, "update_record", return_value=True) as update:
                TaskPage._on_finished_ok(page, result)
            recent.assert_not_called()
            update.assert_called_once_with(
                "record-id", status,
                outputs=["D:\\export\\result.xlsx", "D:\\export\\data.jsonl"],
                error=None,
            )

    def test_history_write_failure_is_only_a_nonfatal_warning(self):
        page = _HistoryPage()
        with patch.object(task_history, "create_record", return_value=task_history.HistoryWriteResult(
            {"id": "record-id"}, False
        )):
            TaskPage._begin_task_history(page, {"out_dir": "D:\\export"})
        self.assertEqual(page._history_record_id, "record-id")
        self.assertTrue(any(level == "warn" for _text, level in page.log_panel.entries))


class TaskPageHookTests(unittest.TestCase):
    def test_comments_reusable_params_are_filled_without_starting(self):
        page = CommentsPage.__new__(CommentsPage)
        page.link_edit = _Field()
        page.out_row = _Field()
        page.sleep_edit = _Field()
        page.tls_check = _Field()
        page.auto_open = _Field()
        params = {
            "url": "BV1Demo", "out_dir": "D:\\export", "sleep": 0.4,
            "use_tls_grpc": True, "open_result": False,
        }
        with patch.object(TaskPage, "on_start") as start:
            page.apply_reusable_params(params)
        start.assert_not_called()
        self.assertEqual(page.link_edit.value, "BV1Demo")
        self.assertEqual(page.out_row.value, "D:\\export")
        self.assertEqual(page.sleep_edit.value, "0.4")
        self.assertTrue(page.tls_check.value)
        self.assertFalse(page.auto_open.value)

    def test_collector_reusable_params_are_filled_without_starting(self):
        page = CollectorPage.__new__(CollectorPage)
        page.src_edit = _Field()
        page.out_row = _Field()
        page.sleep_edit = _Field()
        page.radio_monitor = _Field()
        page.radio_once = _Field()
        page.interval_edit = _Field()
        page.rounds_edit = _Field()
        page.auto_open = _Field()
        params = {
            "sources": ["BV1Demo", "av2"], "out_dir": "D:\\export",
            "sleep": 0.5, "monitor": True, "interval_min": 10,
            "rounds": 3, "open_result": True,
        }
        with patch.object(TaskPage, "on_start") as start:
            page.apply_reusable_params(params)
        start.assert_not_called()
        self.assertEqual(page.src_edit.value, "BV1Demo\nav2")
        self.assertTrue(page.radio_monitor.value)
        self.assertFalse(page.radio_once.value)
        self.assertEqual(page.interval_edit.value, "10")
        self.assertEqual(page.rounds_edit.value, "3")


if __name__ == "__main__":
    unittest.main()
