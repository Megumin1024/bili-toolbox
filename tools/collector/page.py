# -*- coding: utf-8 -*-
"""视频采集页。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit,
                               QRadioButton, QVBoxLayout, QWidget)

from app.task_page import TaskPage
from app.widgets import PathRow, muted

from .pipeline import run_pipeline


class CollectorPage(TaskPage):
    tool_title = "视频采集"
    tool_subtitle = "视频 / 收藏夹 / 合集 批量采集公开数据 → 快照对比 / 定时追踪 → Excel（免登录）"

    def build_params(self):
        self.params_lay.addWidget(QLabel("视频来源（每行一个：视频链接/BV/av 号；收藏夹、合集、"
                                         "系列链接；或 .txt 列表文件）"))
        self.src_edit = QPlainTextEdit()
        self.src_edit.setPlaceholderText(
            "https://www.bilibili.com/video/BVxxxx\n"
            "https://space.bilibili.com/xxx/favlist?fid=xxx\n"
            "https://space.bilibili.com/xxx/channel/collectiondetail?sid=xxx\n"
            "C:\\path\\to\\list.txt")
        self.src_edit.setMaximumHeight(110)
        self.params_lay.addWidget(self.src_edit)

        mode_box = QGroupBox("模式")
        mv = QHBoxLayout(mode_box)
        self.radio_once = QRadioButton("单次快照采集")
        self.radio_once.setChecked(True)
        self.radio_monitor = QRadioButton("定时追踪（时序/增速分析）")
        mv.addWidget(self.radio_once)
        mv.addWidget(self.radio_monitor)
        mv.addWidget(QLabel("间隔(分钟)"))
        self.interval_edit = QLineEdit("60")
        self.interval_edit.setMaximumWidth(60)
        mv.addWidget(self.interval_edit)
        mv.addWidget(QLabel("轮数(0=无限)"))
        self.rounds_edit = QLineEdit("5")
        self.rounds_edit.setMaximumWidth(60)
        mv.addWidget(self.rounds_edit)
        mv.addStretch(1)
        self.params_lay.addWidget(mode_box)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)
        default_out = str((self.cfg.get("out_dir") or ".") + "/采集导出")
        self.out_row = PathRow(None, default_out)
        form.addRow("输出目录", self.out_row)
        self.sleep_edit = QLineEdit("0.3")
        self.sleep_edit.setPlaceholderText("每视频间隔秒（默认 0.3）")
        form.addRow("限速(秒/视频)", self.sleep_edit)
        self.auto_open = QCheckBox("完成后自动打开 Excel")
        form.addRow("", self.auto_open)
        self.params_lay.addLayout(form)
        self.params_lay.addWidget(muted(
            "快照断点续传：已完成部分保留在 snapshots.jsonl，中断后重跑自动衔接。"
            "定时追踪模式每轮输出当前 Excel，结束后生成含增速榜的最终报告。"))

    def collect_params(self):
        src_lines = [l for l in self.src_edit.toPlainText().splitlines() if l.strip()]
        if not src_lines:
            raise ValueError("请先输入视频链接、收藏夹或合集链接（可多行）")
        out_dir = self.out_row.value()
        if not out_dir:
            raise ValueError("请设置输出目录")
        try:
            sleep = max(0.05, float(self.sleep_edit.text().strip() or 0.3))
            interval_min = max(1, int(self.interval_edit.text().strip() or 60))
            rounds = max(0, int(self.rounds_edit.text().strip() or 5))
        except ValueError:
            raise ValueError("限速/间隔/轮数请填数字")
        return {"sources": src_lines, "out_dir": out_dir, "sleep": sleep,
                "monitor": self.radio_monitor.isChecked(),
                "interval_min": interval_min, "rounds": rounds,
                "open_result": self.auto_open.isChecked()}

    def pipeline(self):
        return run_pipeline

    def on_finished(self, result):
        self.result_card.show_result(
            f"完成 ✓ {result['videos']} 个视频 / {result['snapshots']} 快照 / "
            f"{result['rounds']} 轮（上轮失败 {result['fail']}）",
            [("Excel 报告", result["xlsx"]),
             ("快照数据(jsonl)", str(result["dir"] + "/snapshots.jsonl")),
             ("打开输出目录", result["dir"])])
