# -*- coding: utf-8 -*-
"""监控提醒的 Qt 主线程通知适配器。

MonitorServer 的采集线程不能直接调用本模块；MonitorPage 通过 Qt Signal
把事件切回 GUI 线程后才调用这些方法。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from core.redact import sanitize_text

from .alerts import WebhookRateLimiter


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


WEBHOOK_TIMEOUT_SECONDS = 5.0


def webhook_payload(event_type: str, title: str, text: str) -> dict:
    """显式白名单构造推送字段：只允许 title/text/event_type 三个键。

    绝不整包序列化事件对象或 kwargs；title/text 再过一层 sanitize_text——
    正常告警话术只含数字与固定文案，若未来混入 URL/路径/凭据形态的串，
    在构造处就被打码，不靠调用方自觉。
    """
    return {
        "title": sanitize_text(title),
        "text": sanitize_text(text),
        "event_type": str(event_type or ""),
    }


def _post_json(url: str, body: bytes, timeout: float) -> int:
    """同步 POST JSON，返回 HTTP 状态码。

    不读取响应体：响应体可能回显完整 URL，失败详情只保留状态码/错误类别。
    """
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status)


class WebhookAdapter:
    """自定义 Webhook 推送通道（服务端出站，默认关）。

    线程模型：send() 由 GUI 线程调用，只做 URL/频控检查并构造 payload，
    真正的 HTTP 发送在一次性 daemon 线程里执行——绝不阻塞采集线程与
    GUI 主线程。失败（超时/连接错误/非 2xx）不重试、不冒泡为任务失败，
    只经 log 报告一次，URL 一律经 sanitize_text 打码。
    """

    def __init__(self, url_getter: Callable[[], str],
                 log: LogFunc | None = None,
                 clock: Callable[[], float] = time.time,
                 timeout: float = WEBHOOK_TIMEOUT_SECONDS,
                 sender: Callable[[str, bytes, float], int] | None = None,
                 rate_limiter: WebhookRateLimiter | None = None):
        self._url_getter = url_getter
        self._log = log or (lambda _message: None)
        self._timeout = timeout
        self._sender = sender or _post_json
        self._rate_limiter = rate_limiter or WebhookRateLimiter(clock=clock)
        self._closed = False

    @property
    def rate_limiter(self) -> WebhookRateLimiter:
        return self._rate_limiter

    def send(self, event_type: str, title: str, text: str) -> bool:
        """提交一次推送；返回是否已提交（不代表送达）。

        URL 为空、频控超限、已关闭时零动作——不创建线程、不发请求。
        """
        if self._closed:
            return False
        url = str(self._url_getter() or "").strip()
        if not url:
            self._log("Webhook 推送未执行：URL 为空。")
            return False
        if not self._rate_limiter.try_acquire():
            self._log("Webhook 频控：本小时已丢弃 "
                      f"{self._rate_limiter.dropped_in_window()} 条。")
            return False
        body = json.dumps(
            webhook_payload(event_type, title, text),
            ensure_ascii=False).encode("utf-8")
        threading.Thread(target=self._deliver, args=(url, body),
                         daemon=True).start()
        return True

    def _deliver(self, url: str, body: bytes) -> None:
        try:
            status = int(self._sender(url, body, self._timeout))
        except urllib.error.HTTPError as exc:
            # 非 2xx 在 urllib 里以异常形态出现；只取状态码，不读响应体
            # （响应体可能回显完整 URL）。
            self._log(f"Webhook 发送失败：HTTP {exc.code}"
                      f"（{sanitize_text(url)}）。")
        except Exception as exc:  # noqa: BLE001 - 发送失败绝不冒泡为任务失败
            self._log(f"Webhook 发送失败：{type(exc).__name__}"
                      f"（{sanitize_text(url)}）。")
        else:
            if 200 <= status < 300:
                self._log("Webhook 推送已发送。")
            else:
                self._log(f"Webhook 发送失败：HTTP {status}"
                          f"（{sanitize_text(url)}）。")

    def notify(self, title: str, message: str) -> bool:
        """兼容既有通知适配器接口；event_type 固定为 notify。"""
        return self.send("notify", title, message)

    def close(self) -> None:
        self._closed = True
