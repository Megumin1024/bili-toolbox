# -*- coding: utf-8 -*-
"""BiliClient 重试/退避/取消契约的特征化测试。

这些用例锁定"可观察行为"（重试次数、统计计数、异常类型、预热/轮换动作、
返回值），刻意不锁定具体等待秒数——退避参数的调整不应让契约测试变红。

全程离线：clock/sleep 注入，不打网络、不读文件、不真等待。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.backoff import JITTER_RATIO
from core.cancel import TaskCancelledError
from core.client import RETRY_ATTEMPTS, BiliClient
from core.gate import DEFAULT_MIN_INTERVAL, RequestGate
from core.proxy import ProxyPool
from core.transport import (BiliApiError, BiliRateLimitError, RiskBlocked,
                           TransportError)

_OK = {"code": 0, "data": {"ok": True}}


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0
        self.slice_sizes = []

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds
        self.slice_sizes.append(seconds)


class StubTransport:
    """按脚本回放响应/异常，并记录调用与生命周期动作。

    effects 中每个元素可以是异常实例（抛出）或 dict（返回）；脚本用尽后重复最后一项。
    """

    name = "stub"

    def __init__(self, effects):
        self._effects = list(effects)
        self.calls = 0
        self.warmups = 0
        self.closed = 0

    def get_json(self, url):
        self.calls += 1
        item = self._effects[min(self.calls - 1, len(self._effects) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def warmup(self, force=False):
        self.warmups += 1

    def close(self):
        self.closed += 1

    def get_cookies(self):
        return {}


def make_client(transport, pool=None, log=None, cancel=None):
    client = BiliClient(pool or ProxyPool(None), cookie_path=None,
                        log=log, clock=lambda: 0.0, sleep=RecordingSleep(),
                        cancel=cancel)
    client._get_transport = lambda: transport
    return client


class SuccessPathTests(unittest.TestCase):
    def test_success_records_stats_and_proxy_health(self):
        transport = StubTransport([_OK])
        client = make_client(transport)

        self.assertIs(client.fetch_json("https://api.example.invalid/x"), _OK)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.stats["requests"], 1)
        self.assertEqual(client.stats["last_transport"], "stub")
        self.assertEqual(client.pool.entries[0].total_ok, 1)
        self.assertEqual(client._sleep.calls, 0, "成功不应有任何等待")

    def test_recovers_after_one_transport_failure(self):
        transport = StubTransport([TransportError("boom"), _OK])
        client = make_client(transport)

        self.assertIs(client.fetch_json("https://api.example.invalid/x"), _OK)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.stats["net_errors"], 1)
        self.assertEqual(client.stats["requests"], 1)
        self.assertEqual(client.pool.entries[0].total_fail, 1)
        self.assertEqual(client.pool.entries[0].total_ok, 1, "成功应清零失败计数")
        self.assertEqual(client.pool.entries[0].failures, 0)
        self.assertGreater(client._sleep.total, 0.0)


class TerminalFailureTests(unittest.TestCase):
    def test_transport_error_exhausts_retries_and_raises_last(self):
        transport = StubTransport([TransportError("boom")])
        client = make_client(transport)

        with self.assertRaises(TransportError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, RETRY_ATTEMPTS)
        self.assertEqual(client.stats["net_errors"], RETRY_ATTEMPTS)
        self.assertEqual(client.stats["requests"], 0)

    def test_business_api_error_is_not_retried(self):
        transport = StubTransport([BiliApiError("API code=-404 msg=不存在")])
        client = make_client(transport)

        with self.assertRaises(BiliApiError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.stats["net_errors"], 0)
        self.assertEqual(client.pool.entries[0].total_fail, 0)
        self.assertEqual(client._sleep.calls, 0)


class RiskPathTests(unittest.TestCase):
    def test_risk_forces_warmup_and_invalidates_transport(self):
        transport = StubTransport([RiskBlocked("风控 412")])
        client = make_client(transport)

        with self.assertRaises(RiskBlocked):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, RETRY_ATTEMPTS)
        self.assertEqual(client.stats["risk_events"], RETRY_ATTEMPTS)
        self.assertEqual(transport.warmups, RETRY_ATTEMPTS, "每次风控都应强制重新预热")

    def test_risk_rotates_proxy_when_alternative_exists(self):
        pool = ProxyPool("direct,http://u:p@1.2.3.4:8080")
        transport = StubTransport([RiskBlocked("风控 412")])
        client = make_client(transport, pool=pool)

        with self.assertRaises(RiskBlocked):
            client.fetch_json("https://api.example.invalid/x")

        self.assertGreater(pool.rotations, 0, "有备用代理时应轮换")


class RateLimitTests(unittest.TestCase):
    def test_rate_limit_counts_and_retries(self):
        transport = StubTransport([BiliRateLimitError("HTTP 429")])
        client = make_client(transport)

        with self.assertRaises(BiliRateLimitError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, RETRY_ATTEMPTS)
        self.assertEqual(client.stats["rate_limit_events"], RETRY_ATTEMPTS)
        self.assertEqual(client.stats["net_errors"], 0, "限流不计入网络错误")

    def test_retry_after_is_honored_and_logged(self):
        logs = []
        transport = StubTransport([BiliRateLimitError("HTTP 429", retry_after=20.0)])
        client = make_client(transport, log=logs.append)

        with self.assertRaises(BiliRateLimitError):
            client.fetch_json("https://api.example.invalid/x")

        # 2 次等待（第 3 次失败后不再空等），单次下限 20s；默认退避远小于此。
        self.assertGreaterEqual(
            client._sleep.total, 2 * 20.0 * (1 - 1e-9),
            "应遵守 Retry-After，而非套用默认指数退避")
        self.assertTrue(any("Retry-After" in line for line in logs if "限流" in line))

    def test_retry_after_upper_bound_respects_jitter(self):
        transport = StubTransport([BiliRateLimitError("HTTP 429", retry_after=20.0)])
        client = make_client(transport)

        with self.assertRaises(BiliRateLimitError):
            client.fetch_json("https://api.example.invalid/x")

        # 上界 = 2 次 Retry-After 等待（含最大抖动）+ 2 次闸门限速等待
        gate_pacing = 2 * DEFAULT_MIN_INTERVAL
        self.assertLessEqual(
            client._sleep.total,
            2 * 20.0 * (1 + JITTER_RATIO) + gate_pacing + 1e-6)


class CancelTests(unittest.TestCase):
    def test_cancel_before_first_attempt_skips_network(self):
        transport = StubTransport([_OK])
        client = make_client(transport, cancel=lambda: True)

        with self.assertRaises(TaskCancelledError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 0, "取消后不应发起任何请求")
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.stats["net_errors"], 0)

    def test_cancel_argument_overrides_default_predicate(self):
        transport = StubTransport([_OK])
        client = make_client(transport, cancel=lambda: True)

        self.assertIs(
            client.fetch_json("https://api.example.invalid/x", cancel=lambda: False),
            _OK)

    def test_cancel_during_backoff_stops_retrying(self):
        state = {"cancel": False}

        class CancellingTransport(StubTransport):
            def get_json(self, url):
                state["cancel"] = True
                return super().get_json(url)

        transport = CancellingTransport([TransportError("boom")])
        client = make_client(transport, cancel=lambda: state["cancel"])

        with self.assertRaises(TaskCancelledError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 1, "取消应在首次失败后退避阶段生效")
        self.assertEqual(client.stats["net_errors"], 1, "已发生的那次失败仍然记账")
        self.assertEqual(client._sleep.calls, 0, "取消后不应继续等待")


class WaitHelperTests(unittest.TestCase):
    def test_wait_slices_long_sleeps_for_prompt_cancel(self):
        client = make_client(StubTransport([_OK]))

        self.assertTrue(client._wait(1.0))

        self.assertAlmostEqual(sum(client._sleep.slice_sizes), 1.0, places=6)
        self.assertEqual(len(client._sleep.slice_sizes), 4)
        self.assertTrue(all(s <= 0.25 + 1e-9 for s in client._sleep.slice_sizes))

    def test_zero_wait_is_noop(self):
        client = make_client(StubTransport([_OK]))

        self.assertTrue(client._wait(0.0))
        self.assertEqual(client._sleep.calls, 0)

    def test_wait_returns_false_when_cancelled_midway(self):
        state = {"checks": 0}

        def cancel():
            state["checks"] += 1
            return state["checks"] > 2

        client = make_client(StubTransport([_OK]), cancel=cancel)

        self.assertFalse(client._wait(10.0))
        self.assertLess(client._sleep.total, 10.0, "取消后应立即停止剩余等待")


class WaitBudgetTests(unittest.TestCase):
    def test_total_wait_budget_stops_retrying(self):
        transport = StubTransport([TransportError("boom")])
        client = make_client(transport)

        with patch("core.client.TOTAL_WAIT_BUDGET", 0.1):
            with self.assertRaises(TransportError):
                client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 1, "预算不足时应停止重试")
        self.assertEqual(client.stats["net_errors"], 1)
        self.assertEqual(client._sleep.calls, 0)


class TransportSetupFailureTests(unittest.TestCase):
    """失败发生在"拿到通道"之前（代理健康检查、通道构建）。

    这条路径过去整段在 try 之外：既不重试也不记账，更致命的是 gate.acquire()
    已经占住半开探针而没人回报结果——探针会悬空到 probe_timeout，
    期间全进程的 acquire() 都在"等待半开探针结果"里空转。
    """

    @staticmethod
    def broken_setup(exc):
        calls = []

        def factory():
            calls.append(1)
            raise exc

        factory.calls = calls
        return factory

    def test_setup_failure_is_retried_and_counted(self):
        transport = StubTransport([_OK])
        client = make_client(transport)
        client._get_transport = self.broken_setup(
            TransportError("代理健康检查失败: http://1.2.3.4:8080"))

        with self.assertRaises(TransportError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(len(client._get_transport.calls), RETRY_ATTEMPTS,
                         "健康检查失败应走完整重试，而不是一次就放弃")
        self.assertEqual(client.stats["net_errors"], RETRY_ATTEMPTS)
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(transport.calls, 0, "通道没建起来就不该有请求")
        self.assertEqual(client.pool.entries[0].total_fail, 0,
                         "健康检查的 mark_failure 由 _get_transport 负责，不能重复记账")

    def test_setup_failure_releases_the_half_open_probe(self):
        """探针必须无条件落地：否则闸门要空转到 probe_timeout 才放行。

        实测（去掉 finally 后）：这里会连等 120s——60s 冷却睡完、探针仍悬空、
        再睡 60s 才让 probe_timeout(90s) 在逻辑时钟上追平。全进程静默停摆。
        """
        sleeps = RecordingSleep()
        gate = RequestGate(failure_threshold=1, cooldown=60.0, min_interval=0.0,
                           clock=lambda: 0.0, sleep=sleeps)
        client = BiliClient(ProxyPool(None), cookie_path=None, log=[],
                            clock=lambda: 0.0, sleep=RecordingSleep(), gate=gate)
        client._get_transport = self.broken_setup(TransportError("健康检查失败"))

        gate.record_block("risk")            # 开路：下一次 acquire 会放探针并等完冷却
        with self.assertRaises(TransportError):
            client.fetch_json("https://api.example.invalid/x", retries=1)
        self.assertEqual(gate.status()["opens"], 1)

        sleeps.total = 0.0
        self.assertTrue(gate.acquire(), "探针已作废，应允许重新探")
        self.assertEqual(sleeps.total, 0.0,
                         "悬空探针会让闸门空转到 probe_timeout——这正是要钉死的停摆")


if __name__ == "__main__":
    unittest.main()
