# -*- coding: utf-8 -*-
"""评论 gRPC 流接入全局闸门的测试。

评论抓取自建 gRPC 通道、绕开 BiliClient，是全项目请求速率最高的一条流；
这里锁定它必须：发 RPC 前过闸门、成功后闭合熔断、只有明确限流才开熔断、
退避可被取消打断。全程离线：clock/sleep/sleeper 注入，不打网络、不真等待。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

import grpc

from core.gate import STATE_CLOSED, STATE_OPEN, RequestGate, reset_shared_gate, shared_gate
from tools.comments.core import Crawler, TaskCancelled


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds


class RpcError(grpc.RpcError):
    """可控 code 的假 RpcError（grpc 真异常的构造依赖内部 C 层）。"""

    def __init__(self, code):
        super().__init__()
        self._code = code

    def code(self):
        return self._code

    def __str__(self):
        return f"<RpcError {self._code}>"


def make_gate(**kwargs):
    clock, sleeps = FakeClock(), RecordingSleep()
    kwargs.setdefault("min_interval", 0.0)
    return RequestGate(clock=clock, sleep=sleeps, **kwargs), sleeps


class _CrawlerCase(unittest.TestCase):
    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix="comments_gate_")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.addCleanup(reset_shared_gate)
        self.gate, self.gate_sleeps = make_gate()
        self.sleeper = RecordingSleep()
        self.logs = []

    def make(self, **kwargs):
        kwargs.setdefault("gate", self.gate)
        kwargs.setdefault("sleeper", self.sleeper)
        kwargs.setdefault("progress", lambda **k: self.logs.append(k))
        crawler = Crawler(1, 1, self.out_dir, **kwargs)
        self.addCleanup(crawler.channel.close)
        return crawler

    def fn(self, *effects):
        """按脚本回放的假 stub 方法；脚本用尽后重复最后一项。"""
        calls = []

        def call(req, metadata=None, timeout=None):
            calls.append(req)
            item = effects[min(len(calls) - 1, len(effects) - 1)]
            if isinstance(item, BaseException):
                raise item
            return item

        call.calls = calls
        return call


class GateWiringTests(_CrawlerCase):
    def test_defaults_to_the_shared_process_gate(self):
        crawler = self.make(gate=None)
        self.assertIs(crawler.gate, shared_gate())

    def test_shares_one_breaker_with_the_http_network_layer(self):
        """评论 gRPC 与 BiliClient 必须共用同一个熔断器，背压才跨工具生效。"""
        from core.client import BiliClient
        from core.proxy import ProxyPool

        client = BiliClient(ProxyPool(None), cookie_path=None)  # 生产构造：不注入 clock/sleep
        self.addCleanup(client.close)

        self.assertIs(self.make(gate=None).gate, client.gate)
        self.assertIs(client.gate, shared_gate())

    def test_success_passes_the_gate_and_closes_the_breaker(self):
        crawler = self.make()
        fn = self.fn("ok")

        self.assertEqual(crawler._call(fn, object()), "ok")
        self.assertEqual(len(fn.calls), 1)
        self.assertEqual(self.gate.status()["acquires"], 1)
        self.assertEqual(self.gate.state, STATE_CLOSED)

    def test_cancel_before_first_attempt_skips_the_network(self):
        crawler = self.make(cancel=lambda: True)
        fn = self.fn("ok")

        with self.assertRaises(TaskCancelled):
            crawler._call(fn, object())
        self.assertEqual(len(fn.calls), 0, "取消后不应发起任何 RPC")
        self.assertEqual(self.gate.status()["acquires"], 0)

    def test_resource_exhausted_feeds_the_breaker(self):
        gate, _ = make_gate(failure_threshold=3)
        crawler = self.make(gate=gate)
        fn = self.fn(RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED))

        with self.assertRaises(RuntimeError):
            crawler._call(fn, object())

        self.assertEqual(gate.state, STATE_OPEN, "服务端限流应触发全局熔断")
        self.assertEqual(gate.status()["blocks_seen"], 4, "4 次尝试各记一次拦截")

    def test_transport_rpc_errors_do_not_feed_the_breaker(self):
        """UNAVAILABLE 是网络故障，不该被误判成"平台在拦我们"。"""
        crawler = self.make()
        fn = self.fn(RpcError(grpc.StatusCode.UNAVAILABLE))

        with self.assertRaises(RuntimeError):
            crawler._call(fn, object())

        self.assertEqual(self.gate.state, STATE_CLOSED)
        self.assertEqual(self.gate.status()["blocks_seen"], 0)

    def test_open_gate_pauses_the_next_rpc_and_is_reported(self):
        gate, gate_sleeps = make_gate(failure_threshold=1, cooldown=60.0)
        crawler = self.make(gate=gate)
        gate.record_block("risk")

        self.assertEqual(crawler._call(self.fn("ok"), object()), "ok")

        self.assertAlmostEqual(gate_sleeps.total, 60.0, places=6,
                               msg="开路后下一次 RPC 应先等完冷却")
        self.assertTrue(any("熔断" in k.get("text", "") for k in self.logs),
                        "闸门暂停必须写进进度，否则用户只看到卡住")


class InterruptibleBackoffTests(_CrawlerCase):
    def test_rpc_error_backoff_sleeps_through_the_injected_sleeper(self):
        crawler = self.make()
        fn = self.fn(RpcError(grpc.StatusCode.UNAVAILABLE), "ok")

        self.assertEqual(crawler._call(fn, object()), "ok")
        # 首次退避 min(2**0*2, 30) = 2s，按 0.25s 切片
        self.assertAlmostEqual(self.sleeper.total, 2.0, places=6)

    def test_cancel_during_backoff_stops_immediately(self):
        state = {"cancel": False}

        def sleeper(seconds):
            self.sleeper(seconds)
            state["cancel"] = True   # 第一片睡完就收到取消

        crawler = self.make(cancel=lambda: state["cancel"], sleeper=sleeper)
        fn = self.fn(RpcError(grpc.StatusCode.UNAVAILABLE))

        with self.assertRaises(TaskCancelled):
            crawler._call(fn, object())

        self.assertEqual(len(fn.calls), 1, "取消应在退避阶段生效，不再重试")
        self.assertLess(self.sleeper.total, 2.0, "取消后应立即停止剩余等待")

    def test_pacing_wait_is_cancellable(self):
        crawler = self.make(cancel=lambda: True)
        self.assertFalse(crawler._pace(5.0))
        self.assertEqual(self.sleeper.calls, 0)


if __name__ == "__main__":
    unittest.main()
