# -*- coding: utf-8 -*-
"""退避策略内核：纯函数回答"这种失败该等多久"。

与 core.net_errors 的分工：
- net_errors 回答"这是什么性质的失败"；
- backoff  回答"这种失败该等多久"。

约束：纯函数、无 I/O、无副作用——不 sleep、不读真实时钟、不发请求。
随机源与"当前时间"都可注入，保证离线可测。
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .net_errors import ErrorKind, is_retryable

# 单次等待上限（秒）：即使平台给了很长的 Retry-After，也不把一次调用拖死。
MAX_SINGLE_WAIT = 120.0
# 指数退避的基数上限。
RATE_LIMIT_MAX_BASE = 30.0
TRANSPORT_MAX_BASE = 8.0
# 抖动比例：实际等待 = base × (1 + JITTER_RATIO × rand())，用于打散并发重试峰值。
JITTER_RATIO = 0.25

_RATE_LIMIT_BASE = 4.0
_TRANSPORT_BASE = 2.0


def parse_retry_after(value, now=None):
    """Retry-After 头 → 等待秒数（已按 MAX_SINGLE_WAIT 截断）；无法解析返回 None。

    支持两种格式：
    - 秒数，如 "120"
    - HTTP-date，如 "Wed, 21 Oct 2015 07:28:00 GMT"（按与 now 的差值换算）

    now 可注入以便离线测试，默认取当前 UTC 时间。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(int(text))
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = (moment - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds <= 0:
        return 0.0
    return min(float(seconds), MAX_SINGLE_WAIT)


def backoff_seconds(kind, attempt, retry_after=None, rand=None):
    """第 attempt 次失败（0 起）后应等待的秒数，含抖动；不必重试的性质返回 0。

    - 平台明确给了 Retry-After 时优先遵守——这是对限流信号的正确响应；
    - 其余限流走更长指数退避，风控/超时/连接类走标准指数退避。
    """
    if not is_retryable(kind):
        return 0.0
    if retry_after is not None:
        base = retry_after
    elif kind is ErrorKind.RATE_LIMIT:
        base = min(_RATE_LIMIT_BASE * (2 ** attempt), RATE_LIMIT_MAX_BASE)
    else:
        base = min(_TRANSPORT_BASE ** attempt, TRANSPORT_MAX_BASE)
    if base <= 0:
        return 0.0
    draw = rand if rand is not None else random.random
    return min(base * (1.0 + JITTER_RATIO * draw()), MAX_SINGLE_WAIT)
