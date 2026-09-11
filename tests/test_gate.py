# -*- coding: utf-8 -*-
"""全局请求闸门（限速 + 熔断）的离线测试。

全程离线：clock/sleep 注入，不真等待、不打网络。
"""
from __future__ import annotations

import unittest

from core.client import BiliClient
from core.gate import (STATE_CLOSED, STATE_HALF_OPEN, STATE_OPEN, RequestGate,
                       reset_shared_gate, shared_gate)
from core.proxy import ProxyPool
from core.transport import RiskBlocked, TransportError


class FakeClock:
    """可注入时钟。now 只在本测试显式推进或 sleep 推进时变化。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RecordingSleep:
    def __init__(self, clock=None, advance=False):
        self.clock = clock
        self.advance = advance
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds
        if self.advance and self.clock is not None:
            self.clock.now += seconds


def make_gate(**kwargs):
    """恒定时钟 + 记录型 sleep：逻辑时钟只靠等待推进，绝不空转。"""
    clock = FakeClock()
    sleeps = RecordingSleep(clock)
    gate = RequestGate(clock=clock, sleep=sleeps, **kwargs)
    return gate, clock, sleeps


class RateLimitTests(unittest.TestCase):
    def test_first_request_never_waits(self):
        gate, _clock, sleeps = make_gate()
        self.assertTrue(gate.acquire())
        self.assertEqual(sleeps.calls, 0)

    def test_second_request_waits_min_interval(self):
        gate, _clock, sleeps = make_gate(min_interval=0.5)
        gate.acquire()
        self.assertTrue(gate.acquire())
        self.assertAlmostEqual(sleeps.total, 0.5, places=6)

    def test_no_wait_when_real_time_already_elapsed(self):
        gate, clock, sleeps = make_gate(min_interval=0.5)
        gate.acquire()
        clock.now += 10.0            # 真实时间流逝（网络耗时、业务间隔）
        self.assertTrue(gate.acquire())
        self.assertEqual(sleeps.calls, 0, "已经等够了就不该再等")

    def test_cancel_during_gate_wait_returns_false(self):
        gate, _clock, _sleeps = make_gate(min_interval=5.0)
        gate.acquire()
        self.assertFalse(gate.acquire(cancel=lambda: True))

    def test_default_floor_is_invisible_to_a_normally_paced_caller(self):
        """闸门是兜底地板，不是目标速率：调用方自己按 0.25s 节奏走时开销为 0。

        0.25s 是评论抓取实测跑完 16w 条评论全程无风控的间隔——本地板必须低于它，
        否则就会把用户显式设置的业务限速悄悄覆盖掉。
        """
        gate, clock, sleeps = make_gate()          # 用默认地板
        gate.acquire()
        clock.now += 0.25
        self.assertTrue(gate.acquire())
        self.assertEqual(sleeps.calls, 0, "已按业务节奏走够，闸门不该再加任何等待")

    def test_on_wait_reports_reason(self):
        gate, _clock, _sleeps = make_gate(min_interval=0.5)
        seen = []
        gate.acquire()
        gate.acquire(on_wait=lambda secs, reason: seen.append((secs, reason)))
        self.assertEqual(len(seen), 1)
        self.assertIn("限速", seen[0][1])


class BreakerTests(unittest.TestCase):
    def test_blocks_below_threshold_do_not_open(self):
        gate, _c, _s = make_gate(failure_threshold=3)
        gate.record_block("risk")
        gate.record_block("risk")
        self.assertEqual(gate.state, STATE_CLOSED)

    def test_threshold_blocks_open_the_breaker(self):
        gate, _c, _s = make_gate(failure_threshold=3)
        for _ in range(3):
            gate.record_block("risk")
        self.assertEqual(gate.state, STATE_OPEN)

    def test_open_breaker_pauses_the_next_request_for_a_cooldown(self):
        gate, _c, sleeps = make_gate(failure_threshold=1, cooldown=60.0,
                                     min_interval=0.0)
        gate.record_block("risk")
        self.assertTrue(gate.acquire())
        self.assertAlmostEqual(sleeps.total, 60.0, places=6)
        self.assertEqual(gate.state, STATE_HALF_OPEN)

    def test_probe_success_closes_the_breaker(self):
        gate, _c, _s = make_gate(failure_threshold=1, cooldown=60.0,
                                 min_interval=0.0)
        gate.record_block("risk")
        gate.acquire()
        gate.record_success()
        self.assertEqual(gate.state, STATE_CLOSED)
        self.assertEqual(gate.status()["cooldown_seconds"], 60.0)

    def test_probe_failure_doubles_the_cooldown(self):
        gate, _c, sleeps = make_gate(failure_threshold=1, cooldown=60.0,
                                     max_cooldown=600.0, min_interval=0.0)
        gate.record_block("risk")
        gate.acquire()                 # 第一次冷却 60s，放探针
        gate.record_block("risk")      # 探针仍被拦
        self.assertEqual(gate.state, STATE_OPEN)
        self.assertEqual(gate.status()["cooldown_seconds"], 120.0)

        sleeps.total = 0.0
        gate.acquire()
        self.assertAlmostEqual(sleeps.total, 120.0, places=6)

    def test_cooldown_is_capped(self):
        gate, _c, _s = make_gate(failure_threshold=1, cooldown=60.0,
                                 max_cooldown=100.0, min_interval=0.0)
        for _ in range(5):
            gate.record_block("risk")
            gate.acquire()
        self.assertLessEqual(gate.status()["cooldown_seconds"], 100.0)

    def test_success_resets_consecutive_blocks(self):
        gate, _c, _s = make_gate(failure_threshold=3)
        gate.record_block("risk")
        gate.record_block("risk")
        gate.record_success()
        gate.record_block("risk")
        self.assertEqual(gate.state, STATE_CLOSED, "成功应清零连续拦截计数")

    def test_transport_errors_do_not_open_the_breaker(self):
        """传输层故障是代理池的职责，不该被误判成平台在拦我们。"""
        gate, _c, _s = make_gate(failure_threshold=3)
        for _ in range(10):
            gate.record_neutral()
        self.assertEqual(gate.state, STATE_CLOSED)
        self.assertEqual(gate.status()["blocks_seen"], 0)

    def test_hung_probe_is_abandoned_after_timeout(self):
        gate, _c, sleeps = make_gate(failure_threshold=1, cooldown=60.0,
                                     probe_timeout=0.0, min_interval=0.0)
        gate.record_block("risk")
        gate.acquire()                 # 占住探针，但永不回报
        sleeps.total = 0.0
        self.assertTrue(gate.acquire(), "悬空探针不得把闸门永久卡死")
        self.assertEqual(sleeps.total, 0.0, "探针超时应作废，而不是再等一轮冷却")


class ObservabilityTests(unittest.TestCase):
    def test_status_reports_state_and_counters(self):
        gate, _c, _s = make_gate(failure_threshold=2, cooldown=30.0,
                                 min_interval=0.0)
        gate.acquire()
        gate.record_block("risk")
        gate.record_block("risk")
        status = gate.status()
        self.assertEqual(status["state"], STATE_OPEN)
        self.assertEqual(status["opens"], 1)
        self.assertEqual(status["blocks_seen"], 2)
        self.assertEqual(status["acquires"], 1)
        self.assertAlmostEqual(status["remaining_seconds"], 30.0, places=1)

    def test_shared_gate_is_a_process_singleton(self):
        reset_shared_gate()
        self.addCleanup(reset_shared_gate)
        self.assertIs(shared_gate(), shared_gate())


class _StubTransport:
    name = "stub"

    def __init__(self, effects):
        self._effects = list(effects)
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        item = self._effects[min(self.calls - 1, len(self._effects) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def warmup(self, force=False):
        pass

    def close(self):
        pass


class ClientIntegrationTests(unittest.TestCase):
    def _client(self, transport, gate, clock, sleeps, logs):
        client = BiliClient(ProxyPool(None), log=logs.append,
                            clock=clock, sleep=sleeps, gate=gate)
        client._get_transport = lambda: transport
        return client

    def test_repeated_risk_opens_gate_and_pauses_the_next_call(self):
        """一次风控之后，下一次请求必须先被闸门拦住——这正是改前缺失的行为。"""
        clock = FakeClock()
        sleeps = RecordingSleep(clock, advance=True)
        gate = RequestGate(clock=clock, sleep=sleeps)
        logs = []
        client = self._client(_StubTransport([RiskBlocked("风控 412")]),
                              gate, clock, sleeps, logs)

        with self.assertRaises(RiskBlocked):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(gate.state, STATE_OPEN, "连续风控应开路熔断")

        sleeps.total = 0.0
        client._get_transport = lambda: _StubTransport([{"code": 0}])
        self.assertEqual(client.fetch_json("https://api.example.invalid/x"),
                         {"code": 0})

        self.assertGreaterEqual(sleeps.total, gate.base_cooldown,
                                "开路后下一次请求应先等完冷却")
        self.assertEqual(gate.state, STATE_CLOSED, "恢复成功后应闭合")
        self.assertTrue(any("熔断" in line for line in logs))

    def test_transport_errors_alone_never_pause_the_gate(self):
        clock = FakeClock()
        sleeps = RecordingSleep(clock, advance=True)
        gate = RequestGate(clock=clock, sleep=sleeps, min_interval=0.0)
        logs = []
        client = self._client(_StubTransport([TransportError("boom")]),
                              gate, clock, sleeps, logs)

        with self.assertRaises(TransportError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(gate.state, STATE_CLOSED)
        self.assertEqual(gate.status()["waits"], 0, "传输错误不应让闸门暂停")
        self.assertEqual(gate.status()["opens"], 0)
        # sleeps 由 client 与 gate 共用：这里有等待，但全是单次调用内的退避，
        # 量级远小于熔断冷却（60s）。
        self.assertLess(sleeps.total, gate.base_cooldown,
                        "不该出现熔断级别的暂停")

    def test_business_error_does_not_feed_the_breaker(self):
        from core.transport import BiliApiError

        clock = FakeClock()
        sleeps = RecordingSleep(clock, advance=True)
        gate = RequestGate(clock=clock, sleep=sleeps, min_interval=0.0)
        client = self._client(_StubTransport([BiliApiError("code=-404")]),
                              gate, clock, sleeps, [])

        for _ in range(5):
            with self.assertRaises(BiliApiError):
                client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(gate.state, STATE_CLOSED)
        self.assertEqual(gate.status()["blocks_seen"], 0)


if __name__ == "__main__":
    unittest.main()
