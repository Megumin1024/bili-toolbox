# -*- coding: utf-8 -*-
"""评论取消断点续跑回归测试，不建立 gRPC 网络连接。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import grpc

from core.cancel import CANCEL_POLL_SLICE
from core.gate import RequestGate
from tools.comments import core, pipeline


def permissive_gate():
    """离线闸门：不真等待、不依赖真实时钟，只保证 _call 的接线存在。"""
    return RequestGate(min_interval=0.0, clock=lambda: 0.0,
                       sleep=lambda _seconds: None)


class FakeChannel:
    def close(self):
        pass


class FakeCursor:
    isEnd = True
    next = 0


class FakeDetailResponse:
    root = None
    cursor = FakeCursor()


class FakeRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


def make_crawler(root):
    crawler = core.Crawler.__new__(core.Crawler)
    crawler.oid = 1
    crawler.rtype = 1
    crawler.out_dir = Path(root)
    crawler.sleep = 0
    crawler.max_pages = 0
    crawler.progress = lambda **_kwargs: None
    crawler.out_path = crawler.out_dir / "comments.jsonl"
    crawler.ckpt_path = crawler.out_dir / "checkpoint.json"
    crawler.channel = FakeChannel()
    crawler.stub = SimpleNamespace(MainList=object(), DetailList=object())
    crawler.md = []
    crawler._tls_ok = False
    crawler.gate = permissive_gate()
    crawler._sleep_fn = lambda _seconds: None
    return crawler


class CommentResumeTests(unittest.TestCase):
    def test_cancel_during_final_backoff_raises_task_cancelled(self):
        crawler = core.Crawler.__new__(core.Crawler)
        state = {"cancelled": False}
        cancel_checks = []

        def cancel():
            cancel_checks.append(state["cancelled"])
            return state["cancelled"]

        def fail_rpc(*_args, **_kwargs):
            raise FakeRpcError()

        def sleeper(seconds):
            # 退避按 0.25s 切片，取消才能在一秒内生效
            self.assertAlmostEqual(seconds, CANCEL_POLL_SLICE)
            state["cancelled"] = True

        crawler.cancel = cancel
        crawler.progress = lambda **_kwargs: None
        crawler.md = []
        crawler._tls_ok = False
        crawler.gate = permissive_gate()
        crawler._sleep_fn = sleeper
        with self.assertRaises(core.TaskCancelled):
            crawler._call(fail_rpc, object(), tries=1)

        # 取消检查点：循环开头、退避开头、退避首片之前各一次为 False，
        # 首片睡完后（sleeper 置位）立刻变 True 并中断，不等满 2s。
        self.assertEqual(cancel_checks, [False, False, False, True])

    def test_cancel_checkpoint_resumes_main_and_reaches_sub(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_crawler(root)
            first.ckpt = first._load_ckpt()

            def fake_main(_out):
                first.ckpt.update(
                    phase="sub",
                    pending_roots=[[123, 1]],
                    next_root_index=0,
                    sub_cursor_next=0,
                )
                first.cancel = lambda: True

            first._phase_main = fake_main
            first.cancel = lambda: False
            cancelled_stats = first.crawl()

            checkpoint = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertTrue(cancelled_stats["cancelled"])
            self.assertEqual(cancelled_stats["status"], "cancelled")
            self.assertIsNone(cancelled_stats["error"])
            self.assertTrue(checkpoint["aborted"])
            self.assertTrue(checkpoint["cancelled"])
            self.assertEqual(checkpoint["phase"], "sub")

            resumed = make_crawler(root)
            resumed.ckpt = resumed._load_ckpt()

            def fake_call(fn, _request):
                self.assertIs(fn, resumed.stub.DetailList)
                return FakeDetailResponse()

            resumed._call = fake_call
            resumed._phase_sub = Mock(wraps=resumed._phase_sub)
            resumed.cancel = lambda: False
            resumed_stats = resumed.crawl()

            self.assertEqual(resumed._phase_sub.call_count, 1)
            self.assertFalse(resumed_stats["aborted"])
            self.assertFalse(resumed_stats["cancelled"])
            self.assertEqual(resumed_stats["status"], "completed")
            self.assertEqual(resumed.ckpt["next_root_index"], 1)
            self.assertFalse(resumed.ckpt["aborted"])
            self.assertFalse(resumed.ckpt["cancelled"])

    def test_core_distinguishes_limit_error_and_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            limited = make_crawler(root / "limited")
            limited.ckpt_path.parent.mkdir(parents=True)
            limited.ckpt = limited._load_ckpt()
            limited._phase_main = lambda _out: limited._mark_aborted()
            limited_stats = limited.crawl()
            self.assertEqual(limited_stats["status"], "interrupted")
            self.assertTrue(limited_stats["aborted"])
            self.assertFalse(limited_stats["cancelled"])

            failed = make_crawler(root / "failed")
            failed.ckpt_path.parent.mkdir(parents=True)
            failed.ckpt = failed._load_ckpt()
            failed._phase_main = lambda _out: failed._mark_aborted(
                error="主楼阶段终止：gRPC 连续失败"
            )
            failed_stats = failed.crawl()
            self.assertEqual(failed_stats["status"], "error")
            self.assertTrue(failed_stats["aborted"])
            self.assertFalse(failed_stats["cancelled"])
            self.assertIn("gRPC 连续失败", failed_stats["error"])

            completed = make_crawler(root / "completed")
            completed.ckpt_path.parent.mkdir(parents=True)
            completed.ckpt = completed._load_ckpt()
            completed._phase_main = lambda _out: None
            completed._phase_sub = lambda _out: None
            completed_stats = completed.crawl()
            self.assertEqual(completed_stats["status"], "completed")
            self.assertFalse(completed_stats["aborted"])
            self.assertFalse(completed_stats["cancelled"])

    def test_pipeline_promotes_core_error_to_failure(self):
        with self.assertRaisesRegex(RuntimeError, "gRPC 连续失败"):
            pipeline._raise_on_crawl_error(
                {"status": "error", "error": "主楼阶段终止：gRPC 连续失败"}
            )
        pipeline._raise_on_crawl_error(
            {"status": "interrupted", "aborted": True, "cancelled": False}
        )
        pipeline._raise_on_crawl_error(
            {"status": "completed", "aborted": False, "cancelled": False}
        )


if __name__ == "__main__":
    unittest.main()
