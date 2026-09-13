# -*- coding: utf-8 -*-
"""监控页：参数表单 + 启动/停止；仪表盘在系统浏览器中打开。"""
import re
import webbrowser
from datetime import datetime
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox,
                               QDoubleSpinBox,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox,
                               QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
                               QWidget)

from app.task_page import PresetBar
from app.widgets import (LogPanel, PageHeader, PathRow, StatusPill, card, h2,
                         muted)
from core import config as app_config
from core.live import parse_room_input
from core.output import app_base_dir

from .alerts import AlertConfig, AlertEvent, AlertSession, milestones_text
from .notifications import (WEBHOOK_FORMATS, QtNotificationAdapter,
                            WebhookAdapter)
from .server import MonitorServer


class MonitorPage(QWidget):
    monitor_event = Signal(object)
    webhook_log = Signal(str)
    # 与 tools/__init__.py 的 ToolSpec.subtitle 成对（注册一致性由测试锁定），
    # 页头长文案是它的展开形式，两处措辞需同步更新。
    tool_subtitle = "视频/直播间实时数据仪表盘"

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.server = None
        self._session_generation = 0
        self._active_session_id = None
        self.alert_session = None
        self.notification_adapter = None
        self.webhook_adapter = None
        self._recent_alerts = []
        self.setObjectName("transparent")
        self.monitor_event.connect(self._handle_monitor_event)
        # Webhook 发送线程不能直接触碰 Qt 控件；日志经信号排队切回 GUI 线程。
        self.webhook_log.connect(self._log)

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
            "视频/直播间实时数据仪表盘：播放 / 点赞 / 投币 / 收藏 / 分享 / 各分P正在看；"
            "直播间模式为人气与开播状态，历史趋势跨启动续接。",
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
        # 「监控对象」选择：视频作品（默认，行为与历史版本一致）/ 直播间
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("视频作品", "video")
        self.mode_combo.addItem("直播间", "live")
        form.addRow("监控对象", self.mode_combo)
        self.bvid_label = QLabel("视频 BV 号")
        self.bvid_edit = QLineEdit()
        self.bvid_edit.setPlaceholderText("示例：BV1xxxxxxxxx")
        form.addRow(self.bvid_label, self.bvid_edit)
        # 先接线后不会有初始化信号：addItem 阶段连接尚未建立
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setValue(60)
        self.interval_spin.setSuffix(" 秒")
        form.addRow("采集间隔", self.interval_spin)
        # 网络通道以设置页 configure 的统一会话为准（监控已并入 session 单例），
        # 页面不再提供「传输通道」下拉。
        default_data = str(Path(self.cfg.get("out_dir") or app_base_dir()) / "监控数据")
        self.data_row = PathRow(None, default_data)
        form.addRow("数据目录", self.data_row)
        play.addLayout(form)
        root.addWidget(params)

        alert_card, alert_layout = card(margin=12, variant="accent")
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

        # Webhook 渠道独占一行（复选框 + 格式 + URL + 测试按钮），避免给告警卡
        # 再增一行高度——960×640 下卡片高度本就贴近视口上限。
        webhook_row = QHBoxLayout()
        webhook_row.setSpacing(8)
        self.alert_webhook_check = QCheckBox("Webhook 推送")
        self.alert_webhook_check.setChecked(False)
        self.webhook_format_combo = QComboBox()
        for val, label in (("json", "通用 JSON"), ("serverchan", "Server酱")):
            self.webhook_format_combo.addItem(label, val)
        # 先设初值再连信号：初始化阶段不得触发保存（此时 URL 控件还不存在）。
        self.webhook_format_combo.setCurrentIndex(max(
            0, self.webhook_format_combo.findData(
                str(self.cfg.get("webhook_format") or "json"))))
        self.webhook_format_combo.currentIndexChanged.connect(
            self._on_webhook_format_changed)
        self.webhook_url_edit = QLineEdit()
        self.webhook_url_edit.setPlaceholderText(self._webhook_placeholder())
        self.webhook_url_edit.setText(str(self.cfg.get("webhook_url") or ""))
        self.webhook_url_edit.editingFinished.connect(self._save_webhook_url)
        self.btn_test_webhook = QPushButton("发送测试")
        self.btn_test_webhook.setObjectName("flat")
        self.btn_test_webhook.clicked.connect(self.on_test_webhook)
        webhook_row.addWidget(self.alert_webhook_check)
        webhook_row.addWidget(self.webhook_format_combo)
        webhook_row.addWidget(self.webhook_url_edit, 1)
        webhook_row.addWidget(self.btn_test_webhook)
        alert_layout.addLayout(webhook_row)
        alert_layout.addWidget(muted(
            "通用 JSON：POST {title, text, event_type}；Server酱：POST 表单 "
            "title/desp 到 sctapi.ftqq.com，HTTP 2xx 且 code==0 算成功。"
            "每小时最多推送 10 条，SendKey 与 URL 只保存在本机、"
            "不进入任务历史与预设。"))

        rule_grid = QGridLayout()
        rule_grid.setHorizontalSpacing(6)
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

        # 仅直播间模式可见并生效（视频模式的采样里没有开播状态字段）
        self.alert_live_flip_check = QCheckBox("开播/下播提醒")
        self.alert_live_flip_check.setToolTip(
            "开播↔下播↔轮播状态翻转时提醒一次；仅直播间模式可见并生效。")
        self.alert_live_flip_check.setVisible(False)
        rule_grid.addWidget(self.alert_live_flip_check, 5, 0)
        # 960×640 裁切根因（实测，MainWindow 内视口仅 734px）：QSpinBox 的
        # sizeHint 按取值上限位数预留宽度（增长/绝对增长上限 2^31-1 共 10 位），
        # 叠加「分钟冷却」等后缀，把 rule_grid 最小宽度推到 907px → 右缘裁切。
        # 修复（字宽按 150% DPI 环境实测：汉字≈20px、chrome=padding20+border2）：
        # spin 去掉上下微调按钮（NoButtons，键盘/滚轮/方向键调整不受影响）——
        # 带按钮时按钮占 32px，「10 分钟冷却」(110)、「10000 播放」(106) 等
        # 常用值加 chrome 后两个 spin 列需 324px，960 视口网格可用 658px 放不下；
        # 去按钮后按「最长常用值 + 后缀完整显示」设显式下限即可全部完整
        # （列合计 631px ≤ 658px），极端上限值（2147483647、10080 分钟冷却）
        # 的后缀尾部仍会截断，属可接受取舍。1440×900 不劣化。
        # 另配合：网格列距 10→6、告警卡边距 14→12。
        for spin, min_w in (
                (self.alert_stagnation_window_spin, 130),
                (self.alert_stagnation_growth_spin, 130),
                (self.alert_spike_window_spin, 130),
                (self.alert_spike_absolute_spin, 130),
                (self.alert_spike_relative_spin, 125),
                (self.alert_spike_cooldown_spin, 134),
                (self.alert_disconnect_failures_spin, 90)):
            spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
            spin.setMinimumWidth(min_w)
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
            self.alert_webhook_check,
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
        # 960 视口（734px）下运行态 pill 的完整 URL 文本（最小宽 340px）会把
        # action bar 最小宽度顶到 734px，成为告警卡右缘裁切的第二个驱动源：
        # 给 pill 设 200px 显式下限压住该行（小窗时 URL 截断显示、完整地址挂
        # tooltip；1440×900 下 pill 仍按 sizeHint 完整展开，不劣化）。
        self.status_label.setMinimumWidth(200)
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

    def _new_webhook_adapter(self):
        factory = self.cfg.get("_monitor_webhook_adapter_factory")
        if callable(factory):
            return factory()
        return WebhookAdapter(
            url_getter=self.webhook_url_edit.text,
            format_getter=self._webhook_format_value,
            log=self.webhook_log.emit,
        )

    def _ensure_webhook_adapter(self):
        """懒创建并跨会话保留：频控预算不随监控重启而清零。"""
        if self.webhook_adapter is None:
            try:
                self.webhook_adapter = self._new_webhook_adapter()
            except Exception as exc:  # noqa: BLE001 - 通道失败不得影响监控
                self._log(f"Webhook 适配器创建失败（{type(exc).__name__}）。")
                self.webhook_adapter = None
        return self.webhook_adapter

    def _deliver_webhook(self, event_type, title, message, enabled):
        """Webhook 唯一入口：未启用时零动作（不创建适配器、不创建线程）。"""
        if not enabled:
            return False
        adapter = self._ensure_webhook_adapter()
        if adapter is None:
            return False
        return bool(adapter.send(event_type, title, message))

    def _save_webhook_url(self):
        """URL 只落 config.json（与 proxy_spec 同级明文），不进历史/预设。

        用最小字段保存而非整份 self.cfg：设置页持有的是 cfg 的副本，
        整份回写会用副本里的旧值覆盖其他渠道刚保存的字段。
        """
        url = self.webhook_url_edit.text().strip()
        if self.cfg.get("webhook_url") == url:
            return
        self.cfg["webhook_url"] = url
        app_config.save({"webhook_url": url})

    def _webhook_format_value(self) -> str:
        """下拉当前格式；未知值回退通用 JSON（config 层只做键白名单）。"""
        data = self.webhook_format_combo.currentData()
        return data if data in WEBHOOK_FORMATS else "json"

    def _webhook_placeholder(self) -> str:
        if self._webhook_format_value() == "serverchan":
            return "填 SendKey 或 .send 完整链接（仅保存在本机配置文件）"
        return "https://…（接收端 URL，仅保存在本机配置文件）"

    def _on_webhook_format_changed(self):
        self.webhook_url_edit.setPlaceholderText(self._webhook_placeholder())
        self._save_webhook_format()

    def _save_webhook_format(self):
        """格式与 webhook_url 同模式：最小字段、只落 config.json。"""
        value = self._webhook_format_value()
        if self.cfg.get("webhook_format") == value:
            return
        self.cfg["webhook_format"] = value
        app_config.save({"webhook_format": value})

    def _is_live_mode(self):
        """当前监控对象是否直播间；__new__ 构造的假控件页面视为视频模式。"""
        combo = getattr(self, "mode_combo", None)
        return combo is not None and combo.currentData() == "live"

    def _on_mode_changed(self):
        """监控对象切换：只联动文案与可见性，绝不清空用户已填数字。"""
        live = self._is_live_mode()
        self.bvid_label.setText("直播间号/链接" if live else "视频 BV 号")
        self.bvid_edit.setPlaceholderText(
            "示例：直播间号或 live.bilibili.com/6" if live
            else "示例：BV1xxxxxxxxx")
        self.alert_milestone_check.setText("人气里程碑" if live else "播放量里程碑")
        unit = " 人气" if live else " 播放"
        self.alert_stagnation_growth_spin.setSuffix(unit)
        self.alert_spike_absolute_spin.setSuffix(unit)
        self.alert_live_flip_check.setVisible(live)

    def _live_flip_checked(self):
        """开播/下播提醒仅直播间模式生效；假控件页面（无该复选框）为 False。"""
        check = getattr(self, "alert_live_flip_check", None)
        return bool(check is not None and check.isChecked()
                    and self._is_live_mode())

    def _alert_mapping_from_controls(self):
        return AlertConfig.from_mapping({
            "enabled": self.alert_total_check.isChecked(),
            "windows_enabled": self.alert_windows_check.isChecked(),
            "sound_enabled": self.alert_sound_check.isChecked(),
            "webhook_enabled": self.alert_webhook_check.isChecked(),
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
            "live_flip_enabled": self._live_flip_checked(),
        })

    def _apply_alert_mapping(self, raw):
        config = AlertConfig.from_mapping(raw)
        self.alert_total_check.setChecked(config.enabled)
        self.alert_windows_check.setChecked(config.windows_enabled)
        self.alert_sound_check.setChecked(config.sound_enabled)
        self.alert_webhook_check.setChecked(config.webhook_enabled)
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
        flip_check = getattr(self, "alert_live_flip_check", None)
        if flip_check is not None:
            flip_check.setChecked(config.live_flip_enabled)
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
        webhook_on = self.alert_webhook_check.isChecked()
        self.webhook_format_combo.setEnabled(webhook_on)
        self.webhook_url_edit.setEnabled(webhook_on)
        self.btn_test_webhook.setEnabled(webhook_on)

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
        self._deliver_webhook(
            alert.kind, alert.title, alert.message, config.webhook_enabled)

    def on_test_alert(self):
        windows_enabled = self.alert_windows_check.isChecked()
        sound_enabled = self.alert_sound_check.isChecked()
        webhook_enabled = self.alert_webhook_check.isChecked()
        if not windows_enabled and not sound_enabled and not webhook_enabled:
            message = "测试提醒未执行：请至少勾选一种提醒渠道。"
            self._log(message)
            QMessageBox.information(self, "测试提醒", message)
            return
        results = self._deliver_channels(
            "监控提醒测试", "这是一次测试提醒，不会启动或改变监控。",
            windows_enabled, sound_enabled,
        )
        self._log(self._delivery_result_message("测试提醒", results))
        if webhook_enabled:
            self._deliver_webhook(
                "test", "监控提醒测试",
                "这是一次测试推送，不会启动或改变监控。", True)

    def on_test_webhook(self):
        if not self.alert_webhook_check.isChecked():
            self._log("Webhook 测试未发送：请先勾选「Webhook 推送」。")
            return
        self._save_webhook_url()
        if self._deliver_webhook(
                "test", "监控提醒测试",
                "这是一次测试推送，不会启动或改变监控。", True):
            self._log("Webhook 测试推送已提交，发送结果见后续日志。")

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
            # 字段访问层参数化：video 会话读 view，live 会话读 online，
            # 并按 status_field 追加开播/下播翻转判断（video 会话为 None）。
            session = self.alert_session
            alerts = session.process_sample(
                event.get("ts"), event.get(session.value_field))
            if session.status_field:
                alerts = alerts + session.process_status_flip(
                    event.get("ts"), event.get(session.status_field))
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
            "data_dir": self.data_row.value(),
            "mode": "live" if self._is_live_mode() else "video",
        }
        if hasattr(self, "alert_total_check"):
            params["alerts"] = self._alert_mapping_from_controls().to_mapping()
        return params

    def apply_preset_params(self, params):
        # 旧预设/历史无 mode 键 → 默认视频模式；非法值同样回落 video。
        mode = str(params.get("mode") or "video")
        if mode not in ("video", "live"):
            mode = "video"
        mode_combo = getattr(self, "mode_combo", None)
        if mode_combo is not None:
            mode_combo.setCurrentIndex(1 if mode == "live" else 0)
        self.bvid_edit.setText(str(params.get("bvid", "")))
        try:
            self.interval_spin.setValue(int(params.get("interval", 60)))
        except (TypeError, ValueError):
            self.interval_spin.setValue(60)
        # 旧预设/历史可能携带已失效的 transport 键：不读取即容忍忽略。
        self.data_row.set_value(params.get("data_dir", ""))
        if hasattr(self, "alert_total_check"):
            self._apply_alert_mapping(params.get("alerts") or {})
        self.bvid_edit.setFocus()

    def _log(self, msg):
        self.log_panel.append(str(msg))

    def on_start(self):
        if self.server is not None and self.server.running:
            return
        if self._is_live_mode():
            # live 输入只认用户键入内容（不回落 placeholder，避免示例
            # 链接 live.bilibili.com/6 被当成真实目标）。
            try:
                room_id = parse_room_input(self.bvid_edit.text().strip())
            except ValueError as exc:
                self.status_label.set_state("error", f"✕ {exc}")
                return
            target_kwargs = {"mode": "live", "room_id": room_id}
        else:
            raw = self.bvid_edit.text().strip() or self.bvid_edit.placeholderText()
            m = re.search(r"BV[0-9A-Za-z]{10}", raw)  # BV 区分大小写，保留原样
            if not m:
                self.status_label.set_state("error", "✕ BV 号格式有误")
                return
            target_kwargs = {"bvid": m.group(0)}
        self._session_generation += 1
        session_id = f"monitor-{self._session_generation}"
        self._active_session_id = session_id
        live = target_kwargs.get("mode") == "live"
        self.alert_session = AlertSession(
            self._alert_mapping_from_controls(), session_id=session_id,
            value_field="online" if live else "view",
            status_field="live_status" if live else None,
            value_label="人气" if live else "播放量")
        self._clear_recent_alerts()
        self.server = MonitorServer(
            interval=self.interval_spin.value(),
            data_dir=self.data_row.value(),
            log=self._log,
            event_callback=self._on_server_event,
            session_id=session_id,
            **target_kwargs)
        url = self.server.start()
        self.status_label.set_state("running", f"● 运行中 · {url}")
        # 小窗下 pill 文本可能被截断，完整仪表盘地址挂在 tooltip。
        self.status_label.setToolTip(url)
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
        # webhook_adapter 不随会话关闭：它没有需要释放的 Qt/网络资源，
        # 保留实例可让每小时 10 条的频控预算不被重启监控绕过。
        self.status_label.set_state("idle", "○ 未启动")
        self.status_label.setToolTip("")
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
