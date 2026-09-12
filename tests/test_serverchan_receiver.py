# -*- coding: utf-8 -*-
"""Server酱 本机接收端实测：真实 HTTP/form POST 与两种响应形态。

独立成文件的原因（勿合并回 test_monitor_webhook.py）：全量 discover 按
模块名字母序执行，本文件的**真实 socket HTTP + daemon 线程**用例若排在
test_monitor_webhook（Qt 页面测试）之前，会改变 PySide6 事件循环与
daemon urllib 线程并发的时序状态，稳定触发 Qt processEvents 段错误
（faulthandler 实证：主线程停在 processEvents，daemon 线程停在
http_open——Qt 原生层潜在缺陷，非本项目代码）。放在其后运行则
test_monitor_webhook 内的组合恢复基线时序，全量绿。

本文件不创建任何 Qt 对象：等待日志用纯 sleep 轮询。
绝不向 sctapi.ftqq.com 发任何真实请求——sender 桩把域名改写到本机
127.0.0.1 接收端后才调用生产实现的 _post_serverchan。
"""
from __future__ import annotations

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from tools.monitor.notifications import WebhookAdapter, _post_serverchan


class ServerChanReceiverTests(unittest.TestCase):
    """本机 127.0.0.1 接收端模拟 Server酱 两种响应；绝不请求 sctapi.ftqq.com。

    适配器按归一化规则只会产出 sctapi.ftqq.com 目标，而真实端点禁止触碰；
    因此注入的 serverchan_sender 只做一件事：把域名改写到本机接收端后调用
    生产实现的 _post_serverchan。其余路径（send → daemon 线程 →
    _deliver_serverchan → 真实 urllib HTTP/form POST/HTTPError 读取）全部
    走生产代码，日志断言与用户看到的完全一致。

    不继承 QtEnvironmentTestCase：本类不创建任何 Qt 对象，等日志用纯 sleep
    轮询即可（全量跑时 Qt 事件循环与 daemon 线程的真实 HTTP 并发会触发
    PySide6 段错误，见实施报告「已知问题」）。
    """

    @staticmethod
    def _wait_for_log(logs, needle, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(needle in message for message in logs):
                return True
            time.sleep(0.02)
        return False

    @classmethod
    def setUpClass(cls):
        class Receiver(BaseHTTPRequestHandler):
            status_code = 200
            body = b'{"code":0,"message":"ok"}'
            captured = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                type(self).captured.append({
                    "content_type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(length),
                })
                payload = type(self).body
                self.send_response(type(self).status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        cls.receiver = Receiver
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _adapter(self, logs):
        port = self.port

        def rewrite_sender(url, body, timeout):
            # 本机接收端是纯 HTTP：域名连同 scheme 一起改写后再走生产实现
            return _post_serverchan(
                url.replace("https://sctapi.ftqq.com",
                            f"http://127.0.0.1:{port}"),
                body, timeout)

        return WebhookAdapter(
            url_getter=lambda: "https://sctapi.ftqq.com/SCUabcdef12.send",
            format_getter=lambda: "serverchan", log=logs.append,
            serverchan_sender=rewrite_sender)

    def _set_response(self, status_code, body):
        type(self).receiver.captured.clear()
        type(self).receiver.status_code = status_code
        type(self).receiver.body = body

    def test_success_200_code0_posts_form_and_logs_sent(self):
        self._set_response(200, b'{"code":0,"message":"ok"}')
        logs = []
        adapter = self._adapter(logs)
        self.assertTrue(adapter.send(
            "milestone", "播放量里程碑", "播放量已达到 10000。"))
        deadline = time.time() + 5
        while not self.receiver.captured and time.time() < deadline:
            time.sleep(0.02)
        captured = self.receiver.captured
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["content_type"],
                         "application/x-www-form-urlencoded")
        parsed = parse_qs(captured[0]["body"].decode("utf-8"))
        self.assertEqual(set(parsed), {"title", "desp"})
        self.assertEqual(parsed["title"], ["播放量里程碑"])
        self.assertEqual(parsed["desp"], ["播放量已达到 10000。"])
        self.assertTrue(self._wait_for_log(logs, "Server酱 推送已发送"))

    def test_business_failure_200_code_not_zero(self):
        self._set_response(200, '{"code":40001,"message":"key错误"}'
                           .encode("utf-8"))
        logs = []
        adapter = self._adapter(logs)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertTrue(self._wait_for_log(logs, "Server酱 推送失败"))
        self.assertTrue(any(
            "code=40001" in message and "key错误" in message
            for message in logs))
        self.assertFalse(any("推送已发送" in message for message in logs))

    def test_http_400_reads_error_body_for_code_and_message(self):
        self._set_response(400, b'{"code":40001,"message":"BAD KEY"}')
        logs = []
        adapter = self._adapter(logs)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertTrue(self._wait_for_log(logs, "Server酱 推送失败"))
        self.assertTrue(any(
            "HTTP 400" in message and "code=40001" in message
            and "BAD KEY" in message for message in logs))

    def test_non_json_body_reports_status_only_without_raw_body(self):
        self._set_response(500, b"<html>gateway exploded</html>")
        logs = []
        adapter = self._adapter(logs)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertTrue(self._wait_for_log(logs, "Server酱 推送失败"))
        joined = "\n".join(logs)
        self.assertIn("HTTP 500", joined)
        self.assertNotIn("code=", joined)
        self.assertNotIn("gateway exploded", joined)  # 响应原文不落日志

    def test_response_body_never_reaches_log_raw(self):
        long_tail = "x" * 200
        self._set_response(200, (
            '{"code":40001,"message":"invalid '
            'https://sctapi.ftqq.com/SCUsecret99.send ' + long_tail + '"}'
        ).encode("utf-8"))
        logs = []
        adapter = self._adapter(logs)
        self.assertTrue(adapter.send("test", "标题", "内容"))
        self.assertTrue(self._wait_for_log(logs, "Server酱 推送失败"))
        joined = "\n".join(logs)
        self.assertIn("code=40001", joined)
        # message 里的 URL 被 sanitize、超长串被截断到 80 字符
        self.assertNotIn("SCUsecret99", joined)
        self.assertNotIn("x" * 81, joined)


