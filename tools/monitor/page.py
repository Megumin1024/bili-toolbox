# -*- coding: utf-8 -*-
"""监控页：参数表单 + 启动/停止；仪表盘在系统浏览器中打开。"""
import re
import webbrowser
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from app.widgets import LogPanel, PathRow, card, h1, h2, muted
from core.config import COOKIE_FILE
from core.output import app_base_dir

from .server import MonitorServer


class MonitorPage(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.server = None
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(h1("实时监控"))
        head.addWidget(muted("视频实时数据仪表盘：播放/点赞/投币/收藏/分享/各分P正在看，"
                             "历史趋势跨启动续接。"))
        root.addLayout(head)

        params, play = card()
        play.addWidget(h2("监控目标"))
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
        for val, label in (("auto", "auto —— 自动降级（推荐）"),
                           ("h2-ja3", "h2-ja3 —— Chrome TLS 指纹"),
                           ("urllib", "urllib —— 标准库回退")):
            self.transport_combo.addItem(label, val)
        form.addRow("传输通道", self.transport_combo)
        default_data = str(Path(self.cfg.get("out_dir") or app_base_dir()) / "监控数据")
        self.data_row = PathRow(None, default_data)
        form.addRow("数据目录", self.data_row)
        play.addLayout(form)

        btn_row = QHBoxLayout()
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
        self.status_label = QLabel("未启动")
        self.status_label.setObjectName("muted")
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_browser)
        btn_row.addStretch(1)
        btn_row.addWidget(self.status_label)
        play.addLayout(btn_row)
        root.addWidget(params)

        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(140)
        root.addWidget(self.log_panel)

        root.addWidget(h2("仪表盘"), 0)
        self.dash_hint = muted("启动监控后将自动在系统浏览器中打开仪表盘；"
                               "也可随时点击「在浏览器打开」再次打开。")
        self.dash_hint.setAlignment(Qt.AlignCenter)
        root.addWidget(self.dash_hint)
        root.addStretch(1)

    # ---------- 控制 ----------

    def _log(self, msg):
        self.log_panel.append(str(msg))

    def on_start(self):
        if self.server is not None and self.server.running:
            return
        raw = self.bvid_edit.text().strip() or self.bvid_edit.placeholderText()
        m = re.search(r"BV[0-9A-Za-z]{10}", raw)  # BV 区分大小写，保留原样
        if not m:
            self.status_label.setText("BV 号格式有误（应形如 BV1xxxxxxxxx）")
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
        self.status_label.setText(f"运行中 · {url}")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        webbrowser.open(url)

    def on_stop(self):
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.status_label.setText("未启动")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_open_browser(self):
        if self.server is not None and self.server.running:
            webbrowser.open(self.server.url)

    def on_app_close(self):
        self.on_stop()
