# -*- coding: utf-8 -*-
"""取消贯通（core.cancel + session + 采集路径）的离线测试。

全程离线：不打网络、不读文件、不真等待。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core import session
from core.cancel import CANCEL_POLL_SLICE, TaskCancelledError, is_cancelled, wait
from core.client import BiliClient
from core.proxy import ProxyPool
from tools.collector import core as collector_core


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0
        self.slices = []

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds
        self.slices.append(seconds)


class WaitTests(unittest.TestCase):
    def test_no_cancel_predicate_never_cancels(self):
        self.assertFalse(is_cancelled(None))
        sleep = RecordingSleep()
        self.assertTrue(wait(0.5, None, sleep=sleep))
        self.assertAlmostEqual(sleep.total, 0.5, places=6)

    def test_pre_cancelled_returns_false_without_sleeping(self):
        sleep = RecordingSleep()
        self.assertFalse(wait(1.0, lambda: True, sleep=sleep))
        self.assertEqual(sleep.calls, 0)

    def test_cancel_midway_stops_early(self):
        state = {"checks": 0}

        def cancel():
            state["checks"] += 1
            return state["checks"] > 2

        sleep = RecordingSleep()
        self.assertFalse(wait(10.0, cancel, sleep=sleep))
        self.assertLess(sleep.total, 10.0)

    def test_slices_are_bounded(self):
        sleep = RecordingSleep()
        self.assertTrue(wait(1.0, None, sleep=sleep))
        self.assertEqual(sleep.calls, 4)
        self.assertAlmostEqual(sum(sleep.slices), 1.0, places=6)
        self.assertTrue(all(s <= CANCEL_POLL_SLICE + 1e-9 for s in sleep.slices))

    def test_zero_seconds_is_noop(self):
        sleep = RecordingSleep()
        self.assertTrue(wait(0.0, None, sleep=sleep))
        self.assertEqual(sleep.calls, 0)

    def test_default_sleep_resolved_at_call_time(self):
        # sleep 在调用时解析，patch time.sleep 依然生效。
        with patch("core.cancel.time.sleep") as fake:
            self.assertTrue(wait(0.5))
        self.assertTrue(fake.called)


class SessionPassthroughTests(unittest.TestCase):
    def test_http_get_json_forwards_cancel(self):
        seen = {}

        class FakeClient:
            def fetch_json(self, url, retries=3, cancel=None):
                seen["url"] = url
                seen["retries"] = retries
                seen["cancel"] = cancel
                return {"code": 0}

        predicate = lambda: False  # noqa: E731
        with patch.object(session, "get_client", return_value=FakeClient()):
            out = session.http_get_json("https://api.example.invalid/x",
                                        retries=2, cancel=predicate)

        self.assertEqual(out, {"code": 0})
        self.assertIs(seen["cancel"], predicate)
        self.assertEqual(seen["retries"], 2)

    def test_http_get_json_cancel_is_not_swallowed(self):
        class FakeClient:
            def fetch_json(self, url, retries=3, cancel=None):
                raise TaskCancelledError()

        with patch.object(session, "get_client", return_value=FakeClient()):
            with self.assertRaises(TaskCancelledError):
                session.http_get_json("https://api.example.invalid/x")


class CollectorCancelTests(unittest.TestCase):
    def test_fetch_view_forwards_cancel(self):
        seen = {}

        def fake_get_json(url, retries=3, cancel=None):
            seen["url"] = url
            seen["cancel"] = cancel
            return {"code": 0, "data": {"bvid": "BV1", "stat": {}}}

        predicate = lambda: False  # noqa: E731
        with patch.object(collector_core.session, "http_get_json", fake_get_json):
            collector_core.fetch_view("BV1", cancel=predicate)

        self.assertIs(seen["cancel"], predicate)
        self.assertIn("bvid=BV1", seen["url"])

    def test_fetch_view_without_cancel_still_works(self):
        def fake_get_json(url, retries=3, cancel=None):
            return {"code": 0, "data": {"bvid": "BV1", "stat": {}}}

        with patch.object(collector_core.session, "http_get_json", fake_get_json):
            snap = collector_core.fetch_view("BV1")

        self.assertEqual(snap["bvid"], "BV1")

    def test_cancel_during_backoff_is_not_recorded_as_failure(self):
        """请求内部被取消 → 干净收尾，不得混进失败列表。"""
        calls = []

        def fake_fetch(bvid, cancel=None):
            calls.append(bvid)
            raise TaskCancelledError()

        with patch.object(collector_core, "fetch_view", fake_fetch), \
                patch.object(collector_core, "wait_or_cancel", lambda *a, **k: True):
            ok, fail = collector_core.collect_snapshot(["BV1", "BV2", "BV3"])

        self.assertEqual(ok, [])
        self.assertEqual(fail, [], "取消不是失败，不应写入失败列表")
        self.assertEqual(calls, ["BV1"], "取消后应立即停止，不再采下一个")

    def test_pending_cancel_between_videos_breaks_cleanly(self):
        state = {"cancel": False}
        calls = []

        def fake_fetch(bvid, cancel=None):
            calls.append(bvid)
            state["cancel"] = True     # 第一支采完即请求取消
            return {"bvid": bvid, "fetched_at": 0}

        with patch.object(collector_core, "fetch_view", fake_fetch):
            ok, fail = collector_core.collect_snapshot(
                ["BV1", "BV2"], cancel=lambda: state["cancel"])

        self.assertEqual([s["bvid"] for s in ok], ["BV1"])
        self.assertEqual(fail, [])
        self.assertEqual(calls, ["BV1"])

    def test_inter_video_wait_is_interruptible(self):
        state = {"cancel": False}

        def fake_fetch(bvid, cancel=None):
            return {"bvid": bvid, "fetched_at": 0}

        def fake_wait(seconds, cancel=None, **kw):
            state["cancel"] = True     # 间隔等待期间用户点了取消
            return False

        with patch.object(collector_core, "fetch_view", fake_fetch), \
                patch.object(collector_core, "wait_or_cancel", fake_wait):
            ok, fail = collector_core.collect_snapshot(
                ["BV1", "BV2"], cancel=lambda: state["cancel"])

        self.assertEqual([s["bvid"] for s in ok], ["BV1"])
        self.assertEqual(fail, [])

    def test_risk_challenge_still_raises_with_resume_index(self):
        """风控挑战的断点语义不得被取消逻辑改动。"""
        from core.risk import RiskChallengeError

        def fake_fetch(bvid, cancel=None):
            raise RiskChallengeError("voucher")

        with patch.object(collector_core, "fetch_view", fake_fetch), \
                patch.object(collector_core, "wait_or_cancel", lambda *a, **k: True):
            with self.assertRaises(RiskChallengeError) as ctx:
                collector_core.collect_snapshot(["BV1", "BV2", "BV3"])

        self.assertEqual(ctx.exception.resume_index, 0)

    def test_ordinary_failure_still_lands_in_fail_list(self):
        def fake_fetch(bvid, cancel=None):
            raise ValueError("boom")

        with patch.object(collector_core, "fetch_view", fake_fetch), \
                patch.object(collector_core, "wait_or_cancel", lambda *a, **k: True):
            ok, fail = collector_core.collect_snapshot(["BV1", "BV2"])

        self.assertEqual(ok, [])
        self.assertEqual([b for b, _ in fail], ["BV1", "BV2"])


class ClientCancelDelegateTests(unittest.TestCase):
    def test_client_wait_uses_injected_sleep(self):
        client = BiliClient(ProxyPool(None), sleep=RecordingSleep(),
                            clock=lambda: 0.0)

        self.assertTrue(client._wait(0.5))
        self.assertAlmostEqual(client._sleep.total, 0.5, places=6)

    def test_client_wait_honours_argument_predicate(self):
        client = BiliClient(ProxyPool(None), sleep=RecordingSleep(),
                            clock=lambda: 0.0, cancel=lambda: False)

        self.assertFalse(client._wait(1.0, cancel=lambda: True))
        self.assertEqual(client._sleep.calls, 0)


if __name__ == "__main__":
    unittest.main()
