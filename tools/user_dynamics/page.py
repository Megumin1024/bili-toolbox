# -*- coding: utf-8 -*-
"""用户动态页。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QLineEdit)

from app.task_page import TaskPage
from app.widgets import PathRow, muted
from core import output as output_mod

from . import core
from .pipeline import run_pipeline

SAMPLE_TARGET = "UID 或空间链接：946974 · space.bilibili.com/946974/dynamic"


class UserDynamicsPage(TaskPage):
    tool_title = "用户动态"
    tool_subtitle = "输入 UID · 抓取该用户的公开动态 → Excel（无需登录）"
    tool_module = "用户分析"
    history_enabled = True
    history_tool_id = "user_dynamics"
    preset_enabled = True

    def build_params(self):
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)

        self.uid_edit = QLineEdit()
        self.uid_edit.setPlaceholderText(SAMPLE_TARGET)
        form.addRow("用户 UID", self.uid_edit)

        default_out = str(Path(self.cfg.get("out_dir")
                               or output_mod.default_out_dir()) / "用户动态")
        self.out_row = PathRow(None, default_out)
        form.addRow("输出目录", self.out_row)

        self.pages_edit = QLineEdit(str(core.DEFAULT_MAX_PAGES))
        self.pages_edit.setPlaceholderText(
            f"最多抓多少页（每页约 {core.PAGE_SIZE} 条，上限 {core.MAX_PAGES_LIMIT}）")
        form.addRow("页数上限", self.pages_edit)

        self.sleep_edit = QLineEdit(str(core.DEFAULT_SLEEP))
        self.sleep_edit.setPlaceholderText(
            f"页间隔秒（默认 {core.DEFAULT_SLEEP}）")
        form.addRow("限速(秒/页)", self.sleep_edit)

        self.auto_open = QCheckBox("完成后自动打开 Excel")
        self.auto_open.setChecked(True)
        form.addRow("", self.auto_open)
        self.params_lay.addLayout(form)
        self.params_lay.addWidget(muted(
            "读取该用户的公开动态：图文、转发与投稿。\n"
            "接口偶尔返回空结果，会自动重试；连续取空时报错，"
            "不会当成「该用户没有动态」。"))
        self.uid_edit.setFocus()

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
        target = self.uid_edit.text().strip()
        if not target:
            raise ValueError("请先输入 UID 或用户空间链接")
        uid = core.parse_uid(target)          # 提前报错，别等任务跑起来才失败
        out_dir = self.out_row.value()
        if not out_dir:
            raise ValueError("请设置输出目录")
        max_pages = self._parse_int(self.pages_edit.text(), "页数上限",
                                    core.DEFAULT_MAX_PAGES, 1, core.MAX_PAGES_LIMIT)
        try:
            sleep = max(0.0, float(self.sleep_edit.text().strip()
                                   or core.DEFAULT_SLEEP))
        except ValueError:
            raise ValueError("限速需为数字（秒/页）")
        return target, uid, out_dir, max_pages, sleep

    def collect_params(self):
        target, _uid, out_dir, max_pages, sleep = self._read()
        return {"target": target, "out_dir": out_dir, "max_pages": max_pages,
                "sleep": sleep, "open_result": self.auto_open.isChecked()}

    def collect_preset_params(self):
        return self.collect_params()

    def apply_reusable_params(self, params):
        self.uid_edit.setText(str(params.get("target", "")))
        self.out_row.set_value(params.get("out_dir", ""))
        self.pages_edit.setText(str(params.get("max_pages", core.DEFAULT_MAX_PAGES)))
        self.sleep_edit.setText(str(params.get("sleep", core.DEFAULT_SLEEP)))
        self.auto_open.setChecked(bool(params.get("open_result", True)))
        self.uid_edit.setFocus()

    def apply_preset_params(self, params):
        self.apply_reusable_params(params)

    def pipeline(self):
        return run_pipeline

    def history_target_summary(self, params):
        try:
            return f"UID {core.parse_uid(params.get('target', ''))}"
        except ValueError:
            return "用户动态（未识别目标）"

    def history_reusable_params(self, params):
        return {
            "target": params.get("target", ""),
            "out_dir": params.get("out_dir", ""),
            "max_pages": params.get("max_pages", core.DEFAULT_MAX_PAGES),
            "sleep": params.get("sleep", core.DEFAULT_SLEEP),
            "open_result": bool(params.get("open_result", True)),
        }

    def history_output_paths(self, result):
        if not isinstance(result, dict):
            return []
        return [result.get(key) for key in ("xlsx", "jsonl")]

    def on_finished(self, result):
        stats = result.get("stats") or {}
        tail = "（已达页数上限，可能还有更早的动态）" if stats.get("truncated") else ""
        self.result_card.show_result(
            f"完成 ✓ 共 {result['rows']:,} 条动态{tail}",
            [("Excel 报告", result["xlsx"]),
             ("原始数据(jsonl)", result["jsonl"]),
             ("打开输出目录", result["dir"])])
