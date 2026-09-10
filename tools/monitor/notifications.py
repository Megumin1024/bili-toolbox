# -*- coding: utf-8 -*-
"""监控提醒的 Qt 主线程通知适配器。

MonitorServer 的采集线程不能直接调用本模块；MonitorPage 通过 Qt Signal
把事件切回 GUI 线程后才调用这些方法。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


LogFunc = Callable[[str], None]


class QtNotificationAdapter:
    """页面持有的系统托盘适配器；通知和声音两个通道彼此独立。"""

    def __init__(self, log: LogFunc | None = None, parent=None):
        self._log = log or (lambda _message: None)
        self._tray: QSystemTrayIcon | None = None
        self._closed = False
        self._availability_logged = False
        app = QApplication.instance()
        if app is None:
            self._log("Windows通知不可用：当前没有QApplication。")
            self._availability_logged = True
            return
        try:
            self._tray = QSystemTrayIcon(parent)
            if not app.windowIcon().isNull():
                self._tray.setIcon(app.windowIcon())
            self._tray.setToolTip("B站工具箱 · 监控提醒")
            self._tray.show()
        except Exception as exc:  # noqa: BLE001 - 通知通道不得影响页面
            self._tray = None
            self._log(f"Windows通知适配器初始化失败（{type(exc).__name__}）。")

    @property
    def tray(self) -> QSystemTrayIcon | None:
        return self._tray

    def _on_gui_thread(self) -> bool:
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            self._log("提醒通道调用线程不是GUI主线程，已忽略本次操作。")
            return False
        return True

    def _check_notifications(self) -> bool:
        if self._closed or self._tray is None:
            return False
        try:
            available = bool(
                QSystemTrayIcon.isSystemTrayAvailable()
                and QSystemTrayIcon.supportsMessages()
            )
        except Exception as exc:  # noqa: BLE001 - 系统能力探测失败可降级
            available = False
            if not self._availability_logged:
                self._log(f"Windows通知能力检测失败（{type(exc).__name__}）。")
                self._availability_logged = True
        if not available and not self._availability_logged:
            self._log("Windows通知不可用：系统托盘或消息能力不可用，提醒仍保留在列表中。")
            self._availability_logged = True
        return available

    def notify(self, title: str, message: str) -> bool:
        if not self._on_gui_thread() or not self._check_notifications():
            return False
        try:
            self._tray.showMessage(
                str(title), str(message), QSystemTrayIcon.Information, 5000)
            return True
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响声音/采集
            self._log(f"Windows通知发送失败（{type(exc).__name__}）。")
            return False

    def play_sound(self) -> bool:
        if self._closed or not self._on_gui_thread():
            return False
        try:
            QApplication.beep()
            return True
        except Exception as exc:  # noqa: BLE001 - 声音失败不影响通知/采集
            self._log(f"声音提醒失败（{type(exc).__name__}）。")
            return False

    def close(self) -> None:
        if self._tray is None:
            self._closed = True
            return
        if not self._on_gui_thread():
            return
        tray, self._tray = self._tray, None
        self._closed = True
        tray.hide()
        tray.deleteLater()


class RecordingNotificationAdapter:
    """测试/截图用假通道，不会弹出系统通知或播放声音。"""

    def __init__(self, fail_notify: bool = False):
        self.fail_notify = fail_notify
        self.notifications: list[tuple[str, str]] = []
        self.sound_count = 0
        self.closed = False

    def notify(self, title: str, message: str) -> bool:
        if self.fail_notify:
            raise RuntimeError("fake notification failure")
        self.notifications.append((str(title), str(message)))
        return True

    def play_sound(self) -> bool:
        self.sound_count += 1
        return True

    def close(self) -> None:
        self.closed = True
