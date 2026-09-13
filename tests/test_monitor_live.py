# -*- coding: utf-8 -*-
"""监控「直播间模式」：live 采样、/api/latest 契约、AlertSession 字段参数化、
页面模式联动与前端契约键集锁定。

全部离线：B站接口走注入桩；HTTP 仅 127.0.0.1 本地回环；config 不触碰
真实 %APPDATA%。前端契约以「server live 输出键 ⊆ index.html 已渲染点」
的文本级断言锁定，不引入 JS 单测框架。
"""
from __future__ import annotations

import ast
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from tools.live_room import core as live_room_core
from tools.monitor.alerts import AlertConfig, AlertSession
from tools.monitor.notifications import webhook_payload
from tools.monitor.page import MonitorPage
from tools.monitor.server import MonitorServer, _State

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "tools" / "monitor" / "static" / "index.html"

# /api/history 与 video 模式共享的基础键集（server.py::_api_latest 原有键）。
BASE_LATEST_KEYS = {"ok", "server_time", "interval", "bvid", "meta", "latest",
                    "session_start_ts", "sample_count", "last_ok_ts", "error",
                    "net"}
LIVE_META_KEYS = {"room_id", "uid", "title", "live_status",
                  "live_status_label", "parent_area_name", "area_name",
                  "live_time"}
LIVE_SAMPLE_KEYS = {"ts", "live_status", "online", "title"}

# server live 输出键 → index.html 必须存在的渲染点（成对红线）。
LIVE_RENDER_PATHS = (
    "latestData.mode",
    "meta.room_id", "meta.uid", "meta.title", "meta.live_status",
    "meta.live_status_label", "meta.parent_area_name", "meta.area_name",
    "meta.live_time",
    "latest.online", "latest.live_status", "latest.title", "latest.ts",
    "latestData.interval", "latestData.server_time",
    "latestData.sample_count", "latestData.error", "latestData.net",
)


# ---------- 桩 ----------

def make_get_info(room_id=6, live_status=1, title="示例直播间",
                  online="12345", uid=42, area="唱歌", parent="虚拟主播",
                  live_time="1700000000"):
    """形状照抄 live_room 测试 fixture：online 实测以字符串下发。"""
    return {"code": 0, "message": "0", "data": {
        "room_id": room_id, "uid": uid, "live_status": live_status,
        "title": title, "online": online, "area_name": area,
        "parent_area_name": parent, "live_time": live_time, "tags": "标签"}}


def fake_client(dispatch):
    client = Mock()
    client.stats = {"last_transport": "stub", "last_latency_ms": 1}
    client.pool.status.return_value = {"size": 1}
    client.fetch_json.side_effect = dispatch
    return client


def live_dispatch(payload=None):
    def dispatch(url, retries=3):
        assert "room/v1/Room/get_info" in url, url
        return payload if payload is not None else make_get_info()
    return dispatch


def video_dispatch():
    def dispatch(url, retries=3):
        if "web-interface/view" in url:
            return {"code": 0, "data": {
                "bvid": "BV1TestMode", "aid": 1, "title": "视频标题",
                "owner": {"name": "UP主", "mid": 2},
                "pubdate": 1700000000, "duration": 100, "desc": "d",
                "pic": "", "videos": 1,
                "stat": {"view": 100, "danmaku": 5, "reply": 6,
                         "favorite": 7, "coin": 8, "share": 9, "like": 10},
                "pages": [{"cid": 11, "page": 1, "part": "P1",
                           "duration": 100}],
            }}
        if "online/total" in url:
            return {"code": 0, "data": {"total": "66", "count": "33"}}
        raise AssertionError(url)
    return dispatch


def live_alert_config(**overrides):
    values = {"enabled": True, "milestones": (), "stagnation_enabled": False,
              "spike_enabled": False, "disconnect_enabled": False,
              "live_flip_enabled": True}
    values.update(overrides)
    return AlertConfig.from_mapping(values)


class TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "data"


# ---------- 1. A1 上提结构锁 ----------

class LiftStructureTests(unittest.TestCase):
    LIVE_LIFTED_NAMES = ("GET_INFO_URL", "LIVE_STATUS_LABELS", "get_info_url",
                         "live_status_label", "parse_get_info",
                         "parse_room_input", "status_transition", "to_int")

    def test_live_room_reexports_are_core_objects(self):
        import core.live as shared
        for name in self.LIVE_LIFTED_NAMES:
            self.assertIs(
                getattr(live_room_core, name), getattr(shared, name),
                f"tools.live_room.core.{name} 应 re-export core.live.{name}")

    def test_core_live_has_no_tool_or_app_import(self):
        import core.live
        tree = ast.parse(
            Path(core.live.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertFalse(
                    name == "tools" or name.startswith(("tools.", "app", "app.")),
                    f"core/live.py 不得依赖上层：{name}")


# ---------- 2. server 构造 / 历史命名 ----------

class ServerConstructionTests(TmpDirTestCase):
    def _make(self, **kwargs):
        kwargs.setdefault("log", lambda _msg: None)
        with patch("core.session.get_client",
                   return_value=fake_client(live_dispatch())):
            return MonitorServer(**kwargs)

    def test_live_history_file_naming_and_interval_clamp(self):
        server = self._make(mode="live", room_id=77, interval=1,
                            data_dir=self.data_dir)
        self.assertEqual(server.mode, "live")
        self.assertEqual(server.room_id, 77)
        self.assertIsNone(server.bvid)
        self.assertEqual(server.history_file.name, "history_live_77.jsonl")
        # 采样礼仪：间隔沿用 5–3600s 下限钳制
        self.assertEqual(server.interval, 5)
        server.stop()

    def test_video_defaults_unchanged(self):
        server = self._make(bvid="BV1VideoMode", interval=2,
                            data_dir=self.data_dir)
        self.assertEqual(server.mode, "video")
        self.assertIsNone(server.room_id)
        self.assertEqual(server.history_file.name, "history_BV1VideoMode.jsonl")
        self.assertEqual(server.interval, 5)
        server.stop()

    def test_live_requires_room_id(self):
        with self.assertRaises(ValueError):
            self._make(mode="live")


# ---------- 3. live 采样 ----------

class CollectOnceLiveTests(TmpDirTestCase):
    def _server(self, payload=None):
        with patch("core.session.get_client",
                   return_value=fake_client(live_dispatch(payload))):
            return MonitorServer(mode="live", room_id=6, interval=30,
                                 data_dir=self.data_dir,
                                 log=lambda _msg: None)

    def test_sample_and_meta_shape_single_request(self):
        server = self._server(make_get_info(room_id=22474988))
        meta, sample = server.collect_once_live(6)
        self.assertEqual(set(sample), LIVE_SAMPLE_KEYS)
        self.assertEqual(set(meta), LIVE_META_KEYS)
        # 短号 6 在响应 data.room_id 里归一为真实房间号，零额外请求
        self.assertEqual(meta["room_id"], 22474988)
        self.assertEqual(meta["uid"], 42)
        self.assertEqual(meta["live_status_label"], "直播中")
        self.assertEqual(meta["live_time"], "2023-11-15 06:13:20")
        self.assertEqual(sample["online"], 12345)  # 字符串人气已转 int
        self.assertEqual(sample["title"], "示例直播间")
        self.assertEqual(server.client.fetch_json.call_count, 1)
        url = server.client.fetch_json.call_args[0][0]
        self.assertIn("room/v1/Room/get_info?room_id=6", url)

    def test_degrades_when_data_missing(self):
        server = self._server({"code": 0})
        meta, sample = server.collect_once_live(6)
        self.assertEqual(meta["room_id"], 6)  # fallback_room 兜底
        self.assertEqual(meta["live_status"], -1)
        self.assertEqual(sample["online"], 0)
        self.assertEqual(sample["title"], "")


class HistoryLoadTests(TmpDirTestCase):
    def test_live_loads_only_live_shaped_lines(self):
        self.data_dir.mkdir(parents=True)
        path = self.data_dir / "history_live_9.jsonl"
        path.write_text(
            json.dumps({"ts": 1, "live_status": 1, "online": 5, "title": "a"},
                       ensure_ascii=False) + "\n"
            + json.dumps({"ts": 2, "bvid": "BV1VideoMode", "view": 3}) + "\n",
            encoding="utf-8")
        with patch("core.session.get_client",
                   return_value=fake_client(live_dispatch())):
            server = MonitorServer(mode="live", room_id=9,
                                   data_dir=self.data_dir,
                                   log=lambda _msg: None)
        server._load_history()
        self.assertEqual(len(server.state.samples), 1)
        self.assertEqual(server.state.samples[0]["online"], 5)

    def test_video_ignores_live_shaped_lines(self):
        self.data_dir.mkdir(parents=True)
        path = self.data_dir / "history_BV1VideoMode.jsonl"
        path.write_text(
            json.dumps({"ts": 1, "live_status": 1, "online": 5, "title": "a"}) + "\n"
            + json.dumps({"ts": 2, "bvid": "BV1VideoMode", "view": 3}) + "\n",
            encoding="utf-8")
        with patch("core.session.get_client",
                   return_value=fake_client(video_dispatch())):
            server = MonitorServer(bvid="BV1VideoMode",
                                   data_dir=self.data_dir,
                                   log=lambda _msg: None)
        server._load_history()
        self.assertEqual(len(server.state.samples), 1)
        self.assertEqual(server.state.samples[0]["view"], 3)


class PollerLiveEmitTests(unittest.TestCase):
    def test_live_poller_emits_online_and_status_without_view(self):
        server = MonitorServer.__new__(MonitorServer)
        server.mode = "live"
        server.room_id = 6
        server.bvid = None
        server.interval = 1
        server.session_id = "s1"
        server.log = lambda _message: None
        events = []
        server.event_callback = events.append
        server.state = _State(None, 1, mode="live", room_id=6)
        server.history_file = None
        server._stop = threading.Event()
        server.client = Mock()
        server.client.stats = {"last_transport": "t", "last_latency_ms": 1}
        sample = {"ts": 1700000000, "live_status": 1, "online": 88,
                  "title": "t"}

        def collect_once_live(_room_id):
            server._stop.set()
            return {"room_id": 6}, sample

        server.collect_once_live = collect_once_live
        server._append_sample = lambda _sample: None
        server._poller_loop()
        self.assertEqual(server.state.samples, [sample])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "sample_success")
        self.assertEqual(events[0]["online"], 88)
        self.assertEqual(events[0]["live_status"], 1)
        self.assertNotIn("view", events[0])


# ---------- 4. /api/latest 契约（真实回环 HTTP） ----------

class LatestEndpointContractTests(TmpDirTestCase):
    """video 键集合零变化；live 仅新增 mode 键，meta/sample 键集锁定。"""

    def _fetch(self, path):
        with urllib.request.urlopen(path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _wait_samples(self, server, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if server.state.samples:
                return True
            time.sleep(0.05)
        return False

    def _start(self, dispatch, **kwargs):
        kwargs.setdefault("interval", 5)
        kwargs.setdefault("data_dir", self.data_dir)
        kwargs.setdefault("log", lambda _msg: None)
        with patch("core.session.get_client",
                   return_value=fake_client(dispatch)):
            server = MonitorServer(**kwargs)
        server.start()
        self.addCleanup(server.stop)
        self.assertTrue(self._wait_samples(server))
        return server

    def test_video_latest_keys_unchanged(self):
        server = self._start(video_dispatch(), bvid="BV1TestMode")
        payload = self._fetch(f"{server.url}api/latest")
        self.assertEqual(set(payload), BASE_LATEST_KEYS)
        self.assertNotIn("mode", payload)
        self.assertEqual(payload["meta"]["bvid"], "BV1TestMode")
        history = self._fetch(f"{server.url}api/history")
        self.assertIn("view", history["samples"][0])

    def test_live_latest_adds_only_mode_key(self):
        server = self._start(live_dispatch(make_get_info(room_id=22474988)),
                             mode="live", room_id=6)
        payload = self._fetch(f"{server.url}api/latest")
        self.assertEqual(set(payload), BASE_LATEST_KEYS | {"mode"})
        self.assertEqual(payload["mode"], "live")
        self.assertIsNone(payload["bvid"])
        self.assertEqual(set(payload["meta"]), LIVE_META_KEYS)
        self.assertEqual(payload["meta"]["room_id"], 22474988)
        self.assertEqual(set(payload["latest"]), LIVE_SAMPLE_KEYS)
        history = self._fetch(f"{server.url}api/history")
        self.assertEqual(set(history["samples"][0]), LIVE_SAMPLE_KEYS)


# ---------- 5. AlertSession 字段参数化与 live_status_flip ----------

class AlertSessionLiveTests(unittest.TestCase):
    def test_field_params_default_to_video_semantics(self):
        session = AlertSession({"enabled": True}, "s")
        self.assertEqual(session.value_field, "view")
        self.assertIsNone(session.status_field)
        self.assertEqual(session.value_label, "播放量")

    def test_live_events_say_renqi_not_bofangliang(self):
        session = AlertSession(live_alert_config(
            milestones="100", stagnation_enabled=True,
            stagnation_window_min=1, stagnation_max_growth=0,
            spike_enabled=True, spike_window_min=1, spike_min_absolute=10,
            spike_min_relative_percent=20, spike_cooldown_min=0),
            "s", value_field="online", value_label="人气")
        session.process_sample(0, 50)
        events = session.process_sample(60, 100)    # milestone + spike（窗口锚点 t=0）
        events += session.process_sample(120, 100)  # stagnation
        events += session.process_sample(180, 200)  # spike
        self.assertEqual([e.kind for e in events],
                         ["milestone", "spike", "stagnation", "spike"])
        for event in events:
            self.assertIn("人气", event.title + event.message)
            self.assertNotIn("播放量", event.title + event.message)
        self.assertEqual(events[0].title, "人气里程碑")
        self.assertIn("人气已达到 100", events[0].message)
        self.assertIn("人气增长 0", events[2].message)
        self.assertEqual(events[1].title, "人气异常突增")

    def test_flip_fires_once_per_transition(self):
        session = AlertSession(live_alert_config(), "s",
                               value_field="online",
                               status_field="live_status")
        self.assertEqual(session.process_status_flip(0, 1), [])   # 首轮建基线
        self.assertEqual(session.process_status_flip(60, 1), [])  # 相同不重复
        events = session.process_status_flip(120, 0)
        self.assertEqual([e.kind for e in events], ["live_status_flip"])
        self.assertEqual(events[0].title, "下播提醒")
        self.assertEqual(session.process_status_flip(180, 0), [])
        self.assertEqual(session.process_status_flip(240, 1)[0].title, "开播提醒")
        change = session.process_status_flip(300, 2)
        self.assertEqual(change[0].title, "直播状态变化提醒")
        self.assertIn("直播中 → 轮播", change[0].message)

    def test_flip_disabled_by_default_and_master_switch(self):
        session = AlertSession(
            AlertConfig.from_mapping({"enabled": True, "live_flip_enabled": False}),
            "s", status_field="live_status")
        self.assertEqual(session.process_status_flip(0, 1), [])
        self.assertEqual(session.process_status_flip(60, 0), [])
        off = AlertSession({"enabled": False, "live_flip_enabled": True}, "s2",
                           status_field="live_status")
        self.assertEqual(off.process_status_flip(0, 1), [])
        self.assertEqual(off.process_status_flip(60, 0), [])

    def test_flip_ignores_unknown_status_and_keeps_baseline(self):
        session = AlertSession(live_alert_config(), "s",
                               status_field="live_status")
        session.process_status_flip(0, 1)
        self.assertEqual(session.process_status_flip(60, -1), [])
        self.assertEqual(session.process_status_flip(90, None), [])
        self.assertEqual(session.process_status_flip(120, "1"), [])  # 非法形态
        events = session.process_status_flip(180, 0)  # 基线仍是 1 → 翻转成立
        self.assertEqual(len(events), 1)

    def test_flip_respects_stop(self):
        session = AlertSession(live_alert_config(), "s",
                               status_field="live_status")
        session.process_status_flip(0, 1)
        session.stop()
        self.assertEqual(session.process_status_flip(60, 0), [])

    def test_milestone_acts_on_online_values(self):
        session = AlertSession(live_alert_config(milestones="100"), "s",
                               value_field="online")
        self.assertEqual(session.process_sample(0, 50), [])
        events = session.process_sample(60, 100)
        self.assertEqual([e.kind for e in events], ["milestone"])
        self.assertIn("100", events[0].message)

    def test_spike_acts_on_online_values(self):
        session = AlertSession(live_alert_config(
            spike_enabled=True, spike_window_min=1, spike_min_absolute=50,
            spike_min_relative_percent=20, spike_cooldown_min=0), "s",
            value_field="online")
        session.process_sample(0, 5000)
        self.assertEqual([e.kind for e in session.process_sample(60, 6000)],
                         ["spike"])

    def test_live_flip_config_roundtrip(self):
        self.assertFalse(AlertConfig().live_flip_enabled)
        mapping = AlertConfig.from_mapping(
            {"live_flip_enabled": True}).to_mapping()
        self.assertIs(mapping["live_flip_enabled"], True)
        self.assertTrue(AlertConfig.from_mapping(mapping).live_flip_enabled)

    def test_flip_event_payload_stays_whitelisted(self):
        payload = webhook_payload("live_status_flip", "开播提醒",
                                  "直播状态变化：未开播 → 直播中。")
        self.assertEqual(set(payload), {"title", "text", "event_type"})


# ---------- 6. 页面联动 / 事件路由 / 预设 ----------

class QtEnvironmentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class PageLiveModeTests(QtEnvironmentTestCase):
    def test_default_video_and_switch_linkage_preserves_numbers(self):
        page = MonitorPage({"out_dir": ""})
        self.assertEqual(page.mode_combo.currentData(), "video")
        self.assertEqual(page.bvid_label.text(), "视频 BV 号")
        self.assertEqual(page.bvid_edit.placeholderText(), "示例：BV1xxxxxxxxx")
        self.assertEqual(page.alert_milestone_check.text(), "播放量里程碑")
        self.assertTrue(page.alert_stagnation_growth_spin.suffix().endswith("播放"))
        self.assertFalse(page.alert_live_flip_check.isVisibleTo(page))

        page.alert_stagnation_growth_spin.setValue(500)
        page.mode_combo.setCurrentIndex(1)
        self.assertEqual(page.mode_combo.currentData(), "live")
        self.assertEqual(page.bvid_label.text(), "直播间号/链接")
        self.assertEqual(page.bvid_edit.placeholderText(),
                         "示例：直播间号或 live.bilibili.com/6")
        self.assertEqual(page.alert_milestone_check.text(), "人气里程碑")
        self.assertTrue(page.alert_stagnation_growth_spin.suffix().endswith("人气"))
        self.assertTrue(page.alert_spike_absolute_spin.suffix().endswith("人气"))
        # 切模式不清空用户已填数字
        self.assertEqual(page.alert_stagnation_growth_spin.value(), 500)
        self.assertTrue(page.alert_live_flip_check.isVisibleTo(page))

        page.mode_combo.setCurrentIndex(0)
        self.assertEqual(page.alert_milestone_check.text(), "播放量里程碑")
        self.assertFalse(page.alert_live_flip_check.isVisibleTo(page))
        page.on_app_close()

    def test_alert_mapping_gates_flip_by_mode(self):
        page = MonitorPage({"out_dir": ""})
        page.alert_live_flip_check.setChecked(True)
        # 视频模式下勾选也不生效
        self.assertFalse(page._alert_mapping_from_controls().live_flip_enabled)
        page.mode_combo.setCurrentIndex(1)
        self.assertTrue(page._alert_mapping_from_controls().live_flip_enabled)
        page.mode_combo.setCurrentIndex(0)
        self.assertFalse(page._alert_mapping_from_controls().live_flip_enabled)
        page.on_app_close()

    def test_preset_roundtrip_and_legacy_tolerated(self):
        page = MonitorPage({"out_dir": ""})
        page.mode_combo.setCurrentIndex(1)
        params = page.collect_preset_params()
        self.assertEqual(params["mode"], "live")
        self.assertIn("live_flip_enabled", params["alerts"])

        page2 = MonitorPage({"out_dir": ""})
        page2.apply_preset_params(params)
        self.assertEqual(page2.mode_combo.currentData(), "live")
        # 旧预设无 mode → 默认视频模式
        page2.apply_preset_params({"bvid": "BV1legacy", "interval": 30})
        self.assertEqual(page2.mode_combo.currentData(), "video")
        self.assertEqual(page2.bvid_edit.text(), "BV1legacy")
        # 非法 mode 值同样回落 video
        page2.apply_preset_params({"mode": "danmaku"})
        self.assertEqual(page2.mode_combo.currentData(), "video")
        page.on_app_close()
        page2.on_app_close()

    def test_start_routes_target_by_mode(self):
        created = []

        class FakeServer:
            def __init__(self, **kwargs):
                created.append(kwargs)
                self.running = False

            def start(self):
                self.running = True
                return "http://127.0.0.1:0"

            def stop(self):
                self.running = False

        page = MonitorPage({"out_dir": ""})
        page.bvid_edit.setText("https://live.bilibili.com/6")
        page.mode_combo.setCurrentIndex(1)
        with patch("tools.monitor.page.MonitorServer", FakeServer), \
                patch("tools.monitor.page.webbrowser.open"):
            page.on_start()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["mode"], "live")
        self.assertEqual(created[0]["room_id"], 6)
        self.assertNotIn("bvid", created[0])
        self.assertEqual(page.alert_session.value_field, "online")
        self.assertEqual(page.alert_session.status_field, "live_status")
        self.assertEqual(page.alert_session.value_label, "人气")
        page.on_stop()

        page.bvid_edit.setText("BV1VideoMode")
        page.mode_combo.setCurrentIndex(0)
        created.clear()
        with patch("tools.monitor.page.MonitorServer", FakeServer), \
                patch("tools.monitor.page.webbrowser.open"):
            page.on_start()
        self.assertEqual(created[0]["bvid"], "BV1VideoMode")
        self.assertNotIn("room_id", created[0])
        self.assertEqual(page.alert_session.value_field, "view")
        self.assertIsNone(page.alert_session.status_field)
        self.assertEqual(page.alert_session.value_label, "播放量")
        page.on_stop()

        # 非法输入不启动；空输入也不回落 placeholder（示例链接不能被当真）
        page.mode_combo.setCurrentIndex(1)
        page.bvid_edit.setText("abc")
        created.clear()
        with patch("tools.monitor.page.MonitorServer", FakeServer), \
                patch("tools.monitor.page.webbrowser.open"):
            page.on_start()
        self.assertEqual(created, [])
        page.bvid_edit.setText("")
        with patch("tools.monitor.page.MonitorServer", FakeServer), \
                patch("tools.monitor.page.webbrowser.open"):
            page.on_start()
        self.assertEqual(created, [])
        page.on_stop()


class EventRoutingTests(QtEnvironmentTestCase):
    def test_handle_event_uses_session_field_params(self):
        page = MonitorPage.__new__(MonitorPage)
        page._active_session_id = "s"
        page.alert_session = AlertSession(
            {"enabled": True, "milestones": "100", "stagnation_enabled": False,
             "spike_enabled": False, "disconnect_enabled": False,
             "live_flip_enabled": True},
            "s", value_field="online", status_field="live_status")
        published = []
        page._publish_alert = published.append
        page._handle_monitor_event({
            "type": "sample_success", "session_id": "s",
            "ts": 0, "online": 50, "live_status": 1})
        page._handle_monitor_event({
            "type": "sample_success", "session_id": "s",
            "ts": 60, "online": 100, "live_status": 1})
        page._handle_monitor_event({
            "type": "sample_success", "session_id": "s",
            "ts": 120, "online": 100, "live_status": 0})
        self.assertEqual([e.kind for e in published],
                         ["milestone", "live_status_flip"])


# ---------- 7. 前端契约键集锁定（文本级，无 JS 测试框架） ----------

class FrontendContractTests(unittest.TestCase):
    def test_monitor_subtitle_pair_in_sync(self):
        """ToolSpec.subtitle 与 MonitorPage.tool_subtitle 成对（AGENTS.md 规则）。"""
        from tools import TOOLS
        spec = next(s for s in TOOLS if s.id == "monitor")
        self.assertEqual(spec.subtitle, MonitorPage.tool_subtitle)
        self.assertEqual(spec.subtitle, "视频/直播间实时数据仪表盘")

    def test_live_keys_rendered_in_html(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        for path in LIVE_RENDER_PATHS:
            self.assertIn(path, html, f"index.html 缺少 {path} 的渲染点")

    def test_mode_dispatch_exists(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("latestData.mode === 'live'", html)

    def test_video_card_defs_intact(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        for key in ("view", "like", "coin", "favorite", "share", "danmaku",
                    "reply", "online"):
            self.assertIn(f"k: '{key}'", html)


def tearDownModule():
    """释放本模块创建的页面，避免 Qt 原生对象堆积到解释器退出。"""
    try:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        from tools.monitor.page import MonitorPage
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    pages = [widget for widget in QApplication.allWidgets()
             if isinstance(widget, MonitorPage)]
    for page in pages:
        page.on_app_close()
        page.close()
        page.deleteLater()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


if __name__ == "__main__":
    unittest.main()
