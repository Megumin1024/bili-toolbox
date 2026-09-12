# -*- coding: utf-8 -*-
"""跨工具共享的 Webhook 推送通道（自 tools/monitor/notifications.py 行为等价上提）。

包含：白名单 payload 构造（webhook_payload）、通用 JSON 与 Server酱 两种
格式、滑动窗口频控（WebhookRateLimiter 及其常量，自 tools/monitor/alerts.py
逐字节复制）与异步发送适配器（WebhookAdapter）。函数与类本体逐字节搬移、
AST 比对一致；monitor 侧改为 re-export，既有导入路径与行为不变。

隐私语义（上提不得改变任何一条）：

- payload 白名单只允许 title/text/event_type 三个键，文本再过 sanitize_text；
- URL/SendKey 只经 sanitize 后进日志，响应体不回显原文；
- 频控每小时 ≤10 条、跨会话保留；SendKey/URL 不进任务历史与预设。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Callable

from core.redact import sanitize_text

LogFunc = Callable[[str], None]

WEBHOOK_RATE_LIMIT = 10
WEBHOOK_RATE_WINDOW_SECONDS = 3600.0


class WebhookRateLimiter:
    """Webhook 推送的滑动窗口频控：一个窗口内最多放行 limit 条。

    纯逻辑、无 I/O；时钟可注入以便离线测试。约定单线程调用（监控页只在
    GUI 线程申请名额），内部不加锁。超限的申请计入丢弃记录，供日志
    输出「本小时已丢弃 N 条」的累计计数。
    """

    def __init__(self, limit: int = WEBHOOK_RATE_LIMIT,
                 window_seconds: float = WEBHOOK_RATE_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.time):
        self._limit = max(1, int(limit))
        self._window_seconds = max(1.0, float(window_seconds))
        self._clock = clock
        self._sent: deque[float] = deque()
        self._dropped: deque[float] = deque()

    def try_acquire(self) -> bool:
        """申请一个发送名额；窗口未满放行，超限记一次丢弃并返回 False。"""
        now = float(self._clock())
        self._purge(now)
        if len(self._sent) >= self._limit:
            self._dropped.append(now)
            return False
        self._sent.append(now)
        return True

    def dropped_in_window(self) -> int:
        """当前滑动窗口内被丢弃的条数。"""
        self._purge(float(self._clock()))
        return len(self._dropped)

    def _purge(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self._window_seconds:
            self._sent.popleft()
        while self._dropped and now - self._dropped[0] >= self._window_seconds:
            self._dropped.popleft()


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


WEBHOOK_FORMATS = ("json", "serverchan")
SERVERCHAN_HOST = "sctapi.ftqq.com"
# 裸 SendKey 形态：8 位以上字母数字，且不含 scheme 或点号。
SERVERCHAN_SENDKEY_RE = re.compile(r"[A-Za-z0-9]{8,}")
SERVERCHAN_MESSAGE_MAX_CHARS = 80
SERVERCHAN_BODY_LIMIT_BYTES = 65536


def serverchan_url(raw: str) -> str | None:
    """Server酱 输入归一化：完整 .send URL 原样使用，裸 SendKey 包裹。

    返回 None 表示输入不是可识别形态，调用方记「Server酱密钥格式不认识」
    并零动作。原始输入可能就是 SendKey，因此这里不回传任何输入片段——
    拒绝原因由调用方用固定文案记录，SendKey 绝不进日志。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if "://" in text or "." in text:
        parts = urllib.parse.urlsplit(text)
        if (parts.scheme in ("http", "https")
                and (parts.hostname or "").lower() == SERVERCHAN_HOST
                and parts.path.lower().endswith(".send")):
            return text
        return None
    if SERVERCHAN_SENDKEY_RE.fullmatch(text):
        return f"https://{SERVERCHAN_HOST}/{text}.send"
    return None


def _serverchan_message_summary(message) -> str:
    """message 摘要：先 sanitize 再截断到 80 字符。

    顺序必须是先 sanitize 后截断——凭据/URL/路径形态先被整段打码，截断
    只会截到打码标记；反过来先截断会把超长串中段的敏感片段切在中间留下。
    """
    text = sanitize_text(str(message or ""))
    if len(text) > SERVERCHAN_MESSAGE_MAX_CHARS:
        text = text[:SERVERCHAN_MESSAGE_MAX_CHARS] + "…"
    return text


def _parse_serverchan_body(raw: bytes) -> tuple[int | None, str]:
    """从 Server酱 响应体解析 code/message 摘要；响应原文绝不返回。

    Server酱 以 JSON 的 code 字段表达业务成败，不看内容会把 code!=0 的
    「发送失败」误判成已送达。JSON 解析失败、顶层不是对象或 code 不是
    数字时 code 为 None——失败日志退化为只报 HTTP 状态码。
    """
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    code = data.get("code")
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        return None, _serverchan_message_summary(data.get("message"))
    return int(code), _serverchan_message_summary(data.get("message"))


def _post_serverchan(url: str, body: bytes,
                     timeout: float) -> tuple[int, int | None, str]:
    """同步 POST form 表单到 Server酱，读取响应并解析 code/message。

    与 _post_json 不同，这里必须读响应体（理由见 _parse_serverchan_body）。
    返回 (http_status, api_code, message)；message 已 sanitize 并截断，
    响应原文只在函数内瞬时存在，绝不返回、绝不落日志。非 2xx 在 urllib
    以 HTTPError 形态出现，Server酱 以 JSON 报错，因此同样读取错误体解析。
    """
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    status: int
    raw = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read(SERVERCHAN_BODY_LIMIT_BYTES)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            raw = exc.read(SERVERCHAN_BODY_LIMIT_BYTES)
        except Exception:  # noqa: BLE001 - 错误体读不到时只报状态码
            raw = b""
        finally:
            exc.close()
    code, message = _parse_serverchan_body(raw)
    return status, code, message


class WebhookAdapter:
    """自定义 Webhook 推送通道（服务端出站，默认关）。

    线程模型：send() 由 GUI 线程调用，只做 URL/格式/频控检查并构造 payload，
    真正的 HTTP 发送在一次性 daemon 线程里执行——绝不阻塞采集线程与
    GUI 主线程。失败（超时/连接错误/非 2xx/code!=0）不重试、不冒泡为任务
    失败，只经 log 报告一次，URL 一律经 sanitize_text 打码。

    格式在每次发送时经 format_getter 读取（json=通用 JSON，serverchan=
    Server酱 form 表单），非法值回退 json；切换下拉即时生效、无需重建
    适配器。Server酱 的目标 URL 由输入归一化得出（serverchan_url），
    归一化失败零动作，不消耗频控预算。
    """

    def __init__(self, url_getter: Callable[[], str],
                 log: LogFunc | None = None,
                 clock: Callable[[], float] = time.time,
                 timeout: float = WEBHOOK_TIMEOUT_SECONDS,
                 sender: Callable[[str, bytes, float], int] | None = None,
                 rate_limiter: WebhookRateLimiter | None = None,
                 format_getter: Callable[[], str] | None = None,
                 serverchan_sender: Callable[
                     [str, bytes, float], tuple[int, int | None, str]]
                 | None = None):
        self._url_getter = url_getter
        self._format_getter = format_getter or (lambda: "json")
        self._log = log or (lambda _message: None)
        self._timeout = timeout
        self._sender = sender or _post_json
        self._serverchan_sender = serverchan_sender or _post_serverchan
        self._rate_limiter = rate_limiter or WebhookRateLimiter(clock=clock)
        self._closed = False

    @property
    def rate_limiter(self) -> WebhookRateLimiter:
        return self._rate_limiter

    def _format(self) -> str:
        """发送时读取当前格式；未知值一律回退通用 JSON。"""
        fmt = str(self._format_getter() or "").strip().lower()
        return fmt if fmt in WEBHOOK_FORMATS else "json"

    def send(self, event_type: str, title: str, text: str) -> bool:
        """提交一次推送；返回是否已提交（不代表送达）。

        URL 为空、格式无法识别、频控超限、已关闭时零动作——不创建线程、
        不发请求。
        """
        if self._closed:
            return False
        url = str(self._url_getter() or "").strip()
        if not url:
            self._log("Webhook 推送未执行：URL 为空。")
            return False
        fmt = self._format()
        if fmt == "serverchan":
            target = serverchan_url(url)
            if target is None:
                # 原始输入可能就是 SendKey，日志只给固定提示，不回显输入。
                self._log("Server酱 推送未执行：Server酱密钥格式不认识，"
                          "请填 SendKey 或以 .send 结尾的完整链接。")
                return False
        else:
            target = url
        if not self._rate_limiter.try_acquire():
            self._log("Webhook 频控：本小时已丢弃 "
                      f"{self._rate_limiter.dropped_in_window()} 条。")
            return False
        payload = webhook_payload(event_type, title, text)
        if fmt == "serverchan":
            body = urllib.parse.urlencode(
                {"title": payload["title"], "desp": payload["text"]}
            ).encode("utf-8")
            threading.Thread(target=self._deliver_serverchan,
                             args=(target, body), daemon=True).start()
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            threading.Thread(target=self._deliver, args=(target, body),
                             daemon=True).start()
        return True

    def _deliver_serverchan(self, url: str, body: bytes) -> None:
        try:
            status, api_code, message = self._serverchan_sender(
                url, body, self._timeout)
        except urllib.error.HTTPError as exc:
            # 非 2xx 由 _post_serverchan 消化并解析；这里兜住自定义桩仍以
            # 异常抛出的情形——只报状态码，不读响应体。
            self._log(f"Server酱 推送失败：HTTP {exc.code}"
                      f"（{sanitize_text(url)}）。")
        except Exception as exc:  # noqa: BLE001 - 发送失败绝不冒泡为任务失败
            self._log(f"Server酱 推送失败：{type(exc).__name__}"
                      f"（{sanitize_text(url)}）。")
        else:
            if 200 <= status < 300 and api_code == 0:
                self._log("Server酱 推送已发送。")
                return
            detail = f"HTTP {status}"
            if api_code is not None:
                detail += f"，code={api_code}"
            if message:
                detail += f"，message={message}"
            self._log(f"Server酱 推送失败：{detail}"
                      f"（{sanitize_text(url)}）。")

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
