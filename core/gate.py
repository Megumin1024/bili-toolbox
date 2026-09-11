# -*- coding: utf-8 -*-
"""全局请求闸门：进程内所有 B 站请求共用的限速 + 熔断。

与 core.backoff 的分工：
- backoff 回答"本次调用内部，这次失败之后该等多久"——纯函数、一次性；
- gate    回答"现在这一刻，整个进程该不该发请求"——有状态、跨调用、跨工具。

两者互补而非替代。只有 backoff 时，一次风控之后下一个视频会立刻再撞上去，
因为退避状态随调用结束就丢了；只有 gate 时，单次调用内的指数退避会缺失。

时钟：闸门用**逻辑时钟**——真实时钟的前进量 + 自己等待过的时长。这样注入
恒定 clock 与 no-op sleep 的离线测试不会空转（真实时钟不动，逻辑时钟靠等待推进），
而生产环境照常跟随 time.monotonic。

约束：不发起请求、不读写文件；clock/sleep 可注入。
"""
from __future__ import annotations

import threading
import time

from .cancel import wait as cancel_wait

# 两个请求之间的全局最小间隔（秒）。它是**兜底地板，不是目标速率**：
# 判定是 top-up（gap = 上次放行时刻 + min_interval - now），且请求自身的 RTT
# 天然计入间隔，所以只要工具自己的 pacing + RTT 已经超过这个值，闸门开销为 0。
# 取值只求压住"零间隔突发"（监控一个采样内 view + N 个分P 连发，间隔真的是 0），
# 不替代各工具自己的业务间隔。实测参考：评论抓取以 ~0.25s 间隔跑完 16w 条
# 评论全程无风控，本地板取 0.1 远低于该已证明安全的点位，对它是完全隐形的。
# 真正的保护是下面的熔断器，不是这个静态地板。
DEFAULT_MIN_INTERVAL = 0.1
# 连续多少次风控/限流信号后判定"平台正在拦我们"，整体暂停。
DEFAULT_FAILURE_THRESHOLD = 3
# 初始冷却与冷却上限（秒）。熔断反复触发时按倍数增长。
DEFAULT_COOLDOWN = 60.0
DEFAULT_MAX_COOLDOWN = 600.0
# 半开探针的回报超时：超过这么久没等到成败判定（例如任务被取消、
# 调用方忘了回报），就把探针作废，避免闸门被一个悬空探针永久卡住。
DEFAULT_PROBE_TIMEOUT = 90.0

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


class RequestGate:
    """限速 + 熔断的合一闸门。线程安全。"""

    def __init__(self, min_interval=DEFAULT_MIN_INTERVAL,
                 failure_threshold=DEFAULT_FAILURE_THRESHOLD,
                 cooldown=DEFAULT_COOLDOWN,
                 max_cooldown=DEFAULT_MAX_COOLDOWN,
                 probe_timeout=DEFAULT_PROBE_TIMEOUT,
                 clock=None, sleep=None):
        self.min_interval = max(0.0, float(min_interval))
        self.failure_threshold = max(1, int(failure_threshold))
        self.base_cooldown = max(0.0, float(cooldown))
        self.max_cooldown = max(self.base_cooldown, float(max_cooldown))
        self.probe_timeout = max(0.0, float(probe_timeout))
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._lock = threading.Lock()

        # 逻辑时钟：上次采样到的真实时间差 + 本闸门实际等待过的时长
        self._logical = 0.0
        self._last_raw = None

        self._state = STATE_CLOSED
        self._cooldown = self.base_cooldown
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._probe_started_at = 0.0
        self._consecutive_blocks = 0
        self._last_request_at = None
        # 观测计数（只读，供 info()/看板展示）
        self.acquires = 0
        self.waits = 0
        self.blocks_seen = 0
        self.opens = 0

    # ---------- 逻辑时钟 ----------
    def _now_locked(self):
        """调用方必须持有锁。"""
        raw = self._clock()
        if self._last_raw is not None and raw > self._last_raw:
            self._logical += raw - self._last_raw
        self._last_raw = raw
        return self._logical

    def _advance_locked(self, seconds):
        """等待结束后推进逻辑时钟，保证下一轮判定能收敛。"""
        if seconds > 0:
            self._logical += seconds

    # ---------- 准入 ----------
    def _delay_locked(self, now):
        """纯查询：现在还要等多久才能发请求，以及原因。"""
        gap = 0.0
        if self._last_request_at is not None:
            gap = max(0.0, self._last_request_at + self.min_interval - now)

        if self._state == STATE_OPEN:
            remaining = max(0.0, self._opened_at + self._cooldown - now)
            if remaining > 0:
                return remaining, "熔断冷却中"
            return gap, ("全局限速" if gap > 0 else "")

        if self._state == STATE_HALF_OPEN and self._probe_in_flight:
            if now - self._probe_started_at < self.probe_timeout:
                return max(self._cooldown, gap), "等待半开探针结果"
            # 探针回报超时（任务被取消等）：作废，允许重新探
            self._probe_in_flight = False

        return gap, ("全局限速" if gap > 0 else "")

    def acquire(self, cancel=None, on_wait=None):
        """等到允许发请求；被取消返回 False。

        on_wait(seconds, reason) 在即将等待前回调，供调用方把"暂停"写进日志——
        否则熔断会让任务静默卡住，用户不知道发生了什么。
        """
        while True:
            with self._lock:
                now = self._now_locked()
                delay, reason = self._delay_locked(now)
                if delay <= 0:
                    if self._state == STATE_OPEN:
                        self._state = STATE_HALF_OPEN          # 冷却结束，放一个探针
                    if self._state == STATE_HALF_OPEN:
                        self._probe_in_flight = True
                        self._probe_started_at = now
                    self._last_request_at = now
                    self.acquires += 1
                    return True
                self.waits += 1
            if on_wait is not None:
                on_wait(delay, reason)
            if not cancel_wait(delay, cancel, sleep=self._sleep):
                # 等不下去就放弃这次准入；已占的探针名额要还回去，否则闸门卡死
                with self._lock:
                    self._probe_in_flight = False
                    self._advance_locked(delay)
                return False
            with self._lock:
                self._advance_locked(delay)

    # ---------- 回报 ----------
    def record_success(self):
        """请求成功：闭合熔断，清零连续拦截计数。"""
        with self._lock:
            self._state = STATE_CLOSED
            self._cooldown = self.base_cooldown
            self._probe_in_flight = False
            self._consecutive_blocks = 0

    def record_block(self, reason=""):
        """平台拦截信号（风控/限流）：累积到阈值就整体暂停。"""
        with self._lock:
            self.blocks_seen += 1
            self._consecutive_blocks += 1
            if self._state == STATE_HALF_OPEN:
                # 探针仍被拦：说明还没恢复，冷却加倍后再开
                self._cooldown = min(self._cooldown * 2, self.max_cooldown)
                self._open_locked()
            elif (self._state == STATE_CLOSED
                  and self._consecutive_blocks >= self.failure_threshold):
                self._open_locked()

    def record_neutral(self):
        """既非成功也非拦截（传输错误、业务错误）：不据此开关熔断。

        只把悬空的探针名额释放掉，状态留在 half_open，让下一次请求继续探——
        传输层故障不该被误判成"平台在拦我们"，也不该被当成恢复证据。
        """
        with self._lock:
            self._probe_in_flight = False

    def _open_locked(self):
        self._state = STATE_OPEN
        self._opened_at = self._now_locked()
        self._probe_in_flight = False
        self.opens += 1

    # ---------- 观测 ----------
    @property
    def state(self):
        with self._lock:
            return self._state

    def status(self):
        with self._lock:
            now = self._now_locked()
            if self._state == STATE_OPEN:
                remaining = max(0.0, self._opened_at + self._cooldown - now)
            else:
                remaining = 0.0
            return {
                "state": self._state,
                "remaining_seconds": round(remaining, 1),
                "cooldown_seconds": round(self._cooldown, 1),
                "min_interval": self.min_interval,
                "acquires": self.acquires,
                "waits": self.waits,
                "blocks_seen": self.blocks_seen,
                "opens": self.opens,
            }


_SHARED = None
_SHARED_LOCK = threading.Lock()


def shared_gate():
    """进程级共享闸门：session 单例与监控自建 client 共用同一份背压。"""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = RequestGate()
        return _SHARED


def reset_shared_gate():
    """仅供测试：丢弃共享闸门状态，避免用例之间互相污染。"""
    global _SHARED
    with _SHARED_LOCK:
        _SHARED = None
