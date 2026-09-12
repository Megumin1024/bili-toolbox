# -*- coding: utf-8 -*-
"""监控 Webhook 推送通道：白名单 payload、频控、失败路径、开关语义与持久化边界。

全部离线：HTTP 走注入的 sender 桩或仅指向 127.0.0.1 本地接收端；
时钟注入 FakeClock；config 读写 patch 到临时目录，绝不触碰真实 %APPDATA%。
"""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from core import config as app_config
from core import task_history
from tools.monitor.alerts import (AlertConfig, AlertEvent, AlertSession,
                                  WebhookRateLimiter)
from tools.monitor.notifications import (WEBHOOK_TIMEOUT_SECONDS,
                                         RecordingNotificationAdapter,
                                         WebhookAdapter, _post_serverchan,
                                         serverchan_url, webhook_payload)
from tools.monitor.page import MonitorPage


class FakeClock:
    """可手动推进的时钟，供频控滑动窗口测试注入。"""

    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class SenderStub:
    """线程安全的一次性 sender 桩：记录调用并可控返回状态/异常。"""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.calls = []
        self._cond = threading.Condition()

    def __call__(self, url, body, timeout):
        with self._cond:
            self.calls.append((url, body))
            self._cond.notify_all()
        if self.error is not None:
            raise self.error
        return self.status

    def wait_calls(self, count, timeout=5.0):
        deadline = time.time() + timeout
        with self._cond:
            while len(self.calls) < count:
                remaining = deadline - time.time()
                if remaining <= 0 or not self._cond.wait(remaining):
                    break
            return list(self.calls)


class ServerChanSenderStub:
    """线程安全 sender 桩：ServerChan 契约，返回 (status, code, message)。"""

    def __init__(self, status=200, code=0, message="ok", error=None):
        self.status = status
        self.code = code
        self.message = message
        self.error = error
        self.calls = []
        self._cond = threading.Condition()

    def __call__(self, url, body, timeout):
        with self._cond:
            self.calls.append((url, body))
            self._cond.notify_all()
        if self.error is not None:
            raise self.error
        return (self.status, self.code, self.message)

    def wait_calls(self, count, timeout=5.0):
        deadline = time.time() + timeout
        with self._cond:
            while len(self.calls) < count:
                remaining = deadline - time.time()
                if remaining <= 0 or not self._cond.wait(remaining):
                    break
            return list(self.calls)


class _Check:
    def __init__(self, value=False):
        self.value = bool(value)

    def isChecked(self):
        return self.value


class _Text:
    def __init__(self, value=""):
        self.value = str(value)

    def text(self):
        return self.value

    def setText(self, value):
        self.value = str(value)


class _Spin:
    def __init__(self, value=0):
        self.value_ = int(value)

    def value(self):
        return self.value_

    def setValue(self, value):
        self.value_ = int(value)


class _Double(_Spin):
    def __init__(self, value=0):
        self.value_ = float(value)

    def value(self):
        return self.value_

    def setValue(self, value):
        self.value_ = float(value)


class _Combo:
    def __init__(self, value="auto"):
        self.current = value
        self.values = ["auto", "h2-ja3", "urllib"]

    def currentData(self):
        return self.current

    def findData(self, value):
        return self.values.index(value) if value in self.values else -1

    def setCurrentIndex(self, index):
        self.current = self.values[index]


class _Path:
    def __init__(self, value=""):
        self._value = str(value)

    def value(self):
        return self._value


class PayloadWhitelistTests(unittest.TestCase):
    def test_payload_contains_only_whitelist_fields(self):
        payload = webhook_payload("milestone", "播放量里程碑", "播放量已达到 10000。")
        self.assertEqual(set(payload), {"title", "text", "event_type"})
        self.assertEqual(payload["event_type"], "milestone")

    def test_payload_masks_path_and_credential_shaped_text(self):
        # 路径与凭据分开构造：展示层脱敏是整段打码，路径正则会把其后
        # 的文本一并吞掉，两种形态各自断言才指向唯一原因。
        path_payload = webhook_payload("milestone", "标题", r"F:\用户\秘密\路径")
        cred_payload = webhook_payload("milestone", "标题", "password=abc123")
        for payload in (path_payload, cred_payload):
            self.assertEqual(set(payload), {"title", "text", "event_type"})
        body = json.dumps([path_payload, cred_payload], ensure_ascii=False)
        self.assertNotIn("秘密", body)
        self.assertNotIn("abc123", body)
        self.assertIn("[本地路径已脱敏]", path_payload["text"])
        self.assertIn("[已脱敏]", cred_payload["text"])

    def test_adapter_body_is_built_from_whitelist_only(self):
        stub = SenderStub()
        adapter = WebhookAdapter(
            url_getter=lambda: "http://127.0.0.1:9/hook",
            log=lambda _message: None, sender=stub)
        self.assertTrue(adapter.send(
            "spike", "播放量异常突增", r"路径 F:\私密"))
        calls = stub.wait_calls(1)
        self.assertEqual(len(calls), 1)
        url, body = calls[0]
        self.assertEqual(url, "http://127.0.0.1:9/hook")
        text = body.decode("utf-8")
        data = json.loads(text)
        self.assertEqual(set(data), {"title", "text", "event_type"})
        self.assertNotIn("私密", text)

    def test_adapter_skips_send_without_url(self):
        stub = SenderStub()
        logs = []
        adapter = WebhookAdapter(url_getter=lambda: "  ", log=logs.append,
                                 sender=stub)
        self.assertFalse(adapter.send("test", "t", "m"))
        self.assertTrue(any("URL 为空" in message for message in logs))
        self.assertEqual(stub.calls, [])


class RateLimitTests(unittest.TestCase):
    def test_limiter_drops_eleventh_and_recovers_across_window(self):
        clock = FakeClock()
        limiter = WebhookRateLimiter(limit=10, window_seconds=3600, clock=clock)
        for _ in range(10):
            self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        self.assertEqual(limiter.dropped_in_window(), 1)
        self.assertFalse(limiter.try_acquire())
        self.assertEqual(limiter.dropped_in_window(), 2)
        clock.advance(3600)
        self.assertTrue(limiter.try_acquire())
        self.assertEqual(limiter.dropped_in_window(), 0)

    def test_limiter_slides_partially_within_window(self):
        clock = FakeClock()
        limiter = WebhookRateLimiter(limit=3, window_seconds=3600, clock=clock)
        self.assertTrue(limiter.try_acquire())  # t=0
        clock.advance(1000)
        self.assertTrue(limiter.try_acquire())  # t=1000
        clock.advance(1000)
        self.assertTrue(limiter.try_acquire())  # t=2000
        self.assertFalse(limiter.try_acquire())
        clock.advance(1600)  # t=3600：最早一条（t=0）恰好滑出窗口
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())

    def test_adapter_rate_limit_logs_cumulative_count_without_thread(self):
        clock = FakeClock()
        stub = SenderStub()
        logs = []
        adapter = WebhookAdapter(
            url_getter=lambda: "http://127.0.0.1:9/hook", log=logs.append,
            clock=clock, sender=stub,
            rate_limiter=WebhookRateLimiter(
                limit=2, window_seconds=3600, clock=clock))
        self.assertTrue(adapter.send("milestone", "t1", "m1"))
        self.assertTrue(adapter.send("milestone", "t2", "m2"))
        self.assertEqual(len(stub.wait_calls(2)), 2)
        before_threads = threading.active_count()
        self.assertFalse(adapter.send("milestone", "t3", "m3"))
        self.assertTrue(any("本小时已丢弃 1 条" in message for message in logs))
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(threading.active_count(), before_threads)

    def test_manual_test_send_counts_toward_rate_limit(self):
        clock = FakeClock()
        stub = SenderStub()
        logs = []
        adapter = WebhookAdapter(
            url_getter=lambda: "http://127.0.0.1:9/hook", log=logs.append,
            clock=clock, sender=stub,
            rate_limiter=WebhookRateLimiter(
                limit=1, window_seconds=3600, clock=clock))
        page = MonitorPage({
            "out_dir": "",
            "_monitor_webhook_adapter_factory": lambda: adapter,
        })
        page.alert_webhook_check.setChecked(True)
        page.on_test_webhook()
        self.assertEqual(len(stub.wait_calls(1)), 1)
        page.on_test_webhook()
        self.assertTrue(any("本小时已丢弃 1 条" in message for message in logs))
        self.assertEqual(len(stub.calls), 1)
        page.on_app_close()


class QtEnvironmentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wait_for_log(self, logs, needle, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(needle in message for message in logs):
                return True
            self.app.processEvents()
            time.sleep(0.02)
        return False


class WebhookFailurePathTests(QtEnvironmentTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        class Receiver(BaseHTTPRequestHandler):
            status_code = 200
            captured = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                type(self).captured.append(self.rfile.read(length))
                self.send_response(type(self).status_code)
                self.end_headers()

            def log_message(self, *args):
                pass

        cls.receiver = Receiver
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_local_receiver_success_posts_json_payload(self):
        type(self).receiver.captured.clear()
        stub_logs = []
        adapter = WebhookAdapter(
            url_getter=lambda: f"http://127.0.0.1:{self.port}/hook",
            log=stub_logs.append)
        self.assertTrue(adapter.send(
            "milestone", "播放量里程碑", "播放量已达到 10000。"))
        deadline = time.time() + 5
        while not self.receiver.captured and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.receiver.captured), 1)
        data = json.loads(self.receiver.captured[0].decode("utf-8"))
        self.assertEqual(
            data, {"title": "播放量里程碑", "text": "播放量已达到 10000。",
                   "event_type": "milestone"})
        self.assertTrue(self._wait_for_log(stub_logs, "Webhook 推送已发送"))

    def test_local_receiver_500_is_logged_and_does_not_bubble(self):
        type(self).receiver.status_code = 500
        try:
            stub_logs = []
            adapter = WebhookAdapter(
                url_getter=lambda: f"http://127.0.0.1:{self.port}/hook",
                log=stub_logs.append)
            self.assertTrue(adapter.send("spike", "标题", "内容"))
            self.assertTrue(self._wait_for_log(stub_logs, "HTTP 500"))
            self.assertTrue(
                any("[网络地址已脱敏]" in message for message in stub_logs))
            self.assertFalse(
                any("http://127.0.0.1" in message for message in stub_logs))
        finally:
            type(self).receiver.status_code = 200

    def test_unreachable_port_connection_error_is_logged(self):
        stub_logs = []
        adapter = WebhookAdapter(
            url_getter=lambda: "http://127.0.0.1:1/hook",
            log=stub_logs.append)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertTrue(self._wait_for_log(stub_logs, "Webhook 发送失败"))
        self.assertTrue(
            any("[网络地址已脱敏]" in message for message in stub_logs))

    def test_timeout_sender_error_is_logged_without_bubble(self):
        def timeout_sender(_url, _body, _timeout):
            raise socket.timeout("timed out")

        stub_logs = []
        adapter = WebhookAdapter(
            url_getter=lambda: "http://10.255.255.1:81/hook",
            log=stub_logs.append, timeout=WEBHOOK_TIMEOUT_SECONDS,
            sender=timeout_sender)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertTrue(self._wait_for_log(stub_logs, "TimeoutError"))
        self.assertTrue(
            any("[网络地址已脱敏]" in message for message in stub_logs))

    def test_page_stays_usable_after_failed_send(self):
        def failing_sender(_url, _body, _timeout):
            raise OSError("connection refused")

        logs = []

        def factory():
            return WebhookAdapter(
                url_getter=lambda: "http://127.0.0.1:1/hook",
                log=logs.append, sender=failing_sender)

        page = MonitorPage({
            "out_dir": "",
            "_monitor_webhook_adapter_factory": factory,
        })
        page.alert_webhook_check.setChecked(True)
        page.on_test_webhook()
        self.assertTrue(self._wait_for_log(logs, "Webhook 发送失败"))
        self.assertIsNotNone(page.webhook_adapter)
        self.assertTrue(page.btn_test_webhook.isEnabled())
        page.on_app_close()

    def test_default_adapter_logs_via_signal_into_panel(self):
        page = MonitorPage({"out_dir": ""})
        page.alert_webhook_check.setChecked(True)
        page.on_test_webhook()  # URL 为空 → 默认适配器经 webhook_log 信号记日志
        self.assertTrue(self._wait_for_log(
            page.log_panel.toPlainText().splitlines(), "URL 为空"))
        page.on_app_close()


class SwitchSemanticsTests(QtEnvironmentTestCase):
    def test_disabled_channel_creates_no_adapter_thread_or_request(self):
        factory_calls = []
        stub = SenderStub()

        def factory():
            factory_calls.append(1)
            return stub

        page = MonitorPage({
            "out_dir": "",
            "_monitor_webhook_adapter_factory": factory,
        })
        self.assertFalse(page.alert_webhook_check.isChecked())
        before_threads = threading.active_count()
        self.assertFalse(page._deliver_webhook("milestone", "t", "m", False))
        self.assertIsNone(page.webhook_adapter)
        self.assertEqual(factory_calls, [])
        self.assertEqual(stub.calls, [])
        self.assertEqual(threading.active_count(), before_threads)
        page.on_app_close()

    def test_publish_alert_with_channel_off_never_touches_webhook(self):
        page = MonitorPage({"out_dir": ""})
        page.alert_session = AlertSession(
            AlertConfig.from_mapping({"enabled": True, "webhook_enabled": False}),
            "s1")
        page._publish_alert(AlertEvent(
            "milestone", "播放量里程碑", "播放量已达到 10000。", 1000.0))
        self.assertIsNone(page.webhook_adapter)
        page.on_app_close()

    def test_master_switch_off_produces_no_events_at_all(self):
        session = AlertSession(AlertConfig.from_mapping({"enabled": False}), "s1")
        self.assertEqual(session.process_sample(0, 100), [])
        self.assertEqual(session.process_failure(1, 3), [])

    def test_checked_channel_test_button_sends_one(self):
        class RecordingAdapter:
            def __init__(self):
                self.sent = []

            def send(self, event_type, title, text):
                self.sent.append((event_type, title, text))
                return True

        adapter = RecordingAdapter()
        page = MonitorPage({
            "out_dir": "",
            "_monitor_webhook_adapter_factory": lambda: adapter,
        })
        page.alert_webhook_check.setChecked(True)
        page.on_test_webhook()
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0][0], "test")
        page.on_app_close()

    def test_test_alert_requires_at_least_one_channel(self):
        adapter = RecordingNotificationAdapter()
        page = MonitorPage({"out_dir": ""})
        page.notification_adapter = adapter
        page.alert_windows_check.setChecked(False)
        page.alert_sound_check.setChecked(False)
        page.alert_webhook_check.setChecked(False)
        with patch("tools.monitor.page.QMessageBox.information") as info:
            page.on_test_alert()
        info.assert_called_once()
        self.assertEqual(adapter.notifications, [])
        self.assertEqual(adapter.sound_count, 0)
        self.assertIsNone(page.webhook_adapter)
        page.on_app_close()

    def test_test_alert_with_webhook_only_skips_qt_channels(self):
        stub = SenderStub()
        adapter = WebhookAdapter(
            url_getter=lambda: "http://127.0.0.1:9/hook",
            log=lambda _message: None, sender=stub)
        qt_adapter = RecordingNotificationAdapter()
        page = MonitorPage({
            "out_dir": "",
            "_monitor_webhook_adapter_factory": lambda: adapter,
        })
        page.notification_adapter = qt_adapter
        page.alert_windows_check.setChecked(False)
        page.alert_sound_check.setChecked(False)
        page.alert_webhook_check.setChecked(True)
        with patch("tools.monitor.page.QMessageBox.information"):
            page.on_test_alert()
        calls = stub.wait_calls(1)
        self.assertEqual(len(calls), 1)
        data = json.loads(calls[0][1].decode("utf-8"))
        self.assertEqual(data["event_type"], "test")
        self.assertEqual(qt_adapter.notifications, [])
        self.assertEqual(qt_adapter.sound_count, 0)
        page.on_app_close()


class PersistenceBoundaryTests(QtEnvironmentTestCase):
    @staticmethod
    def _preset_page(url):
        """与基线 preset 测试相同的 __new__ 假控件页面（URL 已填入）。"""
        page = MonitorPage.__new__(MonitorPage)
        page.bvid_edit = _Text("BV1")
        page.interval_spin = _Spin(60)
        page.transport_combo = _Combo("auto")
        page.data_row = _Path("")
        page.alert_total_check = _Check(True)
        page.alert_windows_check = _Check(False)
        page.alert_sound_check = _Check(True)
        page.alert_webhook_check = _Check(True)
        page.webhook_url_edit = _Text(url)
        page.alert_milestone_check = _Check(True)
        page.alert_milestone_edit = _Text("10000")
        page.alert_stagnation_check = _Check(False)
        page.alert_stagnation_window_spin = _Spin(30)
        page.alert_stagnation_growth_spin = _Spin(0)
        page.alert_spike_check = _Check(True)
        page.alert_spike_window_spin = _Spin(5)
        page.alert_spike_absolute_spin = _Spin(10000)
        page.alert_spike_relative_spin = _Double(20)
        page.alert_spike_cooldown_spin = _Spin(10)
        page.alert_disconnect_check = _Check(True)
        page.alert_disconnect_failures_spin = _Spin(3)
        return page

    def test_collect_params_exclude_url_and_history_accepts_safe_params(self):
        page = self._preset_page("https://example.com/hook/secret")
        params = page.collect_preset_params()
        self.assertNotIn("webhook_url", params)
        self.assertNotIn("webhook_url", params["alerts"])
        self.assertNotIn("example.com", json.dumps(params, ensure_ascii=False))
        safe, reusable = task_history.prepare_reusable_params(params)
        self.assertTrue(reusable)
        self.assertNotIn("example.com", json.dumps(safe, ensure_ascii=False))

    def test_apply_old_preset_without_webhook_fields_does_not_raise(self):
        page = MonitorPage({"out_dir": ""})
        old_params = {
            "bvid": "BV1old",
            "interval": 30,
            "transport": "auto",
            "data_dir": "",
            "alerts": {
                "enabled": True,
                "windows_enabled": True,
                "milestones": "10000",
            },
        }
        page.apply_preset_params(old_params)
        self.assertFalse(page.alert_webhook_check.isChecked())
        self.assertEqual(page.bvid_edit.text(), "BV1old")
        page.on_app_close()

    def test_apply_new_preset_restores_webhook_switch(self):
        page = MonitorPage({"out_dir": ""})
        page.alert_webhook_check.setChecked(True)
        params = page.collect_preset_params()
        page.alert_webhook_check.setChecked(False)
        page.apply_preset_params(params)
        self.assertTrue(page.alert_webhook_check.isChecked())
        page.on_app_close()

    def test_config_roundtrip_old_file_defaults_empty_then_persists(self):
        with tempfile.TemporaryDirectory(prefix="webhook_cfg_") as tmp:
            root = Path(tmp)
            target = root / "config.json"
            target.write_text(
                json.dumps({"theme": "light", "proxy_spec": "direct"}),
                encoding="utf-8")
            with patch.object(app_config, "CONFIG_DIR", root), \
                    patch.object(app_config, "CONFIG_FILE", target):
                cfg = app_config.load()
                self.assertEqual(cfg.get("webhook_url"), "")
                self.assertEqual(cfg.get("theme"), "light")
                app_config.save({"webhook_url": "https://example.com/hook"})
                reloaded = app_config.load()
            self.assertEqual(
                reloaded.get("webhook_url"), "https://example.com/hook")
            self.assertEqual(reloaded.get("theme"), "light")
            self.assertEqual(reloaded.get("proxy_spec"), "direct")
            self.assertEqual(
                sorted(p.name for p in root.glob(".config.json.*.tmp")), [])

    def test_page_save_webhook_url_writes_config_only(self):
        with tempfile.TemporaryDirectory(prefix="webhook_cfg_") as tmp:
            root = Path(tmp)
            target = root / "config.json"
            with patch.object(app_config, "CONFIG_DIR", root), \
                    patch.object(app_config, "CONFIG_FILE", target):
                page = MonitorPage({"out_dir": ""})
                page.webhook_url_edit.setText("https://example.com/hook")
                page._save_webhook_url()
                stored = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(
                    stored.get("webhook_url"), "https://example.com/hook")
                self.assertEqual(
                    page.cfg.get("webhook_url"), "https://example.com/hook")
                page.webhook_url_edit.setText("https://example.com/hook")
                page._save_webhook_url()  # 未变化时不重写
                page.on_app_close()
            self.assertEqual(
                sorted(p.name for p in root.glob(".config.json.*.tmp")), [])

    def test_alert_config_webhook_default_off_and_roundtrip(self):
        self.assertFalse(AlertConfig().webhook_enabled)
        self.assertFalse(
            AlertConfig.from_mapping({"enabled": True}).webhook_enabled)
        mapping = AlertConfig.from_mapping(
            {"webhook_enabled": True}).to_mapping()
        self.assertEqual(mapping["webhook_enabled"], True)
        self.assertEqual(
            AlertConfig.from_mapping(mapping).webhook_enabled, True)


class ServerChanUrlNormalizationTests(unittest.TestCase):
    """serverchan_url 纯函数：三种输入形态与拒绝边界。"""

    def test_bare_sendkey_wrapped_and_trimmed(self):
        self.assertEqual(
            serverchan_url("SCUabcdef12"), "https://sctapi.ftqq.com/SCUabcdef12.send")
        self.assertEqual(
            serverchan_url("  SCUabcdef12 "),
            "https://sctapi.ftqq.com/SCUabcdef12.send")

    def test_full_send_url_used_as_is(self):
        url = "https://sctapi.ftqq.com/SCUabcdef12.send"
        self.assertEqual(serverchan_url(url), url)
        # 原样使用：大小写与查询参数都保留，不重写
        upper = "HTTP://SCTAPI.FTQQ.COM/SCUabcdef12.SEND"
        self.assertEqual(serverchan_url(upper), upper)
        with_query = "https://sctapi.ftqq.com/SCUabcdef12.send?channel=monitor"
        self.assertEqual(serverchan_url(with_query), with_query)

    def test_rejected_forms(self):
        for bad in ("", "   ", None, "short", "SCU短", "abc.def",
                    "key with space", "sctapi.ftqq.com/SCUabcdef12.send",
                    "ftp://sctapi.ftqq.com/SCUabcdef12.send",
                    "https://example.com/SCUabcdef12.send",
                    "https://sctapi.ftqq.com.evil.com/SCUabcdef12.send",
                    "https://sctapi.ftqq.com/SCUabcdef12"):
            self.assertIsNone(serverchan_url(bad), repr(bad))


class ServerChanSendBranchTests(unittest.TestCase):
    """Server酱 发送分支：归一化、form 编码、format 发送时读取、频控共享。

    全部离线：sender 为注入桩，不创建真实网络连接。
    """

    def _serverchan_adapter(self, url, stub, log=None, **kwargs):
        return WebhookAdapter(
            url_getter=lambda: url, format_getter=lambda: "serverchan",
            log=log or (lambda _message: None), serverchan_sender=stub,
            **kwargs)

    def test_unrecognized_input_zero_action_and_input_not_logged(self):
        for bad in ("https://example.com/hook", "abc.def", "SCU短"):
            stub = ServerChanSenderStub()
            logs = []
            adapter = self._serverchan_adapter(bad, stub, log=logs.append)
            before_threads = threading.active_count()
            self.assertFalse(adapter.send("test", "标题", "内容"))
            self.assertEqual(stub.calls, [])
            self.assertEqual(threading.active_count(), before_threads)
            self.assertTrue(
                any("Server酱密钥格式不认识" in message for message in logs))
            # 原始输入可能就是 SendKey，绝不回显进日志
            self.assertTrue(all(bad not in message for message in logs))

    def test_form_body_keys_exactly_title_desp_and_sanitized(self):
        stub = ServerChanSenderStub()
        adapter = self._serverchan_adapter(
            "SCUabcdef12", stub)
        self.assertTrue(adapter.send(
            "milestone", "播放量里程碑", "password=abc123 通知"))
        url, body = stub.wait_calls(1)[0]
        self.assertEqual(url, "https://sctapi.ftqq.com/SCUabcdef12.send")
        parsed = parse_qs(body.decode("utf-8"))
        self.assertEqual(set(parsed), {"title", "desp"})
        self.assertEqual(parsed["title"], ["播放量里程碑"])
        self.assertEqual(parsed["desp"], ["password=[已脱敏] 通知"])
        self.assertNotIn(b"abc123", body)

    def test_format_read_at_send_time_switch_takes_effect(self):
        fmt = ["json"]
        json_stub = SenderStub()
        sc_stub = ServerChanSenderStub()
        adapter = WebhookAdapter(
            url_getter=lambda: "https://sctapi.ftqq.com/SCUabcdef12.send",
            format_getter=lambda: fmt[0], log=lambda _message: None,
            sender=json_stub, serverchan_sender=sc_stub)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertEqual(len(json_stub.wait_calls(1)), 1)
        self.assertEqual(sc_stub.calls, [])
        fmt[0] = "serverchan"  # 切换下拉：不重建适配器，下一发即时生效
        self.assertTrue(adapter.send("test", "标题", "内容"))
        calls = sc_stub.wait_calls(1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].startswith(b"title="))
        self.assertEqual(len(json_stub.calls), 1)

    def test_invalid_format_value_falls_back_to_json(self):
        stub = SenderStub()
        adapter = WebhookAdapter(
            url_getter=lambda: "http://127.0.0.1:9/hook",
            format_getter=lambda: "dingtalk", log=lambda _message: None,
            sender=stub)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        body = stub.wait_calls(1)[0][1]
        self.assertTrue(body.startswith(b"{"))

    def test_serverchan_shares_rate_limit_budget(self):
        clock = FakeClock()
        stub = ServerChanSenderStub()
        logs = []
        adapter = self._serverchan_adapter(
            "SCUabcdef12", stub, log=logs.append, clock=clock,
            rate_limiter=WebhookRateLimiter(
                limit=1, window_seconds=3600, clock=clock))
        self.assertTrue(adapter.send("test", "t", "m"))
        self.assertEqual(len(stub.wait_calls(1)), 1)
        self.assertFalse(adapter.send("test", "t", "m"))
        self.assertTrue(any("本小时已丢弃 1 条" in message for message in logs))
        self.assertEqual(len(stub.calls), 1)

    def test_serverchan_success_and_code_failures_logged(self):
        stub = ServerChanSenderStub(status=200, code=0, message="ok")
        logs = []
        adapter = self._serverchan_adapter("SCUabcdef12", stub,
                                           log=logs.append)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        stub.wait_calls(1)
        self.assertTrue(any("Server酱 推送已发送" in message
                            for message in logs))

        stub = ServerChanSenderStub(status=200, code=40001,
                                    message="key错误")
        logs = []
        adapter = self._serverchan_adapter("SCUabcdef12", stub,
                                           log=logs.append)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        stub.wait_calls(1)
        self.assertTrue(any(
            "Server酱 推送失败" in message and "code=40001" in message
            and "key错误" in message for message in logs))
        self.assertFalse(any("推送已发送" in message for message in logs))


class WebhookFormatPersistenceTests(QtEnvironmentTestCase):
    """webhook_format 的 config 往返、非法值回退与预设/历史边界。"""

    def test_config_defaults_and_roundtrip(self):
        self.assertEqual(app_config.DEFAULTS.get("webhook_format"), "json")
        with tempfile.TemporaryDirectory(prefix="webhook_fmt_") as tmp:
            root = Path(tmp)
            target = root / "config.json"
            target.write_text(
                json.dumps({"theme": "light", "webhook_url": "u"}),
                encoding="utf-8")
            with patch.object(app_config, "CONFIG_DIR", root), \
                    patch.object(app_config, "CONFIG_FILE", target):
                cfg = app_config.load()
                self.assertEqual(cfg.get("webhook_format"), "json")
                self.assertEqual(cfg.get("theme"), "light")
                app_config.save({"webhook_format": "serverchan"})
                reloaded = app_config.load()
            self.assertEqual(reloaded.get("webhook_format"), "serverchan")
            self.assertEqual(reloaded.get("theme"), "light")
            self.assertEqual(reloaded.get("webhook_url"), "u")
            self.assertEqual(
                sorted(p.name for p in root.glob(".config.json.*.tmp")), [])

    def test_invalid_config_format_falls_back_to_json(self):
        page = MonitorPage({"out_dir": "", "webhook_format": "dingtalk"})
        self.assertEqual(page.webhook_format_combo.currentData(), "json")
        self.assertEqual(page._webhook_format_value(), "json")
        self.assertEqual(
            page.webhook_url_edit.placeholderText(),
            "https://…（接收端 URL，仅保存在本机配置文件）")
        page.on_app_close()

    def test_format_combo_roundtrip_and_placeholder_switch(self):
        with tempfile.TemporaryDirectory(prefix="webhook_fmt_") as tmp:
            root = Path(tmp)
            target = root / "config.json"
            with patch.object(app_config, "CONFIG_DIR", root), \
                    patch.object(app_config, "CONFIG_FILE", target):
                page = MonitorPage({"out_dir": ""})
                self.assertEqual(
                    page.webhook_format_combo.currentData(), "json")
                page.webhook_format_combo.setCurrentIndex(1)
                self.assertEqual(page._webhook_format_value(), "serverchan")
                self.assertEqual(
                    json.loads(target.read_text(encoding="utf-8"))
                    .get("webhook_format"), "serverchan")
                self.assertEqual(
                    page.webhook_url_edit.placeholderText(),
                    "填 SendKey 或 .send 完整链接（仅保存在本机配置文件）")
                self.assertEqual(
                    app_config.load().get("webhook_format"), "serverchan")
                page.webhook_format_combo.setCurrentIndex(0)
                self.assertEqual(
                    app_config.load().get("webhook_format"), "json")
                page.on_app_close()
            self.assertEqual(
                sorted(p.name for p in root.glob(".config.json.*.tmp")), [])

    def test_default_adapter_reads_format_at_send_time(self):
        with tempfile.TemporaryDirectory(prefix="webhook_fmt_") as tmp:
            root = Path(tmp)
            with patch.object(app_config, "CONFIG_DIR", root), \
                    patch.object(app_config, "CONFIG_FILE", root / "config.json"):
                page = MonitorPage({"out_dir": ""})
                adapter = page._new_webhook_adapter()
                self.assertEqual(adapter._format(), "json")
                page.webhook_format_combo.setCurrentIndex(1)
                # 不重建适配器：格式 getter 在每次发送时读取下拉当前值
                self.assertEqual(adapter._format(), "serverchan")
                page.on_app_close()

    def test_format_combo_follows_channel_switch(self):
        page = MonitorPage({"out_dir": ""})
        self.assertFalse(page.webhook_format_combo.isEnabled())
        page.alert_webhook_check.setChecked(True)
        self.assertTrue(page.webhook_format_combo.isEnabled())
        page.alert_webhook_check.setChecked(False)
        self.assertFalse(page.webhook_format_combo.isEnabled())
        page.on_app_close()

    def test_format_never_enters_presets(self):
        with tempfile.TemporaryDirectory(prefix="webhook_fmt_") as tmp:
            root = Path(tmp)
            with patch.object(app_config, "CONFIG_DIR", root), \
                    patch.object(app_config, "CONFIG_FILE", root / "config.json"):
                page = MonitorPage({"out_dir": ""})
                page.webhook_format_combo.setCurrentIndex(1)
                params = page.collect_preset_params()
                self.assertNotIn("webhook_format", params)
                self.assertNotIn(
                    "webhook_format", json.dumps(params, ensure_ascii=False))
                page.on_app_close()


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
