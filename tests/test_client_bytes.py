# -*- coding: utf-8 -*-
"""二进制通道（fetch_bytes）的契约测试。

背景：弹幕是 protobuf 二进制，不是 JSON。二进制接口必须和 JSON 接口共用同一套
闸门 / 重试 / 统计 / 探针回报——否则风控信号会被当成"取到了 0 条数据"静默吞掉，
而且二进制接口会变成绕过熔断的后门。

本文件锁三件事：
1. 风控/限流**以 JSON 体下发**时，字节通道也要认出来并抛错，不能原样返回；
2. 真正的二进制内容原样放行，不被 JSON 解析干扰；
3. fetch_bytes 与 fetch_json 走的是同一个循环（重试次数、统计、探针释放一致）。

全程离线：不建连接、不读文件、不真 sleep。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.cancel import TaskCancelledError
from core.client import RETRY_ATTEMPTS, BiliClient
from core.proxy import ProxyPool
from core.session import http_get_bytes
from core.transport import (BaseTransport, BiliApiError, BiliConnectionError,
                           BiliRateLimitError, RiskBlocked, RiskVoucher,
                           TransportError, UrllibTransport)

# 真实弹幕分段的首字节：protobuf 的 tag（字段1, wire type 2），绝不可能是 '{'
PROTOBUF_BLOB = b"\x0a\x12\x08\x01\x10\xd0\x0f\x18\x01\x22\x03abc\x28\x01\x00\xff\xfe"
RISK_BODY = b'{"code":-352,"message":"\xe9\xa3\x8e\xe6\x8e\xa7","data":null}'
RATE_BODY = b'{"code":-799,"message":"\xe8\xaf\xb7\xe7\xa8\x8d\xe5\x90\x8e\xe9\x87\x8d\xe8\xaf\x95"}'


class BytesClassificationTests(unittest.TestCase):
    """传输层的字节分类：入口在 _classify_bytes。"""

    def test_real_binary_passes_through_untouched(self):
        self.assertEqual(BaseTransport._classify_bytes(PROTOBUF_BLOB), PROTOBUF_BLOB)

    def test_empty_body_is_not_an_error(self):
        # 空分段是"这个 6 分钟区间没有弹幕"，属正常结束，不是风控。
        self.assertEqual(BaseTransport._classify_bytes(b""), b"")

    def test_json_risk_body_is_classified_not_returned(self):
        with self.assertRaises(RiskBlocked):
            BaseTransport._classify_bytes(RISK_BODY, "application/octet-stream")

    def test_voucher_body_raises_risk_voucher(self):
        body = (b'{"code":-352,"message":"risk",'
                b'"data":{"v_voucher":"vv-123"}}')
        with self.assertRaises(RiskVoucher) as ctx:
            BaseTransport._classify_bytes(body)
        self.assertEqual(ctx.exception.v_voucher, "vv-123")

    def test_rate_limit_body_raises_rate_limit(self):
        with self.assertRaises(BiliRateLimitError):
            BaseTransport._classify_bytes(RATE_BODY)

    def test_business_error_body_raises_api_error(self):
        with self.assertRaises(BiliApiError):
            BaseTransport._classify_bytes(b'{"code":-404,"message":"nope"}')

    def test_code_zero_json_still_returns_raw_bytes(self):
        # 正常的 JSON 响应体也从字节通道返回 bytes，不做隐式解析——
        # 返回类型必须由**调用的是哪个方法**决定，不能随响应内容变。
        raw = b'{"code":0,"data":{"ok":true}}'
        self.assertEqual(BaseTransport._classify_bytes(raw), raw)

    def test_unparseable_body_claiming_json_is_passed_through(self):
        # 截断的响应体：自称 JSON 却解不开。放行让调用方去发现数据不对，
        # 不要在这里把真实数据当成错误吞掉。
        truncated = b'{"code":0,"data":{'
        self.assertEqual(
            BaseTransport._classify_bytes(truncated, "application/json"), truncated)

    def test_content_type_json_triggers_sniffing(self):
        # 首字节不是 '{' 但自称 JSON：尝试解析后会因解码失败而放行，不抛。
        raw = b"\x1f\x8b\x08\x00compressed"
        self.assertEqual(BaseTransport._classify_bytes(raw, "application/json"), raw)


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, response):
        self._response = response
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def urllib_with(response):
    transport = UrllibTransport()
    transport.opener = FakeOpener(response)
    return transport


class UrllibBytesWiringTests(unittest.TestCase):
    """证明 get_bytes 真的接上了分类与状态码分支，而不是只返回 resp.read()。"""

    def test_binary_body_returned_raw(self):
        t = urllib_with(FakeResponse(PROTOBUF_BLOB,
                                     headers={"Content-Type": "application/octet-stream"}))
        self.assertEqual(t.get_bytes("https://api.example.invalid/seg.so"), PROTOBUF_BLOB)

    def test_risk_json_body_is_not_silently_returned_as_data(self):
        t = urllib_with(FakeResponse(RISK_BODY, headers={"Content-Type": "application/json"}))
        with self.assertRaises(RiskBlocked):
            t.get_bytes("https://api.example.invalid/seg.so")

    def test_risk_http_status_raises(self):
        # urllib 对 4xx/5xx 抛 HTTPError，而不是返回一个带状态码的响应对象——
        # 复现时也必须走同一条路，否则测的是架空的分支。
        import urllib.error
        exc = urllib.error.HTTPError(
            "https://api.example.invalid/seg.so", 412, "Precondition Failed", {}, None)
        t = urllib_with(exc)
        with self.assertRaises(RiskBlocked):
            t.get_bytes("https://api.example.invalid/seg.so")

    def test_rate_limit_status_carries_retry_after(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            "https://api.example.invalid/seg.so", 429, "Too Many Requests",
            {"Retry-After": "7"}, None)
        t = urllib_with(exc)
        with self.assertRaises(BiliRateLimitError) as ctx:
            t.get_bytes("https://api.example.invalid/seg.so")
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_other_http_error_becomes_connection_error(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            "https://api.example.invalid/seg.so", 500, "Server Error", {}, None)
        t = urllib_with(exc)
        with self.assertRaises(BiliConnectionError):
            t.get_bytes("https://api.example.invalid/seg.so")


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds


class BytesStubTransport:
    """回放 get_bytes 的响应/异常；get_json 故意抛错，用来证明走的不是 JSON 通道。"""

    name = "stub-bytes"

    def __init__(self, effects):
        self._effects = list(effects)
        self.calls = 0
        self.warmups = 0

    def get_bytes(self, url):
        self.calls += 1
        item = self._effects[min(self.calls - 1, len(self._effects) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def get_json(self, url):
        raise AssertionError("二进制通道不该调用 get_json")

    def warmup(self, force=False):
        self.warmups += 1

    def close(self):
        pass

    def get_cookies(self):
        return {}


def make_client(transport, pool=None, log=None, cancel=None):
    client = BiliClient(pool or ProxyPool(None), cookie_path=None,
                        log=log, clock=lambda: 0.0, sleep=RecordingSleep(),
                        cancel=cancel)
    client._get_transport = lambda: transport
    return client


class FetchBytesContractTests(unittest.TestCase):
    def test_success_returns_bytes_and_records_stats(self):
        transport = BytesStubTransport([PROTOBUF_BLOB])
        client = make_client(transport)

        self.assertEqual(client.fetch_bytes("https://api.example.invalid/x"), PROTOBUF_BLOB)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.stats["requests"], 1)
        self.assertEqual(client.stats["last_transport"], "stub-bytes")
        self.assertEqual(client.pool.entries[0].total_ok, 1)
        self.assertEqual(client._sleep.calls, 0, "成功不应有任何等待")

    def test_recovers_after_one_transport_failure(self):
        transport = BytesStubTransport([TransportError("boom"), PROTOBUF_BLOB])
        client = make_client(transport)

        self.assertEqual(client.fetch_bytes("https://api.example.invalid/x"), PROTOBUF_BLOB)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.stats["net_errors"], 1)
        self.assertEqual(client.stats["requests"], 1)
        self.assertGreater(client._sleep.total, 0.0)

    def test_exhausts_retries_like_the_json_channel(self):
        transport = BytesStubTransport([TransportError("boom")])
        client = make_client(transport)

        with self.assertRaises(TransportError):
            client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(transport.calls, RETRY_ATTEMPTS,
                         "二进制通道的重试次数必须与 JSON 通道一致")
        self.assertEqual(client.stats["net_errors"], RETRY_ATTEMPTS)
        self.assertEqual(client.stats["requests"], 0)

    def test_business_error_is_not_retried(self):
        transport = BytesStubTransport([BiliApiError("API code=-404")])
        client = make_client(transport)

        with self.assertRaises(BiliApiError):
            client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.stats["net_errors"], 0)
        self.assertEqual(client._sleep.calls, 0)

    def test_risk_counts_and_forces_warmup(self):
        transport = BytesStubTransport([RiskBlocked("风控 412")])
        client = make_client(transport)

        with self.assertRaises(RiskBlocked):
            client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(client.stats["risk_events"], RETRY_ATTEMPTS)
        self.assertEqual(transport.warmups, RETRY_ATTEMPTS)

    def test_rate_limit_counts_separately_from_net_errors(self):
        transport = BytesStubTransport([BiliRateLimitError("HTTP 429", retry_after=5.0)])
        client = make_client(transport)

        with self.assertRaises(BiliRateLimitError):
            client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(client.stats["rate_limit_events"], RETRY_ATTEMPTS)
        self.assertEqual(client.stats["net_errors"], 0, "限流不计入网络错误")

    def test_gate_probe_is_released_on_failure(self):
        """半开熔断下的探针名额必须无条件释放。

        漏掉这一步，探针会悬空到 probe_timeout，期间全进程的 acquire() 都在空转。
        二进制通道是新加的路径，最容易漏掉回报——这条测试专门守它。
        """
        transport = BytesStubTransport([TransportError("boom")])
        client = make_client(transport)

        with patch.object(client.gate, "record_neutral",
                          wraps=client.gate.record_neutral) as spy:
            with self.assertRaises(TransportError):
                client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(spy.call_count, RETRY_ATTEMPTS,
                         "每次失败尝试都必须恰好释放一次探针")

    def test_gate_acquire_failure_raises_cancelled(self):
        transport = BytesStubTransport([PROTOBUF_BLOB])
        client = make_client(transport)

        with patch.object(client.gate, "acquire", return_value=False):
            with self.assertRaises(TaskCancelledError):
                client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 0)
        self.assertEqual(client.stats["requests"], 0)

    def test_cancel_before_first_request_sends_nothing(self):
        transport = BytesStubTransport([PROTOBUF_BLOB])
        client = make_client(transport, cancel=lambda: True)

        with self.assertRaises(TaskCancelledError):
            client.fetch_bytes("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 0)
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.stats["net_errors"], 0, "取消不计入任何失败统计")


class SessionFacadeTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, result=None, exc=None):
            self.result = result
            self.exc = exc
            self.seen = {}

        def fetch_bytes(self, url, retries=3, cancel=None):
            self.seen.update(url=url, retries=retries, cancel=cancel)
            if self.exc:
                raise self.exc
            return self.result

    def _call(self, client, **kw):
        with patch("core.session._VTOKEN", None), \
                patch("core.session.get_client", return_value=client):
            return http_get_bytes("https://api.example.invalid/seg.so", **kw)

    def test_returns_client_bytes_and_forwards_options(self):
        client = self.FakeClient(result=PROTOBUF_BLOB)
        cancel = lambda: False  # noqa: E731
        self.assertEqual(self._call(client, retries=2, cancel=cancel), PROTOBUF_BLOB)
        self.assertEqual(client.seen["retries"], 2)
        self.assertIs(client.seen["cancel"], cancel)
        self.assertTrue(client.seen["url"].startswith("https://api.example.invalid/seg.so"))

    def test_voucher_becomes_risk_challenge_error(self):
        from core.session import RiskChallengeError
        exc = RiskVoucher("risk", "vv-abc")
        with self.assertRaises(RiskChallengeError) as ctx:
            self._call(self.FakeClient(exc=exc))
        self.assertEqual(ctx.exception.v_voucher, "vv-abc")


if __name__ == "__main__":
    unittest.main()
