# -*- coding: utf-8 -*-
"""关系分析页面：本地粉丝/关注清单 → 集合与快照差异分析 → Excel。

零网络工具：只读用户选择的本地文件，界面固定位置明示来源声明。
页面范式与 tools/data_check 一致（TaskPage + 本地校验 + TaskRunner）。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QWidget)

from app.task_page import TaskPage
from app.widgets import PathRow, muted
from core import output as output_mod

from .core import validate_inputs
from .pipeline import ROSTER_KEYS, run_pipeline

SOURCE_DECLARATION = "仅处理你合法取得的数据；本工具不发起任何网络请求，结果只保存在本机。"
COLUMN_HINT = ("清单列名自动识别：mid（mid/uid/id）、昵称（uname/昵称/name/nickname）、"
               "时间（mtime/ptime/ctime/follow_time/关注时间）；CSV 首行表头，"
               "JSON 为对象数组。")
KEY_TO_ROW = {"fans_t1": "fans1_row", "follows_t1": "follows1_row",
              "fans_t2": "fans2_row", "follows_t2": "follows2_row"}


class _FileRow(QWidget):
    """单行文件选择：输入框 + 浏览按钮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transparent")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.edit = QLineEdit()
        lay.addWidget(self.edit, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

    def _browse(self):
        start = str(Path(self.value()).parent) if self.value() else ""
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择清单文件", start,
            "CSV / JSON (*.csv *.json);;CSV (*.csv);;JSON (*.json)")
        if path:
            self.edit.setText(path)

    def value(self):
        return self.edit.text().strip()

    def set_value(self, value):
        self.edit.setText(str(value))


class RelationAnalysisPage(TaskPage):
    tool_title = "关系分析"
    tool_subtitle = "本地粉丝/关注清单分析 → 互关/快照差异 → Excel（零网络）"
    tool_module = "本地数据分析"
    history_enabled = True
    history_tool_id = "relation_analysis"

    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)
        self.btn_start.setText("开始分析")
        self.btn_cancel.setText("取消分析")

    def build_params(self):
        self.params_lay.addWidget(QLabel(
            "选择本地清单文件（时点 2 两项可留空，做单时点分析）"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)
        self.fans1_row = _FileRow()
        form.addRow("时点 1 · 粉丝清单", self.fans1_row)
        self.follows1_row = _FileRow()
        form.addRow("时点 1 · 关注清单", self.follows1_row)
        self.fans2_row = _FileRow()
        form.addRow("时点 2 · 粉丝清单（可选）", self.fans2_row)
        self.follows2_row = _FileRow()
        form.addRow("时点 2 · 关注清单（可选）", self.follows2_row)
        default_out = str(Path(self.cfg.get("out_dir")
                               or output_mod.default_out_dir()) / "关系分析")
        self.out_row = PathRow(None, default_out)
        form.addRow("输出目录", self.out_row)
        self.params_lay.addLayout(form)
        self.params_lay.addWidget(muted(SOURCE_DECLARATION))
        self.params_lay.addWidget(muted(COLUMN_HINT))

    def collect_params(self):
        paths, out_dir = validate_inputs(
            self.fans1_row.value(), self.follows1_row.value(),
            self.fans2_row.value(), self.follows2_row.value(),
            self.out_row.value())
        kwargs = {"out_dir": str(out_dir)}
        for key in ROSTER_KEYS:
            path = paths.get(key)
            kwargs[key] = str(path) if path else ""
        return kwargs

    def pipeline(self):
        return run_pipeline

    def on_finished(self, result):
        self.result_card.show_result(
            "分析完成 ✓",
            [("分析报告", result.get("excel")),
             ("打开输出目录", result.get("dir"))],
        )

    # ---- 任务历史钩子 ----

    def history_target_summary(self, params):
        names = [Path(params[key]).name for key in ROSTER_KEYS if params.get(key)]
        names = [name for name in names if name]
        summary = f"分析 {len(names)} 个本地清单文件"
        if names:
            summary += f"：{'、'.join(names)}"
        return summary[:320]

    def history_reusable_params(self, params):
        data = {"out_dir": params.get("out_dir", "")}
        for key in ROSTER_KEYS:
            data[key] = params.get(key, "")
        return data

    def history_output_paths(self, result):
        if not isinstance(result, dict) or not result.get("excel"):
            return []
        return [result.get("excel")]

    def apply_reusable_params(self, params):
        for key in ROSTER_KEYS:
            getattr(self, KEY_TO_ROW[key]).set_value(params.get(key, ""))
        self.out_row.set_value(params.get("out_dir", ""))
