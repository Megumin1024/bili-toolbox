# -*- coding: utf-8 -*-
"""任务预算：单次任务的请求数 / 时长 / 记录数上限，到限**优雅安全停止**。

为什么放在 core/：转接评估（2026-09-12）判定原始方向⑥（高并发）⑧（无人值守）
的合规替代底座缺失——`core/` 没有集中的任务预算。预算是对平台施压的自律上限，
与风控处理同向：到限就停下来，把已抓数据完整落盘、断点状态保持可续传，
任务按**正常完成**收尾（stopped_reason="budget_reached"），不是红色失败。

口径与不变量：

- 一次请求 = 一次 `fetch_json` / `fetch_bytes` **调用**（业务请求数），不含其
  内部传输重试（重试已受 `core.client.TOTAL_WAIT_BUDGET` 约束）；
- budget 对象为**每次任务新建**（pipeline 由数值参数构造），绝不做进程级单例；
- 预算自身不做任何等待：所有检查点瞬时完成，无新增不可取消的 sleep；
- 到限不是网络故障：`BudgetExhaustedError` 直接继承 Exception，不进
  TransportError 系，因此不会被重试 / 退避 / 熔断逻辑捕获或分类，也不改变
  gate 状态、不计入 client.stats 的任何失败项；
- 取消优先于预算：cancel 为真时无论预算状态立即取消（判定顺序由
  core.client._request 的检查点顺序保证）。
"""
from __future__ import annotations

import time

# UI 默认值与输入范围。pipeline 层的数值参数缺省 None = 该项无上限，
# 旧调用（不传预算参数）行为与没有预算时逐字节一致。
DEFAULT_MAX_REQUESTS = 20000
DEFAULT_MAX_MINUTES = 240
MAX_REQUESTS_LIMIT = 100000
MAX_MINUTES_LIMIT = 1440


class BudgetExhaustedError(Exception):
    """任务预算已到限：调用方应按正常完成路径收尾，不是失败。"""


class TaskBudget:
    """单次任务的预算账本。纯逻辑、无 I/O；clock 可注入以便离线测试。

    三类上限各自独立生效，None 表示该项无上限。任一上限到达后 `expired()`
    为真，`check_request()` 抛 `BudgetExhaustedError`。计数方法是累加式：
    请求在检查点之后由调用方 `observe_request()` 记账，记录数由落盘方
    `observe_records(n)` 记账。
    """

    def __init__(self, max_requests=None, max_seconds=None, max_records=None,
                 clock=None, sleep=None):
        self.max_requests = (int(max_requests)
                             if max_requests is not None else None)
        self.max_seconds = (float(max_seconds)
                            if max_seconds is not None else None)
        self.max_records = (int(max_records)
                            if max_records is not None else None)
        # clock 用于时长判定；sleep 仅为注入约定保留——当前所有检查点都是
        # 瞬时检查，预算路径不做任何等待（含分段等待）。
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._start = self._clock()
        self.requests = 0
        self.records = 0

    # ---------- 判定 ----------

    def _seconds_used(self):
        return max(0.0, self._clock() - self._start)

    def reason(self):
        """到限原因（人类可读，只含计数与时长）；未到限返回 None。"""
        if self.max_requests is not None and self.requests >= self.max_requests:
            return f"请求数已达上限 {self.max_requests:,} 次"
        if self.max_records is not None and self.records >= self.max_records:
            return f"记录数已达上限 {self.max_records:,} 条"
        if (self.max_seconds is not None
                and self._seconds_used() >= self.max_seconds):
            return f"运行时长已达上限 {self.max_seconds / 60:g} 分钟"
        return None

    def expired(self):
        return self.reason() is not None

    # ---------- 检查点 ----------

    def check_request(self):
        """发起下一次请求前的强制检查；到限抛 BudgetExhaustedError。"""
        reason = self.reason()
        if reason is not None:
            raise BudgetExhaustedError(f"任务预算到限：{reason}")

    def observe_request(self):
        """记账一次业务请求（fetch_json/fetch_bytes 调用，不含内部重试）。"""
        self.requests += 1

    def observe_records(self, n):
        """记账 n 条已落盘记录。"""
        if n and int(n) > 0:
            self.records += int(n)

    # ---------- 观测 ----------

    def status(self):
        """当前预算状态（日志/诊断用；只含计数与时长，无 URL/路径细节）。"""
        reason = self.reason()
        return {
            "requests": self.requests,
            "max_requests": self.max_requests,
            "records": self.records,
            "max_records": self.max_records,
            "elapsed_seconds": round(self._seconds_used(), 3),
            "max_seconds": self.max_seconds,
            "expired": reason is not None,
            "reason": reason,
        }
