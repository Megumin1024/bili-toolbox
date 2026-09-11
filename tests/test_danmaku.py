# -*- coding: utf-8 -*-
"""弹幕工具的离线测试。

全部离线：分段字节用 pb2 现场构造，fetch / sleep / session 注入，不建连接。

这里锁定的是几条容易被"顺手简化"掉的硬约束：
- 空段 = 抓完（不重试）；跑满上限时**不再多发一次注定为空的请求**；
- 非空却解析出 0 条必须报错，不许伪装成"该视频没有弹幕"；
- 取不到任何弹幕而视频自称有弹幕时必须报错，不交假报告。
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from core.cancel import TaskCancelledError
from app.task_page import TaskPage
from tools.danmaku import core
from tools.danmaku import danmaku_pb2
from tools.danmaku import pipeline
from tools.danmaku.page import DanmakuPage


# ---------- 夹具 ----------

def make_elem(eid=1, progress=1000, mode=1, content="测试弹幕",
              color=16777215, ctime=1600000000, pool=0, fontsize=25,
              weight=9, mid="abc123", attr=0, use_idstr=True):
    e = danmaku_pb2.DanmakuElem()
    e.id = int(eid)
    if use_idstr:
        e.idStr = str(eid)
    e.progress = progress
    e.mode = mode
    e.fontsize = fontsize
    e.color = color
    e.ctime = ctime
    e.pool = pool
    e.weight = weight
    e.midHash = mid
    e.attr = attr
    e.content = content
    return e


def make_segment(*elems):
    reply = danmaku_pb2.DmSegMobileReply()
    reply.elems.extend(elems)
    return reply.SerializeToString()


class FakeFetch:
    """按 segment_index 回放字节；未给出的段一律返回空（0 字节）。

    签名要吞掉 **kw：生产代码会传 cancel=... 进来，这是必须透传的那条线。
    """

    def __init__(self, segments):
        self.segments = dict(segments)
        self.urls = []
        self.cancels = []

    def __call__(self, url, **kw):
        self.urls.append(url)
        self.cancels.append(kw.get("cancel"))
        idx = int(re.search(r"segment_index=(\d+)", url).group(1))
        return self.segments.get(idx, b"")

    @property
    def indices(self):
        return [int(re.search(r"segment_index=(\d+)", u).group(1)) for u in self.urls]


class RecordingSleep:
    def __init__(self):
        self.calls = 0
        self.total = 0.0

    def __call__(self, seconds):
        self.calls += 1
        self.total += seconds


def make_crawler(fetch, duration=720, **kw):
    tmp = tempfile.mkdtemp(prefix="danmaku_test_")
    return core.DanmakuCrawler(12345, tmp, duration=duration, fetch=fetch,
                              sleeper=RecordingSleep(), **kw)


# ---------- 纯函数 ----------

class ParseTargetTests(unittest.TestCase):
    def test_plain_bvid(self):
        self.assertEqual(core.parse_target("BV1GJ411x7h7"), ("BV1GJ411x7h7", None, 1))

    def test_full_url_and_trailing_junk(self):
        self.assertEqual(
            core.parse_target("https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=333.999"),
            ("BV1GJ411x7h7", None, 1))

    def test_av_number(self):
        self.assertEqual(core.parse_target("av80433022"), (None, 80433022, 1))

    def test_page_parameter_is_parsed(self):
        _, _, page = core.parse_target("https://www.bilibili.com/video/BV1GJ411x7h7?p=3")
        self.assertEqual(page, 3)

    def test_surrounding_quotes_are_stripped(self):
        self.assertEqual(core.parse_target('"BV1GJ411x7h7"')[0], "BV1GJ411x7h7")

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            core.parse_target("   ")

    def test_unrecognized_input_rejected(self):
        with self.assertRaises(ValueError):
            core.parse_target("这不是一个视频")

    def test_b23_shortlink_is_resolved(self):
        with patch("core.links.resolve_url",
                   return_value="https://www.bilibili.com/video/BV1GJ411x7h7") as m:
            self.assertEqual(core.parse_target("https://b23.tv/abcdefg")[0],
                             "BV1GJ411x7h7")
        m.assert_called_once()


class HhmmssTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(core.hhmmss(0), "00:00:00")

    def test_seconds_only(self):
        self.assertEqual(core.hhmmss(6996), "00:00:06")

    def test_past_an_hour(self):
        self.assertEqual(core.hhmmss(3_661_000), "01:01:01")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(core.hhmmss(-5), "00:00:00")

    def test_fixed_width_sorts_as_text(self):
        # 定宽是为了在 Excel 里按文本排序也正确
        self.assertLess(core.hhmmss(9_000), core.hhmmss(600_000))


class ExpectedSegmentsTests(unittest.TestCase):
    def test_unknown_duration(self):
        self.assertIsNone(core.expected_segments(0))

    def test_shorter_than_one_segment(self):
        self.assertEqual(core.expected_segments(213), 1)

    def test_exact_boundary_is_one_segment(self):
        self.assertEqual(core.expected_segments(360), 1)

    def test_one_second_over_boundary_needs_two(self):
        self.assertEqual(core.expected_segments(361), 2)

    def test_long_video(self):
        # 实测的 BV1sHb56xEhC：10162 秒
        self.assertEqual(core.expected_segments(10162), 29)


class RowFromElemTests(unittest.TestCase):
    def test_field_mapping(self):
        row = core.row_from_elem(make_elem(eid=42, progress=95_453, mode=5,
                                           content="前排", color=0x00FF00,
                                           ctime=1593092327, pool=1))
        self.assertEqual(row["id"], "42")
        self.assertEqual(row["progress_ms"], 95_453)
        self.assertEqual(row["time"], "00:01:35")
        self.assertEqual(row["mode_label"], "顶部")
        self.assertEqual(row["pool_label"], "字幕")
        self.assertEqual(row["color_hex"], "#00FF00")
        self.assertEqual(row["content"], "前排")
        self.assertTrue(row["sent_time"])

    def test_unknown_mode_is_shown_with_raw_value(self):
        row = core.row_from_elem(make_elem(mode=99))
        self.assertEqual(row["mode_label"], "未知(99)")
        self.assertEqual(row["mode"], 99)

    def test_missing_idstr_falls_back_to_numeric_id(self):
        row = core.row_from_elem(make_elem(eid=777, use_idstr=False))
        self.assertEqual(row["id"], "777")

    def test_zero_progress_and_ctime_degrade_quietly(self):
        row = core.row_from_elem(make_elem(progress=0, ctime=0))
        self.assertEqual(row["time"], "00:00:00")
        self.assertEqual(row["sent_time"], "")

    def test_all_known_mode_labels_exist(self):
        for mode in (1, 4, 5, 6, 7, 8, 9):
            self.assertNotIn("未知", core.row_from_elem(make_elem(mode=mode))["mode_label"])


class ParseSegmentTests(unittest.TestCase):
    def test_roundtrip_through_real_pb2(self):
        raw = make_segment(make_elem(1, content="a"), make_elem(2, content="b"))
        rows = core.parse_segment(raw)
        self.assertEqual([r["content"] for r in rows], ["a", "b"])

    def test_empty_body_is_not_an_error(self):
        # 空段是"抓完了"的正常信号，不是失败
        self.assertEqual(core.parse_segment(b""), [])

    def test_garbage_bytes_raise_instead_of_returning_empty(self):
        with self.assertRaises(core.DanmakuDecodeError):
            core.parse_segment(b"\xff\xff\xff\xff\xff\xff\xff\xff\xff")

    def test_nonempty_body_without_elems_raises(self):
        """线上字段号若变更，响应仍能解析但一条都取不到——必须报错。

        这段字节是合法的 protobuf（顶层字段 4，长度 2），但没有 elems。
        静默返回空列表就等于把协议变更伪装成「该视频没有弹幕」。
        """
        crafted = b"\x22\x02\x08\x01"
        with self.assertRaises(core.DanmakuDecodeError) as ctx:
            core.parse_segment(crafted)
        self.assertIn("0 条", str(ctx.exception))


class MinuteBucketsTests(unittest.TestCase):
    def test_buckets_are_half_open_per_minute(self):
        rows = [{"progress_ms": v} for v in (0, 59_999, 60_000, 60_001, 120_000)]
        self.assertEqual(core.minute_buckets(rows), [(0, 2), (1, 2), (2, 1)])

    def test_empty_rows(self):
        self.assertEqual(core.minute_buckets([]), [])

    def test_missing_progress_treated_as_zero(self):
        self.assertEqual(core.minute_buckets([{"progress_ms": None}]), [(0, 1)])


# ---------- 抓取 ----------

class CrawlTests(unittest.TestCase):
    def test_two_segments_then_empty(self):
        fetch = FakeFetch({1: make_segment(make_elem(1), make_elem(2)),
                           2: make_segment(make_elem(3))})
        c = make_crawler(fetch, duration=720)          # 720s → 预计 2 段
        stats = c.crawl()
        self.assertEqual(stats["rows"], 3)
        self.assertEqual(stats["segments"], 2)
        self.assertEqual(stats["requests"], 3, "第 3 次是判定结束的空段")
        self.assertFalse(stats["truncated"], "抓到预计段数后遇到空段，属正常结束")
        self.assertEqual([r["content"] for r in c.rows], ["测试弹幕"] * 3)

    def test_segment_url_shape(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        make_crawler(fetch, duration=360).crawl()
        self.assertTrue(fetch.urls[0].startswith(core.SEG_URL))
        self.assertIn("type=1", fetch.urls[0])
        self.assertIn("oid=12345", fetch.urls[0])
        self.assertIn("segment_index=1", fetch.urls[0])

    def test_video_with_no_danmaku_is_not_flagged_as_truncated(self):
        """本来就没有弹幕的视频不该被扣上「可能未抓全」的帽子。"""
        fetch = FakeFetch({})
        stats = make_crawler(fetch, duration=720).crawl()
        self.assertEqual(stats["rows"], 0)
        self.assertEqual(stats["requests"], 1)
        self.assertFalse(stats["truncated"])

    def test_early_empty_after_data_is_flagged(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        stats = make_crawler(fetch, duration=1800).crawl()   # 预计 5 段，只拿到 1 段
        self.assertTrue(stats["truncated"], "比预计提前结束必须报截断")

    def test_segment_cap_stops_without_extra_probe(self):
        fetch = FakeFetch({i: make_segment(make_elem(i)) for i in (1, 2, 3, 4, 5)})
        stats = make_crawler(fetch, duration=3600, max_segments=2).crawl()
        self.assertEqual(stats["segments"], 2)
        self.assertEqual(fetch.indices, [1, 2], "跑满上限后不许再发注定为空的请求")
        self.assertTrue(stats["truncated"], "被自己的上限截断要如实说明")

    def test_cap_larger_than_video_is_not_truncated(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        stats = make_crawler(fetch, duration=360, max_segments=50).crawl()
        self.assertFalse(stats["truncated"])

    def test_duplicates_across_segments_are_dropped_and_counted(self):
        shared = make_elem(1, content="重复")
        fetch = FakeFetch({1: make_segment(shared, make_elem(2)),
                           2: make_segment(shared, make_elem(3))})
        stats = make_crawler(fetch, duration=720).crawl()
        self.assertEqual(stats["rows"], 3)
        self.assertEqual(stats["duplicates"], 1)

    def test_pacing_happens_before_every_request(self):
        fetch = FakeFetch({1: make_segment(make_elem(1)),
                           2: make_segment(make_elem(2))})
        c = make_crawler(fetch, duration=720, sleep=1.5)
        c.crawl()
        # 两次等待分别落在第 2 段与判定结束的空段之前——发请求前先停下来，
        # 因为当时并不知道下一次会不会是空段。
        # 断言总时长而不是调用次数：等待被切成 0.25s 小片以便及时响应取消，
        # 次数是实现细节，总时长才是契约。
        self.assertAlmostEqual(c._sleep.total, 3.0)

    def test_zero_sleep_skips_pacing_entirely(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        c = make_crawler(fetch, duration=360, sleep=0)
        c.crawl()
        self.assertEqual(c._sleep.calls, 0)

    def test_cancel_midway_keeps_what_was_fetched(self):
        state = {"cancel": False}

        def fetch(url):
            fetch.urls.append(url)
            idx = int(re.search(r"segment_index=(\d+)", url).group(1))
            if idx == 1:
                state["cancel"] = True          # 第 1 段返回的同时用户点了取消
                return make_segment(make_elem(1))
            return make_segment(make_elem(idx))

        fetch.urls = []
        c = make_crawler(fetch, duration=3600, cancel=lambda: state["cancel"])
        stats = c.crawl()
        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["rows"], 1, "取消不等于回滚，已到手的不许丢")
        self.assertEqual(len(fetch.urls), 1, "取消后不许再发下一段请求")
        self.assertTrue(Path(c.out_path).exists(), "取消也要把已抓到的落盘")

    def test_cancel_before_first_request_sends_nothing(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        c = core.DanmakuCrawler(999, tempfile.mkdtemp(), duration=360,
                                fetch=fetch, cancel=lambda: True)
        stats = c.crawl()
        self.assertTrue(stats["cancelled"])
        self.assertEqual(fetch.urls, [])
        self.assertEqual(stats["requests"], 0)

    def test_decode_failure_propagates_instead_of_empty_result(self):
        """第 2 段是坏字节：必须抛，不能交一份"只抓到 1 条"的报告。"""
        fetch = FakeFetch({1: make_segment(make_elem(1)),
                           2: b"\xff\xff\xff\xff\xff\xff\xff\xff"})
        with self.assertRaises(core.DanmakuDecodeError):
            make_crawler(fetch, duration=720).crawl()

    def test_jsonl_is_written_and_reparsable(self):
        import json
        fetch = FakeFetch({1: make_segment(make_elem(1, content="落盘"))})
        c = make_crawler(fetch, duration=360)
        c.crawl()
        lines = Path(c.out_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["content"], "落盘")


# ---------- 导出 ----------

META = {"bvid": "BV1GJ411x7h7", "aid": 80433022, "cid": 137649199,
        "duration": 213, "title": "标题", "owner": "UP主", "part": "",
        "page": 1, "page_count": 1, "pubdate": 1580000000,
        "claimed_danmaku": 1410}


def api_payload(**overrides):
    """view 接口的真实报文形状：值都埋在 data 里，弹幕数在 data.stat.danmaku。"""
    out = {"bvid": META["bvid"], "aid": META["aid"], "cid": META["cid"],
           "duration": META["duration"], "title": META["title"],
           "owner": {"name": META["owner"]}, "pubdate": META["pubdate"],
           "stat": {"danmaku": overrides.pop("claimed", 10)}}
    out.update(overrides)
    return {"code": 0, "data": out}


class ExportTests(unittest.TestCase):
    def _export(self, rows, meta=None, stats=None):
        tmp = Path(tempfile.mkdtemp(prefix="danmaku_xlsx_"))
        path = tmp / "out.xlsx"
        stats = stats or {"segments": 1, "expected_segments": 1, "rows": len(rows),
                          "truncated": False, "cancelled": False, "duplicates": 0}
        core.export_xlsx(rows, meta or META, stats, path)
        return load_workbook(path)

    def test_three_sheets(self):
        wb = self._export([core.row_from_elem(make_elem(1))])
        self.assertEqual(wb.sheetnames, ["概览", "弹幕明细", "密度分布"])

    def test_detail_rows_match_input(self):
        rows = [core.row_from_elem(make_elem(i)) for i in (1, 2, 3)]
        ws = self._export(rows)["弹幕明细"]
        # 布局固定为：标题行 + 占位行 + 表头行 + 数据行
        self.assertEqual(ws.max_row, 3 + 3)

    def test_detail_sheet_is_sorted_by_video_time(self):
        """接口按弹幕 ID（近似发送顺序）下发，导出必须重排成时间轴。

        否则这张表读不出「哪句话出现在哪一段」——而那是它存在的理由。
        """
        rows = [core.row_from_elem(make_elem(1, progress=90_000)),
                core.row_from_elem(make_elem(2, progress=1_000)),
                core.row_from_elem(make_elem(3, progress=30_000))]
        ws = self._export(rows)["弹幕明细"]
        times = [r[2] for r in ws.iter_rows(values_only=True)][3:]
        self.assertEqual(times, ["00:00:01", "00:00:30", "00:01:30"])
        self.assertEqual(times, sorted(times))

    def test_input_order_is_not_mutated(self):
        """排序只影响表，不许就地改调用方的列表（jsonl 要留原始顺序）。"""
        rows = [core.row_from_elem(make_elem(1, progress=90_000)),
                core.row_from_elem(make_elem(2, progress=1_000))]
        before = [r["id"] for r in rows]
        self._export(rows)
        self.assertEqual([r["id"] for r in rows], before)

    def test_density_sheet_aggregates_and_totals(self):
        rows = [core.row_from_elem(make_elem(i, progress=p))
                for i, p in enumerate((1_000, 2_000, 61_000), start=1)]
        ws = self._export(rows)["密度分布"]
        texts = [[c.value for c in r] for r in ws.iter_rows()]
        # 表头行在索引 2（0=占位、1=标题）
        body = [r for r in texts if r[1] and isinstance(r[1], str)
                and r[1].startswith("00:0")]
        self.assertEqual(len(body), 2, "两分钟各一行")
        self.assertEqual(body[0][2], 2)
        self.assertEqual(body[1][2], 1)
        self.assertIn("合计", [r[1] for r in texts])

    def test_empty_rows_still_produces_a_valid_file(self):
        wb = self._export([])
        self.assertEqual(wb.sheetnames, ["概览", "弹幕明细", "密度分布"])
        ws = wb["概览"]
        values = [c.value for r in ws.iter_rows() for c in r]
        self.assertTrue(any(isinstance(v, str) and v.startswith("一段都没抓到")
                            for v in values),
                        "空结果必须在概览里说清，不能交一张看起来正常的表")

    def test_truncation_is_disclosed_in_overview(self):
        wb = self._export([core.row_from_elem(make_elem(1))], stats={
            "segments": 1, "expected_segments": 29, "rows": 1,
            "truncated": True, "cancelled": False, "duplicates": 0})
        values = [c.value for r in wb["概览"].iter_rows() for c in r]
        self.assertTrue(any(isinstance(v, str) and "可能未抓全" in v for v in values))

    def test_cancellation_outranks_truncation_in_the_note(self):
        wb = self._export([core.row_from_elem(make_elem(1))], stats={
            "segments": 1, "expected_segments": 29, "rows": 1,
            "truncated": True, "cancelled": True, "duplicates": 0})
        values = [c.value for r in wb["概览"].iter_rows() for c in r]
        self.assertTrue(any(v == "任务被中途取消" for v in values))


# ---------- 流水线 ----------

class PipelineTests(unittest.TestCase):
    def _run(self, segments, **kw):
        claimed = kw.pop("claimed", 10)
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=claimed)), \
                patch("core.session.http_get_bytes", side_effect=segments):
            return pipeline.run_pipeline("BV1GJ411x7h7", out,
                                         sleep=0, cancel=lambda: False, **kw)

    def test_happy_path_writes_excel_and_jsonl(self):
        fetch = FakeFetch({1: make_segment(make_elem(1), make_elem(2))})
        result = self._run(fetch)
        self.assertEqual(result["rows"], 2)
        self.assertTrue(Path(result["xlsx"]).exists())
        self.assertTrue(Path(result["jsonl"]).exists())
        self.assertEqual(result["label"], "BV1GJ411x7h7")

    def test_zero_rows_with_claimed_danmaku_raises(self):
        """一条没抓到却自称有弹幕：必须报错，不许交"该视频没有弹幕"的假报告。"""
        with self.assertRaises(core.DanmakuUnavailable) as ctx:
            self._run(FakeFetch({}), claimed=1410)
        self.assertIn("1,410", str(ctx.exception))

    def test_zero_rows_with_claimed_zero_is_a_normal_empty_report(self):
        result = self._run(FakeFetch({}), claimed=0)
        self.assertEqual(result["rows"], 0)
        self.assertTrue(Path(result["xlsx"]).exists())

    def test_meta_lookup_uses_the_view_api_once(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=10)) as json_call, \
                patch("core.session.http_get_bytes", side_effect=fetch):
            pipeline.run_pipeline("BV1GJ411x7h7", out, sleep=0,
                                  cancel=lambda: False)
        self.assertEqual(json_call.call_count, 1,
                         "取 cid 只许发一次 view 请求，不能重复打接口")

    def test_cancel_predicate_is_forwarded_to_the_binary_channel(self):
        """取消谓词必须一路传到 HTTP 层，否则退避等待期间按取消要等满预算。"""
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        self._run(fetch)
        self.assertTrue(all(c is not None for c in fetch.cancels),
                        "每次分段请求都必须带上取消谓词")

    def test_multipart_video_warns_about_the_other_parts(self):
        fetch = FakeFetch({1: make_segment(make_elem(1))})
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        pages = [{"cid": 137649199, "duration": 213, "part": "P1"},
                 {"cid": 2, "duration": 213, "part": "P2"},
                 {"cid": 3, "duration": 213, "part": "P3"}]
        logs = []
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=10, pages=pages)), \
                patch("core.session.http_get_bytes", side_effect=fetch):
            pipeline.run_pipeline("BV1GJ411x7h7", out, sleep=0,
                                  cancel=lambda: False,
                                  progress=lambda **kw: logs.append(kw))
        self.assertTrue(any("分P" in str(kw.get("text", "")) for kw in logs),
                        "多分P只抓一个时要明说，不能默默少抓")

class PerPartFetch:
    """按 (cid, segment_index) 回放：多分P的 url 只差 oid 那一截。

    用 FakeFetch 会掩盖"有没有真的换 cid"这个 bug——它对谁问都回同一份字节。
    """

    def __init__(self, by_cid):
        self.by_cid = dict(by_cid)
        self.urls = []

    def __call__(self, url, **kw):
        self.urls.append(url)
        oid = int(re.search(r"oid=(\d+)", url).group(1))
        idx = int(re.search(r"segment_index=(\d+)", url).group(1))
        return self.by_cid.get(oid, {}).get(idx, b"")

    @property
    def cids(self):
        return [int(re.search(r"oid=(\d+)", u).group(1)) for u in self.urls]


MULTI_PAGES = [{"cid": 101, "duration": 213, "part": "第一局"},
               {"cid": 202, "duration": 213, "part": "第二局"},
               {"cid": 303, "duration": 213, "part": "第三局"}]


class MultiPartExportTests(unittest.TestCase):
    """多分P导出：多一列分P、多一张汇总表、密度不串台。

    明细表的列是 [占位, 序号, 分P, 视频内时间, ...]，下标从 0 起，列 A 是占位。
    下面多处断言依赖这个偏移，改动表头时这些下标要一起改。
    """

    def _rows(self):
        a = core.row_from_elem(make_elem(1, progress=1_000, content="P1的话"))
        b = core.row_from_elem(make_elem(2, progress=1_000, content="P2的话"))
        for r, (pg, pt) in zip((a, b), ((1, "第一局"), (2, "第二局"))):
            r["page"], r["part"] = pg, pt
        return [a, b]

    def _parts(self):
        return [{"page": 1, "part": "第一局", "duration": 213, "rows": 1,
                 "segments": 1, "expected_segments": 1, "duplicates": 0,
                 "cancelled": False, "truncated": False},
                {"page": 2, "part": "第二局", "duration": 213, "rows": 1,
                 "segments": 1, "expected_segments": 1, "duplicates": 0,
                 "cancelled": False, "truncated": False}]

    def _export(self, rows, parts=None, stats=None):
        tmp = Path(tempfile.mkdtemp(prefix="danmaku_xlsx_"))
        path = tmp / "out.xlsx"
        stats = stats or {"segments": 2, "expected_segments": 2,
                          "truncated": False, "cancelled": False,
                          "duplicates": 0}
        core.export_xlsx(rows, dict(META, page_count=2), stats, path,
                         parts=self._parts() if parts is None else parts)
        return load_workbook(path)

    def test_sheet_set_and_order(self):
        wb = self._export(self._rows())
        self.assertEqual(wb.sheetnames,
                         ["概览", "分P汇总", "弹幕明细", "密度分布"])

    def test_single_part_has_no_part_column_or_summary_sheet(self):
        """单分P是绝大多数情况：恒为 "P1" 的列和只有一行的汇总表都是噪音。"""
        tmp = Path(tempfile.mkdtemp(prefix="danmaku_xlsx_"))
        path = tmp / "out.xlsx"
        core.export_xlsx([core.row_from_elem(make_elem(1))], META,
                         {"segments": 1, "expected_segments": 1,
                          "truncated": False, "cancelled": False,
                          "duplicates": 0}, path, parts=None)
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["概览", "弹幕明细", "密度分布"])
        header = [c.value for c in wb["弹幕明细"][3]]
        self.assertEqual(header[1:4], ["序号", "视频内时间", "进度(ms)"])

    def test_part_column_is_second_in_detail(self):
        header = [c.value for c in self._export(self._rows())["弹幕明细"][3]]
        self.assertEqual(header[1:4], ["序号", "分P", "视频内时间"])

    def test_detail_is_sorted_by_part_then_time(self):
        a = core.row_from_elem(make_elem(1, progress=90_000))
        b = core.row_from_elem(make_elem(2, progress=1_000))
        for r, pg in ((a, 2), (b, 1)):
            r["page"] = pg
        ws = self._export([a, b])["弹幕明细"]
        body = [list(r) for r in ws.iter_rows(min_row=4, values_only=True)]
        self.assertEqual([r[2] for r in body], ["P1", "P2"],
                         "P2 的第一秒必须排在 P1 的第 90 秒之后")
        self.assertEqual([r[3] for r in body], ["00:00:01", "00:01:30"])

    def test_density_keeps_parts_apart(self):
        """两个分P的同一分钟必须两行，不能合并成一行。

        P1 和 P2 都从 00:00 重新计时，只按分钟聚合会把它们串台——
        那正是这张表要回答的问题（哪个时间点弹幕炸了）被毁掉的地方。
        """
        ws = self._export(self._rows())["密度分布"]
        body = [list(r) for r in ws.iter_rows(min_row=4, values_only=True)
                if isinstance(r[1], str) and r[1].startswith("P")]
        self.assertEqual(len(body), 2, "两个分P各一行，不该合并")
        self.assertEqual([r[1] for r in body], ["P1", "P2"])
        self.assertEqual([r[3] for r in body], [1, 1])

    def test_overview_discloses_all_parts_and_summed_duration(self):
        values = [c.value for r in self._export(self._rows())["概览"].iter_rows()
                  for c in r]
        self.assertTrue(any(v == "全部 2 个分P" for v in values))
        self.assertTrue(any(isinstance(v, str) and "426 秒" in v for v in values),
                        "多分P时概览的时长必须是各分P之和，不能只报 P1")

    def test_summary_sheet_lists_each_part_and_totals_once(self):
        ws = self._export(self._rows())["分P汇总"]
        body = [list(r) for r in ws.iter_rows(min_row=4, values_only=True)
                if r[1]]
        self.assertEqual([r[1] for r in body],
                         ["P1 第一局", "P2 第二局", "合计"])
        self.assertEqual([r[4] for r in body], [1, 1, 2], "合计行给出总条数")
        self.assertEqual([r[5] for r in body], [1, 1, 2])

    def test_part_title_without_a_prefix_reads_naturally(self):
        """B站的分P标题是「第二局」这种，没有 P 前缀；标签必须是 "P2 第二局" 一次。"""
        self.assertEqual(core.part_label(2, "第二局"), "P2 第二局")
        self.assertEqual(core.part_label(3, ""), "P3")

    def test_truncated_part_makes_the_whole_export_admit_it(self):
        """只要有一个分P没抓全，整份导出就不许印"已抓到底"。"""
        wb = self._export(self._rows(), stats={
            "segments": 2, "expected_segments": 5, "truncated": True,
            "cancelled": False, "duplicates": 0})
        values = [c.value for r in wb["概览"].iter_rows() for c in r]
        self.assertTrue(any(isinstance(v, str) and "可能未抓全" in v for v in values))

    def test_overview_records_the_actual_pace_not_the_default(self):
        """概览要如实记下本次的段间隔。写死默认值等于报告与实情不符。"""
        tmp = Path(tempfile.mkdtemp(prefix="danmaku_xlsx_"))
        path = tmp / "out.xlsx"
        core.export_xlsx([core.row_from_elem(make_elem(1))], META,
                         {"segments": 1, "expected_segments": 1,
                          "truncated": False, "cancelled": False,
                          "duplicates": 0}, path, sleep=2.5)
        values = [c.value for r in load_workbook(path)["概览"].iter_rows()
                  for c in r]
        self.assertTrue(any(isinstance(v, str) and "2.5s" in v for v in values))

    def test_unknown_duration_does_not_print_none(self):
        """时长为 0 时预期段数是 None，直接印出来就是"预计约 None 段"。"""
        tmp = Path(tempfile.mkdtemp(prefix="danmaku_xlsx_"))
        path = tmp / "out.xlsx"
        core.export_xlsx([core.row_from_elem(make_elem(1))],
                         dict(META, duration=0), {
                             "segments": 20, "expected_segments": None,
                             "truncated": True, "cancelled": False,
                             "duplicates": 0}, path)
        values = [c.value for r in load_workbook(path)["概览"].iter_rows()
                  for c in r]
        self.assertTrue(any(isinstance(v, str) and "可能未抓全" in v
                            for v in values))
        self.assertFalse(any(isinstance(v, str) and "None" in v for v in values),
                         "不能让 None 漏进给用户看的文案")


class MergeStatsTests(unittest.TestCase):
    """任务级统计的合并口径：截断/取消是"或"，段数是"和"。"""

    def _p(self, page, **kw):
        base = {"page": page, "part": "", "duration": 360, "rows": 1,
                "segments": 1, "expected_segments": 1, "duplicates": 0,
                "cancelled": False, "truncated": False}
        base.update(kw)
        return base

    def test_sums_segments_and_duplicates(self):
        st = pipeline._merge_stats([self._p(1, segments=3, duplicates=2),
                                    self._p(2, segments=4, duplicates=5)])
        self.assertEqual(st["segments"], 7)
        self.assertEqual(st["duplicates"], 7)
        self.assertEqual(st["expected_segments"], 2)

    def test_truncated_if_any_part_truncated(self):
        st = pipeline._merge_stats([self._p(1), self._p(2, truncated=True)])
        self.assertTrue(st["truncated"])

    def test_cancelled_if_any_part_cancelled(self):
        st = pipeline._merge_stats([self._p(1, cancelled=True), self._p(2)])
        self.assertTrue(st["cancelled"])

    def test_unknown_duration_anywhere_makes_the_total_unknown(self):
        st = pipeline._merge_stats([self._p(1),
                                    self._p(2, expected_segments=None)])
        self.assertIsNone(st["expected_segments"])


class AllPagesPipelineTests(unittest.TestCase):
    """all_pages=True：逐P串行、各用各的 cid、各落一份 jsonl。"""

    def _run(self, fetch, pages=MULTI_PAGES, claimed=10, **kw):
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        logs = []
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=claimed, pages=pages)), \
                patch("core.session.http_get_bytes", side_effect=fetch):
            result = pipeline.run_pipeline(
                "BV1GJ411x7h7", out, sleep=0, cancel=lambda: False,
                all_pages=True, progress=lambda **kw: logs.append(kw), **kw)
        return result, logs

    def test_every_part_is_crawled_with_its_own_cid(self):
        fetch = PerPartFetch({101: {1: make_segment(make_elem(1))},
                              202: {1: make_segment(make_elem(2))},
                              303: {1: make_segment(make_elem(3))}})
        result, _ = self._run(fetch)
        self.assertEqual(result["rows"], 3)
        # 每个分P两笔请求：第 1 段有数据、第 2 段空（到头了）。
        self.assertEqual(list(dict.fromkeys(fetch.cids)), [101, 202, 303],
                         "每个分P必须用它自己的 cid 去查弹幕")
        self.assertEqual([p["page"] for p in result["parts"]], [1, 2, 3])

    def test_rows_are_stamped_with_their_part(self):
        fetch = PerPartFetch({101: {1: make_segment(make_elem(1))},
                              202: {1: make_segment(make_elem(2))},
                              303: {1: make_segment(make_elem(3))}})
        result, _ = self._run(fetch)
        ws = load_workbook(result["xlsx"])["弹幕明细"]
        body = [list(r) for r in ws.iter_rows(min_row=4, values_only=True)]
        self.assertEqual([r[2] for r in body], ["P1 第一局", "P2 第二局",
                                                "P3 第三局"])
        self.assertEqual([r[3] for r in body], ["00:00:01"] * 3)

    def test_one_jsonl_per_part(self):
        fetch = PerPartFetch({101: {1: make_segment(make_elem(1))},
                              202: {1: make_segment(make_elem(2))}})
        result, _ = self._run(fetch, pages=MULTI_PAGES[:2])
        self.assertEqual(len(result["jsonl_files"]), 2)
        for path in result["jsonl_files"]:
            self.assertTrue(Path(path).exists(), path)
        self.assertEqual(len({Path(p).name for p in result["jsonl_files"]}), 2,
                         "两个分P不能写进同一个 jsonl，否则分不清哪条属于谁")
        # 多分P时 jsonl 指向目录，单分P时指向具体文件
        self.assertEqual(result["jsonl"], result["dir"])

    def test_meta_is_fetched_once_per_part_but_the_first_is_reused(self):
        fetch = PerPartFetch({101: {1: make_segment(make_elem(1))},
                              202: {1: make_segment(make_elem(2))},
                              303: {1: make_segment(make_elem(3))}})
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=10,
                                            pages=MULTI_PAGES)) as json_call, \
                patch("core.session.http_get_bytes", side_effect=fetch):
            pipeline.run_pipeline("BV1GJ411x7h7", out, sleep=0,
                                  cancel=lambda: False, all_pages=True)
        self.assertEqual(json_call.call_count, 3,
                         "3 个分P共 3 次 view：开头 1 次 + P2/P3 各 1 次，"
                         "首个分P的元信息不会被重复查")

    def test_cancel_between_parts_stops_the_loop(self):
        state = {"done": 0}

        def cancel():
            return state["done"] >= 1

        def progress(**kw):
            if "累计" in str(kw.get("text", "")):
                state["done"] += 1

        fetch = PerPartFetch({101: {1: make_segment(make_elem(1))},
                              202: {1: make_segment(make_elem(2))},
                              303: {1: make_segment(make_elem(3))}})
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=10, pages=MULTI_PAGES)), \
                patch("core.session.http_get_bytes", side_effect=fetch):
            pipeline.run_pipeline("BV1GJ411x7h7", out, sleep=0,
                                  cancel=cancel, all_pages=True,
                                  progress=progress)
        self.assertEqual(fetch.cids, [101], "取消后不许再碰下一个分P的接口")

    def test_cancel_before_the_first_part_still_yields_a_report(self):
        """用户按取消不等于视频没弹幕：得给一份写着"已取消"的报告，不是报错。"""
        fetch = PerPartFetch({})
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=1410, pages=MULTI_PAGES)), \
                patch("core.session.http_get_bytes", side_effect=fetch):
            result = pipeline.run_pipeline("BV1GJ411x7h7", out, sleep=0,
                                           cancel=lambda: True, all_pages=True)
        self.assertEqual(result["rows"], 0)
        self.assertTrue(result["stats"]["cancelled"])
        self.assertTrue(Path(result["xlsx"]).exists())
        self.assertEqual(fetch.cids, [], "取消后不该发出任何分段请求")

    def test_single_part_video_ignores_all_pages(self):
        """只有 1 个分P时勾了全抓也还是单分P路径，不该凭空多出分P列。

        同时锁住"弹幕分析"表在这里就位：分析是纯本地计算，跟分P无关，
        单分P也该有。
        """
        fetch = PerPartFetch({101: {1: make_segment(make_elem(1))}})
        result, _ = self._run(fetch, pages=MULTI_PAGES[:1])
        self.assertEqual(result["rows"], 1)
        self.assertEqual(load_workbook(result["xlsx"]).sheetnames,
                         ["概览", "弹幕分析", "弹幕明细", "密度分布"])

    def test_all_pages_off_only_touches_the_requested_part(self):
        fetch = PerPartFetch({202: {1: make_segment(make_elem(9))}})
        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=10, pages=MULTI_PAGES)), \
                patch("core.session.http_get_bytes", side_effect=fetch):
            pipeline.run_pipeline("https://www.bilibili.com/video/BV1GJ411x7h7?p=2",
                                  out, sleep=0, cancel=lambda: False)
        self.assertEqual(list(dict.fromkeys(fetch.cids)), [202],
                         "不勾全抓时只许碰 ?p= 指定的那个分P")



        payload = {"code": 0, "data": {"bvid": "BV1", "aid": 1, "cid": 9,
                                       "duration": 60, "pages":
                                       [{"cid": 1, "duration": 60, "part": "P1"}]}}
        with patch("core.session.http_get_json", return_value=payload):
            with self.assertRaises(ValueError) as ctx:
                core.fetch_video_meta(bvid="BV1", page=2)
        self.assertIn("分P", str(ctx.exception))

    def test_fetch_video_meta_surfaces_api_errors(self):
        with patch("core.session.http_get_json",
                   return_value={"code": -404, "message": "啥都木有"}):
            with self.assertRaises(ValueError) as ctx:
                core.fetch_video_meta(bvid="BV1")
        self.assertIn("-404", str(ctx.exception))

    def test_cancel_from_binary_channel_is_not_reported_as_no_danmaku(self):
        """二进制通道抛出的取消要按"取消"处理，不能落进"该视频没有弹幕"那条分支。

        视频自称有弹幕、一条没抓到——正常路径下这是要报错的。取消是唯一的例外：
        用户主动放弃不等于视频没数据，此时给一份空报告才是对的。
        """
        def boom(url, cancel=None):
            raise TaskCancelledError()

        out = tempfile.mkdtemp(prefix="danmaku_pipe_")
        with patch("core.session.http_get_json",
                   return_value=api_payload(claimed=1410)), \
                patch("core.session.http_get_bytes", side_effect=boom):
            result = pipeline.run_pipeline("BV1GJ411x7h7", out, sleep=0,
                                           cancel=lambda: True)
        self.assertEqual(result["rows"], 0)
        self.assertTrue(result["stats"]["cancelled"])
        self.assertTrue(Path(result["xlsx"]).exists())


class DanmakuPageTests(unittest.TestCase):
    """页面参数层：不建 QApplication，只验"存得下、读得出、往返不丢"。

    沿用仓库既有做法（`__new__` + 假控件），不启动 GUI。
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

    def _page(self, target="BV1GJ411x7h7", all_pages=False):
        page = DanmakuPage.__new__(DanmakuPage)
        page.target_edit = self._Edit(target)
        page.out_row = self._Row("D:\\out")
        page.segments_edit = self._Edit("20")
        page.sleep_edit = self._Edit("0.8")
        page.all_pages = self._Check(all_pages)
        page.auto_open = self._Check(True)
        return page

    def test_all_pages_defaults_to_off(self):
        self.assertFalse(self._page().collect_params()["all_pages"],
                         "全部分P是更重的一档，默认必须是不抓")

    def test_all_pages_round_trips_through_history(self):
        page = self._page(all_pages=True)
        reusable = page.history_reusable_params(page.collect_params())
        self.assertTrue(reusable["all_pages"])
        fresh = self._page(all_pages=False)
        fresh.apply_reusable_params(reusable)
        self.assertTrue(fresh.all_pages.isChecked(), "历史记录里的勾选要能填回来")

    def test_apply_reusable_params_does_not_start_a_task(self):
        page = self._page()
        with patch.object(TaskPage, "on_start") as start:
            page.apply_reusable_params({"target": "BV1GJ411x7h7",
                                        "all_pages": True})
        start.assert_not_called()
        self.assertTrue(page.all_pages.isChecked())

    def test_history_summary_distinguishes_all_parts(self):
        page = self._page()
        self.assertEqual(page.history_target_summary({"target": "BV1GJ411x7h7"}),
                         "BV1GJ411x7h7 P1")
        self.assertEqual(
            page.history_target_summary({"target": "BV1GJ411x7h7",
                                         "all_pages": True}),
            "BV1GJ411x7h7 全部分P")

    def test_history_output_paths_covers_excel_and_jsonl(self):
        page = self._page()
        self.assertEqual(page.history_output_paths(
            {"xlsx": "a.xlsx", "jsonl": "b.jsonl"}), ["a.xlsx", "b.jsonl"])
        self.assertEqual(page.history_output_paths(None), [])

    def test_bad_target_is_rejected_before_the_task_starts(self):
        """输入校验要在点开始时就报错，而不是等任务跑起来才失败。"""
        with self.assertRaises(ValueError):
            self._page(target="这不是视频").collect_params()


if __name__ == "__main__":
    unittest.main()
