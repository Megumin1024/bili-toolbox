# -*- coding: utf-8 -*-
"""原始异常出口的脱敏断言（P0-3 日志脱敏收口）。

覆盖 6 个出口：session.ensure_ready 日志、session.info() 返回值、
risk.register/validate 两处日志、transport bootstrap_credentials 的
spi/ticket 两处失败日志（StubTransport 触发）。

每处构造携带代理凭据/令牌的异常，断言出口文本不含明文且保留错误类别
（脱敏只打码敏感串）。全程离线：不打网络、不读用户配置。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core import risk, session, transport as transport_mod
from core.transport import TransportError, bootstrap_credentials

CREDS_URL = "http://proxyuser0:secretpass9@127.0.0.1:8080"


class SessionEgressTests(unittest.TestCase):
    def test_ensure_ready_failure_log_is_sanitized(self):
        class BoomClient:
            def _get_transport(self):
                raise TransportError(
                    f"通道不可用: {CREDS_URL}；bili_ticket=ticket-secret9")

            def cookie_header(self):
                return ""

        captured = []
        with patch.object(session, "get_client", return_value=BoomClient()), \
                patch.object(session, "log", lambda msg: captured.append(msg)):
            result = session.ensure_ready()

        self.assertEqual(result, "")
        self.assertEqual(len(captured), 1, "预热失败必须留一条日志")
        self.assertIn("凭证预热失败", captured[0], "错误类别必须保留")
        self.assertNotIn("proxyuser0", captured[0])
        self.assertNotIn("secretpass9", captured[0])
        self.assertNotIn("ticket-secret9", captured[0])

    def test_info_error_field_is_sanitized(self):
        def boom():
            raise TransportError(f"通道不可用: {CREDS_URL}")

        with patch.object(session, "get_client", boom):
            state = session.info()

        self.assertIn("error", state)
        self.assertNotIn("proxyuser0", state["error"])
        self.assertNotIn("secretpass9", state["error"])
        self.assertNotEqual(state["error"], "", "错误信息不能为空串")


class RiskEgressTests(unittest.TestCase):
    def test_register_exception_log_is_sanitized(self):
        captured = []

        def boom(url, fields):
            raise TransportError(f"gaia 请求失败: {CREDS_URL} "
                                 f"v_voucher=vv-secret9")

        with patch.object(session, "post_form", side_effect=boom), \
                patch.object(session, "log", lambda msg: captured.append(msg)):
            self.assertIsNone(risk.gaia_register("vv-token"))

        register_logs = [m for m in captured if "register 异常" in m]
        self.assertEqual(len(register_logs), 1, "register 失败必须留一条日志")
        self.assertNotIn("proxyuser0", register_logs[0])
        self.assertNotIn("secretpass9", register_logs[0])
        self.assertNotIn("vv-secret9", register_logs[0])

    def test_validate_exception_log_is_sanitized(self):
        captured = []

        def boom(url, fields):
            raise TransportError(f"gaia 请求失败: {CREDS_URL}")

        with patch.object(session, "post_form", side_effect=boom), \
                patch.object(session, "log", lambda msg: captured.append(msg)):
            self.assertIsNone(risk.gaia_validate("tk", "ch", "va", "se"))

        validate_logs = [m for m in captured if "validate 异常" in m]
        self.assertEqual(len(validate_logs), 1, "validate 失败必须留一条日志")
        self.assertNotIn("proxyuser0", validate_logs[0])
        self.assertNotIn("secretpass9", validate_logs[0])


class _SpiFailTransport:
    """spi 请求即抛（携带凭据），其余调用给出正常形状的应答。"""

    name = "stub"

    def get_json(self, url):
        raise TransportError(f"spi 连接失败: {CREDS_URL}")

    def post_json(self, url, json_body, headers=None, cookies=None):
        return {"code": 0, "data": {"ticket": "tk"}}

    def set_cookies(self, cookies):
        pass


class _TicketFailTransport:
    """spi 正常，bili_ticket 签发即抛（携带凭据）。"""

    name = "stub"

    def get_json(self, url):
        return {"code": 0, "data": {"b_3": "buvid3-x", "b_4": "buvid4-x"}}

    def post_json(self, url, json_body, headers=None, cookies=None):
        raise TransportError(f"ticket 通道失败: {CREDS_URL}")

    def set_cookies(self, cookies):
        pass


class TransportEgressTests(unittest.TestCase):
    def test_spi_failure_log_is_sanitized(self):
        captured = []
        with patch.object(transport_mod, "LOG", captured.append):
            ok = bootstrap_credentials(_SpiFailTransport())

        self.assertTrue(ok, "spi 失败后应自生成 buvid3 兜底")
        spi_logs = [m for m in captured if "spi 获取失败" in m]
        self.assertEqual(len(spi_logs), 1)
        self.assertIn("自生成 buvid3 兜底", spi_logs[0], "兜底说明必须保留")
        self.assertNotIn("proxyuser0", spi_logs[0])
        self.assertNotIn("secretpass9", spi_logs[0])

    def test_ticket_failure_log_is_sanitized(self):
        captured = []
        with patch.object(transport_mod, "LOG", captured.append):
            ok = bootstrap_credentials(_TicketFailTransport())

        self.assertTrue(ok, "spi 成功时应有 buvid3")
        ticket_logs = [m for m in captured if "bili_ticket 签发失败" in m]
        self.assertEqual(len(ticket_logs), 1)
        self.assertNotIn("proxyuser0", ticket_logs[0])
        self.assertNotIn("secretpass9", ticket_logs[0])


if __name__ == "__main__":
    unittest.main()
