# -*- coding: utf-8 -*-
"""监控页：参数表单 + 启动/停止；仪表盘在系统浏览器中打开。"""
import re
import webbrowser
from datetime import datetime
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox,
                               QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
                               QWidget)

from app.task_page import PresetBar
from app.widgets import (LogPanel, PageHeader, PathRow, StatusPill, card, h2,
                         muted)
from core.config import COOKIE_FILE
from core.output import app_base_dir

from .alerts import AlertConfig, AlertEvent, AlertSession, milestones_text
from .notifications import QtNotificationAdapter
from .server import MonitorServer


class MonitorPage(QWidget):
    monitor_event = Signal(object)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.server = None
        self._session_generation = 0
        self._active_session_id = None
        self.alert_session = None
        self.notification_adapter = None
        self._recent_alerts = []
        self.setObjectName("transparent")
        self.monitor_event.connect(self._handle_monitor_event)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("pageScroll")

        content = QWidget()
        content.setObjectName("transparent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(14)

        root.addWidget(PageHeader(
            "实时监控",
            "视频实时数据仪表盘：播放 / 点赞 / 投币 / 收藏 / 分享 / 各分P正在看，历史趋势跨启动续接。",
            "实时监控",
        ))

        params, play = card(variant="accent")
        play.addWidget(h2("监控参数"))
        self.preset_bar = PresetBar(
            "monitor", "实时监控", self.collect_preset_params,
            self.apply_preset_params,
        )
        play.addWidget(self.preset_bar)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)
        self.bvid_edit = QLineEdit()
        self.bvid_edit.setPlaceholderText("示例：BV1xxxxxxxxx")
        form.addRow("视频 BV 号", self.bvid_edit)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setValue(60)
        self.interval_spin.setSuffix(" 秒")
        form.addRow("采集间隔", self.interval_spin)
        self.transport_combo = QComboBox()
        for val, label in (("auto", "自动切换（推荐）"),
                           ("h2-ja3", "TLS 指纹通道"),
                           ("urllib", "标准通道回退")):
            self.transport_combo.addItem(label, val)
        form.addRow("传输通道", self.transport_combo)
        default_data = str(Path(self.cfg.get("out_dir") or app_base_dir()) / "监控数据")
        self.data_row = PathRow(None, default_data)
        form.addRow("数据目录", self.data_row)
        play.addLayout(form)
        root.addWidget(params)

        alert_card, alert_layout = card(variant="accent")
        self.alert_card = alert_card
        alert_head = QHBoxLayout()
        alert_head.addWidget(h2("监控提醒"))
        alert_head.addStretch(1)
        alert_chip = QLabel("仅当前会话")
        alert_chip.setObjectName("moduleChip")
        alert_head.addWidget(alert_chip)
        alert_layout.addLayout(alert_head)

        channel_row = QHBoxLayout()
        self.alert_total_check = QCheckBox("启用监控提醒")
        self.alert_total_check.setChecked(False)
        self.alert_windows_check = QCheckBox("Windows 通知")
        self.alert_windows_check.setChecked(True)
        self.alert_sound_check = QCheckBox("声音提醒")
        self.alert_sound_check.setChecked(False)
        channel_row.addWidget(self.alert_total_check)
        channel_row.addWidget(self.alert_windows_check)
        channel_row.addWidget(self.alert_sound_check)
        channel_row.addStretch(1)
        alert_layout.addLayout(channel_row)

        rule_grid = QGridLayout()
        rule_grid.setHorizontalSpacing(10)
        rule_grid.setVerticalSpacing(8)
        rule_grid.setColumnStretch(1, 1)

        self.alert_milestone_check = QCheckBox("播放量里程碑")
        self.alert_milestone_check.setChecked(True)
        self.alert_milestone_edit = QLineEdit(milestones_text(AlertConfig().milestones))
        self.alert_milestone_edit.setPlaceholderText("例如：10000,50000,100000")
        rule_grid.addWidget(self.alert_milestone_check, 0, 0)
        rule_grid.addWidget(self.alert_milestone_edit, 0, 1, 1, 3)

        self.alert_stagnation_check = QCheckBox("增长停滞")
        self.alert_stagnation_check.setChecked(True)
        self.alert_stagnation_window_spin = QSpinBox()
        self.alert_stagnation_window_spin.setRange(1, 10080)
        self.alert_stagnation_window_spin.setValue(30)
        self.alert_stagnation_window_spin.setSuffix(" 分钟")
        self.alert_stagnation_growth_spin = QSpinBox()
        self.alert_stagnation_growth_spin.setRange(0, 2_147_483_647)
        self.alert_stagnation_growth_spin.setValue(0)
        self.alert_stagnation_growth_spin.setSuffix(" 播放")
        rule_grid.addWidget(self.alert_stagnation_check, 1, 0)
        rule_grid.addWidget(QLabel("观察窗口"), 1, 1)
        rule_grid.addWidget(self.alert_stagnation_window_spin, 1, 2)
        rule_grid.addWidget(QLabel("最大增长"), 1, 3)
        rule_grid.addWidget(self.alert_stagnation_growth_spin, 1, 4)

        self.alert_spike_check = QCheckBox("异常突增")
        self.alert_spike_check.setChecked(True)
        self.alert_spike_window_spin = QSpinBox()
        self.alert_spike_window_spin.setRange(1, 10080)
        self.alert_spike_window_spin.setValue(5)
        self.alert_spike_window_spin.setSuffix(" 分钟")
        self.alert_spike_absolute_spin = QSpinBox()
        self.alert_spike_absolute_spin.setRange(1, 2_147_483_647)
        self.alert_spike_absolute_spin.setValue(10000)
        self.alert_spike_absolute_spin.setSuffix(" 播放")
        self.alert_spike_relative_spin = QDoubleSpinBox()
        self.alert_spike_relative_spin.setRange(0.0, 10000.0)
        self.alert_spike_relative_spin.setDecimals(1)
        self.alert_spike_relative_spin.setSingleStep(1.0)
        self.alert_spike_relative_spin.setValue(20.0)
        self.alert_spike_relative_spin.setSuffix(" %")
        rule_grid.addWidget(self.alert_spike_check, 2, 0)
        rule_grid.addWidget(QLabel("观察窗口"), 2, 1)
        rule_grid.addWidget(self.alert_spike_window_spin, 2, 2)
        rule_grid.addWidget(QLabel("绝对增长"), 2, 3)
        rule_grid.addWidget(self.alert_spike_absolute_spin, 2, 4)
        rule_grid.addWidget(QLabel("相对增长"), 3, 3)
        rule_grid.addWidget(self.alert_spike_relative_spin, 3, 4)
        self.alert_spike_cooldown_spin = QSpinBox()
        self.alert_spike_cooldown_spin.setRange(0, 10080)
        self.alert_spike_cooldown_spin.setValue(10)
        self.alert_spike_cooldown_spin.setSuffix(" 分钟冷却")
        rule_grid.addWidget(QLabel("同类提醒"), 3, 1)
        rule_grid.addWidget(self.alert_spike_cooldown_spin, 3, 2)

        self.alert_disconnect_check = QCheckBox("监控断线")
        self.alert_disconnect_check.setChecked(True)
        self.alert_disconnect_failures_spin = QSpinBox()
        self.alert_disconnect_failures_spin.setRange(1, 100)
        self.alert_disconnect_failures_spin.setValue(3)
        self.alert_disconnect_failures_spin.setSuffix(" 次")
        rule_grid.addWidget(self.alert_disconnect_check, 4, 0)
        rule_grid.addWidget(QLabel("连续失败次数"), 4, 1)
        rule_grid.addWidget(self.alert_disconnect_failures_spin, 4, 2)
        alert_layout.addLayout(rule_grid)

        alert_action_row = QHBoxLayout()
        alert_action_row.addWidget(muted(
            "首条新样本只建立本次会话基线；历史 JSONL 不参与提醒计算。"))
        alert_action_row.addStretch(1)
        self.btn_test_alert = QPushButton("测试提醒")
        self.btn_test_alert.setObjectName("flat")
        self.btn_test_alert.clicked.connect(self.on_test_alert)
        alert_action_row.addWidget(self.btn_test_alert)
        alert_layout.addLayout(alert_action_row)

        recent_head = QHBoxLayout()
        recent_head.addWidget(QLabel("当前会话最近提醒（最多 100 条）"))
        recent_head.addStretch(1)
        alert_layout.addLayout(recent_head)
        self.alert_empty_label = muted("本次会话尚无提醒。")
        alert_layout.addWidget(self.alert_empty_label)
        self.alert_list = QListWidget()
        self.alert_list.setWordWrap(True)
        self.alert_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.alert_list.setMinimumHeight(78)
        self.alert_list.setMaximumHeight(156)
        self.alert_list.hide()
        alert_layout.addWidget(self.alert_list)
        for checkbox in (
            self.alert_milestone_check, self.alert_stagnation_check,
            self.alert_spike_check, self.alert_disconnect_check,
        ):
            checkbox.toggled.connect(self._refresh_alert_rule_states)
        self._refresh_alert_rule_states()
        root.addWidget(alert_card)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        btn_row = QHBoxLayout(action_bar)
        btn_row.setContentsMargins(14, 12, 14, 12)
        btn_row.setSpacing(10)
        self.btn_start = QPushButton("启动监控")
        self.btn_start.setObjectName("primary")
        self.btn_start.setIcon(qta.icon("fa5s.play"))
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setIcon(qta.icon("fa5s.stop"))
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_browser = QPushButton("在浏览器打开")
        self.btn_browser.setObjectName("flat")
        self.btn_browser.clicked.connect(self.on_open_browser)
        self.status_label = StatusPill("○ 未启动", "idle")
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_browser)
        btn_row.addStretch(1)
        btn_row.addWidget(self.status_label)
        root.addWidget(action_bar)

        dashboard_card, dashboard_layout = card(variant="accent")
        dashboard_head = QHBoxLayout()
        dashboard_head.addWidget(h2("浏览器仪表盘"))
        dashboard_head.addStretch(1)
        dashboard_chip = QLabel("本地仪表盘")
        dashboard_chip.setObjectName("moduleChip")
        dashboard_head.addWidget(dashboard_chip)
        dashboard_layout.addLayout(dashboard_head)
        self.dash_hint = muted(
            "启动监控后将自动在系统浏览器中打开仪表盘；也可随时点击「在浏览器打开」再次打开。")
        dashboard_layout.addWidget(self.dash_hint)
        root.addWidget(dashboard_card)

        console_header = QHBoxLayout()
        console_header.setContentsMargins(2, 2, 2, 0)
        console_header.addWidget(h2("监控通讯"))
        console_header.addStretch(1)
        console_chip = QLabel("监控日志")
        console_chip.setObjectName("moduleChip")
        console_header.addWidget(console_chip)
        root.addLayout(console_header)
        self.log_panel = LogPanel()
        self.log_panel.setMinimumHeight(165)
        root.addWidget(self.log_panel)
        root.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ---------- 控制 ----------

    def _new_notification_adapter(self):
        adapter_factory = self.cfg.get("_monitor_notification_adapter_factory")
        if callable(adapter_factory):
            return adapter_factory()
        adapter = self.cfg.get("_monitor_notification_adapter")
        if adapter is not None:
            return adapter
        return QtNotificationAdapter(log=self._log, parent=self)

    def _ensure_notification_adapter(self):
        if self.notification_adapter is None:
            try:
                self.notification_adapter = self._new_notification_adapter()
            except Exception as exc:  # noqa: BLE001 - 通道失败不得影响监控
                self._log(f"提醒适配器创建失败（{type(exc).__name__}）。")
                self.notification_adapter = None
        return self.notification_adapter

    def _alert_mapping_from_controls(self):
        return AlertConfig.from_mapping({
            "enabled": self.alert_total_check.isChecked(),
            "windows_enabled": self.alert_windows_check.isChecked(),
            "sound_enabled": self.alert_sound_check.isChecked(),
            "milestone_enabled": self.alert_milestone_check.isChecked(),
            "milestones": self.alert_milestone_edit.text(),
            "stagnation_enabled": self.alert_stagnation_check.isChecked(),
            "stagnation_window_min": self.alert_stagnation_window_spin.value(),
            "stagnation_max_growth": self.alert_stagnation_growth_spin.value(),
            "spike_enabled": self.alert_spike_check.isChecked(),
            "spike_window_min": self.alert_spike_window_spin.value(),
            "spike_min_absolute": self.alert_spike_absolute_spin.value(),
            "spike_min_relative_percent": self.alert_spike_relative_spin.value(),
            "spike_cooldown_min": self.alert_spike_cooldown_spin.value(),
            "disconnect_enabled": self.alert_disconnect_check.isChecked(),
            "disconnect_failures": self.alert_disconnect_failures_spin.value(),
        })

    def _apply_alert_mapping(self, raw):
        config = AlertConfig.from_mapping(raw)
        self.alert_total_check.setChecked(config.enabled)
        self.alert_windows_check.setChecked(config.windows_enabled)
        self.alert_sound_check.setChecked(config.sound_enabled)
        self.alert_milestone_check.setChecked(config.milestone_enabled)
        self.alert_milestone_edit.setText(milestones_text(config.milestones))
        self.alert_stagnation_check.setChecked(config.stagnation_enabled)
        self.alert_stagnation_window_spin.setValue(config.stagnation_window_min)
        self.alert_stagnation_growth_spin.setValue(config.stagnation_max_growth)
        self.alert_spike_check.setChecked(config.spike_enabled)
        self.alert_spike_window_spin.setValue(config.spike_window_min)
        self.alert_spike_absolute_spin.setValue(config.spike_min_absolute)
        self.alert_spike_relative_spin.setValue(config.spike_min_relative_percent)
        self.alert_spike_cooldown_spin.setValue(config.spike_cooldown_min)
        self.alert_disconnect_check.setChecked(config.disconnect_enabled)
        self.alert_disconnect_failures_spin.setValue(config.disconnect_failures)
        self._refresh_alert_rule_states()

    def _refresh_alert_rule_states(self):
        self.alert_milestone_edit.setEnabled(self.alert_milestone_check.isChecked())
        for widget in (
            self.alert_stagnation_window_spin, self.alert_stagnation_growth_spin,
        ):
            widget.setEnabled(self.alert_stagnation_check.isChecked())
        for widget in (
            self.alert_spike_window_spin, self.alert_spike_absolute_spin,
            self.alert_spike_relative_spin, self.alert_spike_cooldown_spin,
        ):
            widget.setEnabled(self.alert_spike_check.isChecked())
        self.alert_disconnect_failures_spin.setEnabled(
            self.alert_disconnect_check.isChecked())

    def _clear_recent_alerts(self):
        self._recent_alerts.clear()
        self.alert_list.clear()
        self.alert_list.hide()
        self.alert_empty_label.show()

    def _record_alert(self, alert: AlertEvent):
        self._recent_alerts.insert(0, alert)
        del self._recent_alerts[100:]
        try:
            timestamp = datetime.fromtimestamp(float(alert.ts)).astimezone().strftime("%H:%M:%S")
        except (OSError, OverflowError, ValueError):
            timestamp = "未知时间"
        item = QListWidgetItem(f"{timestamp} · {alert.title}：{alert.message}")
        item.setToolTip(item.text())
        self.alert_list.insertItem(0, item)
        while self.alert_list.count() > 100:
            self.alert_list.takeItem(self.alert_list.count() - 1)
        self.alert_empty_label.hide()
        self.alert_list.show()

    def _deliver_channels(self, title, message, windows_enabled, sound_enabled):
        results = {"windows": None, "sound": None}
        if not windows_enabled and not sound_enabled:
            return results
        adapter = self._ensure_notification_adapter()
        if adapter is None:
            if windows_enabled:
                results["windows"] = False
            if sound_enabled:
                results["sound"] = False
            return results
        if windows_enabled:
            try:
                results["windows"] = bool(adapter.notify(title, message))
            except Exception as exc:  # noqa: BLE001 - 声音通道仍需继续
                results["windows"] = False
                suffix = "，声音通道继续" if sound_enabled else ""
                self._log(f"Windows通知发送失败（{type(exc).__name__}）{suffix}。")
        if sound_enabled:
            try:
                results["sound"] = bool(adapter.play_sound())
            except Exception as exc:  # noqa: BLE001 - 采集/页面不得被声音拖垮
                results["sound"] = False
                self._log(f"声音提醒失败（{type(exc).__name__}）。")
        return results

    def _delivery_result_message(self, prefix, results):
        labels = {"windows": "Windows 通知", "sound": "声音提醒"}
        selected = [key for key, result in results.items() if result is not None]
        succeeded = [key for key in selected if results[key]]
        failed = [key for key in selected if not results[key]]
        if not selected:
            return f"{prefix}未执行：未勾选任何提醒通道。"
        if not succeeded:
            return f"{prefix}发送失败/系统不可用：" + "、".join(labels[key] for key in failed) + "。"
        if not failed:
            return f"{prefix}发送成功：" + "、".join(labels[key] for key in succeeded) + "。"
        return (
            f"{prefix}部分成功：成功 {','.join(labels[key] for key in succeeded)}；"
            f"失败 {','.join(labels[key] for key in failed)}。"
        )

    def _publish_alert(self, alert: AlertEvent):
        config = self.alert_session.config if self.alert_session else AlertConfig()
        self._record_alert(alert)
        results = self._deliver_channels(
            alert.title, alert.message,
            config.windows_enabled, config.sound_enabled,
        )
        if any(result is not None for result in results.values()):
            self._log(self._delivery_result_message("提醒", results))

    def on_test_alert(self):
        windows_enabled = self.alert_windows_check.isChecked()
        sound_enabled = self.alert_sound_check.isChecked()
        if not windows_enabled and not sound_enabled:
            message = "测试提醒未执行：请至少勾选 Windows 通知或声音提醒。"
            self._log(message)
            QMessageBox.information(self, "测试提醒", message)
            return
        results = self._deliver_channels(
            "监控提醒测试", "这是一次测试提醒，不会启动或改变监控。",
            windows_enabled, sound_enabled,
        )
        self._log(self._delivery_result_message("测试提醒", results))

    def _on_server_event(self, event):
        """采集线程入口：只发 Qt Signal，不触碰控件或通知对象。"""
        self.monitor_event.emit(dict(event))

    def _handle_monitor_event(self, event):
        """GUI 线程槽：先校验 session_id，再运行纯 Python 规则。"""
        if not isinstance(event, dict):
            return
        if (self._active_session_id is None
                or event.get("session_id") != self._active_session_id
                or self.alert_session is None):
            return
        event_type = event.get("type")
        if event_type == "sample_success":
            alerts = self.alert_session.process_sample(event.get("ts"), event.get("view"))
        elif event_type == "sample_failure":
            alerts = self.alert_session.process_failure(
                event.get("ts"), event.get("consecutive_failures"))
        else:
            return
        for alert in alerts:
            self._publish_alert(alert)

    def collect_preset_params(self):
        params = {
            "bvid": self.bvid_edit.text().strip(),
            "interval": self.interval_spin.value(),
            "transport": self.transport_combo.currentData(),
            "data_dir": self.data_row.value(),
        }
        if hasattr(self, "alert_total_check"):
            params["alerts"] = self._alert_mapping_from_controls().to_mapping()
        return params

    def apply_preset_params(self, params):
        self.bvid_edit.setText(str(params.get("bvid", "")))
        try:
            self.interval_spin.setValue(int(params.get("interval", 60)))
        except (TypeError, ValueError):
            self.interval_spin.setValue(60)
        transport = params.get("transport", "auto")
        index = self.transport_combo.findData(transport)
        self.transport_combo.setCurrentIndex(max(0, index))
        self.data_row.set_value(params.get("data_dir", ""))
        if hasattr(self, "alert_total_check"):
            self._apply_alert_mapping(params.get("alerts") or {})
        self.bvid_edit.setFocus()

    def _log(self, msg):
        self.log_panel.append(str(msg))

    def on_start(self):
        if self.server is not None and self.server.running:
            return
        raw = self.bvid_edit.text().strip() or self.bvid_edit.placeholderText()
        m = re.search(r"BV[0-9A-Za-z]{10}", raw)  # BV 区分大小写，保留原样
        if not m:
            self.status_label.set_state("error", "✕ BV 号格式有误")
            return
        self._session_generation += 1
        session_id = f"monitor-{self._session_generation}"
        self._active_session_id = session_id
        self.alert_session = AlertSession(
            self._alert_mapping_from_controls(), session_id=session_id)
        self._clear_recent_alerts()
        self.server = MonitorServer(
            bvid=m.group(0),
            interval=self.interval_spin.value(),
            transport=self.transport_combo.currentData(),
            proxy_spec=self.cfg.get("proxy_spec") or None,
            data_dir=self.data_row.value(),
            cookie_path=COOKIE_FILE,
            log=self._log,
            event_callback=self._on_server_event,
            session_id=session_id)
        url = self.server.start()
        self.status_label.set_state("running", f"● 运行中 · {url}")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        webbrowser.open(url)

    def on_stop(self):
        # 先撤销 active id，再停止线程；这样 stop 与下一次 start 之间迟到的
        # 事件也无法进入提醒引擎。
        self._active_session_id = None
        if self.alert_session is not None:
            self.alert_session.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.alert_session = None
        self._close_notification_adapter()
        self.status_label.set_state("idle", "○ 未启动")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _close_notification_adapter(self):
        adapter = self.notification_adapter
        self.notification_adapter = None
        if adapter is None:
            return
        try:
            adapter.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不得阻止页面退出
            self._log(f"提醒通道关闭失败（{type(exc).__name__}）。")

    def on_open_browser(self):
        if self.server is not None and self.server.running:
            webbrowser.open(self.server.url)

    def on_app_close(self):
        self.on_stop()
