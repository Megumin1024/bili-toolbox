# -*- coding: utf-8 -*-
"""监控提醒规则、事件隔离、通道降级和页面 Signal 回归测试。"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from tools.monitor.alerts import (MAX_WINDOW_MINUTES,
                                  MIN_SAMPLE_INTERVAL_SECONDS, AlertConfig,
                                  AlertSession, _count, parse_milestones)
from tools.monitor.notifications import QtNotificationAdapter, RecordingNotificationAdapter
from tools.monitor.page import MonitorPage
from tools.monitor.server import MonitorServer, _State


def enabled_config(**overrides):
    values = {
        "enabled": True,
        "milestones": "10000,50000,100000",
        "stagnation_window_min": 30,
        "stagnation_max_growth": 0,
        "spike_window_min": 5,
        "spike_min_absolute": 10000,
        "spike_min_relative_percent": 20,
        "spike_cooldown_min": 10,
        "disconnect_failures": 3,
    }
    values.update(overrides)
    return AlertConfig.from_mapping(values)


class AlertRuleTests(unittest.TestCase):
    def test_milestone_parser_normalizes_values(self):
        self.assertEqual(parse_milestones(" 10000,50000,10000, bad, 0 "), (10000, 50000))

    def test_long_numeric_text_is_ignored_without_interrupting_samples(self):
        too_long = "9" * 5000
        self.assertEqual(parse_milestones(f"{too_long}, 100, bad, 200"), (100, 200))
        self.assertIsNone(_count(too_long))
        session = AlertSession(enabled_config(milestones="100"), "s1")
        self.assertEqual(session.process_sample(0, too_long), [])
        self.assertEqual(session.process_sample(1, 50), [])
        self.assertEqual(session.process_sample(2, 100)[0].kind, "milestone")

    def test_first_sample_is_baseline_without_old_milestone_replay(self):
        session = AlertSession(enabled_config(milestones="100,200"), "s1")
        self.assertEqual(session.process_sample(0, 150), [])
        self.assertTrue(session.baseline_set)
        self.assertEqual(session.process_sample(60, 199), [])
        events = session.process_sample(120, 200)
        self.assertEqual([event.kind for event in events], ["milestone"])
        self.assertIn(100, session.fired_milestones)

    def test_exact_and_multiple_milestones_fire_once(self):
        session = AlertSession(enabled_config(milestones="100,200,300"), "s1")
        session.process_sample(0, 50)
        events = session.process_sample(60, 250)
        self.assertEqual(len(events), 1)
        self.assertIn("100、200", events[0].message)
        self.assertEqual(session.process_sample(120, 300)[0].kind, "milestone")
        self.assertEqual(session.process_sample(180, 350), [])

    def test_stagnation_window_and_rearm(self):
        session = AlertSession(enabled_config(milestones=(), spike_enabled=False), "s1")
        self.assertEqual(session.process_sample(0, 100), [])
        self.assertEqual(session.process_sample(29 * 60, 100), [])
        self.assertEqual(session.process_sample(30 * 60, 100)[0].kind, "stagnation")
        self.assertEqual(session.process_sample(31 * 60, 100), [])
        self.assertEqual(session.process_sample(32 * 60, 101), [])
        events = session.process_sample(62 * 60, 101)
        self.assertEqual([event.kind for event in events], ["stagnation"])

    def test_long_legal_window_survives_old_sample_capacity(self):
        session = AlertSession(enabled_config(
            milestones=(), spike_enabled=False,
            stagnation_window_min=MAX_WINDOW_MINUTES), "long-window")
        session.process_sample(0, 100)
        sample_count = MAX_WINDOW_MINUTES * 60 // MIN_SAMPLE_INTERVAL_SECONDS
        events = []
        for index in range(1, sample_count + 1):
            events = session.process_sample(index * MIN_SAMPLE_INTERVAL_SECONDS, 100)
        self.assertGreater(len(session._samples), 10000)
        self.assertEqual([event.kind for event in events], ["stagnation"])

    def test_recovery_resets_rule_windows_but_preserves_milestones_and_disconnect(self):
        session = AlertSession(enabled_config(
            milestones="1000", stagnation_window_min=1,
            stagnation_max_growth=0, spike_window_min=1,
            spike_min_absolute=50, spike_min_relative_percent=20,
            spike_cooldown_min=0), "recovery")
        session.process_sample(0, 100)
        self.assertEqual(session.process_failure(1, 3)[0].kind, "disconnect")

        recovered = session.process_sample(120, 100)
        self.assertEqual([event.kind for event in recovered], ["recovered"])

        stagnation = session.process_sample(180, 100)
        self.assertEqual([event.kind for event in stagnation], ["stagnation"])
        spike = session.process_sample(240, 200)
        self.assertEqual([event.kind for event in spike], ["spike"])

        milestone_events = session.process_sample(300, 1000)
        milestone_events += session.process_sample(360, 1000)
        self.assertEqual(
            sum(event.kind == "milestone" for event in milestone_events), 1)
        self.assertEqual(session.process_failure(361, 3)[0].kind, "disconnect")

    def test_spike_requires_absolute_and_relative_thresholds(self):
        absolute_only = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False,
            spike_min_absolute=10000, spike_min_relative_percent=20), "s1")
        absolute_only.process_sample(0, 100000)
        self.assertEqual(absolute_only.process_sample(5 * 60, 110000), [])

        relative_only = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False,
            spike_min_absolute=10000, spike_min_relative_percent=20), "s2")
        relative_only.process_sample(0, 10000)
        self.assertEqual(relative_only.process_sample(5 * 60, 15000), [])

        both = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False,
            spike_min_absolute=10000, spike_min_relative_percent=20), "s3")
        both.process_sample(0, 50000)
        self.assertEqual(both.process_sample(5 * 60, 60000)[0].kind, "spike")

    def test_spike_ignores_missing_zero_time_backwards_and_decrease(self):
        missing = AlertSession(enabled_config(milestones=(), stagnation_enabled=False), "s1")
        missing.process_sample(0, 50000)
        self.assertEqual(missing.process_sample(5 * 60, None), [])

        zero = AlertSession(enabled_config(milestones=(), stagnation_enabled=False), "s2")
        zero.process_sample(0, 0)
        self.assertEqual(zero.process_sample(5 * 60, 20000), [])

        backwards = AlertSession(enabled_config(milestones=(), stagnation_enabled=False), "s3")
        backwards.process_sample(300, 50000)
        self.assertEqual(backwards.process_sample(299, 70000), [])

        decrease = AlertSession(enabled_config(milestones=(), stagnation_enabled=False), "s4")
        decrease.process_sample(0, 60000)
        self.assertEqual(decrease.process_sample(5 * 60, 40000), [])

    def test_spike_cooldown(self):
        session = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False, spike_cooldown_min=10), "s1")
        session.process_sample(0, 50000)
        self.assertEqual(session.process_sample(5 * 60, 60000)[0].kind, "spike")
        self.assertEqual(session.process_sample(6 * 60, 75000), [])
        self.assertEqual(session.process_sample(15 * 60, 90000)[0].kind, "spike")

    def test_disconnect_deduplicates_and_recovers(self):
        session = AlertSession(enabled_config(milestones=(), stagnation_enabled=False,
                                               spike_enabled=False), "s1")
        self.assertEqual(session.process_failure(0, 2), [])
        self.assertEqual(session.process_failure(1, 3)[0].kind, "disconnect")
        self.assertEqual(session.process_failure(2, 4), [])
        events = session.process_sample(3, 100)
        self.assertEqual([event.kind for event in events], ["recovered"])

    def test_disconnect_recovery_and_rearm_without_duplicate(self):
        session = AlertSession(enabled_config(milestones=(), stagnation_enabled=False,
                                               spike_enabled=False), "s1")
        session.process_sample(0, 100)
        self.assertEqual(session.process_failure(1, 3)[0].kind, "disconnect")
        self.assertEqual(session.process_failure(2, 3), [])
        self.assertEqual(session.process_sample(3, 101)[0].kind, "recovered")
        self.assertEqual(session.process_failure(4, 3)[0].kind, "disconnect")

    def test_stop_has_no_disconnect_or_recovery(self):
        session = AlertSession(enabled_config(milestones=(), stagnation_enabled=False,
                                               spike_enabled=False), "s1")
        session.stop()
        self.assertEqual(session.process_failure(0, 3), [])
        self.assertEqual(session.process_sample(1, 100), [])

    def test_global_and_rule_switches(self):
        disabled = AlertSession(AlertConfig.from_mapping({"enabled": False}), "s1")
        self.assertEqual(disabled.process_sample(0, 1), [])
        self.assertEqual(disabled.process_failure(0, 3), [])
        no_disconnect = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False, spike_enabled=False,
            disconnect_enabled=False), "s2")
        self.assertEqual(no_disconnect.process_failure(0, 3), [])

    def test_notification_text_has_no_credentials_or_sensitive_paths(self):
        session = AlertSession(enabled_config(
            milestones="100", stagnation_window_min=1,
            spike_window_min=1, spike_min_absolute=10,
            spike_min_relative_percent=20, disconnect_failures=1), "s1")
        texts = []
        session.process_sample(0, 50)
        texts.extend(event.message for event in session.process_sample(60, 100))
        texts.extend(event.message for event in session.process_failure(120, 1))
        forbidden = ("Cookie", "Token", "Authorization", "Bearer", "password", "F:\\", "/")
        for message in texts:
            self.assertFalse(any(word in message for word in forbidden), message)


class ChannelAndBoundaryTests(unittest.TestCase):
    def test_notification_and_sound_channels_are_independent(self):
        adapter = RecordingNotificationAdapter()
        page = MonitorPage.__new__(MonitorPage)
        page.notification_adapter = adapter
        page._log = lambda _message: None
        self.assertEqual(
            page._deliver_channels("title", "message", True, False),
            {"windows": True, "sound": None},
        )
        self.assertEqual(len(adapter.notifications), 1)
        self.assertEqual(adapter.sound_count, 0)
        self.assertEqual(
            page._deliver_channels("title", "message", False, True),
            {"windows": None, "sound": True},
        )
        self.assertEqual(adapter.sound_count, 1)

    def test_notification_failure_does_not_block_sound(self):
        adapter = RecordingNotificationAdapter(fail_notify=True)
        logs = []
        page = MonitorPage.__new__(MonitorPage)
        page.notification_adapter = adapter
        page._log = logs.append
        self.assertEqual(
            page._deliver_channels("title", "message", True, True),
            {"windows": False, "sound": True},
        )
        self.assertEqual(adapter.sound_count, 1)
        self.assertTrue(any("声音通道继续" in message for message in logs))

    def test_test_button_reports_partial_failure_and_sound_success(self):
        adapter = RecordingNotificationAdapter(fail_notify=True)
        logs = []
        page = MonitorPage.__new__(MonitorPage)
        page.notification_adapter = adapter
        page.alert_windows_check = _Check(True)
        page.alert_sound_check = _Check(True)
        page._log = logs.append
        page.on_test_alert()
        self.assertEqual(adapter.sound_count, 1)
        self.assertTrue(any("测试提醒部分成功" in message for message in logs))
        self.assertFalse(any("测试提醒发送成功" in message for message in logs))

    def test_delivery_feedback_distinguishes_all_channels_failed(self):
        class FailingAdapter:
            def notify(self, _title, _message):
                raise RuntimeError("notification unavailable")

            def play_sound(self):
                return False

        logs = []
        page = MonitorPage.__new__(MonitorPage)
        page.notification_adapter = FailingAdapter()
        page.alert_windows_check = _Check(True)
        page.alert_sound_check = _Check(True)
        page._log = logs.append
        with patch("tools.monitor.page.QMessageBox.information"):
            page.on_test_alert()
        self.assertTrue(any("测试提醒发送失败/系统不可用" in message for message in logs))
        self.assertFalse(any("测试提醒发送成功" in message for message in logs))

    def test_unavailable_tray_channel_degrades_without_notification(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
        app = QApplication.instance() or QApplication([])
        del app
        logs = []
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            adapter = QtNotificationAdapter(log=logs.append)
            self.assertFalse(adapter.notify("title", "message"))
            adapter.close()
        self.assertTrue(any("Windows通知不可用" in message for message in logs))

    def test_test_button_with_both_channels_off_gives_feedback_and_does_nothing(self):
        adapter = RecordingNotificationAdapter()
        page = MonitorPage.__new__(MonitorPage)
        page.alert_windows_check = _Check(False)
        page.alert_sound_check = _Check(False)
        page.notification_adapter = adapter
        page._log = Mock()
        with patch("tools.monitor.page.QMessageBox.information") as info:
            page.on_test_alert()
        info.assert_called_once()
        self.assertEqual(adapter.notifications, [])
        self.assertEqual(adapter.sound_count, 0)

    def test_notification_adapter_factory_is_lazy_and_recreated(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        created = []

        def factory():
            adapter = RecordingNotificationAdapter()
            created.append(adapter)
            return adapter

        page = MonitorPage({
            "out_dir": "",
            "_monitor_notification_adapter_factory": factory,
        })
        self.assertIsNone(page.notification_adapter)
        self.assertEqual(created, [])

        class FakeServer:
            def __init__(self, **_kwargs):
                self.running = False

            def start(self):
                self.running = True
                return "http://127.0.0.1:0"

            def stop(self):
                self.running = False

        page.bvid_edit.setText("BV1" + "a" * 9)
        with patch("tools.monitor.page.MonitorServer", FakeServer), \
                patch("tools.monitor.page.webbrowser.open"):
            page.on_start()
        self.assertIsNone(page.notification_adapter)
        page.on_stop()

        page.alert_windows_check.setChecked(False)
        page.alert_sound_check.setChecked(False)
        self.assertEqual(
            page._deliver_channels("title", "message", False, False),
            {"windows": None, "sound": None},
        )
        self.assertEqual(created, [])

        page.alert_windows_check.setChecked(True)
        self.assertEqual(page._deliver_channels("title", "message", True, False)["windows"], True)
        self.assertEqual(len(created), 1)
        page.on_stop()
        self.assertTrue(created[0].closed)
        self.assertIsNone(page.notification_adapter)

        page.on_test_alert()
        self.assertEqual(len(created), 2)
        page.on_app_close()
        self.assertTrue(created[1].closed)

    def test_monitor_server_callback_exception_isolated(self):
        server = MonitorServer.__new__(MonitorServer)
        server.event_callback = lambda _event: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertIsNone(server._emit_event(
            {"type": "sample_success", "session_id": "s", "ts": 1}))

    def test_monitor_server_loop_continues_after_callback_exception(self):
        server = MonitorServer.__new__(MonitorServer)
        server.bvid = "BV1Test"
        server.interval = 1
        server.session_id = "s1"
        server.log = lambda _message: None
        server.event_callback = lambda _event: (_ for _ in ()).throw(RuntimeError("boom"))
        server.state = _State(server.bvid, server.interval)
        server.history_file = None
        server._stop = threading.Event()
        server.client = Mock()
        server.client.stats = {"last_transport": "test", "last_latency_ms": 1}
        sample = {
            "ts": 1700000000, "bvid": server.bvid, "view": 100,
            "like": 1, "online": 2,
        }

        def collect_once(_bvid):
            server._stop.set()
            return {"bvid": server.bvid}, sample

        server.collect_once = collect_once
        server._append_sample = lambda _sample: None
        server._poller_loop()
        self.assertEqual(server.state.samples, [sample])

    def test_event_session_gate_rejects_stale_and_fast_restart_events(self):
        page = MonitorPage.__new__(MonitorPage)
        page._active_session_id = "new"
        page.alert_session = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False, spike_enabled=False), "new")
        published = []
        page._publish_alert = published.append
        page._handle_monitor_event({
            "type": "sample_failure", "session_id": "old",
            "ts": 1, "consecutive_failures": 3,
        })
        self.assertEqual(published, [])
        page._active_session_id = None
        page._handle_monitor_event({
            "type": "sample_failure", "session_id": "old",
            "ts": 2, "consecutive_failures": 3,
        })
        self.assertEqual(published, [])
        page._active_session_id = "newer"
        page.alert_session = AlertSession(enabled_config(
            milestones=(), stagnation_enabled=False, spike_enabled=False), "newer")
        page._handle_monitor_event({
            "type": "sample_failure", "session_id": "newer",
            "ts": 3, "consecutive_failures": 3,
        })
        self.assertEqual([event.kind for event in published], ["disconnect"])

    def test_page_preset_contains_alert_configuration_without_starting(self):
        page = MonitorPage.__new__(MonitorPage)
        page.bvid_edit = _Text("BV1")
        page.interval_spin = _Spin(60)
        page.transport_combo = _Combo("auto")
        page.data_row = _Path("")
        page.alert_total_check = _Check(True)
        page.alert_windows_check = _Check(False)
        page.alert_sound_check = _Check(True)
        page.alert_milestone_check = _Check(True)
        page.alert_milestone_edit = _Text(" 500,100,500 ")
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
        params = page.collect_preset_params()
        self.assertEqual(params["alerts"]["milestones"], "100,500")
        self.assertFalse(params["alerts"]["windows_enabled"])


class PageSignalThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_signal_moves_alert_notification_to_gui_thread(self):
        expected_thread = threading.get_ident()

        class ThreadRecorder(RecordingNotificationAdapter):
            def __init__(self):
                super().__init__()
                self.on_gui_thread = []

            def notify(self, title, message):
                self.on_gui_thread.append(threading.get_ident() == expected_thread)
                return super().notify(title, message)

        adapter = ThreadRecorder()
        page = MonitorPage({
            "out_dir": "",
            "_monitor_notification_adapter_factory": lambda: adapter,
        })
        page.alert_total_check.setChecked(True)
        page.alert_windows_check.setChecked(True)
        page.alert_sound_check.setChecked(False)
        page.alert_milestone_edit.setText("100")
        page._active_session_id = "s"
        page.alert_session = AlertSession(
            page._alert_mapping_from_controls(), session_id="s")

        def emit_events():
            page.monitor_event.emit({
                "type": "sample_success", "session_id": "s", "ts": 0, "view": 50,
            })
            page.monitor_event.emit({
                "type": "sample_success", "session_id": "s", "ts": 60, "view": 100,
            })

        worker = threading.Thread(target=emit_events)
        worker.start()
        worker.join()
        for _ in range(10):
            self.app.processEvents()
        self.assertEqual(len(adapter.notifications), 1)
        self.assertEqual(adapter.on_gui_thread, [True])
        page.on_app_close()
        self.assertTrue(adapter.closed)


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

    def set_value(self, value):
        self.value = str(value)

    def setFocus(self):
        pass


class _Path:
    def __init__(self, value=""):
        self._value = str(value)

    def value(self):
        return self._value


class _Spin:
    def __init__(self, value=0):
        self.value_ = int(value)

    def value(self):
        return self.value_

    def setValue(self, value):
        self.value_ = int(value)

    def setEnabled(self, _value):
        pass


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


if __name__ == "__main__":
    unittest.main()
