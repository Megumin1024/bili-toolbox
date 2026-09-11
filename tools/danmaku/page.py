# -*- coding: utf-8 -*-
"""弹幕抓取页。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QLineEdit)

from app.task_page import TaskPage
from app.widgets import PathRow, muted
from core import output as output_mod

from . import core
from .pipeline import run_pipeline

SAMPLE_TARGET = "视频链接或 BV 号：BV1GJ411x7h7 · 多分P可写 ?p=2"


class DanmakuPage(TaskPage):
    tool_title = "弹幕抓取"
    tool_subtitle = "输入视频链接 · 抓取弹幕 → 热词/高频弹幕/热点分钟 + 密度分布 Excel（无需登录）"
    tool_module = "弹幕分析"
    history_enabled = True
    history_tool_id = "danmaku"
    preset_enabled = True

    def build_params(self):
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)

        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText(SAMPLE_TARGET)
        form.addRow("视频", self.target_edit)

        default_out = str(Path(self.cfg.get("out_dir")
                               or output_mod.default_out_dir()) / "弹幕")
        self.out_row = PathRow(None, default_out)
        form.addRow("输出目录", self.out_row)

        self.segments_edit = QLineEdit(str(core.DEFAULT_MAX_SEGMENTS))
        self.segments_edit.setPlaceholderText(
            f"最多抓多少段（每段 {core.SEGMENT_SECONDS // 60} 分钟，"
            f"上限 {core.MAX_SEGMENTS_LIMIT}）")
        form.addRow("段数上限", self.segments_edit)

        self.sleep_edit = QLineEdit(str(core.DEFAULT_SLEEP))
        self.sleep_edit.setPlaceholderText(f"段间隔秒（默认 {core.DEFAULT_SLEEP}）")
        form.addRow("限速(秒/段)", self.sleep_edit)

        self.all_pages = QCheckBox("抓取全部分P（多分P视频串行抓完，耗时更长）")
        form.addRow("", self.all_pages)

        self.auto_open = QCheckBox("完成后自动打开 Excel")
        self.auto_open.setChecked(True)
        form.addRow("", self.auto_open)
        self.params_lay.addLayout(form)
        self.params_lay.addWidget(muted(
            "导出概览、弹幕分析（热词/高频弹幕/热点分钟，纯本机计算）、弹幕明细、"
            "按分钟的密度分布，并另出一份 Markdown 分析报告；\n"
            "多分P时另有分P汇总、并按分P重算密度。\n"
            "不勾「全部分P」时：链接里加 ?p=2 指定要抓哪个分P。"))
        self.target_edit.setFocus()

    @staticmethod
    def _parse_int(text, label, default, lo, hi):
        raw = (text or "").strip()
        if not raw:
            return default
        try:
            value = int(float(raw))
        except ValueError:
            raise ValueError(f"{label}需为数字")
        if not lo <= value <= hi:
            raise ValueError(f"{label}需在 {lo}~{hi} 之间")
        return value

    def _read(self):
        target = self.target_edit.text().strip()
        if not target:
            raise ValueError("请先输入视频链接或 BV 号")
        core.parse_target(target)             # 提前报错，别等任务跑起来才失败
        out_dir = self.out_row.value()
        if not out_dir:
            raise ValueError("请设置输出目录")
        max_segments = self._parse_int(
            self.segments_edit.text(), "段数上限",
            core.DEFAULT_MAX_SEGMENTS, 1, core.MAX_SEGMENTS_LIMIT)
        try:
            sleep = max(0.0, float(self.sleep_edit.text().strip()
                                   or core.DEFAULT_SLEEP))
        except ValueError:
            raise ValueError("限速需为数字（秒/段）")
        return target, out_dir, max_segments, sleep

    def collect_params(self):
        target, out_dir, max_segments, sleep = self._read()
        return {"target": target, "out_dir": out_dir,
                "max_segments": max_segments, "sleep": sleep,
                "all_pages": self.all_pages.isChecked(),
                "open_result": self.auto_open.isChecked()}

    def collect_preset_params(self):
        return self.collect_params()

    def apply_reusable_params(self, params):
        self.target_edit.setText(str(params.get("target", "")))
        self.out_row.set_value(params.get("out_dir", ""))
        self.segments_edit.setText(
            str(params.get("max_segments", core.DEFAULT_MAX_SEGMENTS)))
        self.sleep_edit.setText(str(params.get("sleep", core.DEFAULT_SLEEP)))
        self.all_pages.setChecked(bool(params.get("all_pages", False)))
        self.auto_open.setChecked(bool(params.get("open_result", True)))
        self.target_edit.setFocus()

    def apply_preset_params(self, params):
        self.apply_reusable_params(params)

    def pipeline(self):
        return run_pipeline

    def history_target_summary(self, params):
        try:
            bvid, aid, page = core.parse_target(params.get("target", ""))
        except ValueError:
            return "弹幕抓取（未识别目标）"
        scope = "全部分P" if params.get("all_pages") else f"P{page}"
        return f"{bvid or f'av{aid}'} {scope}"

    def history_reusable_params(self, params):
        return {
            "target": params.get("target", ""),
            "out_dir": params.get("out_dir", ""),
            "max_segments": params.get("max_segments", core.DEFAULT_MAX_SEGMENTS),
            "sleep": params.get("sleep", core.DEFAULT_SLEEP),
            "all_pages": bool(params.get("all_pages", False)),
            "open_result": bool(params.get("open_result", True)),
        }

    def history_output_paths(self, result):
        if not isinstance(result, dict):
            return []
        # 过滤空值：分析报告写失败时 report 是空串，而空路径会被 Path("")
        # 当成当前目录塞进历史记录里，点开就落到别处去了。
        return [path for path in
                (result.get(key) for key in ("xlsx", "report", "jsonl")) if path]

    def on_finished(self, result):
        stats = result.get("stats") or {}
        parts = result.get("parts") or []
        tail = "（已达段数上限，可能还有更多）" if stats.get("truncated") else ""
        scope = f" · {len(parts)} 个分P" if len(parts) > 1 else ""
        jsonl_label = "原始数据(jsonl)" if len(parts) <= 1 else "原始数据(jsonl 目录)"
        links = [("Excel 报告", result["xlsx"])]
        # 报告可能写失败，失败时是空串——空路径会被 Path("") 当成当前目录，
        # 于是"打开报告"会静默指向工作目录。宁可不显示这一项。
        if result.get("report"):
            links.append(("分析报告(MD)", result["report"]))
        links += [(jsonl_label, result["jsonl"]),
                  ("打开输出目录", result["dir"])]
        self.result_card.show_result(
            f"完成 ✓ 共 {result['rows']:,} 条弹幕{scope}{tail}", links)
