# -*- coding: utf-8 -*-
"""视频采集页。"""
import re
from pathlib import Path
from urllib.parse import urlsplit

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication,
                               QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QRadioButton, QSizePolicy, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app.task_page import TaskPage
from app.widgets import PathRow, StatusPill, card, h2, muted
from core import output as output_mod

from .comparison import METRICS, STATE_LABELS, SessionComparison, sort_rows
from .comparison_chart import ComparisonChart
from .pipeline import run_pipeline


_TABLE_FIELDS = (
    "bvid", "title", "owner", "status", "view", "delta_view",
    "view_per_hour", "like", "delta_like", "engagement_rate", "fetched_at",
)
_TABLE_HEADERS = (
    "BV号", "标题", "UP主", "本轮状态", "播放总量", "播放增量",
    "播放/小时", "点赞总量", "点赞增量", "点赞互动率", "最后成功采集时间",
)


def _format_number(value, decimals=0):
    if value is None:
        return "—"
    try:
        if not decimals and isinstance(value, int) and not isinstance(value, bool):
            return f"{value:,}"
        if decimals:
            return f"{float(value):,.{decimals}f}"
        return f"{int(value):,}" if float(value).is_integer() else f"{float(value):,.2f}"
    except (TypeError, ValueError, OverflowError):
        return "—"


def _format_time(value):
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _dashboard_summary_text(payload):
    summary = payload.get("summary") or {}
    count = payload.get("video_count", summary.get("video_count", 0))
    text = (f"视频数量：{count}  ·  播放总量：{_format_number(summary.get('total_view'))}  ·  "
            f"本次总增量：{_format_number(summary.get('total_delta_view'))}  ·  "
            f"最高增速：{summary.get('highest_rate_bvid') or '—'}"
            f"（{_format_number(summary.get('highest_rate'), decimals=1)}/小时）")
    if int(count or 0) < 2:
        text += "\n至少需要两个视频才能进行对比；原采集任务不受影响。"
    return text


def _set_dashboard_status(pill, state):
    label = STATE_LABELS.get(state, state)
    style_state = {
        "waiting_first": "idle",
        "only_one_round": "running",
        "tracking": "running",
        "cancelled": "warning",
        "partial_failure": "warning",
        "completed": "success",
    }.get(state, "idle")
    prefix = {"idle": "○", "running": "●", "warning": "◇", "success": "✓"}[style_state]
    pill.set_state(style_state, f"{prefix} {label}")


class ComparisonDialog(QDialog):
    """当前采集会话的非模态看板窗口；数据由 CollectorPage 持有。"""

    def __init__(self, page):
        super().__init__(page)
        self._page = page
        self.setObjectName("collectorComparisonDialog")
        self.setWindowTitle("多视频对比看板")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.addWidget(h2("多视频对比看板"))
        title_row.addStretch(1)
        self.dashboard_status = StatusPill("○ 等待首轮", "idle")
        title_row.addWidget(self.dashboard_status)
        root.addLayout(title_row)

        self.dashboard_summary = muted("任务开始并完成首轮后显示当前会话数据。")
        self.dashboard_summary.setWordWrap(True)
        root.addWidget(self.dashboard_summary)

        metric_row = QHBoxLayout()
        metric_row.addWidget(QLabel("图表指标"))
        self.dashboard_metric = QComboBox()
        for key, label in METRICS.items():
            self.dashboard_metric.addItem(label, key)
        self.dashboard_metric.currentIndexChanged.connect(page._on_dashboard_metric_changed)
        metric_row.addWidget(self.dashboard_metric)
        metric_row.addStretch(1)
        root.addLayout(metric_row)

        self.empty_hint = muted("暂无看板数据；完成首轮后会在这里显示。")
        self.empty_hint.setWordWrap(True)
        root.addWidget(self.empty_hint)

        self.dashboard_chart = ComparisonChart()
        self.dashboard_chart.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.dashboard_chart, 2)

        self.dashboard_table = QTableWidget(0, len(_TABLE_HEADERS))
        self.dashboard_table.setHorizontalHeaderLabels(list(_TABLE_HEADERS))
        self.dashboard_table.setObjectName("collectorComparisonTable")
        self.dashboard_table.setMinimumSize(0, 120)
        self.dashboard_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.dashboard_table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.dashboard_table.setSortingEnabled(False)
        self.dashboard_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.dashboard_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.dashboard_table.setWordWrap(False)
        self.dashboard_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header = self.dashboard_table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(page._on_dashboard_header_clicked)
        for index, width in enumerate((58, 105, 65, 72, 62, 62, 70, 62, 62, 78, 110)):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
            self.dashboard_table.setColumnWidth(index, width)
        root.addWidget(self.dashboard_table, 3)

        footer = QHBoxLayout()
        footer.addWidget(muted("图表仅展示当前指标前 10 名；表格保留全部已解析视频。点击表头可排序。"), 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        root.addLayout(footer)

        self.resize(1000, 700)

    def render(self, payload, sort_field, sort_descending):
        payload = payload if isinstance(payload, dict) else {}
        self.dashboard_summary.setText(_dashboard_summary_text(payload))
        state = str(payload.get("state") or "waiting_first")
        _set_dashboard_status(self.dashboard_status, state)

        rows = payload.get("rows") or []
        self.empty_hint.setVisible(not rows)
        table_rows = sort_rows(rows, sort_field, sort_descending)
        self.dashboard_table.setRowCount(0)
        for row_data in table_rows:
            row = self.dashboard_table.rowCount()
            self.dashboard_table.insertRow(row)
            values = [
                row_data.get("bvid") or "—",
                row_data.get("title") or "—",
                row_data.get("owner") or "—",
                row_data.get("status") or "—",
                _format_number(row_data.get("view")),
                _format_number(row_data.get("delta_view")),
                _format_number(row_data.get("view_per_hour"), decimals=1),
                _format_number(row_data.get("like")),
                _format_number(row_data.get("delta_like")),
                (_format_number(row_data.get("engagement_rate"), decimals=2) + "%"
                 if row_data.get("engagement_rate") is not None else "—"),
                _format_time(row_data.get("fetched_at")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, row_data.get(_TABLE_FIELDS[column]))
                if column in (1, 2):
                    item.setToolTip(str(value))
                self.dashboard_table.setItem(row, column, item)

        metric = str(self.dashboard_metric.currentData() or "view")
        metric_label = METRICS.get(metric, "播放总量")
        chart_rows = [row for row in sort_rows(rows, metric, True)[:10]
                      if row.get(metric) is not None]
        self.dashboard_chart.set_items(
            metric_label,
            [(row.get("bvid"), row.get(metric)) for row in chart_rows],
        )
        self.sync_sort_indicator(sort_field, sort_descending)

    def sync_sort_indicator(self, sort_field, sort_descending):
        try:
            column = _TABLE_FIELDS.index(sort_field)
        except ValueError:
            column = _TABLE_FIELDS.index("view")
        order = Qt.DescendingOrder if sort_descending else Qt.AscendingOrder
        self.dashboard_table.horizontalHeader().setSortIndicator(column, order)

    def select_metric(self, field):
        index = self.dashboard_metric.findData(field)
        if index >= 0 and index != self.dashboard_metric.currentIndex():
            self.dashboard_metric.blockSignals(True)
            self.dashboard_metric.setCurrentIndex(index)
            self.dashboard_metric.blockSignals(False)

    def fit_to_available(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = min(1000, max(420, available.width() - 48))
        height = min(700, max(360, available.height() - 72))
        self.setGeometry(
            available.x() + max(0, (available.width() - width) // 2),
            available.y() + max(0, (available.height() - height) // 2),
            width,
            height,
        )

class CollectorPage(TaskPage):
    tool_title = "视频采集"
    tool_subtitle = "视频 / 收藏夹 / 合集 批量采集公开数据 → 快照对比 / 定时追踪 → Excel（免登录）"
    tool_module = "视频采集"
    history_enabled = True
    history_tool_id = "collector"
    preset_enabled = True

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
        default_out = str(Path(self.cfg.get("out_dir")
                               or output_mod.default_out_dir()) / "采集导出")
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
        cached = getattr(self, "_validated_start_params", None)
        if cached is not None:
            return dict(cached)
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

    def build_post_result_card(self):
        self._comparison = SessionComparison()
        self._dashboard_payload = self._comparison.initial_payload()
        self._dashboard_sort_field = "view"
        self._dashboard_sort_descending = True
        self._dashboard_metric_key = "view"
        self._dashboard_dialog = None

        frame, layout = card(margin=12, spacing=6)
        title_row = QHBoxLayout()
        title_row.addWidget(h2("多视频对比看板"))
        title_row.addStretch(1)
        self.dashboard_status = StatusPill("○ 等待首轮", "idle")
        title_row.addWidget(self.dashboard_status)
        self.dashboard_open_button = QPushButton("打开看板")
        self.dashboard_open_button.setObjectName("secondary")
        self.dashboard_open_button.clicked.connect(self._open_dashboard)
        title_row.addWidget(self.dashboard_open_button)
        layout.addLayout(title_row)

        self.dashboard_summary = muted("任务开始并完成首轮后显示当前会话数据。")
        self.dashboard_summary.setWordWrap(True)
        layout.addWidget(self.dashboard_summary)

        scroll = self.findChild(QAbstractScrollArea, "pageScroll")
        if scroll is not None and scroll.widget() is not None:
            scroll.widget().setMinimumWidth(0)
            scroll.widget().setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        for widget in (self.params_card, self.result_card, frame):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._render_dashboard(self._dashboard_payload)
        return frame

    def collect_preset_params(self):
        try:
            sleep = max(0.05, float(self.sleep_edit.text().strip() or 0.3))
            interval_min = max(1, int(self.interval_edit.text().strip() or 60))
            rounds = max(0, int(self.rounds_edit.text().strip() or 5))
        except ValueError:
            raise ValueError("限速/间隔/轮数请填数字")
        return {
            "sources": [line for line in self.src_edit.toPlainText().splitlines() if line.strip()],
            "out_dir": self.out_row.value(),
            "sleep": sleep,
            "monitor": self.radio_monitor.isChecked(),
            "interval_min": interval_min,
            "rounds": rounds,
            "open_result": self.auto_open.isChecked(),
        }

    def pipeline(self):
        return run_pipeline

    def on_start(self):
        if self.runner is not None and self.runner.isRunning():
            return
        try:
            params = self.collect_params()
        except ValueError:
            # 交给基类显示统一的参数错误提示；此时必须保留上一份完整看板。
            super().on_start()
            return
        self._clear_dashboard()
        self._validated_start_params = params
        try:
            super().on_start()
        finally:
            self._validated_start_params = None

    def _ensure_dashboard_dialog(self):
        if self._dashboard_dialog is None:
            self._dashboard_dialog = ComparisonDialog(self)
        return self._dashboard_dialog

    def _open_dashboard(self):
        dialog = self._ensure_dashboard_dialog()
        dialog.select_metric(self._dashboard_metric_key)
        dialog.render(
            self._dashboard_payload,
            self._dashboard_sort_field,
            self._dashboard_sort_descending,
        )
        dialog.fit_to_available()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _clear_dashboard(self):
        self._comparison.reset()
        self._dashboard_payload = self._comparison.initial_payload()
        self._dashboard_sort_field = "view"
        self._dashboard_sort_descending = True
        self._dashboard_metric_key = "view"
        self._render_dashboard(self._dashboard_payload)

    def _on_progress(self, kw):
        super()._on_progress(kw)
        roster = kw.get("dashboard_roster")
        if isinstance(roster, (list, tuple)):
            self._comparison.reset(roster)
            self._dashboard_payload = self._comparison.initial_payload()
            self._render_dashboard(self._dashboard_payload)
        payload = kw.get("dashboard")
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            self._render_dashboard(payload)
        state = kw.get("dashboard_status")
        if state:
            self._set_dashboard_status(str(state))

    def _on_cancelled(self, result=None):
        super()._on_cancelled(result)
        payload = result.get("dashboard") if isinstance(result, dict) else None
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            payload = dict(self._dashboard_payload)
        else:
            payload = dict(payload)
        payload["state"] = "cancelled"
        payload["state_text"] = STATE_LABELS["cancelled"]
        self._render_dashboard(payload)

    def _on_dashboard_metric_changed(self, _index):
        dialog = self._dashboard_dialog
        if dialog is None:
            return
        field = str(dialog.dashboard_metric.currentData() or "view")
        self._dashboard_metric_key = field
        self._dashboard_sort_field = field
        self._dashboard_sort_descending = True
        self._render_dashboard(self._dashboard_payload)

    def _on_dashboard_header_clicked(self, column):
        field = _TABLE_FIELDS[column]
        if field == self._dashboard_sort_field:
            self._dashboard_sort_descending = not self._dashboard_sort_descending
        else:
            self._dashboard_sort_field = field
            self._dashboard_sort_descending = False
        self._render_dashboard(self._dashboard_payload)

    def _set_dashboard_status(self, state):
        _set_dashboard_status(self.dashboard_status, state)

    @staticmethod
    def _format_number(value, decimals=0):
        return _format_number(value, decimals)

    @staticmethod
    def _format_time(value):
        return _format_time(value)

    def _render_dashboard(self, payload):
        self._dashboard_payload = payload if isinstance(payload, dict) else self._comparison.initial_payload()
        self.dashboard_summary.setText(_dashboard_summary_text(self._dashboard_payload))
        self._set_dashboard_status(str(self._dashboard_payload.get("state") or "waiting_first"))
        if self._dashboard_dialog is not None:
            self._dashboard_dialog.select_metric(self._dashboard_metric_key)
            self._dashboard_dialog.render(
                self._dashboard_payload,
                self._dashboard_sort_field,
                self._dashboard_sort_descending,
            )

    def closeEvent(self, event):  # noqa: N802
        if self._dashboard_dialog is not None:
            self._dashboard_dialog.close()
        super().closeEvent(event)

    def hideEvent(self, event):  # noqa: N802
        if self._dashboard_dialog is not None:
            self._dashboard_dialog.hide()
        super().hideEvent(event)

    def history_target_summary(self, params):
        sources = params.get("sources", [])
        summaries = [_safe_source_summary(source) for source in sources]
        return "；".join(summaries)[:320] or "视频来源（已脱敏）"

    def history_reusable_params(self, params):
        return {
            "sources": list(params.get("sources", [])),
            "out_dir": params.get("out_dir", ""),
            "sleep": params.get("sleep", 0.3),
            "monitor": bool(params.get("monitor", False)),
            "interval_min": params.get("interval_min", 60),
            "rounds": params.get("rounds", 5),
            "open_result": bool(params.get("open_result", False)),
        }

    def history_output_paths(self, result):
        if not isinstance(result, dict):
            return []
        paths = [result.get("xlsx")]
        output_dir = result.get("dir")
        if output_dir:
            paths.append(str(Path(output_dir) / "snapshots.jsonl"))
        return paths

    def apply_reusable_params(self, params):
        sources = params.get("sources", [])
        if not isinstance(sources, (list, tuple)):
            sources = []
        self.src_edit.setPlainText("\n".join(str(source) for source in sources))
        self.out_row.set_value(params.get("out_dir", ""))
        self.sleep_edit.setText(str(params.get("sleep", 0.3)))
        self.radio_monitor.setChecked(bool(params.get("monitor", False)))
        self.radio_once.setChecked(not bool(params.get("monitor", False)))
        self.interval_edit.setText(str(params.get("interval_min", 60)))
        self.rounds_edit.setText(str(params.get("rounds", 5)))
        self.auto_open.setChecked(bool(params.get("open_result", False)))
        self.src_edit.setFocus()

    def apply_preset_params(self, params):
        self.apply_reusable_params(params)

    def on_finished(self, result):
        if isinstance(result, dict) and isinstance(result.get("dashboard"), dict):
            payload = result["dashboard"]
            if isinstance(payload.get("rows"), list):
                self._render_dashboard(payload)
        self.result_card.show_result(
            f"完成 ✓ {result['videos']} 个视频 / {result['snapshots']} 快照 / "
            f"{result['rounds']} 轮（上轮失败 {result['fail']}）",
            [("Excel 报告", result["xlsx"]),
             ("快照数据(jsonl)", str(result["dir"] + "/snapshots.jsonl")),
             ("打开输出目录", result["dir"])])


def _safe_source_summary(value):
    text = " ".join(str(value or "").split())
    match = re.search(r"(?i)\b(BV[0-9A-Za-z]+|av\d+)\b", text)
    if match:
        return match.group(1)
    try:
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if host == "b23.tv" or host.endswith(".bilibili.com") or host == "bilibili.com":
            path = parsed.path.rstrip("/") or "/"
            return f"{host}{path}"[:240]
    except ValueError:
        pass
    if text.lower().endswith(".txt"):
        return f"列表文件：{Path(text).name}"
    return "视频来源（已脱敏）"
