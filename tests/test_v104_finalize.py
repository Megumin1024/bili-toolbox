# -*- coding: utf-8 -*-
"""v1.0.4 收尾边界：取消、通道构建和本地原子持久化。

全程离线：网络接口使用 mock，配置与 Cookie 只写临时目录，不读取真实凭据。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
from core import config
from core.cancel import TaskCancelledError
from core.client import RETRY_ATTEMPTS, BiliClient
from core.proxy import ProxyPool
from core.transport import (CookieStore, TransportConfigError, TransportError,
                            build_transport)
from tools.danmaku import core as danmaku_core
from tools.danmaku import pipeline


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds


def view_payload():
    return {
        "code": 0,
        "data": {
            "bvid": "BV1GJ411x7h7",
            "aid": 1,
            "cid": 101,
            "duration": 60,
            "title": "离线测试视频",
            "stat": {"danmaku": 0},
            "pages": [
                {"cid": 101, "duration": 60, "part": "第一段"},
                {"cid": 202, "duration": 60, "part": "第二段"},
            ],
        },
    }


class DanmakuCancellationTests(unittest.TestCase):
    def test_first_meta_request_forwards_the_cancel_predicate(self):
        cancel = lambda: False
        with patch("core.session.http_get_json",
                   return_value=view_payload()) as request:
            danmaku_core.fetch_video_meta(
                bvid="BV1GJ411x7h7", cancel=cancel)

        self.assertIs(request.call_args.kwargs["cancel"], cancel)

    def test_followup_part_meta_requests_reuse_the_same_cancel_predicate(self):
        cancel = lambda: False
        with tempfile.TemporaryDirectory(prefix="danmaku_meta_cancel_") as out, \
                patch("core.session.http_get_json",
                      return_value=view_payload()) as meta_request, \
                patch("core.session.http_get_bytes", return_value=b""):
            pipeline.run_pipeline(
                "BV1GJ411x7h7", out, sleep=0, cancel=cancel,
                all_pages=True)

        self.assertEqual(meta_request.call_count, 2)
        self.assertTrue(all(
            call.kwargs["cancel"] is cancel for call in meta_request.call_args_list
        ))

    def test_cancelled_meta_request_returns_cancelled_not_failed(self):
        with tempfile.TemporaryDirectory(prefix="danmaku_meta_cancel_") as out, \
                patch("core.session.http_get_json",
                      side_effect=TaskCancelledError()):
            result = pipeline.run_pipeline(
                "BV1GJ411x7h7", out, cancel=lambda: True)

        self.assertTrue(result["stats"]["cancelled"])
        self.assertEqual(result["stats"]["segments"], 0)
        self.assertEqual(result["xlsx"], "")


class TransportBuildTests(unittest.TestCase):
    @staticmethod
    def failing_factory(message):
        def factory(*_args, **_kwargs):
            raise RuntimeError(message)

        return factory

    def test_all_channel_build_failures_are_transport_errors(self):
        with patch("core.transport.Ja3H2Transport",
                   self.failing_factory("h2 failed")), \
                patch("core.transport.UrllibTransport",
                      self.failing_factory("urllib failed")):
            with self.assertRaises(TransportError) as ctx:
                build_transport("auto", proxy_url="http://user:pass@1.2.3.4:8080")

        self.assertNotIn("user:pass", str(ctx.exception))

    def test_build_failure_enters_standard_retry_and_net_error_stats(self):
        sleeps = RecordingSleep()
        client = BiliClient(
            ProxyPool(None), cookie_path=None, log=lambda _msg: None,
            clock=lambda: 0.0, sleep=sleeps)
        with patch("core.transport.Ja3H2Transport",
                   self.failing_factory("h2 failed")), \
                patch("core.transport.UrllibTransport",
                      self.failing_factory("urllib failed")):
            with self.assertRaises(TransportError):
                client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(client.stats["net_errors"], RETRY_ATTEMPTS)
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.pool.entries[0].total_fail, 0,
                         "通道构建失败不能绕过 client，也不能重复计入代理失败")
        self.assertGreater(sleeps.calls, 0)

    def test_unknown_transport_is_a_non_retryable_configuration_error(self):
        sleeps = RecordingSleep()
        client = BiliClient(
            ProxyPool(None), preferred_transport="made-up", cookie_path=None,
            log=lambda _msg: None, clock=lambda: 0.0, sleep=sleeps)
        with self.assertRaises(TransportConfigError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(client.stats["net_errors"], 0)
        self.assertEqual(sleeps.calls, 0)

    def test_half_open_probe_is_released_exactly_once_on_build_failure(self):
        from core.gate import RequestGate

        class CountingGate(RequestGate):
            def __init__(self):
                super().__init__(failure_threshold=1, cooldown=0,
                                 min_interval=0, clock=lambda: 0.0,
                                 sleep=RecordingSleep())
                self.neutral_calls = 0

            def record_neutral(self):
                self.neutral_calls += 1
                super().record_neutral()

        gate = CountingGate()
        gate.record_block("open")
        client = BiliClient(
            ProxyPool(None), cookie_path=None, log=lambda _msg: None,
            clock=lambda: 0.0, sleep=RecordingSleep(), gate=gate)
        client._get_transport = lambda: (_ for _ in ()).throw(
            TransportError("constructor failed"))

        with self.assertRaises(TransportError):
            client.fetch_json("https://api.example.invalid/x", retries=1)

        self.assertEqual(gate.neutral_calls, 1)
        self.assertEqual(gate.status()["state"], "half_open")
        self.assertTrue(gate.acquire(), "释放后下一次探针应能再次进入")
        self.assertEqual(gate.neutral_calls, 1)

    def test_cancel_during_backoff_does_not_start_another_request(self):
        state = {"cancel": False, "calls": 0}

        class FailingTransport:
            name = "offline"

            def get_json(self, _url):
                state["calls"] += 1
                state["cancel"] = True
                raise TransportError("one real network failure")

            def close(self):
                pass

        sleeps = RecordingSleep()
        client = BiliClient(
            ProxyPool(None), cookie_path=None, log=lambda _msg: None,
            clock=lambda: 0.0, sleep=sleeps,
            cancel=lambda: state["cancel"])
        client._get_transport = lambda: FailingTransport()

        with self.assertRaises(TaskCancelledError):
            client.fetch_json("https://api.example.invalid/x")

        self.assertEqual(state["calls"], 1)
        self.assertEqual(client.stats["net_errors"], 1)
        self.assertEqual(sleeps.calls, 0)


class AtomicPersistenceTests(unittest.TestCase):
    @staticmethod
    def temp_files(root, name):
        return sorted(root.glob(f".{name}.*.tmp"))

    def test_config_atomic_replace_preserves_shape_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory(prefix="config_atomic_") as tmp:
            root = Path(tmp)
            target = root / "config.json"
            with patch.object(config, "CONFIG_DIR", root), \
                    patch.object(config, "CONFIG_FILE", target):
                config.save({"theme": "light",
                             "proxy_spec": "http://user:pass@1.2.3.4:8080"})
                data = json.loads(target.read_text(encoding="utf-8"))

            self.assertEqual(set(data), set(config.DEFAULTS))
        self.assertEqual(data["theme"], "light")
        self.assertNotIn("schema_version", data)
        self.assertEqual(self.temp_files(root, target.name), [])

    def test_config_write_failure_keeps_old_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(prefix="config_atomic_") as tmp:
            root = Path(tmp)
            target = root / "config.json"
            old = '{"theme":"old","proxy_spec":"direct"}'
            target.write_text(old, encoding="utf-8")
            with patch.object(config, "CONFIG_DIR", root), \
                    patch.object(config, "CONFIG_FILE", target), \
                    patch.object(config.os, "fdopen",
                                 side_effect=OSError("write denied")):
                config.save({"theme": "new"})

            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual(self.temp_files(root, target.name), [])

    def test_cookie_store_atomic_replace_preserves_json_shape(self):
        with tempfile.TemporaryDirectory(prefix="cookie_atomic_") as tmp:
            root = Path(tmp)
            target = root / "session_cookies.json"
            store = CookieStore(target)
            store.save("device-1", {"buvid3": "value"}, activated=True)
            data = json.loads(target.read_text(encoding="utf-8"))

            self.assertEqual(set(data), {"device-1"})
            self.assertEqual(data["device-1"]["cookies"], {"buvid3": "value"})
            self.assertTrue(data["device-1"]["activated"])
            self.assertNotIn("schema_version", data)
            self.assertEqual(self.temp_files(root, target.name), [])

    def test_cookie_store_write_failure_keeps_old_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(prefix="cookie_atomic_") as tmp:
            root = Path(tmp)
            target = root / "session_cookies.json"
            old = '{"device-1":{"saved_at":9999999999,"cookies":{"buvid3":"old"}}}'
            target.write_text(old, encoding="utf-8")
            store = CookieStore(target)
            with patch.object(config.os, "replace",
                              side_effect=OSError("replace denied")):
                store.save("device-1", {"buvid3": "new"})

            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual(self.temp_files(root, target.name), [])


class DocumentationConsistencyTests(unittest.TestCase):
    def test_v104_docs_and_danmaku_copy_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        registry = (root / "tools" / "__init__.py").read_text(encoding="utf-8")
        page = (root / "tools" / "danmaku" / "page.py").read_text(encoding="utf-8")

        self.assertIn("**用户动态**", readme)
        self.assertIn("**弹幕抓取/分析**", readme)
        self.assertIn("%USERPROFILE%\\BiliToolbox\\导出", changelog)
        self.assertNotIn("我的文档\\BiliToolbox\\导出", changelog)
        self.assertIn("[1.0.4]: https://github.com/Megumin1024/bili-toolbox/compare/v1.0.3...v1.0.4", changelog)
        self.assertEqual(core.__version__, "1.0.4")
        for phrase in ("热词", "高频弹幕", "热点分钟", "密度分布"):
            self.assertIn(phrase, registry)
            self.assertIn(phrase, page)


if __name__ == "__main__":
    unittest.main()
