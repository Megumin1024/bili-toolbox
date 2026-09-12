# -*- coding: utf-8 -*-
"""core.links 接入全局闸门与取消的测试。

resolve_url 是最后一条绕过 shared_gate 的运行时出站请求，这里锁定它必须：
发请求前过闸门、等待原因写日志、成功/中性如实回报、取消抛 TaskCancelledError
且不污染闸门状态；expand 链路的页间等待可取消、页数上限不变、cancel/budget
透传到 session。全程离线：clock/sleep 注入、build_opener 打桩，不打网络、
不真等待。
"""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import ANY, patch

from core import links
from core.cancel import TaskCancelledError
from core.gate import (STATE_CLOSED, STATE_OPEN, RequestGate,
                       reset_shared_gate, shared_gate)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RecordingSleep:
    def __init__(self):
        self.calls = 0

    def __call__(self, seconds):
        self.calls += 1


class StubOpener:
    """假 opener：open 按脚本回放（异常或返回值），并记录请求参数。"""

    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        item = self.effects[min(len(self.calls) - 1, len(self.effects) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


class RecordingGate(RequestGate):
    """记录回报次数的闸门：直接核对 success/neutral 回报契约。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.success_calls = 0
        self.neutral_calls = 0

    def record_success(self):
        self.success_calls += 1
        super().record_success()

    def record_neutral(self):
        self.neutral_calls += 1
        super().record_neutral()


def make_gate(**kwargs):
    kwargs.setdefault("min_interval", 0.0)
    return RequestGate(clock=FakeClock(), sleep=RecordingSleep(), **kwargs)


def stub_open(effects):
    """打桩 build_opener，返回 (StubOpener, context manager)。"""
    stub = StubOpener(effects)
    return stub, patch("core.links.urllib.request.build_opener",
                       return_value=stub)


def fav_page(pn, count=20, total=100):
    """一页收藏夹响应：每页 count 条，media_count=total（驱动翻页判定）。"""
    medias = [{"bv": f"BV{pn:03d}{i:07d}", "title": f"v{pn}-{i}"}
              for i in range(1, count + 1)]
    return {"code": 0, "data": {"info": {"media_count": total}, "medias": medias}}


class ResolveUrlGateTests(unittest.TestCase):
    """resolve_url 的闸门接线、回报契约与取消语义。"""

    def setUp(self):
        # 全部用例结束后丢弃共享闸门状态，避免用例间互相污染
        self.addCleanup(reset_shared_gate)
        reset_shared_gate()

    def test_uses_the_shared_process_gate(self):
        gate = RecordingGate(min_interval=0.0)
        stub, open_patch = stub_open([object()])
        with patch("core.links.shared_gate", return_value=gate), open_patch:
            self.assertEqual(links.resolve_url("https://b23.tv/abc"),
                             "https://b23.tv/abc")
        self.assertEqual(gate.status()["acquires"], 1)
        self.assertEqual(gate.success_calls, 1)
        self.assertEqual(gate.neutral_calls, 0)
        self.assertEqual(gate.state, STATE_CLOSED)

    def test_sends_fixed_ua_and_10s_timeout(self):
        """透明固定配置：UA 字符串不变、超时 10s、单次请求。"""
        gate = make_gate()
        stub, open_patch = stub_open([object()])
        with patch("core.links.shared_gate", return_value=gate), open_patch:
            links.resolve_url("https://b23.tv/abc")
        self.assertEqual(len(stub.calls), 1)
        request, timeout = stub.calls[0]
        self.assertEqual(timeout, 10)
        ua = {k.lower(): v for k, v in request.headers.items()}["user-agent"]
        self.assertEqual(ua, links.UA_WEB)

    def test_open_breaker_waits_logs_and_then_proceeds(self):
        """熔断开启：先等待并写日志（on_wait 范式），冷却结束后放行。"""
        sleep = RecordingSleep()
        gate = RecordingGate(clock=FakeClock(), sleep=sleep, min_interval=0.0)
        for _ in range(gate.failure_threshold):
            gate.record_block("risk:test")          # 熔断开启，冷却 60s
        stub, open_patch = stub_open([object()])
        logs = []
        with patch("core.links.shared_gate", return_value=gate), open_patch, \
                patch.object(links.session, "log",
                             side_effect=lambda msg: logs.append(msg)):
            self.assertEqual(links.resolve_url("https://b23.tv/abc"),
                             "https://b23.tv/abc")
        self.assertEqual(gate.waits, 1)             # 真的进了等待
        self.assertGreater(sleep.calls, 0)
        self.assertTrue(any("[gate]" in m and "熔断" in m for m in logs),
                        f"等待原因必须写日志: {logs}")
        self.assertEqual(gate.state, STATE_CLOSED)  # 成功回报闭合熔断
        self.assertEqual(gate.neutral_calls, 0)

    def test_cancel_during_gate_wait_raises_and_leaves_gate_clean(self):
        """真实 shared_gate 接线下：等待中取消抛 TaskCancelledError，
        不发请求、无 block 误报、探针名额归还。"""
        gate = shared_gate()
        for _ in range(gate.failure_threshold):
            gate.record_block("risk:test")
        before = gate.status()
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 1                   # 发起前未取消，进入等待后取消

        stub, open_patch = stub_open([object()])
        with patch.object(links.session, "log"), open_patch:
            with self.assertRaises(TaskCancelledError):
                links.resolve_url("https://b23.tv/abc", cancel=cancel)
        self.assertEqual(stub.calls, [])
        after = gate.status()
        self.assertEqual(after["state"], STATE_OPEN)
        self.assertEqual(after["blocks_seen"], before["blocks_seen"])
        self.assertEqual(after["opens"], before["opens"])
        self.assertEqual(after["acquires"], 0)
        self.assertEqual(after["waits"], 1)
        self.assertFalse(gate._probe_in_flight)     # 探针名额已归还

    def test_cancelled_before_request_skips_gate_and_network(self):
        gate = RecordingGate(min_interval=0.0)
        stub, open_patch = stub_open([object()])
        with patch("core.links.shared_gate", return_value=gate), open_patch:
            with self.assertRaises(TaskCancelledError):
                links.resolve_url("https://b23.tv/abc", cancel=lambda: True)
        self.assertEqual(gate.status()["acquires"], 0)
        self.assertEqual(stub.calls, [])


class ResolveUrlOutcomeTests(unittest.TestCase):
    """resolve_url 的返回语义：30x → Location，失败回报中性后原样上抛。"""

    def setUp(self):
        self.addCleanup(reset_shared_gate)
        reset_shared_gate()

    def test_302_returns_location_header(self):
        gate = RecordingGate(min_interval=0.0)
        target = "https://www.bilibili.com/video/BV1GJ411x7h7"
        err = urllib.error.HTTPError("https://b23.tv/abc", 302, "Found",
                                     {"Location": target}, None)
        stub, open_patch = stub_open([err])
        with patch("core.links.shared_gate", return_value=gate), open_patch:
            self.assertEqual(links.resolve_url("https://b23.tv/abc"), target)
        self.assertEqual(gate.success_calls, 1)     # 拿到 30x 响应也算请求完成
        self.assertEqual(gate.state, STATE_CLOSED)

    def test_30x_without_location_returns_original_url(self):
        gate = RecordingGate(min_interval=0.0)
        err = urllib.error.HTTPError("https://b23.tv/abc", 302, "Found", {}, None)
        stub, open_patch = stub_open([err])
        with patch("core.links.shared_gate", return_value=gate), open_patch:
            self.assertEqual(links.resolve_url("https://b23.tv/abc"),
                             "https://b23.tv/abc")
        self.assertEqual(gate.success_calls, 1)

    def test_transport_error_reports_neutral_and_reraises(self):
        """非 HTTPError 异常：record_neutral 后原样上抛，不包装、不重试。"""
        gate = RecordingGate(min_interval=0.0)
        err = urllib.error.URLError("connection refused")
        stub, open_patch = stub_open([err])
        with patch("core.links.shared_gate", return_value=gate), open_patch:
            with self.assertRaises(urllib.error.URLError) as ctx:
                links.resolve_url("https://b23.tv/abc")
        self.assertIs(ctx.exception, err)
        self.assertEqual(gate.neutral_calls, 1)
        self.assertEqual(gate.success_calls, 0)


class ParseLinkForwardTests(unittest.TestCase):
    def test_parse_link_forwards_cancel_to_resolve(self):
        with patch.object(links, "resolve_url",
                          return_value="https://t.bilibili.com/1234567890") as m:
            kind, oid, _ = links.parse_link("https://b23.tv/abc",
                                            cancel=lambda: False)
        self.assertEqual(kind, "dynamic")
        self.assertEqual(oid, 1234567890)
        m.assert_called_once_with("https://b23.tv/abc", cancel=ANY)

    def test_meta_fetchers_forward_cancel_to_session(self):
        """bvid_to_aid / get_dynamic_meta 把 cancel 透传给统一会话。"""
        seen = {}

        def fake_json(url, **kwargs):
            seen["cancel"] = kwargs.get("cancel")
            if "view" in url:
                return {"code": 0, "data": {"aid": 1, "bvid": "BV1X",
                                            "title": "t", "owner": {"name": "u"},
                                            "pubdate": None,
                                            "stat": {"reply": 0}}}
            return {"code": 0, "data": {"item": {"modules": {}}}}

        cancel = lambda: False  # noqa: E731
        with patch.object(links.session, "http_get_json", side_effect=fake_json):
            links.bvid_to_aid("BV1X", cancel=cancel)
            self.assertIs(seen["cancel"], cancel)
            links.get_dynamic_meta(42, cancel=cancel)
            self.assertIs(seen["cancel"], cancel)


class ExpandCancelTests(unittest.TestCase):
    """expand 链路：页间等待可取消、页数上限不变、cancel/budget 透传。"""

    FAV_URL = "https://api.bilibili.com/x/v3/fav/resource/list?media_id=42"
    SEASON_URL = ("https://space.bilibili.com/1/upload/video?sid=7&mid=1")

    def setUp(self):
        self.addCleanup(reset_shared_gate)
        reset_shared_gate()

    def test_expand_page_wait_is_cancellable(self):
        """第一页之后取消：返回已展开的部分，不再发下一页（不是失败）。"""
        calls = {"n": 0}

        def fake_json(url, **kwargs):
            calls["n"] += 1
            return fav_page(calls["n"])             # 每页 20 条，media_count=100 → 会翻页

        def cancel():
            return calls["n"] >= 1                  # 第一页请求完成后即取消

        with patch.object(links.session, "http_get_json", side_effect=fake_json), \
                patch("core.cancel.time.sleep"):    # 防御：取消失效也不真睡
            out = links.expand_source(self.FAV_URL, cancel=cancel)
        self.assertEqual(len(out), 20)
        self.assertEqual(calls["n"], 1)

    def test_season_page_wait_is_cancellable(self):
        calls = {"n": 0}

        def fake_json(url, **kwargs):
            calls["n"] += 1
            return {"code": 0, "data": {"items": {"archives": [
                {"bvid": f"BV{i:010d}", "title": "t"} for i in range(30)]}}}

        def cancel():
            return calls["n"] >= 1

        with patch.object(links.session, "http_get_json", side_effect=fake_json), \
                patch("core.cancel.time.sleep"):
            out = links.expand_source(self.SEASON_URL, cancel=cancel)
        self.assertEqual(len(out), 30)
        self.assertEqual(calls["n"], 1)

    def test_expand_without_cancel_completes_all_pages(self):
        calls = {"n": 0}

        def fake_json(url, **kwargs):
            calls["n"] += 1
            return fav_page(calls["n"])             # 5 页 × 20 = 100

        with patch.object(links.session, "http_get_json", side_effect=fake_json), \
                patch("core.cancel.time.sleep"):
            out = links.expand_source(self.FAV_URL)
        self.assertEqual(len(out), 100)
        self.assertEqual(calls["n"], 5)

    def test_fav_page_limit_unchanged(self):
        """页数上限保持不变：收藏夹 ≤100 页（上限语义回归）。"""
        calls = {"n": 0}

        def fake_json(url, **kwargs):
            calls["n"] += 1
            return fav_page(calls["n"], total=10 ** 9)   # 永不满足提前终止

        with patch.object(links.session, "http_get_json", side_effect=fake_json), \
                patch("core.cancel.time.sleep"):
            out = links.expand_source(self.FAV_URL)
        self.assertEqual(calls["n"], 100)
        self.assertEqual(len(out), 2000)

    def test_season_page_limit_unchanged(self):
        """页数上限保持不变：合集 ≤200 页。"""
        calls = {"n": 0}

        def fake_json(url, **kwargs):
            calls["n"] += 1
            return {"code": 0, "data": {"items": {"archives": [
                {"bvid": f"BV{i:010d}", "title": "t"} for i in range(30)]}}}

        with patch.object(links.session, "http_get_json", side_effect=fake_json), \
                patch("core.cancel.time.sleep"):
            out = links.expand_source(self.SEASON_URL)
        self.assertEqual(calls["n"], 200)
        self.assertEqual(len(out), 6000)

    def test_expand_passes_cancel_and_budget_to_session(self):
        """目标 4：expand 经 session 的请求透传调用方 cancel 与任务 budget。"""
        seen = {}

        def fake_json(url, cancel=None, budget=None):
            seen["url"], seen["cancel"], seen["budget"] = url, cancel, budget
            return {"code": 0, "data": {"info": {"media_count": 1},
                                        "medias": [{"bv": "BV1", "title": "t"}]}}

        budget = object()
        marker = lambda: False  # noqa: E731
        with patch.object(links.session, "http_get_json", side_effect=fake_json):
            out = links.expand_source(self.FAV_URL, cancel=marker, budget=budget)
        self.assertEqual(out, [("BV1", "t")])
        self.assertIs(seen["cancel"], marker)
        self.assertIs(seen["budget"], budget)

    def test_txt_recursion_forwards_cancel_and_budget(self):
        seen = {}

        def fake_json(url, cancel=None, budget=None):
            seen["cancel"], seen["budget"] = cancel, budget
            return {"code": 0, "data": {"info": {"media_count": 1},
                                        "medias": [{"bv": "BV1", "title": "t"}]}}

        budget = object()
        marker = lambda: False  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "list.txt"
            txt.write_text(self.FAV_URL + "\n", encoding="utf-8")
            with patch.object(links.session, "http_get_json",
                              side_effect=fake_json):
                out = links.expand_source(str(txt), cancel=marker, budget=budget)
        self.assertEqual(out, [("BV1", "t")])
        self.assertIs(seen["cancel"], marker)
        self.assertIs(seen["budget"], budget)

    def test_plain_video_line_needs_no_network(self):
        """向后兼容：纯 BV 行不触网，返回结构不变。"""
        self.assertEqual(links.expand_source("BV1GJ411x7h7"),
                         [("BV1GJ411x7h7", "")])

    def test_unrecognized_line_still_raises_valueerror(self):
        """异常类型不变：无法识别的输入仍是 ValueError。"""
        with self.assertRaises(ValueError):
            links.expand_source("这不是有效来源")


if __name__ == "__main__":
    unittest.main()
