# -*- coding: utf-8 -*-
"""评论抓取页。"""
import re
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QLineEdit, QWidget)

from app.task_page import TaskPage
from app.widgets import PathRow, muted

from .pipeline import run_pipeline

SAMPLE_LINK = "粘贴动态或视频链接：t.bilibili.com/… · bilibili.com/video/BV… · b23.tv/… · av…"


class CommentsPage(TaskPage):
    tool_title = "评论抓取"
    tool_subtitle = "动态 / 视频 · 游客 gRPC 通道全量评论 → 精确分析 → Excel（免登录）"
    tool_module = "评论分析"
    history_enabled = True
    history_tool_id = "comments"
    preset_enabled = True

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

    def collect_preset_params(self):
        try:
            sleep = max(0.05, float(self.sleep_edit.text().strip() or 0.2))
        except ValueError:
            raise ValueError("限速需为数字（秒/页）")
        return {
            "url": self.link_edit.text().strip(),
            "out_dir": self.out_row.value(),
            "sleep": sleep,
            "use_tls_grpc": self.tls_check.isChecked(),
            "open_result": self.auto_open.isChecked(),
        }

    def pipeline(self):
        return run_pipeline

    def history_target_summary(self, params):
        return _safe_target_summary(params.get("url", ""))

    def history_reusable_params(self, params):
        return {
            "url": params.get("url", ""),
            "out_dir": params.get("out_dir", ""),
            "sleep": params.get("sleep", 0.2),
            "use_tls_grpc": bool(params.get("use_tls_grpc", False)),
            "open_result": bool(params.get("open_result", True)),
        }

    def history_output_paths(self, result):
        if not isinstance(result, dict):
            return []
        return [result.get(key) for key in ("xlsx", "report", "jsonl")]

    def apply_reusable_params(self, params):
        self.link_edit.setText(str(params.get("url", "")))
        self.out_row.set_value(params.get("out_dir", ""))
        self.sleep_edit.setText(str(params.get("sleep", 0.2)))
        self.tls_check.setChecked(bool(params.get("use_tls_grpc", False)))
        self.auto_open.setChecked(bool(params.get("open_result", True)))
        self.link_edit.setFocus()

    def apply_preset_params(self, params):
        self.apply_reusable_params(params)

    def on_finished(self, result):
        self.result_card.show_result(
            f"完成 ✓ 共 {result['rows']:,} 条评论（主楼 {result['stats']['main']:,} + "
            f"楼中楼 {result['stats']['sub']:,}）",
            [("Excel 报告", result["xlsx"]),
             ("分析报告(MD)", result["report"]),
             ("原始数据(jsonl)", result["jsonl"]),
             ("打开输出目录", result["dir"])])


def _safe_target_summary(value):
    """只保留 B 站目标的主机/路径或显式 ID，丢弃查询参数。"""
    text = " ".join(str(value or "").split())
    if not text:
        return "评论目标（已脱敏）"
    match = re.search(r"(?i)\b(BV[0-9A-Za-z]+|av\d+)\b", text)
    if match:
        return match.group(1)
    if text.isdigit():
        return f"动态 ID {text}"
    try:
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if host == "b23.tv" or host.endswith(".bilibili.com") or host == "bilibili.com":
            path = parsed.path.rstrip("/") or "/"
            return f"{host}{path}"[:240]
    except ValueError:
        pass
    return "评论目标（已脱敏）"
