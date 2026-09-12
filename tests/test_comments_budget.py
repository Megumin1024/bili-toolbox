# -*- coding: utf-8 -*-
"""评论工具任务预算：gRPC 通道记账、到限安全停止、断点保留、取消优先。

全程离线：预算记账点在真实的 Crawler._call 内（与 core.client 的
attempt==0 口径一致），假 stub 只回放响应、**不补记账**——记账走的是被测
的真实通道代码，这正是与此前轮次踩过的「fake 通道漏记账导致到限断言失真」
的区别：那是记账点在 client 里、测试把 client patch 掉的场景。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.budget import TaskBudget
from core.gate import RequestGate
from tools.comments.core import Crawler, TaskCancelled
from tools.comments import pipeline
from tools.comments.page import CommentsPage


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def fake_reply(rid, count=0):
    """norm_grpc 需要的全部字段；默认 count=0 不产生楼中楼待办。"""
    return SimpleNamespace(
        id=rid, parent=0, root=0,
        member=SimpleNamespace(name=f"用户{rid}", mid=1000 + rid, sex="保密",
                               level=6, vip_status=0),
        content=SimpleNamespace(message=f"评论内容{rid}"),
        like=rid, count=count, ctime=1_700_000_000 + rid,
        reply_control=SimpleNamespace(location="IP属地：火星",
                                      is_up_top=False, is_admin_top=False))


def main_page(next_cursor, is_end=False, replies=()):
    return SimpleNamespace(replies=list(replies),
                           cursor=SimpleNamespace(isEnd=is_end,
                                                  next=next_cursor))


def detail_page(replies=()):
    root = SimpleNamespace(replies=list(replies)) if replies else None
    return SimpleNamespace(root=root,
                           cursor=SimpleNamespace(isEnd=True, next=0))


class CrawlerBudgetCase(unittest.TestCase):
    def setUp(self):
        self.out_root = Path(tempfile.mkdtemp(prefix="comments_budget_"))
        self.addCleanup(shutil.rmtree, self.out_root, ignore_errors=True)
        self.gate = RequestGate(min_interval=0.0, clock=lambda: 0.0,
                                sleep=lambda _seconds: None)
        self.logs = []

    def make(self, budget=None, cancel=None):
        crawler = Crawler(1, 17, self.out_root / "out", sleep=0.0,
                          progress=lambda **k: self.logs.append(k),
                          cancel=cancel or (lambda: False),
                          metadata=[("user-agent", "test")],
                          gate=self.gate, sleeper=lambda _seconds: None,
                          budget=budget)
        self.addCleanup(crawler.channel.close)
        return crawler

    def script(self, *effects):
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


class BudgetStopTests(CrawlerBudgetCase):
    def test_budget_exhaustion_stops_before_the_next_rpc(self):
        budget = TaskBudget(max_requests=2, clock=FakeClock())
        crawler = self.make(budget=budget)
        crawler.stub = SimpleNamespace(
            MainList=self.script(
                main_page(10, replies=[fake_reply(1)]),
                # 第二页带楼中楼待办：到限后不许再进楼中楼阶段
                main_page(20, replies=[fake_reply(2, count=3)]),
                main_page(30, replies=[fake_reply(3)])),
            DetailList=self.script())

        stats = crawler.crawl()

        self.assertEqual(stats["stopped_reason"], "budget_reached")
        self.assertEqual(stats["status"], "completed", "到限按正常完成收尾")
        self.assertFalse(stats["aborted"])
        self.assertFalse(stats["cancelled"])
        self.assertIsNone(stats["error"])
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(budget.requests, 2, "一次 _call 调用只记一次账")
        self.assertEqual(len(crawler.stub.MainList.calls), 2,
                         "到限后不再发出下一次 RPC")
        self.assertEqual(len(crawler.stub.DetailList.calls), 0,
                         "主楼到限后不再进入楼中楼阶段")
        self.assertEqual(
            sum(1 for k in self.logs if "已达预算上限" in k.get("text", "")),
            1, "到限警告只报一次")
        lines = crawler.out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "已抓页完整落盘")
        ck = json.loads(crawler.ckpt_path.read_text(encoding="utf-8"))
        self.assertEqual(ck["cursor_next"], 20, "断点停在未抓的下一页游标")
        self.assertEqual(ck["phase"], "main")
        self.assertFalse(ck["aborted"], "预算停止不是中断标记")
        self.assertFalse(ck["cancelled"])

    def test_budget_stop_checkpoint_resumes_with_a_fresh_budget(self):
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        first = self.make(budget=budget)
        first.stub = SimpleNamespace(
            MainList=self.script(main_page(50, replies=[fake_reply(1)])),
            DetailList=self.script())
        self.assertEqual(first.crawl()["stopped_reason"], "budget_reached")

        second = self.make()  # 同一输出目录：构造时自动读回断点
        second.stub = SimpleNamespace(
            MainList=self.script(
                main_page(60, is_end=True, replies=[fake_reply(2)])),
            DetailList=self.script())
        resumed = second.crawl()

        self.assertEqual(resumed["status"], "completed")
        self.assertNotIn("stopped_reason", resumed)
        self.assertEqual(resumed["pages"], 2, "页数计数跨两次运行累计")
        self.assertEqual(resumed["main"], 2)
        self.assertEqual(second.ckpt["cursor_next"], 60)

    def test_budget_stop_mid_subtree_keeps_the_root_cursor(self):
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        crawler = self.make(budget=budget)
        # 预置断点：主楼已完成，楼中楼停在 root 123 的第 1 页之后
        crawler.ckpt = {"phase": "sub", "cursor_next": 0, "main_done": 3,
                        "sub_done": 1, "pending_roots": [[123, 5], [456, 2]],
                        "next_root_index": 0, "sub_cursor_next": 1,
                        "pages": 3, "aborted": False, "cancelled": False}
        crawler.stub = SimpleNamespace(
            MainList=self.script(),
            DetailList=self.script(SimpleNamespace(
                root=SimpleNamespace(replies=[fake_reply(11)]),
                cursor=SimpleNamespace(isEnd=False, next=7))))

        stats = crawler.crawl()

        self.assertEqual(stats["stopped_reason"], "budget_reached")
        self.assertEqual(stats["sub"], 2)
        ck = json.loads(crawler.ckpt_path.read_text(encoding="utf-8"))
        self.assertEqual(ck["phase"], "sub")
        self.assertEqual(ck["next_root_index"], 0, "没抓完的楼中楼停在原 root")
        self.assertEqual(ck["sub_cursor_next"], 7, "从上一页响应的游标续传")

    def test_time_budget_stops_between_requests(self):
        clock = FakeClock()
        budget = TaskBudget(max_seconds=100, clock=clock)
        crawler = self.make(budget=budget)

        def slow_page(req, metadata=None, timeout=None):
            clock.now += 60  # 每页推进 60 秒
            return main_page(10, replies=[fake_reply(1)])

        crawler.stub = SimpleNamespace(MainList=slow_page,
                                       DetailList=self.script())
        stats = crawler.crawl()
        self.assertEqual(stats["stopped_reason"], "budget_reached")
        self.assertEqual(stats["pages"], 2, "第 3 页前 180s ≥ 100s 到限")


class BudgetPriorityTests(CrawlerBudgetCase):
    def test_call_cancel_check_precedes_budget_check(self):
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        budget.observe_request()  # 预先把预算耗尽
        crawler = self.make(budget=budget, cancel=lambda: True)
        fn = self.script()

        with self.assertRaises(TaskCancelled):
            crawler._call(fn, object())

        self.assertEqual(len(fn.calls), 0, "取消后不应发起任何 RPC")
        self.assertEqual(self.gate.status()["acquires"], 0)
        self.assertEqual(budget.requests, 1, "取消路径不记账")

    def test_cancelled_run_is_not_marked_as_budget_stop(self):
        budget = TaskBudget(max_requests=1, clock=FakeClock())
        budget.observe_request()
        crawler = self.make(budget=budget, cancel=lambda: True)
        crawler.stub = SimpleNamespace(MainList=self.script(),
                                       DetailList=self.script())

        stats = crawler.crawl()

        self.assertEqual(stats["status"], "cancelled")
        self.assertTrue(stats["cancelled"])
        self.assertNotIn("stopped_reason", stats, "取消与预算互不冒充")


class BudgetChannelTests(CrawlerBudgetCase):
    def test_tls_channel_requests_are_counted(self):
        budget = TaskBudget(max_requests=10, clock=FakeClock())
        crawler = self.make(budget=budget)
        crawler._tls_ok = True
        tls_calls = []

        def fake_tls(fn, req):
            tls_calls.append(req)
            return main_page(1, is_end=True, replies=[fake_reply(1)])

        crawler._call_tls = fake_tls
        crawler._call(crawler.stub.MainList, object())

        self.assertEqual(budget.requests, 1, "TLS 通道与 grpcio 同一口径记账")
        self.assertEqual(len(tls_calls), 1)

    def test_budget_none_keeps_the_old_stats_shape(self):
        crawler = self.make()
        self.assertIsNone(crawler.budget)
        crawler.stub = SimpleNamespace(
            MainList=self.script(
                main_page(0, is_end=True, replies=[fake_reply(1)])),
            DetailList=self.script())

        stats = crawler.crawl()

        self.assertEqual(
            set(stats),
            {"main", "sub", "pages", "aborted", "cancelled", "status", "error"},
            "budget=None 的返回结构与引入预算前逐字段一致")
        self.assertEqual(stats["status"], "completed")


class PipelineBudgetWiringTests(unittest.TestCase):
    """run_pipeline 只负责造账本并塞给 Crawler；强制点在 Crawler._call。"""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="comments_pipe_budget_")
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def _run(self, **kwargs):
        meta = {"title": "动态标题", "author": "作者", "pub_ts": None,
                "claimed_comment_count": 3}
        captured = {}

        class FakeCrawler:
            def __init__(self, *args, **kw):
                captured["args"] = args
                captured["kw"] = kw
                Path(args[2]).mkdir(parents=True, exist_ok=True)
                self.out_path = Path(args[2]) / "comments.jsonl"
                self.out_path.touch()

            def crawl(self):
                return {"main": 0, "sub": 0, "pages": 0, "aborted": False,
                        "cancelled": False, "status": "completed",
                        "error": None}

        with patch.object(pipeline.links, "parse_link",
                          return_value=("dynamic", 123,
                                        {"source": "t.bilibili.com/123"})), \
                patch.object(pipeline.links, "get_dynamic_meta",
                             return_value=meta), \
                patch.object(pipeline.session, "grpc_metadata",
                             return_value=[]), \
                patch.object(pipeline.core, "Crawler", FakeCrawler), \
                patch.object(pipeline.core, "analyze",
                             return_value=("", {})), \
                patch.object(pipeline.core, "export_xlsx", return_value=None):
            result = pipeline.run_pipeline("t.bilibili.com/123", self.out,
                                           open_result=False, **kwargs)
        return result, captured

    def test_budget_is_constructed_and_handed_to_the_crawler(self):
        _result, captured = self._run(max_requests=77, max_minutes=5)
        budget = captured["kw"]["budget"]
        self.assertIsInstance(budget, TaskBudget)
        self.assertEqual(budget.max_requests, 77)
        self.assertEqual(budget.max_seconds, 300.0)

    def test_no_budget_params_means_budget_stays_none(self):
        result, captured = self._run()
        self.assertIsNone(captured["kw"]["budget"])
        self.assertNotIn("stopped_reason", result["stats"])


class CommentsPageParamTests(unittest.TestCase):
    """页面参数层：不建 QApplication，只验「存得下、读得出、往返不丢」。

    沿用仓库既有做法（__new__ + 假控件），不启动 GUI。
    """

    class _Edit:
        def __init__(self, text=""):
            self._t = text
            self.focused = False

        def setText(self, text):
            self._t = text

        def text(self):
            return self._t

        def setFocus(self):
            self.focused = True

    class _Check:
        def __init__(self, checked=False):
            self._c = checked

        def setChecked(self, value):
            self._c = bool(value)

        def isChecked(self):
            return self._c

    class _Row:
        def __init__(self, value=""):
            self._v = value

        def set_value(self, value):
            self._v = value

        def value(self):
            return self._v

    class _Card:
        def __init__(self):
            self.shown = None

        def show_result(self, head, links):
            self.shown = head

    def _page(self, url="t.bilibili.com/123", sleep="0.2",
              max_requests="20000", max_minutes="240"):
        page = CommentsPage.__new__(CommentsPage)
        page.link_edit = self._Edit(url)
        page.out_row = self._Row("D:\\out")
        page.sleep_edit = self._Edit(sleep)
        page.max_requests_edit = self._Edit(max_requests)
        page.max_minutes_edit = self._Edit(max_minutes)
        page.tls_check = self._Check(False)
        page.auto_open = self._Check(True)
        return page

    def test_budget_inputs_default_to_core_budget_defaults(self):
        params = self._page().collect_params()
        self.assertEqual(params["max_requests"], 20000)
        self.assertEqual(params["max_minutes"], 240)

    def test_blank_budget_inputs_fall_back_to_defaults(self):
        params = self._page(max_requests="", max_minutes="").collect_params()
        self.assertEqual(params["max_requests"], 20000)
        self.assertEqual(params["max_minutes"], 240)

    def test_budget_ranges_are_enforced_before_the_task_starts(self):
        for bad in ("0", "100001", "abc", "1.5x"):
            with self.assertRaises(ValueError, msg=f"应拒绝输入：{bad!r}"):
                self._page(max_requests=bad).collect_params()
        with self.assertRaises(ValueError):
            self._page(max_minutes="0").collect_params()
        with self.assertRaises(ValueError):
            self._page(max_minutes="1441").collect_params()
        ok = self._page(max_requests="100000", max_minutes="1440")
        self.assertEqual(ok.collect_params()["max_requests"], 100000)
        self.assertEqual(ok.collect_params()["max_minutes"], 1440)

    def test_budget_params_round_trip_through_history(self):
        page = self._page(max_requests="500", max_minutes="30")
        reusable = page.history_reusable_params(page.collect_params())
        self.assertEqual(reusable["max_requests"], 500)
        self.assertEqual(reusable["max_minutes"], 30)
        fresh = self._page()
        fresh.apply_reusable_params(reusable)
        self.assertEqual(fresh.max_requests_edit.text(), "500")
        self.assertEqual(fresh.max_minutes_edit.text(), "30")

    def test_preset_params_include_the_budget_whitelist(self):
        params = self._page(max_requests="500", max_minutes="30") \
            .collect_preset_params()
        self.assertEqual(params["max_requests"], 500)
        self.assertEqual(params["max_minutes"], 30)
        self.assertNotIn("max_pages", params, "预设沿用既有白名单形状")

    def test_finished_summary_mentions_safe_budget_stop(self):
        page = self._page()
        page.result_card = self._Card()
        page.on_finished({"rows": 5,
                          "stats": {"main": 3, "sub": 2,
                                    "stopped_reason": "budget_reached"},
                          "xlsx": "a", "report": "b", "jsonl": "c",
                          "dir": "d"})
        self.assertIn("已达上限安全停止", page.result_card.shown)

        plain = self._page()
        plain.result_card = self._Card()
        plain.on_finished({"rows": 5,
                           "stats": {"main": 3, "sub": 2},
                           "xlsx": "a", "report": "b", "jsonl": "c",
                           "dir": "d"})
        self.assertNotIn("已达上限", plain.result_card.shown)


if __name__ == "__main__":
    unittest.main()
