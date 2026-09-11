# -*- coding: utf-8 -*-
"""网络错误分类内核的离线单元测试。

全部离线：不打网络、不读文件、不 sleep（client 用例注入记录型 sleep）。
"""
from __future__ import annotations

import unittest

from core.client import BiliClient
from core.net_errors import ErrorKind, classify, describe, is_retryable
from core.proxy import ProxyPool
from core.transport import (BaseTransport, BiliApiError, BiliRateLimitError,
                           RiskBlocked, RiskVoucher, TransportError)


class ClassifyTests(unittest.TestCase):
    def test_risk_http_status(self):
        self.assertIs(classify(http_status=412), ErrorKind.RISK)
        self.assertIs(classify(http_status=403), ErrorKind.RISK)

    def test_rate_limit_http_status(self):
        self.assertIs(classify(http_status=429), ErrorKind.RATE_LIMIT)

    def test_risk_api_code(self):
        self.assertIs(classify(payload={"code": -352}), ErrorKind.RISK)
        self.assertIs(classify(payload={"code": -412}), ErrorKind.RISK)

    def test_risk_voucher_only_when_voucher_present(self):
        self.assertIs(
            classify(payload={"code": -352, "data": {"v_voucher": "vv"}}),
            ErrorKind.RISK_VOUCHER,
        )
        self.assertIs(
            classify(payload={"code": -352, "data": {}}),
            ErrorKind.RISK,
        )

    def test_rate_limit_api_codes(self):
        for code in (-799, -502):
            self.assertIs(classify(payload={"code": code}), ErrorKind.RATE_LIMIT)

    def test_business_api_error_is_distinct_from_transport(self):
        self.assertIs(
            classify(payload={"code": -404, "message": "视频不存在"}),
            ErrorKind.API_ERROR,
        )

    def test_success_returns_none(self):
        self.assertIsNone(classify(http_status=200, payload={"code": 0, "data": {}}))
        self.assertIsNone(classify())

    def test_non_json_body(self):
        self.assertIs(
            classify(http_status=200, payload="<html>风控页</html>"),
            ErrorKind.NON_JSON,
        )

    def test_risk_status_beats_non_json_body(self):
        # 412 返回 HTML 风控页时必须判 RISK，而不是 NON_JSON。
        self.assertIs(classify(http_status=412, payload="<html>"), ErrorKind.RISK)

    def test_api_code_beats_http_status(self):
        self.assertIs(
            classify(http_status=200, payload={"code": -799}),
            ErrorKind.RATE_LIMIT,
        )

    def test_other_http_errors(self):
        self.assertIs(classify(http_status=500), ErrorKind.HTTP_ERROR)
        self.assertIs(classify(http_status=404), ErrorKind.HTTP_ERROR)

    def test_exception_classification(self):
        self.assertIs(classify(exc=TimeoutError()), ErrorKind.TIMEOUT)
        self.assertIs(classify(exc=ConnectionResetError()), ErrorKind.CONNECTION)
        self.assertIs(classify(exc=ValueError("boom")), ErrorKind.UNKNOWN)

    def test_retryable_truth_table(self):
        retryable = {
            ErrorKind.RISK, ErrorKind.RISK_VOUCHER, ErrorKind.RATE_LIMIT,
            ErrorKind.NON_JSON, ErrorKind.TIMEOUT, ErrorKind.CONNECTION,
        }
        for kind in ErrorKind:
            self.assertEqual(is_retryable(kind), kind in retryable, kind)
        # 业务错误与其它 HTTP 错误不得自动重试。
        self.assertFalse(is_retryable(ErrorKind.API_ERROR))
        self.assertFalse(is_retryable(ErrorKind.HTTP_ERROR))
        self.assertFalse(is_retryable(ErrorKind.UNKNOWN))

    def test_describe_mentions_status(self):
        self.assertIn("非 JSON", describe(ErrorKind.NON_JSON, http_status=200))
        self.assertIn("HTTP 412", describe(ErrorKind.RISK, http_status=412))

    def test_api_error_is_not_a_transport_error(self):
        self.assertFalse(issubclass(BiliApiError, TransportError))


class TransportClassifyJsonTests(unittest.TestCase):
    """transport 的 JSON 分类改走内核后，异常类型保持不变。"""

    def test_success_passes_through(self):
        data = {"code": 0, "data": {"ok": True}}
        self.assertIs(BaseTransport._classify_json(data), data)

    def test_api_error(self):
        with self.assertRaises(BiliApiError):
            BaseTransport._classify_json({"code": -404, "message": "不存在"})

    def test_risk_without_voucher(self):
        with self.assertRaises(RiskBlocked):
            BaseTransport._classify_json({"code": -352})

    def test_risk_with_voucher(self):
        with self.assertRaises(RiskVoucher):
            BaseTransport._classify_json({"code": -352, "data": {"v_voucher": "vv"}})

    def test_rate_limit(self):
        with self.assertRaises(BiliRateLimitError):
            BaseTransport._classify_json({"code": -799})


class _StubTransport:
    """只实现 fetch_json 需要的 get_json，并记录调用次数。"""

    name = "stub"

    def __init__(self, exc_factory):
        self._exc_factory = exc_factory
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        raise self._exc_factory()

    def close(self):
        pass


class _RecordingSleep:
    """记录被请求的等待时长，但不真的睡——保证测试瞬时且离线。"""

    def __init__(self):
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds


class ClientRetrySemanticsTests(unittest.TestCase):
    """业务错误不得触发重试 / 网络计数 / 代理失败计数。"""

    @staticmethod
    def _client_with(transport):
        """注入 clock/sleep，全程不触达真实时钟与真实等待。"""
        client = BiliClient(ProxyPool(None), clock=lambda: 0.0, sleep=_RecordingSleep())
        client._get_transport = lambda: transport
        return client

    def test_api_error_is_not_retried_and_does_not_touch_proxy(self):
        transport = _StubTransport(lambda: BiliApiError("API code=-404 msg=不存在"))
        client = self._client_with(transport)

        with self.assertRaises(BiliApiError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 1, "业务错误不应重试")
        self.assertEqual(client.stats["net_errors"], 0)
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.stats["risk_events"], 0)
        self.assertEqual(client.stats["rate_limit_events"], 0)
        entry = client.pool.entries[0]
        self.assertEqual(entry.failures, 0)
        self.assertEqual(entry.total_fail, 0)
        self.assertEqual(client._sleep.calls, 0, "业务错误不应产生任何退避等待")

    def test_transport_error_still_retries_and_counts(self):
        transport = _StubTransport(lambda: TransportError("boom"))
        client = self._client_with(transport)

        with self.assertRaises(TransportError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(transport.calls, 3, "传输错误仍应按 RETRY_ATTEMPTS 重试")
        self.assertEqual(client.stats["net_errors"], 3)
        self.assertEqual(client.pool.entries[0].total_fail, 3)
        self.assertGreater(client._sleep.total, 0.0, "传输错误应产生退避等待")


if __name__ == "__main__":
    unittest.main()
