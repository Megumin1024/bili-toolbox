# -*- coding: utf-8 -*-
"""任务页状态分流测试，不启动完整 GUI 或真实任务。"""
import unittest
from unittest.mock import patch

from app.task_page import TaskPage
from core import diagnostics


class FakeProgress:
    def __init__(self):
        self.state = None
        self.label = None

    def set_warning(self, text):
        self.state = "warning"
        self.label = text

    def set_error(self, text):
        self.state = "error"
        self.label = text

    def set_success(self, text):
        self.state = "success"
        self.label = text

    def set_busy(self, text):
        self.state = "running"
        self.label = text


class FakeButton:
    def __init__(self):
        self.enabled = None

    def setEnabled(self, value):
        self.enabled = value


class FakeLog:
    def __init__(self):
        self.entries = []

    def append(self, text, level=None):
        self.entries.append((text, level))


class FakePage:
    tool_title = "测试任务"
    tool_module = "测试模块"

    def __init__(self):
        self.runner = None
        self._cancel_requested = False
        self.progress_block = FakeProgress()
        self.btn_start = FakeButton()
        self.btn_cancel = FakeButton()
        self.log_panel = FakeLog()
        self.finished_result = None

    def on_finished(self, result):
        self.finished_result = result

    def _on_cancelled(self, result=None):
        TaskPage._on_cancelled(self, result)

    def _on_interrupted(self, result=None):
        TaskPage._on_interrupted(self, result)

    def _on_failed(self, msg):
        TaskPage._on_failed(self, msg)


class TaskStateTests(unittest.TestCase):
    def test_cancelled_result_does_not_record_or_finish_successfully(self):
        page = FakePage()
        with patch.object(diagnostics, "record_error") as record:
            TaskPage._on_finished_ok(page, {"stats": {"cancelled": True}})
        record.assert_not_called()
        self.assertEqual(page.progress_block.label, "已取消")
        self.assertIsNone(page.finished_result)
        self.assertTrue(page.btn_start.enabled)
        self.assertFalse(page.btn_cancel.enabled)

    def test_cancel_request_wins_over_normal_result(self):
        page = FakePage()
        page._cancel_requested = True
        TaskPage._on_finished_ok(page, {"xlsx": "result.xlsx"})
        self.assertEqual(page.progress_block.label, "已取消")
        self.assertIsNone(page.finished_result)

    def test_limit_interruption_is_warning_without_error_record(self):
        page = FakePage()
        with patch.object(diagnostics, "record_error") as record:
            TaskPage._on_finished_ok(
                page, {"stats": {"status": "interrupted", "aborted": True}}
            )
        record.assert_not_called()
        self.assertEqual(page.progress_block.label, "已中断，可继续")
        self.assertIsNone(page.finished_result)

    def test_network_error_is_failure_and_records_recent_error(self):
        page = FakePage()
        with patch.object(diagnostics, "record_error") as record:
            TaskPage._on_finished_ok(
                page,
                {"stats": {"status": "error", "error": "主楼阶段终止：gRPC 连续失败"}},
            )
        record.assert_called_once_with(
            source="测试模块", summary="任务执行失败",
            details="主楼阶段终止：gRPC 连续失败",
        )
        self.assertEqual(page.progress_block.label, "任务失败（详见日志）")
        self.assertIsNone(page.finished_result)

    def test_parameter_error_does_not_record_recent_error(self):
        page = FakePage()

        def invalid_params():
            raise ValueError("参数不完整")

        page.collect_params = invalid_params
        with patch("app.task_page.QMessageBox.warning"), \
                patch.object(diagnostics, "record_error") as record:
            TaskPage.on_start(page)
        record.assert_not_called()
        self.assertEqual(page.progress_block.label, "参数有误")

    def test_normal_result_finishes_successfully_without_error_record(self):
        page = FakePage()
        with patch.object(diagnostics, "record_error") as record:
            TaskPage._on_finished_ok(page, {"xlsx": "result.xlsx"})
        record.assert_not_called()
        self.assertEqual(page.progress_block.label, "任务完成")
        self.assertEqual(page.finished_result["xlsx"], "result.xlsx")

    def test_real_exception_records_recent_error(self):
        page = FakePage()
        with patch.object(diagnostics, "record_error") as record:
            TaskPage._on_failed(page, "RuntimeError: network failure")
        record.assert_called_once_with(
            source="测试模块", summary="任务执行失败",
            details="RuntimeError: network failure",
        )
        self.assertEqual(page.progress_block.label, "任务失败（详见日志）")


if __name__ == "__main__":
    unittest.main()
