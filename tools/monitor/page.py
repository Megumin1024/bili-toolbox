# -*- coding: utf-8 -*-
"""监控页：参数表单 + 启动/停止；仪表盘在系统浏览器中打开。"""
import re
import webbrowser
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSpinBox, QVBoxLayout, QWidget)

from app.widgets import (LogPanel, PageHeader, PathRow, StatusPill, card, h2,
                         muted)
from core.config import COOKIE_FILE
from core.output import app_base_dir

from .server import MonitorServer


class MonitorPage(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.server = None
        self.setObjectName("transparent")

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
        self.server = MonitorServer(
            bvid=m.group(0),
            interval=self.interval_spin.value(),
            transport=self.transport_combo.currentData(),
            proxy_spec=self.cfg.get("proxy_spec") or None,
            data_dir=self.data_row.value(),
            cookie_path=COOKIE_FILE,
            log=self._log)
        url = self.server.start()
        self.status_label.set_state("running", f"● 运行中 · {url}")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        webbrowser.open(url)

    def on_stop(self):
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.status_label.set_state("idle", "○ 未启动")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_open_browser(self):
        if self.server is not None and self.server.running:
            webbrowser.open(self.server.url)

    def on_app_close(self):
        self.on_stop()
