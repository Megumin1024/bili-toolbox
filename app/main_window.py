# -*- coding: utf-8 -*-
"""主窗口：左侧边栏导航（注册表自动生成）+ 右侧页面栈。"""
import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

import core
from .registry import all_tools
from .settings_page import SettingsPage, VERSION


def _nav_button(icon_name, text):
    btn = QPushButton(f"  {text}")
    btn.setObjectName("nav")
    btn.setIcon(qta.icon(icon_name))
    btn.setCheckable(True)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("B站工具箱")
        self.resize(1120, 760)
        self.setMinimumSize(960, 640)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.setCentralWidget(central)

        # ---------- 侧边栏 ----------
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(196)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(4)
        logo = QLabel("B站工具箱")
        logo.setObjectName("logo")
        logo.setAlignment(Qt.AlignHCenter)
        sub = QLabel("BiliToolbox")
        sub.setObjectName("muted")
        sub.setAlignment(Qt.AlignHCenter)
        sl.addWidget(logo)
        sl.addWidget(sub)
        sl.addSpacing(14)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._navs = []

        self.tools = all_tools()
        for spec in self.tools:
            btn = _nav_button(spec.icon, spec.name)
            self._group.addButton(btn)
            self._navs.append(btn)
            sl.addWidget(btn)
            btn.clicked.connect(lambda _=False, i=len(self._navs) - 1: self.switch(i))

        sl.addStretch(1)
        sep = QFrame()
        sep.setObjectName("line")
        sep.setFrameShape(QFrame.HLine)
        sl.addWidget(sep)
        btn_set = _nav_button("fa5s.cog", "设置")
        self._group.addButton(btn_set)
        self._navs.append(btn_set)
        sl.addWidget(btn_set)
        btn_set.clicked.connect(
            lambda _=False, i=len(self._navs) - 1: self.switch(i))

        foot = QLabel(f"{VERSION}\n仅采集公开数据 · 游客接口")
        foot.setObjectName("muted")
        foot.setAlignment(Qt.AlignHCenter)
        sl.addWidget(foot)

        # ---------- 页面栈 ----------
        self.stack = QStackedWidget()
        self.pages = []
        for spec in self.tools:
            page = spec.factory(self.cfg)
            self.pages.append(page)
            self.stack.addWidget(page)
        self.settings_page = SettingsPage(cfg, on_theme_change=self._apply_theme)
        self.pages.append(self.settings_page)
        self.stack.addWidget(self.settings_page)

        lay.addWidget(side)
        lay.addWidget(self.stack, 1)
        self.switch(0)

    def switch(self, index):
        self._navs[index].setChecked(True)
        self.stack.setCurrentIndex(index)

    def _apply_theme(self, theme):
        from PySide6.QtWidgets import QApplication
        from .theme import apply
        app = QApplication.instance()
        if app:
            apply(app, theme)

    def closeEvent(self, event):
        for page in self.pages:
            closer = getattr(page, "on_app_close", None)
            if closer:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        super().closeEvent(event)
