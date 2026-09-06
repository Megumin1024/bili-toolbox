# -*- coding: utf-8 -*-
"""评论抓取页。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QLineEdit, QWidget)

from app.task_page import TaskPage
from app.widgets import PathRow, muted

from .pipeline import run_pipeline

SAMPLE_LINK = "https://t.bilibili.com/1242626908874604548"


class CommentsPage(TaskPage):
    tool_title = "评论抓取"
    tool_subtitle = "动态 / 视频 · 游客 gRPC 通道全量评论 → 精确分析 → Excel（免登录）"

    def build_params(self):
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)

        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText(SAMPLE_LINK)
        form.addRow("动态/视频链接", self.link_edit)

        default_out = str((self.cfg.get("out_dir") or ".") + "/评论导出")
        self.out_row = PathRow(None, default_out)
        form.addRow("输出目录", self.out_row)

        self.sleep_edit = QLineEdit("0.2")
        self.sleep_edit.setPlaceholderText("每页间隔秒（默认 0.2）")
        form.addRow("限速(秒/页)", self.sleep_edit)

        self.tls_check = QCheckBox("实验：Chrome TLS 指纹 gRPC 通道（失败自动回退 grpcio）")
        form.addRow("", self.tls_check)
        self.auto_open = QCheckBox("完成后自动打开 Excel")
        self.auto_open.setChecked(True)
        form.addRow("", self.auto_open)
        self.params_lay.addLayout(form)
        self.params_lay.addWidget(muted(
            "支持：t.bilibili.com/xxx · bilibili.com/opus/xxx · bilibili.com/video/BVxxxx · "
            "b23.tv 短链 · av号 · 纯数字动态ID。支持断点续传：中断后重跑自动从断点继续。"))
        self.link_edit.setFocus()

    def collect_params(self):
        url = self.link_edit.text().strip()
        if not url:
            raise ValueError("请先输入动态或视频链接")
        out_dir = self.out_row.value()
        if not out_dir:
            raise ValueError("请设置输出目录")
        try:
            sleep = max(0.05, float(self.sleep_edit.text().strip() or 0.2))
        except ValueError:
            raise ValueError("限速需为数字（秒/页）")
        return {"url": url, "out_dir": out_dir, "sleep": sleep, "max_pages": 0,
                "use_tls_grpc": self.tls_check.isChecked(),
                "open_result": self.auto_open.isChecked()}

    def pipeline(self):
        return run_pipeline

    def on_finished(self, result):
        self.result_card.show_result(
            f"完成 ✓ 共 {result['rows']:,} 条评论（主楼 {result['stats']['main']:,} + "
            f"楼中楼 {result['stats']['sub']:,}）",
            [("Excel 报告", result["xlsx"]),
             ("分析报告(MD)", result["report"]),
             ("原始数据(jsonl)", result["jsonl"]),
             ("打开输出目录", result["dir"])])
