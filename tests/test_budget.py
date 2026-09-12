# -*- coding: utf-8 -*-
"""任务预算（core/budget.py）与三个采集工具到限行为的离线测试。

锁定"可观察行为"：
1. TaskBudget 三类上限各自到限与未到限不误伤；status() 计数正确；
   无上限字段时永不到限；BudgetExhaustedError 不属于 TransportError 系。
2. BiliClient._request 的强制点：cancel 之后、gate.acquire 之前；一次
   fetch_* 调用（含内部重试）只检查/记账一次；到限异常不进 except 链，
   不改变 gate 状态、不计入失败统计；cancel 与预算同时到期时按取消语义。
3. 三个采集工具到限后按正常完成收尾：stats["stopped_reason"]=="budget_reached"，
   已写 JSONL 行数完整、Excel 照常生成；取消场景不得记成预算停止。

全程离线：clock/sleep/fetch 全部注入，不打网络、不真等待。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.budget import (DEFAULT_MAX_MINUTES, DEFAULT_MAX_REQUESTS,
                         MAX_MINUTES_LIMIT, MAX_REQUESTS_LIMIT,
                         BudgetExhaustedError, TaskBudget)
from core.cancel import TaskCancelledError
from core.client import BiliClient
from core.proxy import ProxyPool
from core.transport import TransportError
from core.wbi import WbiKeyCache

from tools.collector import core as collector_core
from tools.collector import pipeline as collector_pipeline
from tools.danmaku import core as danmaku_core
from tools.danmaku import danmaku_pb2
from tools.danmaku import pipeline as danmaku_pipeline
from tools.user_dynamics import core as dynamics_core
from tools.user_dynamics import pipeline as dynamics_pipeline


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def noop_sleep(_seconds):
    pass


# ---------- 合成 fixture（形状来自既有实测测试，内容全部合成） ----------

def nav_payload():
    return {"code": -101, "data": {"wbi_img": {
        "img_url": f"https://i0.hdslb.com/bfs/wbi/{'a' * 32}.png",
        "sub_url": f"https://i0.hdslb.com/bfs/wbi/{'b' * 32}.png"}}}


def dyn_item(dyn_id):
    return {
        "id_str": str(dyn_id),
        "type": "DYNAMIC_TYPE_AV",
        "visible": True,
        "modules": {
            "module_author": {"mid": 1, "name": "某UP", "pub_ts": "1700000000"},
            "module_dynamic": {
                "desc": None,
                "major": {"type": "MAJOR_TYPE_ARCHIVE",
                          "archive": {"title": f"标题{dyn_id}", "bvid": f"BV{dyn_id}",
                                      "jump_url": "", "desc": "", "aid": "1"}},
            },
            "module_stat": {"forward": {"count": "1"}, "comment": {"count": "2"},
                            "like": {"count": "3"}, "coin": {"count": "0"},
                            "favorite": {"count": "0"}},
        },
    }


def dyn_page(dyn_ids, has_more=False, offset=""):
    return {"code": 0, "message": "0",
            "data": {"items": [dyn_item(i) for i in dyn_ids],
                     "has_more": has_more, "offset": offset}}


def view_payload(claimed=10, pages=None):
    data = {"bvid": "BV1GJ411x7h7", "aid": 1, "cid": 101, "duration": 213,
            "title": "测试视频", "owner": {"name": "UP"},
            "stat": {"danmaku": claimed}}
    if pages:
        data["pages"] = pages
    return {"code": 0, "message": "0", "data": data}


def make_elem(elem_id, content="666"):
    e = danmaku_pb2.DanmakuElem()
    e.id = elem_id
    e.idStr = str(elem_id)
    e.progress = 1000 * elem_id
    e.mode = 1
    e.color = 16777215
    e.ctime = 1600000000
    e.pool = 0
    e.midHash = "hash"
    e.content = content
    return e


def make_segment(*elems):
    reply = danmaku_pb2.DmSegMobileReply()
    reply.elems.extend(elems)
    return reply.SerializeToString()


def snapshot(bvid):
    return {"bvid": bvid, "aid": 1, "title": f"标题-{bvid}", "owner": "UP",
            "owner_mid": 2, "tname": "测试", "pubdate": 1600000000,
            "duration": 60, "view": 100, "danmaku": 1, "reply": 2,
            "favorite": 3, "coin": 4, "share": 5, "like": 10,
            "fetched_at": 1700000000}


# ---------- TaskBudget 单元 ----------

class TaskBudgetTests(unittest.TestCase):
    def test_no_limits_never_expires(self):
        budget = TaskBudget(clock=FakeClock())
        for _ in range(5):
            budget.check_request()          # 不抛
            budget.observe_request()
        budget.observe_records(10 ** 6)
        self.assertFalse(budget.expired())
        self.assertIsNone(budget.reason())

    def test_request_limit_blocks_after_last_allowed_request(self):
        budget = TaskBudget(max_requests=2, clock=FakeClock())
        budget.check_request()
        budget.observe_request()
        budget.check_request()              # 第 2 次仍放行
        budget.observe_request()
        self.assertTrue(budget.expired())
        self.assertIn("请求数", budget.reason())
        with self.assertRaises(BudgetExhaustedError):
            budget.check_request()

    def test_zero_request_limit_blocks_even_the_first_request(self):
        budget = TaskBudget(max_requests=0, clock=FakeClock())
        with self.assertRaises(BudgetExhaustedError):
            budget.check_request()

    def test_duration_limit_uses_injected_clock(self):
        clock = FakeClock()
        budget = TaskBudget(max_seconds=60, clock=clock)
        budget.check_request()
        self.assertFalse(budget.expired())
        clock.advance(59.9)
        self.assertFalse(budget.expired())
        clock.advance(0.1)
        self.assertTrue(budget.expired())
        self.assertIn("时长", budget.reason())
        with self.assertRaises(BudgetExhaustedError):
            budget.check_request()

    def test_record_limit_counts_accumulated_records(self):
        budget = TaskBudget(max_records=10, clock=FakeClock())
        budget.observe_records(4)
        self.assertFalse(budget.expired())
        budget.observe_records(6)
        self.assertTrue(budget.expired())
        self.assertIn("记录数", budget.reason())

    def test_observe_records_ignores_non_positive(self):
        budget = TaskBudget(max_records=5, clock=FakeClock())
        budget.observe_records(0)
        budget.observe_records(-3)
        self.assertEqual(budget.records, 0)
        self.assertFalse(budget.expired())

    def test_status_counts_are_accurate(self):
        clock = FakeClock()
        budget = TaskBudget(max_requests=10, max_seconds=120,
                            max_records=100, clock=clock)
        budget.observe_request()
        budget.observe_request()
        budget.observe_records(7)
        clock.advance(30)
        status = budget.status()
        self.assertEqual(status["requests"], 2)
        self.assertEqual(status["max_requests"], 10)
        self.assertEqual(status["records"], 7)
        self.assertEqual(status["max_records"], 100)
        self.assertEqual(status["elapsed_seconds"], 30)
        self.assertEqual(status["max_seconds"], 120)
        self.assertFalse(status["expired"])
        self.assertIsNone(status["reason"])

    def test_error_is_not_in_transport_or_cancel_family(self):
        self.assertFalse(issubclass(BudgetExhaustedError, TransportError))
        self.assertFalse(issubclass(BudgetExhaustedError, TaskCancelledError))

    def test_defaults_match_task_card(self):
        self.assertEqual(DEFAULT_MAX_REQUESTS, 20000)
        self.assertEqual(DEFAULT_MAX_MINUTES, 240)
        self.assertEqual(MAX_REQUESTS_LIMIT, 100000)
        self.assertEqual(MAX_MINUTES_LIMIT, 1440)


# ---------- BiliClient 强制点 ----------

class StubTransport:
    """按脚本回放响应/异常；脚本用尽后重复最后一项。"""

    name = "stub"

    def __init__(self, effects):
        self._effects = list(effects)
        self.calls = 0

    def get_json(self, url):
        return self._next()

    def get_bytes(self, url):
        return self._next()

    def _next(self):
        self.calls += 1
        item = self._effects[min(self.calls - 1, len(self._effects) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def warmup(self, force=False):
        pass

    def close(self):
        pass

    def get_cookies(self):
        return {}


def make_client(transport, cancel=None):
    client = BiliClient(ProxyPool(None), cookie_path=None,
                        log=lambda msg: None, clock=lambda: 0.0,
                        sleep=noop_sleep, cancel=cancel)
    client._get_transport = lambda: transport
    return client


class ClientBudgetTests(unittest.TestCase):
    def test_exhausted_budget_raises_before_transport_and_gate(self):
        transport = StubTransport([{"code": 0}])
        client = make_client(transport)
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        budget.observe_request()                    # 已到限

        with self.assertRaises(BudgetExhaustedError):
            client.fetch_json("https://api.example.invalid/x", budget=budget)

        self.assertEqual(transport.calls, 0, "到限后不得发出任何传输请求")
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.stats["net_errors"], 0)
        self.assertEqual(client.stats["risk_events"], 0)

    def test_budget_counts_one_per_call_including_internal_retries(self):
        transport = StubTransport([TransportError("boom"), {"code": 0}])
        client = make_client(transport)
        budget = TaskBudget(max_requests=1, clock=FakeClock())

        client.fetch_json("https://api.example.invalid/x", budget=budget)

        self.assertEqual(transport.calls, 2, "内部重试要真的发生")
        self.assertEqual(budget.requests, 1, "一次 fetch_* 调用只计一次请求")

    def test_budget_expiry_is_not_counted_as_failure_or_gate_block(self):
        transport = StubTransport([{"code": 0}])
        client = make_client(transport)
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        client.fetch_json("https://api.example.invalid/x", budget=budget)
        gate_before = client.gate.status()

        with self.assertRaises(BudgetExhaustedError):
            client.fetch_json("https://api.example.invalid/x", budget=budget)

        self.assertEqual(client.gate.status(), gate_before,
                         "预算到限不得改变闸门状态（熔断/统计口径）")
        self.assertEqual(client.stats["net_errors"], 0)
        self.assertEqual(client.stats["rate_limit_events"], 0)

    def test_cancel_outranks_exhausted_budget(self):
        transport = StubTransport([{"code": 0}])
        client = make_client(transport, cancel=lambda: True)
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        budget.observe_request()

        with self.assertRaises(TaskCancelledError):
            client.fetch_json("https://api.example.invalid/x", budget=budget)
        self.assertEqual(transport.calls, 0)

    def test_bytes_channel_shares_the_same_checkpoint(self):
        transport = StubTransport([b"data"])
        client = make_client(transport)
        budget = TaskBudget(max_requests=1, clock=FakeClock())

        self.assertEqual(client.fetch_bytes("https://api.example.invalid/x",
                                            budget=budget), b"data")
        with self.assertRaises(BudgetExhaustedError):
            client.fetch_bytes("https://api.example.invalid/x", budget=budget)
        self.assertEqual(budget.requests, 1)


# ---------- 用户动态 ----------

class DynamicsBudgetTests(unittest.TestCase):
    def make_crawler(self, tmp, pages, budget, has_more=True):
        calls = {"n": 0}

        def fetch(url, **kw):
            budget.observe_request()
            calls["n"] += 1
            if calls["n"] < len(pages):
                return dyn_page(pages[calls["n"] - 1], has_more=has_more,
                                offset="next")
            return dyn_page(pages[calls["n"] - 1], has_more=False)

        crawler = dynamics_core.DynamicsCrawler(
            946974, tmp, max_pages=10, sleep=0, fetch=fetch,
            sleeper=noop_sleep,
            key_cache=WbiKeyCache(fetch=lambda: ("a" * 32, "b" * 32)),
            budget=budget)
        return crawler, calls

    def test_soft_boundary_stops_with_budget_reason_and_keeps_jsonl(self):
        with tempfile.TemporaryDirectory(prefix="budget_dyn_") as tmp:
            budget = TaskBudget(max_requests=1, clock=FakeClock())
            crawler, calls = self.make_crawler(
                tmp, [[1, 2, 3], [4, 5, 6]], budget)

            stats = crawler.crawl()

            self.assertEqual(calls["n"], 1, "到限后不得再发第二次请求")
            self.assertEqual(stats.get("stopped_reason"), "budget_reached")
            self.assertFalse(stats.get("cancelled"))
            self.assertEqual(len(crawler.rows), 3, "第一页数据必须完整保留")
            lines = Path(crawler.out_path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3, "JSONL 已写行数与断点状态必须完整")

    def test_hard_stop_from_http_layer_keeps_flushed_rows(self):
        with tempfile.TemporaryDirectory(prefix="budget_dyn_") as tmp:
            budget = TaskBudget(max_requests=100, clock=FakeClock())
            state = {"n": 0}

            def fetch(url, **kw):
                state["n"] += 1
                if state["n"] == 1:
                    return dyn_page([1, 2], has_more=True, offset="next")
                raise BudgetExhaustedError("任务预算到限")

            crawler = dynamics_core.DynamicsCrawler(
                946974, tmp, max_pages=10, sleep=0, fetch=fetch,
                sleeper=noop_sleep,
                key_cache=WbiKeyCache(fetch=lambda: ("a" * 32, "b" * 32)),
                budget=budget)

            stats = crawler.crawl()

            self.assertEqual(stats.get("stopped_reason"), "budget_reached")
            self.assertFalse(stats.get("cancelled"), "预算停止不得记成取消")
            self.assertEqual(len(crawler.rows), 2)
            self.assertTrue(Path(crawler.out_path).exists(), "兜底路径也要落盘")

    def test_cancel_is_never_marked_as_budget_stop(self):
        with tempfile.TemporaryDirectory(prefix="budget_dyn_") as tmp:
            budget = TaskBudget(max_requests=100, clock=FakeClock())

            def fetch(url, **kw):
                return dyn_page([1], has_more=True, offset="next")

            crawler = dynamics_core.DynamicsCrawler(
                946974, tmp, max_pages=10, sleep=0, fetch=fetch,
                sleeper=noop_sleep,
                key_cache=WbiKeyCache(fetch=lambda: ("a" * 32, "b" * 32)),
                budget=budget, cancel=lambda: True)

            stats = crawler.crawl()

            self.assertTrue(stats.get("cancelled"))
            self.assertNotIn("stopped_reason", stats)

    def test_pipeline_reports_budget_stop_and_writes_excel(self):
        with tempfile.TemporaryDirectory(prefix="budget_dyn_pipe_") as tmp:
            pages = [dyn_page([1, 2, 3], has_more=True, offset="next"),
                     dyn_page([4, 5], has_more=False)]

            def fake_http(url, **kw):
                if "nav" in url:
                    return nav_payload()
                budget = kw.get("budget")
                if budget is not None:
                    budget.observe_request()
                return pages.pop(0)

            with patch("core.session.http_get_json", side_effect=fake_http):
                result = dynamics_pipeline.run_pipeline(
                    "946974", tmp, sleep=0, max_requests=1)

            stats = result["stats"]
            self.assertEqual(stats.get("stopped_reason"), "budget_reached")
            self.assertEqual(result["rows"], 3)
            self.assertTrue(Path(result["xlsx"]).exists(), "到限后 Excel 必须照常生成")
            self.assertTrue(Path(result["jsonl"]).exists())


# ---------- 弹幕 ----------

class DanmakuBudgetTests(unittest.TestCase):
    def test_segment_loop_stops_at_budget_and_keeps_jsonl(self):
        with tempfile.TemporaryDirectory(prefix="budget_dm_") as tmp:
            budget = TaskBudget(max_requests=1, clock=FakeClock())
            calls = {"n": 0}

            def fetch(url, **kw):
                budget.observe_request()
                calls["n"] += 1
                return make_segment(make_elem(1), make_elem(2))

            crawler = danmaku_core.DanmakuCrawler(
                101, tmp, duration=0, max_segments=10, sleep=0,
                fetch=fetch, sleeper=noop_sleep, name="t", budget=budget)

            stats = crawler.crawl()

            self.assertEqual(calls["n"], 1)
            self.assertEqual(stats.get("stopped_reason"), "budget_reached")
            self.assertFalse(stats.get("cancelled"))
            self.assertEqual(len(crawler.rows), 2)
            lines = Path(crawler.out_path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)

    def test_pipeline_budget_stop_before_any_segment(self):
        with tempfile.TemporaryDirectory(prefix="budget_dm_pipe_") as tmp:
            def fake_json(url, **kw):
                budget = kw.get("budget")
                if budget is not None:
                    budget.observe_request()
                return view_payload(claimed=10)

            def fake_bytes(url, **kw):
                budget = kw.get("budget")
                if budget is not None:
                    budget.observe_request()
                return make_segment(make_elem(1))

            with patch("core.session.http_get_json", side_effect=fake_json), \
                    patch("core.session.http_get_bytes", side_effect=fake_bytes):
                result = danmaku_pipeline.run_pipeline(
                    "BV1GJ411x7h7", tmp, sleep=0, max_requests=1)

            self.assertEqual(result["rows"], 0)
            self.assertEqual(result["stats"].get("stopped_reason"),
                             "budget_reached")
            self.assertFalse(result["stats"].get("cancelled"))
            self.assertTrue(Path(result["xlsx"]).exists(),
                            "0 行也要交一份说明性 Excel，且不是失败")

    def test_pipeline_budget_stops_between_parts(self):
        pages = [{"cid": 101, "duration": 213, "part": "P1"},
                 {"cid": 202, "duration": 213, "part": "P2"},
                 {"cid": 303, "duration": 213, "part": "P3"}]
        with tempfile.TemporaryDirectory(prefix="budget_dm_parts_") as tmp:
            def fake_json(url, **kw):
                budget = kw.get("budget")
                if budget is not None:
                    budget.observe_request()
                return view_payload(claimed=10, pages=pages)

            def fake_bytes(url, **kw):
                budget = kw.get("budget")
                if budget is not None:
                    budget.observe_request()
                idx = int(re.search(r"segment_index=(\d+)", url).group(1))
                return make_segment(make_elem(1)) if idx == 1 else b""

            with patch("core.session.http_get_json", side_effect=fake_json), \
                    patch("core.session.http_get_bytes", side_effect=fake_bytes):
                result = danmaku_pipeline.run_pipeline(
                    "BV1GJ411x7h7", tmp, sleep=0, all_pages=True,
                    max_requests=3)   # meta(1) + P1段1(2) + P1段2空(3) → 到限

            self.assertEqual(len(result["parts"]), 1, "P2/P3 不得再抓")
            self.assertEqual(result["stats"].get("stopped_reason"),
                             "budget_reached")
            self.assertEqual(result["rows"], 1)
            self.assertTrue(Path(result["xlsx"]).exists())

    def test_pipeline_cancel_is_not_marked_as_budget(self):
        with tempfile.TemporaryDirectory(prefix="budget_dm_cancel_") as tmp:
            with patch("core.session.http_get_json",
                       side_effect=TaskCancelledError()):
                result = danmaku_pipeline.run_pipeline(
                    "BV1GJ411x7h7", tmp, cancel=lambda: True)

            self.assertTrue(result["stats"].get("cancelled"))
            self.assertNotIn("stopped_reason", result["stats"])


# ---------- 视频采集 ----------

class CollectorBudgetTests(unittest.TestCase):
    def test_expired_budget_requests_nothing(self):
        with tempfile.TemporaryDirectory(prefix="budget_col_") as tmp:
            budget = TaskBudget(max_requests=1, clock=FakeClock())
            budget.observe_request()
            with patch.object(collector_core, "fetch_view") as fetch_view:
                ok, fail = collector_core.collect_snapshot(
                    ["BV1", "BV2"], sleep=0, budget=budget,
                    snapshot_path=Path(tmp) / "snapshots.jsonl")
            fetch_view.assert_not_called()
            self.assertEqual((ok, fail), ([], []))

    def test_soft_boundary_keeps_snapshots_and_records(self):
        with tempfile.TemporaryDirectory(prefix="budget_col_") as tmp:
            budget = TaskBudget(max_requests=2, clock=FakeClock())
            requested = []

            def fake_fetch_view(bvid, cancel=None, budget=None):
                budget.observe_request()
                requested.append(bvid)
                return snapshot(bvid)

            snap_path = Path(tmp) / "snapshots.jsonl"
            with patch.object(collector_core, "fetch_view",
                              side_effect=fake_fetch_view):
                ok, fail = collector_core.collect_snapshot(
                    ["BV1", "BV2", "BV3"], sleep=0, budget=budget,
                    snapshot_path=snap_path)

            self.assertEqual(requested, ["BV1", "BV2"], "第 3 个视频不得请求")
            self.assertEqual([s["bvid"] for s in ok], ["BV1", "BV2"])
            self.assertEqual(fail, [], "预算到限不是失败")
            self.assertEqual(budget.records, 2, "落盘快照要记账记录数")
            lines = snap_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2, "断点文件必须完整")

    def test_http_layer_hard_stop_is_not_failure(self):
        with tempfile.TemporaryDirectory(prefix="budget_col_") as tmp:
            budget = TaskBudget(max_requests=100, clock=FakeClock())

            def fake_fetch_view(bvid, cancel=None, budget=None):
                raise BudgetExhaustedError("任务预算到限")

            with patch.object(collector_core, "fetch_view",
                              side_effect=fake_fetch_view):
                ok, fail = collector_core.collect_snapshot(
                    ["BV1", "BV2"], sleep=0, budget=budget)
            self.assertEqual(ok, [])
            self.assertEqual(fail, [], "BudgetExhaustedError 不得混进失败列表")

    def _run_pipeline(self, tmp, collect, **kwargs):
        with patch.object(collector_pipeline.session, "ensure_ready"), \
                patch.object(collector_pipeline.links, "expand_source",
                             side_effect=lambda line, progress=None, **kwargs:
                             [("BV1", ""), ("BV2", "")]), \
                patch.object(collector_pipeline.core, "collect_snapshot",
                             side_effect=collect), \
                patch.object(collector_pipeline.core, "export_xlsx"):
            return collector_pipeline.run_pipeline(
                ["BV1"], tmp, sleep=0, progress=lambda **kw: None, **kwargs)

    @staticmethod
    def _collect_with_disk(budget_requests=1, attempt_ratio=1.0):
        """fake collect_snapshot：与真实实现一样把快照落盘并记账预算。"""

        def collect(values, progress, snapshot_path, **kwargs):
            budget = kwargs.get("budget")
            snaps = []
            for bv in values:
                if budget is not None:
                    budget.observe_request()
                value = snapshot(bv)
                snaps.append(value)
                with open(snapshot_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(value, ensure_ascii=False) + "\n")
                if budget is not None and budget.expired():
                    break
            return snaps, []

        return collect

    def test_pipeline_monitor_stops_before_next_round(self):
        with tempfile.TemporaryDirectory(prefix="budget_col_pipe_") as tmp:
            result = self._run_pipeline(
                tmp, self._collect_with_disk(), monitor=True, rounds=5,
                interval_min=0, max_requests=1)

            self.assertEqual(result["rounds"], 1, "到限后不得开下一轮")
            self.assertEqual(result.get("stopped_reason"), "budget_reached")

    def test_pipeline_single_round_still_records_budget_stop(self):
        with tempfile.TemporaryDirectory(prefix="budget_col_once_") as tmp:
            result = self._run_pipeline(
                tmp, self._collect_with_disk(), monitor=False, rounds=1,
                max_requests=1)

            self.assertEqual(result.get("stopped_reason"), "budget_reached",
                             "单次快照模式同样要记预算停止")

    def test_pipeline_cancel_is_not_marked_as_budget(self):
        with tempfile.TemporaryDirectory(prefix="budget_col_cancel_") as tmp:
            def collect(values, progress, snapshot_path, **kwargs):
                for bv in values:
                    value = snapshot(bv)
                    with open(snapshot_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(value, ensure_ascii=False) + "\n")
                return [], []

            result = self._run_pipeline(
                tmp, collect, monitor=False, rounds=1, cancel=lambda: True)

            self.assertIsNone(result.get("stopped_reason"))


if __name__ == "__main__":
    unittest.main()
