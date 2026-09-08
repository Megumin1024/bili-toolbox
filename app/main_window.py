# -*- coding: utf-8 -*-
"""主窗口：轻二次元数据终端侧边栏 + 注册表驱动的页面栈。"""
import sys
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from .registry import all_tools
from .settings_page import SettingsPage, VERSION
from .theme import tokens


def _asset(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / name


def _set_nav_icon(btn, theme):
    palette = tokens(theme)
    btn.setIcon(qta.icon(btn.property("iconName"),
                         color=palette["muted"],
                         color_active=palette["accent"],
                         color_disabled=palette["muted"]))


def _nav_button(icon_name, text, theme):
    btn = QPushButton(f"  {text}")
    btn.setObjectName("nav")
    btn.setProperty("iconName", icon_name)
    _set_nav_icon(btn, theme)
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

        theme = self.cfg.get("theme") or "dark"

        # ---------- 侧边栏 ----------
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(216)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(15, 18, 15, 14)
        sl.setSpacing(5)

        brand = QWidget()
        brand.setObjectName("transparent")
        brand_lay = QHBoxLayout(brand)
        brand_lay.setContentsMargins(3, 0, 3, 0)
        brand_lay.setSpacing(10)
        badge = QLabel("✦")
        badge.setObjectName("brandBadge")
        badge.setAlignment(Qt.AlignCenter)
        icon_path = _asset("assets/icon.png")
        if icon_path.exists():
            pixmap = QPixmap(str(icon_path)).scaled(
                30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if not pixmap.isNull():
                badge.setText("")
                badge.setPixmap(pixmap)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel("B站工具箱")
        title.setObjectName("brandTitle")
        caption = QLabel("公开数据工具")
        caption.setObjectName("brandCaption")
        brand_text.addWidget(title)
        brand_text.addWidget(caption)
        brand_lay.addWidget(badge)
        brand_lay.addLayout(brand_text, 1)
        sl.addWidget(brand)
        sl.addSpacing(18)

        tools_label = QLabel("功能模块")
        tools_label.setObjectName("eyebrow")
        sl.addWidget(tools_label)
        sl.addSpacing(3)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._navs = []

        self.tools = all_tools()
        for spec in self.tools:
            btn = _nav_button(spec.icon, spec.name, theme)
            self._group.addButton(btn)
            self._navs.append(btn)
            sl.addWidget(btn)
            btn.clicked.connect(lambda _=False, i=len(self._navs) - 1: self.switch(i))

        sl.addStretch(1)
        sep = QFrame()
        sep.setObjectName("line")
        sep.setFrameShape(QFrame.HLine)
        sl.addWidget(sep)
        btn_set = _nav_button("fa5s.cog", "设置", theme)
        self._group.addButton(btn_set)
        self._navs.append(btn_set)
        sl.addWidget(btn_set)
        btn_set.clicked.connect(
            lambda _=False, i=len(self._navs) - 1: self.switch(i))

        foot = QLabel(f"{VERSION}\n仅处理公开数据")
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
        self.settings_page = SettingsPage(
            cfg,
            on_theme_change=self._apply_theme,
            on_task_reuse=self._reuse_task_params,
        )
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
        for btn in self._navs:
            _set_nav_icon(btn, theme)

    def _reuse_task_params(self, record):
        """切到历史记录对应页面并回填参数；绝不触发 on_start。"""
        tool_id = record.get("tool_id") if isinstance(record, dict) else None
        for index, page in enumerate(self.pages[:-1]):
            if getattr(page, "history_tool_id", "") != tool_id:
                continue
            if not record.get("reusable"):
                return False
            try:
                self.switch(index)
                page.apply_reusable_params(record.get("reusable_params") or {})
            except Exception:  # noqa: BLE001 - 回填失败不应启动或破坏主窗口
                return False
            return True
        return False

    def closeEvent(self, event):
        for page in self.pages:
            closer = getattr(page, "on_app_close", None)
            if closer:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        super().closeEvent(event)
