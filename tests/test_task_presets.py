# -*- coding: utf-8 -*-
"""任务预设存储、隐私边界和三类页面回填测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.task_page import PresetBar, TaskPage
from app.task_runner import TaskRunner
from core import task_history, task_presets
from tools.collector.page import CollectorPage
from tools.comments.page import CommentsPage
from tools.monitor.page import MonitorPage, MonitorServer


class _TextField:
    def __init__(self, value=""):
        self._value = str(value)

    @property
    def value(self):
        return self._value

    def text(self):
        return self._value

    def setText(self, value):
        self._value = str(value)

    def toPlainText(self):
        return self._value

    def setPlainText(self, value):
        self._value = str(value)

    def set_value(self, value):
        self._value = str(value)

    def setFocus(self):
        pass


class _CheckField:
    def __init__(self, value=False):
        self.value = bool(value)

    def isChecked(self):
        return self.value

    def setChecked(self, value):
        self.value = bool(value)


class _SpinField:
    def __init__(self, value=60):
        self._value = int(value)

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = int(value)


class _ComboField:
    def __init__(self, value="auto"):
        self.values = ["auto", "h2-ja3", "urllib"]
        self.current = value

    def currentData(self):
        return self.current

    def findData(self, value):
        try:
            return self.values.index(value)
        except ValueError:
            return -1

    def setCurrentIndex(self, index):
        self.current = self.values[index]


class _PathField:
    def __init__(self, value=""):
        self._value = str(value)

    def value(self):
        return self._value

    def set_value(self, value):
        self._value = str(value)

    def setFocus(self):
        pass


class TaskPresetStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.presets_file = Path(self.temp.name) / "task_presets.json"
        self.history_file = Path(self.temp.name) / "task_history.json"
        self.patch_presets = patch.object(task_presets, "PRESETS_FILE", self.presets_file)
        self.patch_history = patch.object(task_history, "HISTORY_FILE", self.history_file)
        self.patch_presets.start()
        self.patch_history.start()

    def tearDown(self):
        self.patch_history.stop()
        self.patch_presets.stop()
        self.temp.cleanup()

    def _create(self, name="日常监控", tool_id="monitor", params=None, overwrite=False):
        return task_presets.create_or_update_preset(
            name, tool_id, tool_id, params or {"bvid": "BV1Demo"},
            overwrite=overwrite,
        )

    def test_create_read_update_delete(self):
        created = self._create()
        self.assertEqual(task_presets.load_presets(), [created])
        updated = self._create(params={"bvid": "BV2Updated"}, overwrite=True)
        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["created_at"], created["created_at"])
        self.assertEqual(task_presets.load_presets()[0]["params"]["bvid"], "BV2Updated")
        self.assertTrue(task_presets.delete_preset(created["id"]))
        self.assertEqual(task_presets.load_presets(), [])

    def test_same_tool_duplicate_requires_overwrite_and_different_tool_allows_name(self):
        self._create("活动投票", "comments", {"url": "BV1"})
        with self.assertRaises(task_presets.PresetNameConflict):
            self._create("活动投票", "comments", {"url": "BV2"})
        other = self._create("活动投票", "collector", {"sources": ["BV3"]})
        self.assertEqual(other["name"], "活动投票")
        self.assertEqual(len(task_presets.load_presets()), 2)

    def test_name_is_trimmed_and_limited(self):
        record = self._create("  日常监控  ")
        self.assertEqual(record["name"], "日常监控")
        with self.assertRaises(ValueError):
            self._create("x" * 41)

    def test_corrupted_file_does_not_block_startup(self):
        self.presets_file.write_text("{not-json", encoding="utf-8")
        self.assertEqual(task_presets.load_presets(), [])

    def test_single_corrupted_preset_is_skipped(self):
        valid = self._create("有效", "comments", {"url": "BV1"})
        payload = json.loads(self.presets_file.read_text(encoding="utf-8"))
        payload["presets"].append({"id": "bad", "name": "坏记录"})
        payload["presets"].append({
            "id": "sensitive", "name": "敏感记录", "tool_id": "comments",
            "tool_name": "评论抓取", "created_at": valid["created_at"],
            "updated_at": valid["updated_at"],
            "params": {"url": "https://example.com/?access_token=secret"},
        })
        self.presets_file.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual([p["name"] for p in task_presets.load_presets()], ["有效"])

    def test_atomic_write_failure_keeps_old_file_and_cleans_temp(self):
        old = self._create("旧预设")
        original = self.presets_file.read_text(encoding="utf-8")
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            self.assertFalse(task_presets.save_presets([old]))
        self.assertEqual(self.presets_file.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.presets_file.parent.glob(".task-presets-*.tmp")), [])

    def test_sensitive_values_are_rejected_and_not_written(self):
        cases = (
            {"cookie": "cookie-secret"},
            {"token": "token-secret"},
            {"Authorization": "authorization-secret"},
            {"value": "Bearer bearer-secret"},
            {"password": "password-secret"},
            {"proxy": "http://proxy-user:proxy-pass@127.0.0.1:8080"},
            {"url": "https://www.bilibili.com/video/BV1?token=url-secret"},
            {"proxy_spec": "http://user:pass@127.0.0.1:8080"},
        )
        for index, params in enumerate(cases):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    self._create(f"敏感{index}", "comments", params)
        self.assertFalse(self.presets_file.exists())

    def test_normal_ids_urls_and_windows_paths_are_safe(self):
        record = self._create("普通参数", "collector", {
            "sources": ["BV1Demo", "av2", "fid=123", "sid=456",
                        "https://www.bilibili.com/video/BV1Demo?fid=123&sid=456"],
            "out_dir": r"D:\exports\normal-result.xlsx",
        })
        self.assertEqual(record["params"]["sources"][0], "BV1Demo")
        self.assertEqual(record["params"]["out_dir"], r"D:\exports\normal-result.xlsx")

    def test_save_preset_does_not_create_history(self):
        self._create()
        self.assertFalse(self.history_file.exists())

    def test_delete_preset_does_not_delete_history(self):
        history = task_history.create_record(
            "comments", "评论抓取", target_summary="BV1",
            reusable_params={"url": "BV1"},
        )
        self.assertTrue(history.persisted)
        created = self._create()
        self.assertTrue(task_presets.delete_preset(created["id"]))
        self.assertTrue(self.history_file.exists())
        self.assertEqual(len(task_history.load_history()), 1)

    def test_clear_history_does_not_delete_presets(self):
        self._create()
        task_history.create_record("comments", "评论抓取", target_summary="BV1")
        self.assertTrue(task_history.clear_history())
        self.assertEqual(len(task_presets.load_presets()), 1)


class TaskPresetPageTests(unittest.TestCase):
    def test_comments_collect_and_apply_allow_empty_target(self):
        page = CommentsPage.__new__(CommentsPage)
        page.link_edit = _TextField("")
        page.out_row = _PathField("")
        page.sleep_edit = _TextField("0.4")
        page.tls_check = _CheckField(True)
        page.auto_open = _CheckField(False)
        params = page.collect_preset_params()
        self.assertEqual(params, {
            "url": "", "out_dir": "", "sleep": 0.4,
            "use_tls_grpc": True, "open_result": False,
        })
        page.apply_preset_params({
            "url": "BV1Demo", "out_dir": r"D:\export", "sleep": 0.8,
            "use_tls_grpc": False, "open_result": True,
        })
        self.assertEqual(page.link_edit.value, "BV1Demo")
        self.assertEqual(page.out_row.value(), r"D:\export")
        self.assertEqual(page.sleep_edit.value, "0.8")
        self.assertFalse(page.tls_check.value)
        self.assertTrue(page.auto_open.value)

    def test_collector_collect_and_apply_allow_empty_sources(self):
        page = CollectorPage.__new__(CollectorPage)
        page.src_edit = _TextField("")
        page.out_row = _PathField("")
        page.sleep_edit = _TextField("0.5")
        page.radio_monitor = _CheckField(True)
        page.radio_once = _CheckField(False)
        page.interval_edit = _TextField("10")
        page.rounds_edit = _TextField("3")
        page.auto_open = _CheckField(True)
        params = page.collect_preset_params()
        self.assertEqual(params["sources"], [])
        page.apply_preset_params({
            "sources": ["BV1Demo", "av2"], "out_dir": r"D:\export",
            "sleep": 0.5, "monitor": False, "interval_min": 7,
            "rounds": 2, "open_result": False,
        })
        self.assertEqual(page.src_edit.value, "BV1Demo\nav2")
        self.assertFalse(page.radio_monitor.value)
        self.assertTrue(page.radio_once.value)
        self.assertEqual(page.interval_edit.value, "7")
        self.assertEqual(page.rounds_edit.value, "2")

    def test_monitor_collect_and_apply_without_starting_server(self):
        page = MonitorPage.__new__(MonitorPage)
        page.bvid_edit = _TextField("")
        page.interval_spin = _SpinField(60)
        page.transport_combo = _ComboField("auto")
        page.data_row = _PathField("")
        params = page.collect_preset_params()
        self.assertEqual(params, {
            "bvid": "", "interval": 60, "transport": "auto", "data_dir": "",
        })
        with patch.object(MonitorPage, "on_start") as start:
            page.apply_preset_params({
                "bvid": "BV1Monitor", "interval": 120,
                "transport": "urllib", "data_dir": r"D:\monitor",
            })
        start.assert_not_called()
        self.assertEqual(page.bvid_edit.value, "BV1Monitor")
        self.assertEqual(page.interval_spin.value(), 120)
        self.assertEqual(page.transport_combo.current, "urllib")
        self.assertEqual(page.data_row.value(), r"D:\monitor")

    def test_apply_preset_hooks_do_not_call_task_page_start(self):
        page = CommentsPage.__new__(CommentsPage)
        page.link_edit = _TextField()
        page.out_row = _PathField()
        page.sleep_edit = _TextField()
        page.tls_check = _CheckField()
        page.auto_open = _CheckField()
        with patch.object(TaskPage, "on_start") as start, \
                patch.object(TaskRunner, "start") as runner_start, \
                patch.object(MonitorServer, "start") as server_start:
            page.apply_preset_params({})
        start.assert_not_called()
        runner_start.assert_not_called()
        server_start.assert_not_called()

    def test_preset_bar_only_lists_current_tool(self):
        app = _qt_app()
        del app
        self._write_records([
            {"id": "monitor", "name": "日常监控", "tool_id": "monitor",
             "tool_name": "实时监控", "created_at": "2026-01-01T00:00:00+08:00",
             "updated_at": "2026-01-01T00:00:00+08:00", "params": {"bvid": "BV1"}},
            {"id": "comments", "name": "日常监控", "tool_id": "comments",
             "tool_name": "评论抓取", "created_at": "2026-01-02T00:00:00+08:00",
             "updated_at": "2026-01-02T00:00:00+08:00", "params": {"url": "BV2"}},
        ])
        applied = []
        bar = PresetBar("monitor", "实时监控", lambda: {}, applied.append)
        self.assertEqual(bar.combo.count(), 2)
        self.assertEqual(bar.combo.itemText(1), "日常监控")
        bar.combo.setCurrentIndex(1)
        self.assertTrue(bar.btn_apply.isEnabled())
        bar._on_apply()
        self.assertEqual(applied, [{"bvid": "BV1"}])

    def _write_records(self, records):
        task_presets.PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        task_presets.PRESETS_FILE.write_text(json.dumps({
            "schema_version": 1, "presets": records,
        }), encoding="utf-8")


def _qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


if __name__ == "__main__":
    unittest.main()
