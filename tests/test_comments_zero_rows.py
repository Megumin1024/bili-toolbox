# -*- coding: utf-8 -*-
"""comments 0 行评论的防御回归：启动即取消等路径拿到空行集也要能出报告。

历史缺陷（comments 预算验收备忘的预存项）：analyze([]) 在 0 赞占比、大会员
占比、盖楼占比上直接除以 n=0；export_xlsx 的「时间跨度」说明行对空 ctime
序列调 min()/max() 抛 ValueError。修复口径：占比与跨度说明降级为 "-"，
与 coverage 的既有兜底同口径，analyze + export 全程不抛异常。
"""
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from tools.comments.core import analyze, export_xlsx


def zero_meta():
    return {"author": "演示UP", "title": "演示视频", "claimed_comment_count": 0}


def sample_row(**overrides):
    row = {
        "rpid": 1, "is_main": True, "mid": 100, "uname": "用户", "vip": False,
        "sex": "男", "level": 6, "message": "普通评论内容", "like": 0,
        "rcount": 0, "ctime": 1_700_000_000, "location": "IP属地：北京",
    }
    row.update(overrides)
    return row


class ZeroRowCommentsTests(unittest.TestCase):
    def test_analyze_zero_rows_does_not_raise_and_degrades_kpi(self):
        report, kpi = analyze([], zero_meta())
        # 占比口径降级为 "-"（与 coverage 的既有兜底同口径）
        self.assertEqual(kpi["zero_like_pct"], "-")
        self.assertEqual(kpi["vip_pct"], "-")
        self.assertEqual(kpi["build_pct"], "-")
        self.assertEqual(kpi["hours"], "0")
        self.assertEqual(kpi["fetched"], 0)
        self.assertIn("0赞占比 -", report)

    def test_export_xlsx_zero_rows_writes_report_with_placeholder(self):
        _report, kpi = analyze([], zero_meta())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "zero-row.xlsx"
            export_xlsx([], zero_meta(), kpi, out)
            self.assertTrue(out.is_file())
            ws = load_workbook(str(out))["统计概览"]
            for row in ws.iter_rows(values_only=True):
                if "时间跨度" in [str(v) for v in row if v is not None]:
                    texts = [str(v) for v in row if v is not None]
                    self.assertIn("0 小时", " ".join(texts))
                    self.assertIn("-", texts)
                    break
            else:
                self.fail("统计概览中未找到「时间跨度」行")

    def test_nonzero_rows_keep_original_percent_strings(self):
        """非 0 行路径不变：占比字符串与改动前的 f-string 输出逐字节一致。"""
        rows = [
            sample_row(rpid=1, like=0, ctime=1_700_000_000),
            sample_row(rpid=2, mid=200, like=5, ctime=1_700_003_600),
        ]
        meta = zero_meta()
        meta["pub_ts"] = 1_699_900_000
        _report, kpi = analyze(rows, meta)
        self.assertEqual(kpi["zero_like_pct"], "50.0%")
        self.assertEqual(kpi["vip_pct"], "0.0%")
        self.assertEqual(kpi["build_pct"], "0.0%")
        self.assertEqual(kpi["hours"], "1")
        # 时间跨度说明行保持原有 "~" 连接格式
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rows.xlsx"
            export_xlsx(rows, meta, kpi, out)
            ws = load_workbook(str(out))["统计概览"]
            for row in ws.iter_rows(values_only=True):
                if "时间跨度" in [str(v) for v in row if v is not None]:
                    joined = " ".join(str(v) for v in row if v is not None)
                    self.assertIn("~", joined)
                    # 1_700_000_000 / 1_700_003_600 在本地时区均为 11-15
                    self.assertIn("11-15 06:13 ~ 11-15 07:13", joined)
                    break
            else:
                self.fail("统计概览中未找到「时间跨度」行")


if __name__ == "__main__":
    unittest.main()
