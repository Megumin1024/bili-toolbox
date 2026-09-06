# -*- coding: utf-8 -*-
"""设置页：外观 / 默认输出目录 / 网络与风控（通道+代理池）/ 关于。"""
import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from core import config, session

import core
from .theme import apply as apply_theme
from .widgets import PathRow, card, h2, muted

VERSION = f"v{core.__version__}"

THEME_LABELS = [("dark", "深色"), ("light", "浅色")]
TRANSPORT_LABELS = [
    ("auto", "auto —— 自动降级（推荐）"),
    ("h2-ja3", "h2-ja3 —— Chrome TLS 指纹"),
    ("urllib", "urllib —— 标准库回退"),
]


class SettingsPage(QWidget):
    def __init__(self, cfg, on_theme_change=None, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.on_theme_change = on_theme_change
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        # 外观
        c1, l1 = card()
        l1.addWidget(h2("外观"))
        row = QHBoxLayout()
        row.addWidget(QLabel("主题"))
        self.theme_combo = QComboBox()
        for _val, label in THEME_LABELS:
            self.theme_combo.addItem(label, _val)
        self.theme_combo.setCurrentIndex(
            max(0, [v for v, _ in THEME_LABELS].index(self.cfg.get("theme", "dark"))))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        row.addWidget(self.theme_combo)
        row.addStretch(1)
        l1.addLayout(row)
        root.addWidget(c1)

        # 默认输出目录
        c2, l2 = card()
        l2.addWidget(h2("默认输出目录"))
        self.out_row = PathRow(None, self.cfg.get("out_dir") or "")
        self.out_row.edit.setPlaceholderText("留空 = 自动（exe 旁/仓库旁 的「导出」目录）")
        l2.addWidget(self.out_row)
        root.addWidget(c2)

        # 网络
        c3, l3 = card()
        l3.addWidget(h2("网络"))
        row_t = QHBoxLayout()
        row_t.addWidget(QLabel("传输通道"))
        self.transport_combo = QComboBox()
        for val, label in TRANSPORT_LABELS:
            self.transport_combo.addItem(label, val)
        cur = self.cfg.get("transport", "auto")
        self.transport_combo.setCurrentIndex(
            max(0, [v for v, _ in TRANSPORT_LABELS].index(cur)))
        row_t.addWidget(self.transport_combo, 1)
        l3.addLayout(row_t)
        l3.addWidget(QLabel("代理池（逗号分隔，direct=直连）"))
        self.proxy_edit = QLineEdit(self.cfg.get("proxy_spec", ""))
        self.proxy_edit.setPlaceholderText(
            "direct,socks5://127.0.0.1:7890,http://user:pass@1.2.3.4:8080")
        l3.addWidget(self.proxy_edit)
        root.addWidget(c3)

        save = QPushButton("保存设置")
        save.setObjectName("primary")
        save.setIcon(qta.icon("fa5s.check"))
        save.clicked.connect(self.on_save)
        row_s = QHBoxLayout()
        row_s.addWidget(save)
        row_s.addStretch(1)
        root.addLayout(row_s)
        root.addStretch(1)

        # 关于
        c4, l4 = card()
        l4.addWidget(h2("关于"))
        l4.addWidget(muted(
            f"B站工具箱 {VERSION} · B站公开数据采集与分析工具。"))
        l4.addWidget(muted(
            "仅抓取游客可见的公开数据，请遵守 B 站用户协议与 robots 精神，勿用于"
            "刷量等违规用途。"))
        root.addWidget(c4)

    def _on_theme_changed(self, *_):
        """切换主题下拉框立即生效（无需点保存）。"""
        theme = self.theme_combo.currentData()
        self.cfg["theme"] = theme
        config.save({"theme": theme})
        if self.on_theme_change:
            self.on_theme_change(theme)

    def on_save(self):
        theme = self.theme_combo.currentData()
        transport = self.transport_combo.currentData()
        proxy_spec = self.proxy_edit.text().strip()
        out_dir = self.out_row.value()
        changed_net = (transport != self.cfg.get("transport")
                       or proxy_spec != self.cfg.get("proxy_spec", ""))
        self.cfg.update({"theme": theme, "transport": transport,
                         "proxy_spec": proxy_spec, "out_dir": out_dir})
        config.save(self.cfg)
        session.configure(proxy_spec=proxy_spec or None, transport=transport,
                          cookie_path=config.COOKIE_FILE, force=changed_net)
        if self.on_theme_change:
            self.on_theme_change(theme)
        QMessageBox.information(self, "设置", "已保存"
                                + ("，网络配置已即时生效" if changed_net else ""))
