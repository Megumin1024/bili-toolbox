# -*- coding: utf-8 -*-
"""用户动态抓取的离线测试。

fixture 的形状来自对动态接口真实响应的实测（见 core.wbi 模块注释记录的那次探测），
但**所有内容都是合成的**——不把真实用户的数据带进仓库。

全程离线：fetch/sleeper/密钥缓存全部注入，不打网络、不真等待、不读文件。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from core import wbi
from tools.user_dynamics import core

IMG_KEY = "0123456789abcdef0123456789abcdef"
SUB_KEY = "fedcba9876543210fedcba9876543210"
TS = 1700000000


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds


# ---------- 合成 fixture（形状照抄实测结构） ----------

def make_stat(forward=0, comment=0, like=0, coin=0, favorite=0):
    """计数字段实测是**字符串**，故意保持字符串以覆盖真实情况。"""
    def node(v):
        return {"status": True, "count": str(v), "forbidden": False,
                "disabled": False, "silent": False}
    return {"forward": node(forward), "comment": node(comment),
            "like": node(like), "coin": node(coin), "favorite": node(favorite)}


def make_archive_item(dyn_id="1000000000000000001", pub_ts=str(TS),
                      title="示例投稿标题", bvid="BV1xx411c7mD",
                      jump="//www.bilibili.com/video/BV1xx411c7mD",
                      desc=None, counts=(7, 8, 9, 1, 2), mid=12345):
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_AV",
        "basic": {"rid_str": "1", "comment_type": 1},
        "visible": True,
        "modules": {
            "module_author": {"mid": mid, "name": "某UP", "pub_ts": pub_ts,
                              "pub_time": "01-01", "pub_action": "投稿了视频"},
            "module_dynamic": {
                "desc": None if desc is None else {"text": desc},
                "major": {"type": "MAJOR_TYPE_ARCHIVE",
                          "archive": {"title": title, "bvid": bvid,
                                      "jump_url": jump,
                                      "desc": "视频简介", "aid": "1"}},
            },
            "module_stat": make_stat(*counts),
        },
    }


def make_forward_item(dyn_id="1000000000000000002", desc="转发时说的话",
                      orig_id="999", orig_author="原作者", pub_ts=str(TS)):
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_FORWARD",
        "visible": True,
        "orig": {"id_str": orig_id,
                 "modules": {"module_author": {"mid": 777, "name": orig_author}}},
        "modules": {
            "module_author": {"mid": 12345, "name": "某UP", "pub_ts": pub_ts},
            "module_dynamic": {"desc": {"text": desc}, "major": None},
            "module_stat": make_stat(3, 4, 5),
        },
    }


def make_opus_item(dyn_id="1000000000000000003", text="图文正文内容",
                   pub_ts=str(TS)):
    return {
        "id_str": dyn_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "visible": True,
        "modules": {
            "module_author": {"mid": 12345, "name": "某UP", "pub_ts": pub_ts},
            "module_dynamic": {
                "desc": None,
                "major": {"type": "MAJOR_TYPE_OPUS",
                          "opus": {"title": "",
                                   "summary": {"text": text},
                                   "jump_url": "//www.bilibili.com/opus/123"}},
            },
            "module_stat": make_stat(1, 2, 3),
        },
    }


def page(items, has_more=False, offset=""):
    return {"code": 0, "message": "0",
            "data": {"items": items, "has_more": has_more, "offset": offset}}


class ParseUidTests(unittest.TestCase):
    def test_plain_digits(self):
        self.assertEqual(core.parse_uid("946974"), 946974)

    def test_full_space_url(self):
        self.assertEqual(
            core.parse_uid("https://space.bilibili.com/946974/dynamic"), 946974)

    def test_url_without_scheme(self):
        self.assertEqual(core.parse_uid("space.bilibili.com/946974"), 946974)

    def test_url_with_query_is_still_read(self):
        self.assertEqual(
            core.parse_uid("https://space.bilibili.com/946974?spm_id_from=x"), 946974)

    def test_surrounding_quotes_and_space_are_tolerated(self):
        self.assertEqual(core.parse_uid('  "946974"  '), 946974)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            core.parse_uid("   ")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            core.parse_uid("这不是一个UID")


class ToIntTests(unittest.TestCase):
    def test_string_number(self):
        self.assertEqual(core.to_int("1700000000"), 1700000000)

    def test_int_passthrough(self):
        self.assertEqual(core.to_int(42), 42)

    def test_bool_is_not_an_int(self):
        """True 是 int 的子类，当计数用会得到 1——必须挡掉。"""
        self.assertEqual(core.to_int(True), 0)
        self.assertEqual(core.to_int(False), 0)

    def test_junk_and_none_become_default(self):
        for bad in ("", None, "abc", [], {}):
            self.assertEqual(core.to_int(bad), 0)

    def test_custom_default(self):
        self.assertEqual(core.to_int(None, default=-1), -1)


class ParseItemTests(unittest.TestCase):
    def test_archive_item_reads_title_bvid_and_counts(self):
        row = core.parse_item(make_archive_item())
        self.assertEqual(row["id"], "1000000000000000001")
        self.assertEqual(row["type"], "视频投稿")
        self.assertEqual(row["text"], "示例投稿标题")
        self.assertEqual(row["bvid"], "BV1xx411c7mD")
        self.assertEqual((row["forward"], row["comment"], row["like"]),
                         (7, 8, 9))
        self.assertEqual((row["coin"], row["favorite"]), (1, 2))
        self.assertFalse(row["is_forward"])

    def test_protocol_relative_url_is_made_absolute(self):
        row = core.parse_item(make_archive_item())
        self.assertEqual(row["url"],
                         "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_forward_item_takes_text_from_desc_and_records_origin(self):
        row = core.parse_item(make_forward_item())
        self.assertEqual(row["text"], "转发时说的话")
        self.assertTrue(row["is_forward"])
        self.assertEqual(row["orig_author"], "原作者")
        self.assertEqual(row["orig_id"], "999")

    def test_opus_item_takes_text_from_summary(self):
        row = core.parse_item(make_opus_item())
        self.assertEqual(row["text"], "图文正文内容")
        self.assertEqual(row["url"], "https://www.bilibili.com/opus/123")

    def test_pub_ts_string_becomes_int_and_formats(self):
        row = core.parse_item(make_archive_item(pub_ts="1700000000"))
        self.assertEqual(row["pub_ts"], TS)
        self.assertEqual(row["time"],
                         datetime.fromtimestamp(TS).strftime("%Y-%m-%d %H:%M"))

    def test_whitespace_in_text_is_collapsed(self):
        row = core.parse_item(make_forward_item(desc="多行\n\n文本  带空格"))
        self.assertEqual(row["text"], "多行 文本 带空格")

    def test_missing_everything_degrades_gracefully(self):
        row = core.parse_item({"id_str": "1", "type": "DYNAMIC_TYPE_WORD"})
        self.assertEqual(row["id"], "1")
        self.assertEqual(row["text"], "")
        self.assertEqual(row["forward"], 0)
        self.assertEqual(row["pub_ts"], 0)
        self.assertEqual(row["time"], "")

    def test_none_item_does_not_raise(self):
        self.assertEqual(core.parse_item(None)["id"], "")

    def test_unknown_type_falls_back_to_raw_value(self):
        row = core.parse_item({"id_str": "1", "type": "DYNAMIC_TYPE_BRAND_NEW"})
        self.assertEqual(row["type"], "DYNAMIC_TYPE_BRAND_NEW")

    def test_stat_missing_count_key_is_zero(self):
        item = make_archive_item()
        item["modules"]["module_stat"] = {"like": {"status": True}}
        self.assertEqual(core.parse_item(item)["like"], 0)


class ParsePageTests(unittest.TestCase):
    def test_reads_items_and_cursor(self):
        items, has_more, offset = core.parse_page(
            page([{"id_str": "1"}], has_more=True, offset="abc"))
        self.assertEqual(len(items), 1)
        self.assertTrue(has_more)
        self.assertEqual(offset, "abc")

    def test_empty_payload_is_all_empty(self):
        self.assertEqual(core.parse_page(None), ([], False, ""))

    def test_items_wrong_type_is_treated_as_empty(self):
        self.assertEqual(core.parse_page({"data": {"items": "oops"}})[0], [])


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="user_dyn_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sleeps = RecordingSleep()

    def make(self, effects, **kwargs):
        """effects 按序回放；用尽后重复最后一项。"""
        calls = []

        def fetch(url):
            calls.append(url)
            item = effects[min(len(calls) - 1, len(effects) - 1)]
            if isinstance(item, BaseException):
                raise item
            return item

        fetch.calls = calls
        keys = wbi.WbiKeyCache(fetch=lambda: (IMG_KEY, SUB_KEY), ttl=1e9,
                               clock=lambda: 0.0)
        kwargs.setdefault("progress", lambda **kw: None)
        kwargs.setdefault("sleeper", self.sleeps)
        crawler = core.DynamicsCrawler(12345, self.tmp, fetch=fetch,
                                       key_cache=keys, **kwargs)
        return crawler, fetch

    def test_walks_pages_until_has_more_is_false(self):
        crawler, fetch = self.make([
            page([make_archive_item("1")], has_more=True, offset="c1"),
            page([make_archive_item("2")], has_more=False),
        ])
        stats = crawler.crawl()

        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["rows"], 2)
        self.assertFalse(stats["truncated"])
        self.assertEqual([r["id"] for r in crawler.rows], ["1", "2"])

    def test_requests_are_signed(self):
        crawler, fetch = self.make([page([make_archive_item("1")])])
        crawler.crawl()
        self.assertIn("w_rid=", fetch.calls[0])
        self.assertIn("wts=", fetch.calls[0])
        self.assertIn("host_mid=12345", fetch.calls[0])

    def test_max_pages_caps_and_flags_truncated(self):
        crawler, fetch = self.make(
            [page([make_archive_item("1")], has_more=True, offset="c")],
            max_pages=2)
        stats = crawler.crawl()

        self.assertEqual(stats["pages"], 2)
        self.assertTrue(stats["truncated"], "达到页数上限必须标注可能还有更多")
        self.assertEqual(len(fetch.calls), 2)

    def test_offset_is_taken_from_previous_page(self):
        crawler, fetch = self.make([
            page([make_archive_item("1")], has_more=True, offset="CURSOR-1"),
            page([make_archive_item("2")]),
        ])
        crawler.crawl()
        self.assertIn("offset=CURSOR-1", fetch.calls[1])

    def test_duplicate_ids_are_dropped(self):
        crawler, _ = self.make([
            page([make_archive_item("same")], has_more=True, offset="c"),
            page([make_archive_item("same")]),
        ])
        stats = crawler.crawl()
        self.assertEqual(stats["rows"], 1)

    def test_empty_page_is_retried_then_succeeds(self):
        """实测：带签名的请求也会间歇返回空，重试就能拿到数据。"""
        crawler, fetch = self.make([
            page([]),
            page([make_archive_item("1")]),
        ])
        stats = crawler.crawl()

        self.assertEqual(len(fetch.calls), 2, "空结果必须重试")
        self.assertEqual(stats["rows"], 1)
        self.assertGreater(self.sleeps.total, 0.0)

    def test_first_page_empty_after_retries_refuses_to_conclude(self):
        """第一页反复取空时，不许说"该用户没有动态"。"""
        crawler, fetch = self.make([page([])], empty_retries=3)

        with self.assertRaises(core.DynamicsUnavailable):
            crawler.crawl()
        self.assertEqual(len(fetch.calls), 3)
        self.assertEqual(crawler.stats["rows"], 0)

    def test_empty_page_midway_truncates_but_keeps_rows(self):
        crawler, fetch = self.make([
            page([make_archive_item("1")], has_more=True, offset="c"),
            page([]),
        ], empty_retries=2)
        stats = crawler.crawl()

        self.assertEqual(stats["rows"], 1, "已经拿到的数据不能丢")
        self.assertTrue(stats["truncated"])
        self.assertFalse(stats["cancelled"])

    def test_cancel_before_first_request_skips_network(self):
        crawler, fetch = self.make([page([make_archive_item("1")])],
                                   cancel=lambda: True)
        stats = crawler.crawl()

        self.assertEqual(len(fetch.calls), 0)
        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["rows"], 0)

    def test_cancel_midway_keeps_what_was_fetched(self):
        """第一页数据在返回途中用户点了取消：循环必须在下一页之前停下，
        但已经到手的那一页不能丢——取消不等于回滚。"""
        state = {"cancel": False}

        def fetch(url):
            fetch.calls.append(url)
            state["cancel"] = True        # 第一页返回的同时用户取消
            return page([make_archive_item(str(len(fetch.calls)))],
                        has_more=True, offset="c")

        fetch.calls = []
        keys = wbi.WbiKeyCache(fetch=lambda: (IMG_KEY, SUB_KEY), ttl=1e9,
                               clock=lambda: 0.0)
        crawler = core.DynamicsCrawler(
            12345, self.tmp, fetch=fetch, key_cache=keys, sleeper=self.sleeps,
            progress=lambda **kw: None, cancel=lambda: state["cancel"])
        stats = crawler.crawl()

        self.assertTrue(stats["cancelled"])
        self.assertEqual(len(fetch.calls), 1, "取消后不许再发下一页请求")
        self.assertEqual(stats["rows"], 1)
        self.assertTrue((crawler.out_dir / "dynamics_12345.jsonl").exists(),
                        "取消后也要把已抓到的落盘")

    def test_cancel_is_forwarded_to_the_http_layer(self):
        """取消必须穿透到 HTTP 层，不能只停在爬取器这一层。

        HTTP 层内部的退避等待受 TOTAL_WAIT_BUDGET(90s) 约束；拿不到取消谓词
        就只能睡满预算，用户按了取消也毫无反应。爬取器自己的取消检查在这之下
        够不着——所以这个谓词必须一路传下去。
        """
        crawler, _ = self.make([page([make_archive_item("1")])])
        seen = {}

        def fake_http(url, retries=3, cancel=None):
            seen["cancel"] = cancel
            return page([make_archive_item("1")])

        with mock.patch("core.session.http_get_json", side_effect=fake_http):
            crawler._session_fetch("https://api.example.invalid/x")

        self.assertTrue(callable(seen["cancel"]),
                        "cancel 没传下去：取消信号到不了 HTTP 层")
        self.assertIs(seen["cancel"], crawler.cancel,
                      "传下去的必须是爬取器自己的谓词，不能另造一个")

    def test_forwarded_cancel_reflects_crawler_state(self):
        """防"传了个恒 False 的假谓词"：状态一变，透传下去的谓词必须跟着变。"""
        state = {"cancel": False}
        crawler, _ = self.make([page([make_archive_item("1")])],
                               cancel=lambda: state["cancel"])
        seen = {}

        def fake_http(url, retries=3, cancel=None):
            seen["cancel"] = cancel
            return page([make_archive_item("1")])

        with mock.patch("core.session.http_get_json", side_effect=fake_http):
            crawler._session_fetch("https://api.example.invalid/x")

        self.assertFalse(seen["cancel"]())
        state["cancel"] = True
        self.assertTrue(seen["cancel"](), "取消状态翻转后，HTTP 层看到的仍是旧值")

    def test_key_fetch_also_carries_the_cancel_predicate(self):
        """取密钥那条路也得能取消。

        首次请求前会先取一次 nav，那条路同样会退避重试——漏掉它等于留了个
        小一号的同款窗口。
        """
        seen = {}

        def fake_fetch_keys(cancel=None):
            seen["cancel"] = cancel
            return IMG_KEY, SUB_KEY

        crawler = core.DynamicsCrawler(
            12345, self.tmp, fetch=lambda url: page([]), key_cache=None,
            progress=lambda **kw: None, sleeper=self.sleeps,
            cancel=lambda: False)
        with mock.patch("core.wbi.fetch_keys", side_effect=fake_fetch_keys):
            crawler._keys.get()

        self.assertIs(seen["cancel"], crawler.cancel)

    def test_rows_are_written_to_jsonl(self):
        crawler, _ = self.make([page([make_archive_item("1")])])
        crawler.crawl()

        lines = crawler.out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], "1")

    def test_empty_retries_are_bounded_by_the_argument(self):
        crawler, fetch = self.make([page([])], empty_retries=1)
        with self.assertRaises(core.DynamicsUnavailable):
            crawler.crawl()
        self.assertEqual(len(fetch.calls), 1, "empty_retries=1 就只发一次")


if __name__ == "__main__":
    unittest.main()
