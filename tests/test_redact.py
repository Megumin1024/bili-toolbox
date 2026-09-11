# -*- coding: utf-8 -*-
"""隐私脱敏（core.redact + 各出口）的离线测试。

覆盖三层：脱敏器本身的规则、产生消息的源头、消息离开进程的出口。
全程离线：不打网络、不读用户配置。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from core import client as client_mod
from core import diagnostics, redact
from core.proxy import ProxyEntry, ProxyPool
from core.redact import sanitize_text
from core.transport import TransportError
from tools.monitor.server import MonitorServer

PROXY_WITH_CREDS = "http://proxyuser:proxypass@127.0.0.1:8080"


class RedactPatternTests(unittest.TestCase):
    def test_full_proxy_url_is_fully_masked(self):
        safe = sanitize_text(f"代理健康检查失败: {PROXY_WITH_CREDS}")
        self.assertNotIn("proxypass", safe)
        self.assertNotIn("proxyuser", safe)
        self.assertIn("[网络地址已脱敏]", safe)

    def test_schemeless_userinfo_is_masked(self):
        """底层库异常常丢掉 scheme，只剩 user:pass@host。"""
        safe = sanitize_text("Failed to connect to proxyuser:proxypass@127.0.0.1:8080")
        self.assertNotIn("proxypass", safe)
        self.assertNotIn("proxyuser", safe)

    def test_bilibili_credentials_and_signatures_are_masked(self):
        raw = (
            "v_voucher=vv-secret; access_key=ak-secret; buvid3=buvid-secret; "
            "buvid_fp=fp-secret; w_rid=rid-secret; wts=1700000000; "
            "gaia_vtoken=gv-secret; bili_ticket=ticket-secret; grisk_id=grisk-secret"
        )
        safe = sanitize_text(raw)
        for secret in ("vv-secret", "ak-secret", "buvid-secret", "fp-secret",
                       "rid-secret", "gv-secret", "ticket-secret",
                       "grisk-secret"):
            self.assertNotIn(secret, safe, secret)
        self.assertIn("[已脱敏]", safe)

    def test_previously_covered_secrets_still_masked(self):
        raw = ('Cookie=ck; SESSDATA=sd; bili_jct=jct; access_token=at; '
               'proxy_password=pp; csrf=cs')
        safe = sanitize_text(raw)
        for secret in ("ck", "sd", "jct", "at", "pp", "cs"):
            self.assertNotIn(f"={secret}", safe, secret)

    def test_benign_text_is_preserved(self):
        """脱敏不能把正常排障信息也糊掉。"""
        for text in ("HTTP 429", "API code=-352", "Python 3.14.6",
                     "[rate] 限流(stub): HTTP 429，遵守 Retry-After 20s",
                     "风控拦截，重新预热并轮换代理"):
            self.assertEqual(sanitize_text(text), text, text)

    def test_is_idempotent(self):
        raw = f"失败 {PROXY_WITH_CREDS} v_voucher=vv-secret"
        once = sanitize_text(raw)
        self.assertEqual(sanitize_text(once), once)

    def test_empty_and_none(self):
        self.assertEqual(sanitize_text(None), "")
        self.assertEqual(sanitize_text(""), "")

    def test_diagnostics_reexports_same_function(self):
        """diagnostics.sanitize_text 只是入口迁移后的兼容别名。"""
        self.assertIs(diagnostics.sanitize_text, redact.sanitize_text)


class ProxySourceTests(unittest.TestCase):
    def test_entry_display_hides_credentials(self):
        self.assertEqual(ProxyEntry("http://u:p@1.2.3.4:8080").display(),
                         "http://1.2.3.4:8080")
        self.assertEqual(ProxyEntry(None).display(), "直连")

    def test_unsupported_scheme_error_hides_credentials(self):
        with self.assertRaises(ValueError) as ctx:
            ProxyPool("ftp://u:p@1.2.3.4:8080")
        self.assertNotIn("p@", str(ctx.exception))
        self.assertNotIn("u:p", str(ctx.exception))

    def test_pool_status_hides_credentials(self):
        status = ProxyPool(PROXY_WITH_CREDS).status()
        self.assertNotIn("proxypass", str(status))
        self.assertEqual(status["current"], "http://127.0.0.1:8080")


class _FailingHealthcheckTransport:
    name = "stub"

    def warmup(self, force=False):
        pass

    def healthcheck(self):
        return False

    def close(self):
        pass


class ClientSourceTests(unittest.TestCase):
    def test_healthcheck_error_carries_no_credentials(self):
        client = client_mod.BiliClient(ProxyPool(PROXY_WITH_CREDS))
        with patch.object(client_mod, "build_transport",
                          return_value=_FailingHealthcheckTransport()):
            with self.assertRaises(TransportError) as ctx:
                client._get_transport()

        message = str(ctx.exception)
        self.assertNotIn("proxypass", message)
        self.assertNotIn("proxyuser", message)
        self.assertIn("127.0.0.1:8080", message, "应保留可定位的出口信息")

    def test_client_log_is_sanitized(self):
        captured = []
        client = client_mod.BiliClient(ProxyPool(None), log=captured.append)

        client.log(f"传输失败: {PROXY_WITH_CREDS} v_voucher=vv-secret")

        self.assertEqual(len(captured), 1)
        self.assertNotIn("proxypass", captured[0])
        self.assertNotIn("vv-secret", captured[0])

    def test_client_log_keeps_benign_lines_intact(self):
        captured = []
        client = client_mod.BiliClient(ProxyPool(None), log=captured.append)

        client.log("[rate] 限流(stub): HTTP 429，加倍退避")

        self.assertEqual(captured, ["[rate] 限流(stub): HTTP 429，加倍退避"])


class MonitorEgressTests(unittest.TestCase):
    def test_last_error_is_sanitized_before_reaching_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = MonitorServer("BV1", data_dir=tmp, log=lambda msg: None)

            def failing_collect(bvid):
                server._stop.set()      # 让轮询循环在这次失败后退出
                raise TransportError(f"代理健康检查失败: {PROXY_WITH_CREDS}")

            server.collect_once = failing_collect
            server._poller_loop()

            self.assertIsNotNone(server.state.last_error)
            self.assertNotIn("proxypass", server.state.last_error)
            self.assertNotIn("proxyuser", server.state.last_error)


class TaskRunnerEgressTests(unittest.TestCase):
    def _runner(self):
        from app.task_runner import TaskRunner
        return TaskRunner

    def test_failed_signal_is_sanitized(self):
        TaskRunner = self._runner()
        captured = []

        def boom(**kwargs):
            raise TransportError(f"代理健康检查失败: {PROXY_WITH_CREDS}")

        runner = TaskRunner(boom)
        runner.failed.connect(captured.append)
        runner.run()

        self.assertEqual(len(captured), 1)
        self.assertIn("TransportError", captured[0])
        self.assertNotIn("proxypass", captured[0])
        self.assertNotIn("proxyuser", captured[0])


if __name__ == "__main__":
    unittest.main()
