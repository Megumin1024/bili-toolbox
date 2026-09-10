# -*- coding: utf-8 -*-
"""P0 本地报告中心页面。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QHeaderView,
)

from app.widgets import PageHeader, PathRow, StatusPill, card, h2, muted
from core import output as output_mod, task_history

from . import core
from .chart import ReportChart
from .pipeline import (
    LocalWorker, run_export, run_file_comparison, run_period_comparison,
    run_refresh, run_search,
)


class ReportCenterPage(QWidget):
    """本地文件报告页；所有耗时操作走本页自有 QThread。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.manual_files: list[str] = []
        self.manual_dirs: list[str] = []
        self.sources: list[core.Source] = []
        self._source_map: dict[str, core.Source] = {}
        self._comparison: core.ComparisonResult | None = None
        self._period_comparison: core.PeriodComparisonResult | None = None
        self._thread: QThread | None = None
        self._worker: LocalWorker | None = None
        self._action = ""
        self._pending_search = False
        self._search_match_modes: dict[str, str] = {}
        self._build_ui()
        self.refresh_sources()

    def _build_ui(self):
        self.setObjectName("transparent")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("pageScroll")

        content = QWidget()
        content.setObjectName("transparent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(14)
        root.addWidget(PageHeader(
            "本地报告中心",
            "统一浏览评论结果、视频快照和监控历史；只读本地文件，不重新抓取。",
            "本地分析",
        ))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_browse_tab(), "浏览报告")
        self.tabs.addTab(self._build_compare_tab(), "文件对比")
        self.tabs.addTab(self._build_period_tab(), "时间段对比")
        self.tabs.addTab(self._build_export_tab(), "导出结果")
        root.addWidget(self.tabs)
        root.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def _build_browse_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(12)

        source_card, source_layout = card(variant="accent")
        head = QHBoxLayout()
        head.addWidget(h2("本地来源"))
        head.addStretch(1)
        self.status_pill = StatusPill("首次读取中…", "running")
        head.addWidget(self.status_pill)
        source_layout.addLayout(head)

        actions = QHBoxLayout()
        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_files.setObjectName("primary")
        self.btn_add_files.clicked.connect(self.add_files)
        self.btn_scan_dir = QPushButton("扫描目录")
        self.btn_scan_dir.clicked.connect(self.scan_directory)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_sources)
        self.btn_cancel = QPushButton("取消读取")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_current)
        for widget in (self.btn_add_files, self.btn_scan_dir, self.btn_refresh, self.btn_cancel):
            actions.addWidget(widget)
        actions.addStretch(1)
        source_layout.addLayout(actions)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索来源元数据或全量记录内容…")
        self.search_edit.textChanged.connect(self._apply_filters)
        filters.addWidget(self.search_edit, 3)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("全部类型", "")
        for kind in (core.KIND_COMMENT, core.KIND_VIDEO, core.KIND_MONITOR):
            self.kind_combo.addItem(core.KIND_LABELS[kind], kind)
        self.kind_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.kind_combo)
        self.format_combo = QComboBox()
        self.format_combo.addItem("全部格式", "")
        self.format_combo.addItem("JSONL", core.FORMAT_JSONL)
        self.format_combo.addItem("XLSX", core.FORMAT_XLSX)
        self.format_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.format_combo)
        self.status_combo = QComboBox()
        self.status_combo.addItem("全部状态", "")
        for status in (core.STATUS_READABLE, core.STATUS_INCOMPLETE, core.STATUS_DAMAGED,
                       core.STATUS_UNREADABLE, core.STATUS_UNCOMPARABLE):
            self.status_combo.addItem(core.STATUS_LABELS[status], status)
        self.status_combo.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.status_combo)
        source_layout.addLayout(filters)

        time_filters = QHBoxLayout()
        self.start_filter = QLineEdit()
        self.start_filter.setPlaceholderText("时间起点 ISO，例如 2026-09-01T00:00:00+08:00")
        self.start_filter.textChanged.connect(self._apply_filters)
        self.end_filter = QLineEdit()
        self.end_filter.setPlaceholderText("时间终点 ISO")
        self.end_filter.textChanged.connect(self._apply_filters)
        time_filters.addWidget(QLabel("时间范围"))
        time_filters.addWidget(self.start_filter, 1)
        time_filters.addWidget(QLabel("至"))
        time_filters.addWidget(self.end_filter, 1)
        source_layout.addLayout(time_filters)

        self.source_table = self._table(
            ["类型", "文件名", "路径", "格式", "大小 / 修改时间", "记录 / 状态", "数据时间范围", "可对比 / 导出"],
            minimum_height=170,
        )
        self.source_table.itemSelectionChanged.connect(self._show_selected_source)
        source_layout.addWidget(self.source_table)
        self.source_detail = muted("选择来源查看字段、SHA-256 和具体错误原因。")
        self.source_detail.setWordWrap(True)
        source_layout.addWidget(self.source_detail)
        layout.addWidget(source_card)

        preview_card, preview_layout = card()
        preview_layout.addWidget(h2("记录预览（仅展示上限，不影响全量统计）"))
        self.preview_table = self._table([], minimum_height=150)
        preview_layout.addWidget(self.preview_table)
        layout.addWidget(preview_card)
        return page

    def _build_compare_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        compare_card, compare_layout = card(variant="accent")
        compare_layout.addWidget(h2("任意两个本地文件"))
        compare_layout.addWidget(muted("必须选择两个具体 JSONL/XLSX 文件；类型不兼容时禁止开始，缺少稳定键时只做汇总。"))
        form = QFormLayout()
        self.compare_a = QComboBox()
        self.compare_b = QComboBox()
        form.addRow("文件 A", self.compare_a)
        form.addRow("文件 B", self.compare_b)
        compare_layout.addLayout(form)
        compare_actions = QHBoxLayout()
        self.btn_compare = QPushButton("开始文件对比")
        self.btn_compare.setObjectName("primary")
        self.btn_compare.clicked.connect(self.compare_files)
        compare_actions.addWidget(self.btn_compare)
        compare_actions.addWidget(QLabel("结果也可在“导出结果”标签导出"))
        compare_actions.addStretch(1)
        compare_layout.addLayout(compare_actions)
        layout.addWidget(compare_card)

        result_card, result_layout = card()
        self.compare_summary = muted("尚未生成文件对比。")
        self.compare_summary.setWordWrap(True)
        result_layout.addWidget(self.compare_summary)
        self.compare_table = self._table(
            ["指标", "A 值", "B 值", "绝对变化", "百分比变化", "可比较记录", "A 缺失", "B 缺失", "说明"],
            minimum_height=155,
        )
        result_layout.addWidget(self.compare_table)
        self.compare_chart = ReportChart()
        result_layout.addWidget(self.compare_chart)
        layout.addWidget(result_card)
        layout.addStretch(1)
        return page

    def _build_period_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        settings_card, settings_layout = card(variant="accent")
        settings_layout.addWidget(h2("同一来源的两个时间段"))
        settings_layout.addWidget(muted("时间格式必须包含时区或使用当前本地 +08:00；没有可靠时间字段或数值指标时禁止计算。"))
        form = QFormLayout()
        self.period_source = QComboBox()
        self.period_source.currentIndexChanged.connect(self._update_period_metrics)
        self.period_metric = QComboBox()
        form.addRow("数据来源", self.period_source)
        form.addRow("指标", self.period_metric)
        settings_layout.addLayout(form)
        periods = QHBoxLayout()
        self.period_a_start = QLineEdit("2026-01-01T00:00:00+08:00")
        self.period_a_end = QLineEdit("2026-01-01T23:59:59+08:00")
        self.period_b_start = QLineEdit("2026-01-02T00:00:00+08:00")
        self.period_b_end = QLineEdit("2026-01-02T23:59:59+08:00")
        for label, field in (("A 起", self.period_a_start), ("A 止", self.period_a_end),
                             ("B 起", self.period_b_start), ("B 止", self.period_b_end)):
            periods.addWidget(QLabel(label))
            periods.addWidget(field, 1)
        settings_layout.addLayout(periods)
        period_actions = QHBoxLayout()
        self.btn_period = QPushButton("开始时间段对比")
        self.btn_period.setObjectName("primary")
        self.btn_period.clicked.connect(self.compare_periods)
        period_actions.addWidget(self.btn_period)
        period_actions.addStretch(1)
        settings_layout.addLayout(period_actions)
        layout.addWidget(settings_card)

        period_result_card, period_result_layout = card()
        self.period_summary = muted("尚未生成时间段对比。")
        self.period_summary.setWordWrap(True)
        period_result_layout.addWidget(self.period_summary)
        self.period_table = self._table(["指标", "时间段 A", "时间段 B"], minimum_height=180)
        period_result_layout.addWidget(self.period_table)
        self.period_chart = ReportChart()
        period_result_layout.addWidget(self.period_chart)
        layout.addWidget(period_result_card)
        layout.addStretch(1)
        return page

    def _build_export_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        export_card, export_layout = card(variant="accent")
        export_layout.addWidget(h2("重新导出当前本地结果"))
        export_layout.addWidget(muted("不会重新抓取、不会启动任务；源文件以只读方式打开，导出前后验证 SHA-256、大小和 mtime_ns。"))
        default_dir = Path(self.cfg.get("out_dir") or output_mod.default_out_dir()) / "报告中心"
        form = QFormLayout()
        self.export_dir = PathRow(None, str(default_dir))
        form.addRow("报告输出目录", self.export_dir)
        export_layout.addLayout(form)
        current_row = QHBoxLayout()
        current_row.addWidget(QLabel("当前筛选来源"))
        self.export_source_hint = muted("请先在“浏览报告”中选择一个来源。")
        current_row.addWidget(self.export_source_hint, 1)
        export_layout.addLayout(current_row)
        current_actions = QHBoxLayout()
        for label, fmt in (("当前结果 JSONL", core.FORMAT_JSONL), ("当前结果 XLSX", core.FORMAT_XLSX)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=fmt: self.export_filtered(value))
            current_actions.addWidget(button)
        export_layout.addLayout(current_actions)
        compare_actions = QHBoxLayout()
        for label, fmt in (("文件对比 JSONL", core.FORMAT_JSONL), ("文件对比 XLSX", core.FORMAT_XLSX)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=fmt: self.export_comparison(value))
            compare_actions.addWidget(button)
        export_layout.addLayout(compare_actions)
        period_actions = QHBoxLayout()
        for label, fmt in (("时间段对比 JSONL", core.FORMAT_JSONL), ("时间段对比 XLSX", core.FORMAT_XLSX)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=fmt: self.export_period(value))
            period_actions.addWidget(button)
        export_layout.addLayout(period_actions)
        self.export_status = muted("导出状态：暂无操作。")
        self.export_status.setWordWrap(True)
        export_layout.addWidget(self.export_status)
        self.btn_open_export = QPushButton("打开报告所在目录")
        self.btn_open_export.setObjectName("flat")
        self.btn_open_export.clicked.connect(self.open_export_dir)
        export_layout.addWidget(self.btn_open_export)
        layout.addWidget(export_card)
        layout.addStretch(1)
        return page

    @staticmethod
    def _table(headers, minimum_height=120):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setMinimumHeight(minimum_height)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(False)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        for index in range(len(headers)):
            header.setSectionResizeMode(index, QHeaderView.Stretch)
        return table

    def _set_status(self, text, state="idle"):
        self.status_pill.setText(text)
        self.status_pill.set_state(state, text)

    def _start_worker(self, action, function, kwargs, done):
        if self._thread is not None and self._thread.isRunning():
            return False
        self._action = action
        action_text = {
            "refresh": "读取中…", "search": "搜索中…", "export": "导出中…",
            "compare": "文件对比中…", "period": "时间段对比中…",
        }
        self._set_status(action_text.get(action, "处理中…"), "running")
        self.btn_cancel.setEnabled(True)
        self.btn_refresh.setEnabled(False)
        self.btn_add_files.setEnabled(False)
        self.btn_scan_dir.setEnabled(False)
        self.btn_compare.setEnabled(False)
        self.btn_period.setEnabled(False)
        thread = QThread(self)
        worker = LocalWorker(function, kwargs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(done)
        worker.finished.connect(thread.quit)
        worker.failed.connect(self._on_worker_failed)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_finished)
        self._thread, self._worker = thread, worker
        thread.start()
        return True

    def _worker_finished(self):
        rerun_search = self._pending_search
        self._pending_search = False
        self._thread = None
        self._worker = None
        self.btn_cancel.setEnabled(False)
        self.btn_refresh.setEnabled(True)
        self.btn_add_files.setEnabled(True)
        self.btn_scan_dir.setEnabled(True)
        self.btn_compare.setEnabled(True)
        self.btn_period.setEnabled(True)
        if rerun_search:
            QTimer.singleShot(0, self._apply_filters)

    def _on_progress(self, value):
        text = value.get("text") if isinstance(value, dict) else "读取中…"
        self._set_status(str(text or "处理中…"), "running")

    def _on_worker_failed(self, message):
        if "源文件已变化" in message:
            self._clear_analysis_results()
        elif self._action == "compare":
            self._clear_comparison_result()
            self.compare_summary.setText(f"无法开始：{message}")
        elif self._action == "period":
            self._clear_period_result()
            self.period_summary.setText(f"无法开始：{message}")
        self._set_status("失败", "error")
        self.export_status.setText(f"导出/读取失败：{message}")
        self.source_detail.setText(f"操作失败：{message}")

    def refresh_sources(self):
        self._clear_analysis_results()
        if self._thread is not None and self._thread.isRunning():
            return
        self._start_worker(
            "refresh", run_refresh,
            {
                "history_records": task_history.load_history(),
                "manual_files": list(self.manual_files),
                "manual_dirs": list(self.manual_dirs),
                "monitor_roots": [str(item) for item in core.default_monitor_roots(self.cfg)],
            },
            self._on_refresh_done,
        )

    def _on_refresh_done(self, result):
        if result.get("cancelled"):
            self._set_status("已取消读取", "warning")
            return
        self.sources = list(result.get("sources", []))
        self._source_map = {source.path.casefold(): source for source in self.sources}
        self._populate_source_controls()
        self._set_status(f"已加载 {len(self.sources)} 个来源", "success")
        self._apply_filters()

    def add_files(self):
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "添加本地报告文件", "", "JSONL / XLSX (*.jsonl *.xlsx);;JSONL (*.jsonl);;XLSX (*.xlsx)"
        )
        existing = {str(Path(item).resolve(strict=False)).casefold() for item in self.manual_files}
        for value in paths:
            path = str(Path(value).resolve(strict=False))
            if path.casefold() not in existing:
                self.manual_files.append(path)
                existing.add(path.casefold())
        if paths:
            self._clear_analysis_results()
            self.refresh_sources()

    def scan_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择本地报告目录", "")
        if not path:
            return
        value = str(Path(path).resolve(strict=False))
        if value.casefold() not in {item.casefold() for item in self.manual_dirs}:
            self.manual_dirs.append(value)
        self._clear_analysis_results()
        self.refresh_sources()

    def cancel_current(self):
        if self._worker is not None:
            self._worker.cancel()
            self._set_status("正在取消…", "warning")

    def _apply_filters(self):
        start = core.parse_user_datetime(self.start_filter.text()) if hasattr(self, "start_filter") else None
        end = core.parse_user_datetime(self.end_filter.text()) if hasattr(self, "end_filter") else None
        kind = self.kind_combo.currentData() if hasattr(self, "kind_combo") else ""
        fmt = self.format_combo.currentData() if hasattr(self, "format_combo") else ""
        status = self.status_combo.currentData() if hasattr(self, "status_combo") else ""
        values = core.filter_sources(self.sources, "", kind, fmt, status, start, end)
        query = self.search_edit.text().strip().casefold() if hasattr(self, "search_edit") else ""
        if query:
            self._start_search(values, query)
            return
        self._search_match_modes = {}
        if self._thread is not None and self._thread.isRunning() and self._action == "search":
            self._pending_search = True
            self._worker.cancel()
            return
        self._fill_source_table(values)

    def _start_search(self, values, query):
        self._search_match_modes = {}
        if self._thread is not None and self._thread.isRunning():
            if self._action == "search":
                self._pending_search = True
                self._worker.cancel()
            else:
                self._pending_search = True
            return
        self._start_worker(
            "search", run_search,
            {"sources": values, "query": query}, self._on_search_done,
        )

    def _on_search_done(self, result):
        if result.get("cancelled"):
            self._search_match_modes = {}
            return
        self._search_match_modes = dict(result.get("match_modes", {}))
        self._fill_source_table(list(result.get("sources", [])))
        self._set_status(f"搜索命中 {len(result.get('sources', []))} 个来源", "success")

    def _clear_analysis_results(self):
        self._clear_comparison_result()
        self._clear_period_result()

    def _clear_comparison_result(self):
        self._comparison = None
        if hasattr(self, "compare_summary"):
            self.compare_summary.setText("尚未生成文件对比。")
            self.compare_table.setRowCount(0)
            self.compare_chart.clear_chart("尚未生成文件对比")

    def _clear_period_result(self):
        self._period_comparison = None
        if hasattr(self, "period_summary"):
            self.period_summary.setText("尚未生成时间段对比。")
            self.period_table.setRowCount(0)
            self.period_chart.clear_chart("尚未生成时间段对比")

    def _fill_source_table(self, values):
        self.source_table.setRowCount(0)
        for source in values:
            row = self.source_table.rowCount()
            self.source_table.insertRow(row)
            changed = [source.kind_label, source.file_name, source.path, source.fmt,
                       f"{_human_size(source.size)} / {_mtime(source)}",
                       f"{source.record_count:,} / {source.status_label}", source.time_range_label,
                       "是" if source.can_compare else "否"]
            for column, value in enumerate(changed):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value) if column != 2 else source.path)
                self.source_table.setItem(row, column, item)
            self.source_table.item(row, 0).setData(Qt.UserRole, source.path)

    def _show_selected_source(self):
        items = self.source_table.selectedItems()
        if not items:
            return
        path = self.source_table.item(items[0].row(), 0).data(Qt.UserRole)
        source = self._source_map.get(str(path).casefold())
        if source is None:
            return
        self.source_detail.setText(
            f"路径：{source.path}\n格式：{source.fmt} · SHA-256：{source.sha256 or '不可用'}\n"
            f"字段：{', '.join(source.fields) or '无'}\n"
            f"状态：{source.status_label} · {source.message or '无额外错误'}"
        )
        self._fill_preview(source)
        self.export_source_hint.setText(source.file_name)

    def _selected_source(self):
        items = self.source_table.selectedItems()
        if not items:
            return None
        path = self.source_table.item(items[0].row(), 0).data(Qt.UserRole)
        return self._source_map.get(str(path).casefold())

    def _export_query_for_source(self, source):
        """元数据命中导出完整来源；只有记录内容命中才沿用查询过滤。"""
        query = self.search_edit.text().strip()
        if not query:
            return ""
        return query if self._search_match_modes.get(source.path.casefold()) == "content" else ""

    def _fill_preview(self, source):
        rows = source.preview_rows[:core.PREVIEW_LIMIT]
        headers = list(source.original_headers) if source.original_headers else list(source.fields)
        if not headers and rows:
            headers = list(core._public_fields(rows[0]))
        headers = headers[:10]
        self.preview_table.clear()
        self.preview_table.setColumnCount(len(headers))
        self.preview_table.setHorizontalHeaderLabels(headers)
        for index in range(len(headers)):
            self.preview_table.horizontalHeader().setSectionResizeMode(index, QHeaderView.Stretch)
        self.preview_table.setRowCount(0)
        for row_data in rows:
            raw = row_data.get("_raw_fields") if isinstance(row_data, dict) else None
            values = raw if isinstance(raw, dict) else row_data
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            for column, header in enumerate(headers):
                value = values.get(header, "")
                item = QTableWidgetItem(str(core._json_value(value) or ""))
                item.setToolTip(item.text())
                self.preview_table.setItem(row, column, item)

    def _populate_source_controls(self):
        previous_a = self.compare_a.currentData() if hasattr(self, "compare_a") else None
        previous_b = self.compare_b.currentData() if hasattr(self, "compare_b") else None
        previous_period = self.period_source.currentData() if hasattr(self, "period_source") else None
        for combo in (self.compare_a, self.compare_b, self.period_source):
            combo.blockSignals(True)
            combo.clear()
        for source in self.sources:
            label = f"{source.kind_label} · {source.file_name} · {source.status_label}"
            self.compare_a.addItem(label, source.path)
            self.compare_b.addItem(label, source.path)
            if source.time_field:
                self.period_source.addItem(label, source.path)
        for combo in (self.compare_a, self.compare_b, self.period_source):
            combo.blockSignals(False)
        _restore_combo(self.compare_a, previous_a)
        _restore_combo(self.compare_b, previous_b)
        _restore_combo(self.period_source, previous_period)
        self._update_period_metrics()

    def compare_files(self):
        source_a = self._source_map.get(str(self.compare_a.currentData() or "").casefold())
        source_b = self._source_map.get(str(self.compare_b.currentData() or "").casefold())
        if not source_a or not source_b:
            self.compare_summary.setText("请选择文件 A 和文件 B。")
            return
        if source_a.path.casefold() == source_b.path.casefold():
            self.compare_summary.setText("文件 A 和文件 B 必须是两个具体文件。")
            return
        if source_a.kind != source_b.kind or source_a.kind not in {
            core.KIND_COMMENT, core.KIND_VIDEO, core.KIND_MONITOR,
        }:
            self._clear_comparison_result()
            self.compare_summary.setText(
                f"已阻止对比：类型不兼容：{source_a.kind_label} 不能与 {source_b.kind_label} 比较"
            )
            self.compare_chart.clear_chart("类型不兼容，暂无图表")
            return
        self._clear_comparison_result()
        self._start_worker(
            "compare", run_file_comparison,
            {"source_a": source_a, "source_b": source_b}, self._on_compare_done,
        )

    def _on_compare_done(self, result):
        if result.get("cancelled"):
            self._clear_comparison_result()
            self._set_status("已取消文件对比", "warning")
            return
        self._comparison = result.get("comparison")
        self._render_comparison(self._comparison)

    def _render_comparison(self, result):
        if result is None:
            self._clear_comparison_result()
            return
        if not result.compatible:
            self.compare_summary.setText(f"已阻止对比：{result.message}")
            self.compare_chart.clear_chart("类型不兼容，暂无图表")
            self.compare_table.setRowCount(0)
            return
        mode = "稳定键逐条匹配" if result.matching_mode == "key" else "仅汇总，无法逐条匹配"
        self.compare_summary.setText(
            f"A：{result.a_records:,} 条 · B：{result.b_records:,} 条 · {mode}\n"
            f"共同：{_display_count(result.common_records)} · 仅 A：{_display_count(result.only_a_records)} · "
            f"仅 B：{_display_count(result.only_b_records)} · 变化：{_display_count(result.changed_records)}\n"
            f"A 时间：{_range_text(result.time_range_a)}\nB 时间：{_range_text(result.time_range_b)}\n"
            f"缺失字段：A 缺 {', '.join(result.missing_fields_a) or '无'}；B 缺 {', '.join(result.missing_fields_b) or '无'}"
        )
        self.compare_table.setRowCount(0)
        for item in result.metric_changes:
            row = self.compare_table.rowCount()
            self.compare_table.insertRow(row)
            values = [item.field, item.a_value, item.b_value, item.absolute_change,
                      f"{item.percent_change:.2f}%" if item.percent_change is not None else "不可计算",
                      item.compared_records, item.missing_a, item.missing_b, item.note]
            for column, value in enumerate(values):
                self.compare_table.setItem(row, column, QTableWidgetItem(str(value if value is not None else "—")))
        series_a = [(item.field, item.a_value) for item in result.metric_changes if item.a_value is not None]
        series_b = [(item.field, item.b_value) for item in result.metric_changes if item.b_value is not None]
        self.compare_chart.set_series({"A": series_a, "B": series_b})

    def _update_period_metrics(self):
        self.period_metric.clear()
        source = self._source_map.get(str(self.period_source.currentData() or "").casefold())
        if source:
            self.period_metric.addItems(list(source.metrics))

    def compare_periods(self):
        source = self._source_map.get(str(self.period_source.currentData() or "").casefold())
        if not source:
            self.period_summary.setText("请选择一个有可靠时间字段的来源。")
            return
        metric = self.period_metric.currentText().strip()
        if not source.time_field:
            self._clear_period_result()
            self.period_summary.setText("无法开始：来源没有可靠时间字段，不能进行时间段对比")
            self.period_chart.clear_chart("暂无可绘制数据")
            return
        if metric not in source.metrics:
            self._clear_period_result()
            self.period_summary.setText(f"无法开始：指标“{metric}”不存在或不是可计算数值")
            self.period_chart.clear_chart("暂无可绘制数据")
            return
        self._clear_period_result()
        self._start_worker(
            "period", run_period_comparison,
            {
                "source": source,
                "period_a": (self.period_a_start.text(), self.period_a_end.text()),
                "period_b": (self.period_b_start.text(), self.period_b_end.text()),
                "metric": metric,
            }, self._on_period_done,
        )

    def _on_period_done(self, result):
        if result.get("cancelled"):
            self._clear_period_result()
            self._set_status("已取消时间段对比", "warning")
            return
        self._period_comparison = result.get("comparison")
        self._render_period_comparison(self._period_comparison)

    def _render_period_comparison(self, result):
        if result is None:
            self._clear_period_result()
            return
        self.period_summary.setText(f"指标：{result.metric} · A {result.period_a.start.isoformat()} ~ {result.period_a.end.isoformat()} · "
                                    f"B {result.period_b.start.isoformat()} ~ {result.period_b.end.isoformat()}")
        self.period_table.setRowCount(0)
        labels = ["样本数", "有效数值数", "缺失数据数", "缺失时间数", "起始值", "结束值", "平均值", "最大值", "最小值", "绝对变化", "百分比变化"]
        for label in labels:
            row = self.period_table.rowCount()
            self.period_table.insertRow(row)
            key = {"样本数": "sample_count", "有效数值数": "valid_count", "缺失数据数": "missing_data",
                   "缺失时间数": "missing_time", "起始值": "start_value", "结束值": "end_value",
                   "平均值": "average", "最大值": "maximum", "最小值": "minimum",
                   "绝对变化": "absolute_change", "百分比变化": "percent_change"}[label]
            for column, stats in enumerate((result.period_a, result.period_b), 1):
                value = getattr(stats, key)
                if key == "percent_change" and value is not None:
                    value = f"{value:.2f}%"
                self.period_table.setItem(row, column, QTableWidgetItem(str(value if value is not None else "—")))
            self.period_table.setItem(row, 0, QTableWidgetItem(label))
        series = {
            "A": [(core.format_datetime(item[0]), item[1]) for item in result.values_a],
            "B": [(core.format_datetime(item[0]), item[1]) for item in result.values_b],
        }
        if not any(series.values()):
            self.period_chart.clear_chart("暂无可绘制数据")
        else:
            self.period_chart.set_series(series)

    def export_filtered(self, fmt):
        source = self._selected_source()
        if not source:
            self.export_status.setText("导出失败：请先在“浏览报告”中选择来源。")
            return
        self._start_worker("export", run_export,
                           {"kind": "filtered", "payload": {"source": source, "query": self._export_query_for_source(source)},
                            "out_dir": self.export_dir.value(), "fmt": fmt}, self._on_export_done)

    def export_comparison(self, fmt):
        if not self._comparison:
            self.export_status.setText("导出失败：请先生成文件对比。")
            return
        self._start_worker("export", run_export,
                           {"kind": "file_comparison", "payload": {"comparison": self._comparison},
                            "out_dir": self.export_dir.value(), "fmt": fmt}, self._on_export_done)

    def export_period(self, fmt):
        if not self._period_comparison:
            self.export_status.setText("导出失败：请先生成时间段对比。")
            return
        self._start_worker("export", run_export,
                           {"kind": "period_comparison", "payload": {"comparison": self._period_comparison},
                            "out_dir": self.export_dir.value(), "fmt": fmt}, self._on_export_done)

    def _on_export_done(self, result):
        if result.get("cancelled"):
            self.export_status.setText("导出已取消。")
            return
        self.export_status.setText(f"导出成功：{result.get('path', '')}\n源文件 SHA-256、大小、mtime_ns 未变化。")
        self._set_status("导出完成", "success")

    def open_export_dir(self):
        path = Path(self.export_dir.value())
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606

    def on_app_close(self):
        self.cancel_current()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(1000)


def _restore_combo(combo, value):
    if value is None:
        return
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


def _human_size(value):
    number = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f} {unit}"
        number /= 1024


def _mtime(source):
    if not source.mtime_ns:
        return "—"
    return core.format_datetime(__import__("datetime").datetime.fromtimestamp(source.mtime_ns / 1_000_000_000, tz=core.LOCAL_TZ))


def _range_text(value):
    return f"{core.format_datetime(value[0]) or '—'} ~ {core.format_datetime(value[1]) or '—'}"


def _display_count(value):
    return "无法确定" if value is None else f"{value:,}"
