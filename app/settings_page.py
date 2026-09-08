# -*- coding: utf-8 -*-
"""设置页：外观 / 默认输出目录 / 网络与风控（通道+代理池）/ 关于。"""
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QListWidget, QPlainTextEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from core import config, diagnostics, session, task_history

import core
from .widgets import PageHeader, PathRow, StatusPill, card, h2, muted, open_path

VERSION = f"v{core.__version__}"

THEME_LABELS = [("dark", "深色"), ("light", "浅色")]
TRANSPORT_LABELS = [
    ("auto", "自动切换（推荐）"),
    ("h2-ja3", "TLS 指纹通道"),
    ("urllib", "标准通道回退"),
]


def _compact_text(value, limit=72):
    """将诊断摘要压缩为单行；完整内容仍保留在详情中。"""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


class SettingsPage(QWidget):
    def __init__(self, cfg, on_theme_change=None, on_task_reuse=None, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.on_theme_change = on_theme_change
        self.on_task_reuse = on_task_reuse
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
            "设置",
            "统一管理外观、默认输出位置与网络连接偏好；主题切换会立即应用。",
            "应用设置",
        ))

        # 外观
        c1, l1 = card(variant="accent")
        l1.addWidget(h2("外观"))
        row = QHBoxLayout()
        row.addWidget(QLabel("主题模式"))
        self.theme_combo = QComboBox()
        for _val, label in THEME_LABELS:
            self.theme_combo.addItem(label, _val)
        self.theme_combo.setCurrentIndex(
            max(0, [v for v, _ in THEME_LABELS].index(self.cfg.get("theme", "dark"))))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        row.addWidget(self.theme_combo)
        row.addStretch(1)
        live_chip = QLabel("即时预览")
        live_chip.setObjectName("moduleChip")
        row.addWidget(live_chip)
        l1.addLayout(row)
        root.addWidget(c1)

        # 默认输出目录
        c2, l2 = card()
        l2.addWidget(h2("默认输出目录"))
        self.out_row = PathRow(None, self.cfg.get("out_dir") or "")
        self.out_row.edit.setPlaceholderText("留空 = 自动（exe 旁 / 仓库旁的「导出」目录）")
        l2.addWidget(self.out_row)
        root.addWidget(c2)

        # 网络
        c3, l3 = card()
        l3.addWidget(h2("网络连接"))
        row_t = QHBoxLayout()
        row_t.addWidget(QLabel("传输通道"))
        self.transport_combo = QComboBox()
        for val, label in TRANSPORT_LABELS:
            self.transport_combo.addItem(label, val)
        cur = self.cfg.get("transport", "auto")
        self.transport_combo.setCurrentIndex(
            max(0, [v for v, _ in TRANSPORT_LABELS].index(cur)))
        row_t.addWidget(self.transport_combo, 1)
        l3.addLayout(row_t)
        l3.addWidget(QLabel("代理池（逗号分隔，direct=直连）"))
        self.proxy_edit = QLineEdit(self.cfg.get("proxy_spec", ""))
        self.proxy_edit.setPlaceholderText(
            "direct,socks5://127.0.0.1:7890,http://user:pass@1.2.3.4:8080")
        l3.addWidget(self.proxy_edit)
        l3.addWidget(muted("这些选项仅保存并应用现有连接配置，不改变任务的数据格式与采集流程。"))
        root.addWidget(c3)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        row_s = QHBoxLayout(action_bar)
        row_s.setContentsMargins(14, 12, 14, 12)
        save = QPushButton("保存设置")
        save.setObjectName("primary")
        save.setIcon(qta.icon("fa5s.check"))
        save.clicked.connect(self.on_save)
        row_s.addWidget(save)
        row_s.addStretch(1)
        save_hint = muted("本地配置 · 即时生效")
        row_s.addWidget(save_hint)
        root.addWidget(action_bar)

        # 运行诊断
        diagnostics_card, diagnostics_layout = card(variant="accent")
        self.diagnostics_card = diagnostics_card
        diagnostics_header = QHBoxLayout()
        diagnostics_header.addWidget(h2("运行诊断"))
        diagnostics_header.addStretch(1)
        self.diagnostics_status = StatusPill("检查中", "running")
        diagnostics_header.addWidget(self.diagnostics_status)
        diagnostics_layout.addLayout(diagnostics_header)
        self.diagnostics_summary = muted(
            "点击“查看诊断”查看 Python、依赖、目录和打包资源的详细检查结果。")
        diagnostics_layout.addWidget(self.diagnostics_summary)
        diagnostics_actions = QHBoxLayout()
        self.btn_diagnostics_details = QPushButton("查看诊断")
        self.btn_diagnostics_details.setObjectName("primary")
        self.btn_diagnostics_details.clicked.connect(self._show_diagnostics_details)
        diagnostics_actions.addWidget(self.btn_diagnostics_details)
        self.btn_recheck = QPushButton("重新检查")
        self.btn_recheck.clicked.connect(self._refresh_diagnostics)
        diagnostics_actions.addWidget(self.btn_recheck)
        diagnostics_actions.addStretch(1)
        diagnostics_layout.addLayout(diagnostics_actions)
        root.addWidget(diagnostics_card)

        # 最近错误
        recent_card, recent_layout = card()
        self.recent_card = recent_card
        recent_header = QHBoxLayout()
        recent_header.addWidget(h2("最近错误"))
        recent_header.addStretch(1)
        self.recent_status = StatusPill("暂无记录", "idle")
        recent_header.addWidget(self.recent_status)
        recent_layout.addLayout(recent_header)
        self.recent_meta = muted("错误时间：暂无\n来源模块：暂无")
        recent_layout.addWidget(self.recent_meta)
        self.recent_summary = QLabel("当前没有记录到任务或应用错误。")
        self.recent_summary.setWordWrap(True)
        recent_layout.addWidget(self.recent_summary)
        self.recent_detail = QPlainTextEdit()
        self.recent_detail.setReadOnly(True)
        self.recent_detail.setMinimumHeight(76)
        self.recent_detail.setMaximumHeight(180)
        self.recent_detail.setPlaceholderText("错误详情会显示在这里…")
        self.recent_detail.setVisible(False)
        recent_layout.addWidget(self.recent_detail)
        recent_actions = QHBoxLayout()
        self.btn_view_error = QPushButton("查看错误详情")
        self.btn_view_error.clicked.connect(self._show_recent_error_details)
        recent_actions.addWidget(self.btn_view_error)
        self.btn_copy_error = QPushButton("复制错误详情")
        self.btn_copy_error.clicked.connect(self._copy_recent_error)
        recent_actions.addWidget(self.btn_copy_error)
        self.btn_clear_error = QPushButton("清除最近错误")
        self.btn_clear_error.setObjectName("danger")
        self.btn_clear_error.clicked.connect(self._clear_recent_error)
        recent_actions.addWidget(self.btn_clear_error)
        recent_actions.addStretch(1)
        recent_layout.addLayout(recent_actions)
        root.addWidget(recent_card)

        # 任务历史
        history_card, history_layout = card(variant="accent")
        self.history_card = history_card
        history_header = QHBoxLayout()
        history_header.addWidget(h2("任务历史"))
        history_header.addStretch(1)
        self.history_status = StatusPill("暂无记录", "idle")
        history_header.addWidget(self.history_status)
        history_layout.addLayout(history_header)
        self.history_meta = muted("历史记录：0 条\n最近任务：暂无")
        history_layout.addWidget(self.history_meta)
        self.history_summary = QLabel("当前没有任务历史记录。")
        self.history_summary.setWordWrap(True)
        history_layout.addWidget(self.history_summary)
        history_actions = QHBoxLayout()
        self.btn_view_history = QPushButton("查看任务历史")
        self.btn_view_history.setObjectName("primary")
        self.btn_view_history.clicked.connect(self._show_task_history)
        history_actions.addWidget(self.btn_view_history)
        history_actions.addStretch(1)
        history_layout.addLayout(history_actions)
        root.addWidget(history_card)

        # 关于
        c4, l4 = card()
        l4.addWidget(h2("关于"))
        l4.addWidget(muted(
            f"B站工具箱 {VERSION} · B站公开数据采集与分析工具。"))
        l4.addWidget(muted(
            "仅抓取游客可见的公开数据，请遵守 B 站用户协议与 robots 精神，勿用于刷量等违规用途。"))
        root.addWidget(c4)
        root.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)
        self._diagnostic_items = []
        self._diagnostic_output_dir = diagnostics.output_dir(self.cfg)
        self._refresh_diagnostics()
        self._refresh_recent_error()
        self._refresh_task_history()

    def _on_theme_changed(self, *_):
        """切换主题下拉框立即生效（无需点保存）。"""
        theme = self.theme_combo.currentData()
        self.cfg["theme"] = theme
        config.save({"theme": theme})
        if self.on_theme_change:
            self.on_theme_change(theme)

    def _refresh_diagnostics(self):
        self._diagnostic_items = diagnostics.collect_diagnostics(self.cfg)
        self._diagnostic_output_dir = diagnostics.output_dir(self.cfg)

        failures = sum(item.status == diagnostics.STATUS_ERROR
                       for item in self._diagnostic_items)
        warnings = sum(item.status == diagnostics.STATUS_WARNING
                       for item in self._diagnostic_items)
        normal = len(self._diagnostic_items) - failures - warnings
        if failures:
            self.diagnostics_status.set_state("error", f"失败 {failures} 项")
        elif warnings:
            self.diagnostics_status.set_state("warning", f"警告 {warnings} 项")
        else:
            self.diagnostics_status.set_state("success", "全部正常")
        self.diagnostics_summary.setText(
            f"上次检查完成：正常 {normal} 项，警告 {warnings} 项，失败 {failures} 项。"
            " 详细结果请点击“查看诊断”。"
        )

    def _toggle_diagnostic_detail(self, detail, button):
        visible = not detail.isVisible()
        detail.setVisible(visible)
        button.setText("收起" if visible else "详情")

    def _diagnostic_row(self, item):
        row = QFrame()
        row.setObjectName("card")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 10, 8)
        row_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title = QLabel(item.name)
        summary = muted(_compact_text(item.summary))
        summary.setWordWrap(False)
        summary.setToolTip(item.summary)
        title_row.addWidget(title)
        title_row.addWidget(summary, 1)
        title_row.addWidget(StatusPill(item.status_label, {
            diagnostics.STATUS_OK: "success",
            diagnostics.STATUS_WARNING: "warning",
            diagnostics.STATUS_ERROR: "error",
        }.get(item.status, "warning")))
        detail = muted(
            f"详情：{item.details}\n建议：{item.suggestion}"
        )
        expanded = item.status != diagnostics.STATUS_OK
        detail.setVisible(expanded)
        detail_button = QPushButton("收起" if expanded else "详情")
        detail_button.setObjectName("flat")
        detail_button.setMinimumWidth(48)
        detail_button.clicked.connect(
            lambda _checked=False, detail=detail, button=detail_button:
            self._toggle_diagnostic_detail(detail, button)
        )
        title_row.addWidget(detail_button)
        row_layout.addLayout(title_row)
        row_layout.addWidget(detail)
        return row

    def _dialog_size(self, dialog, width=780, height=560):
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            width = min(width, max(480, available.width() - 48))
            height = min(height, max(340, available.height() - 72))
        dialog.resize(width, height)

    def _show_text_dialog(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        self._dialog_size(dialog, 720, 460)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        layout.addWidget(editor)

        actions = QHBoxLayout()
        copy_button = QPushButton("复制")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(text)
        )
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        actions.addWidget(copy_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        dialog.exec()

    def _show_diagnostics_details(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("运行诊断")
        self._dialog_size(dialog)
        layout = QVBoxLayout(dialog)

        header = QHBoxLayout()
        header.addWidget(QLabel("环境检查结果"))
        header.addStretch(1)
        header.addWidget(StatusPill(self.diagnostics_status.text(),
                                    self.diagnostics_status.property("state")))
        layout.addLayout(header)
        layout.addWidget(muted(
            "仅检查本机环境，不会发起网络请求。点击“详情”查看具体信息和修复建议。"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("transparent")
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 0, 6, 0)
        rows.setSpacing(8)
        priority = {
            diagnostics.STATUS_ERROR: 0,
            diagnostics.STATUS_WARNING: 1,
            diagnostics.STATUS_OK: 2,
        }
        for item in sorted(self._diagnostic_items,
                           key=lambda value: priority.get(value.status, 1)):
            rows.addWidget(self._diagnostic_row(item))
        rows.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        copy_button = QPushButton("复制诊断信息")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(
                diagnostics.format_diagnostics(self._diagnostic_items)
            )
        )
        open_config_button = QPushButton("打开配置目录")
        open_config_button.clicked.connect(lambda: open_path(config.CONFIG_DIR))
        open_output_button = QPushButton("打开输出目录")
        open_output_button.clicked.connect(
            lambda: open_path(self._diagnostic_output_dir)
        )
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        actions.addWidget(copy_button)
        actions.addWidget(open_config_button)
        actions.addWidget(open_output_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        dialog.exec()

    def _refresh_recent_error(self):
        record = diagnostics.load_recent_error()
        if not record:
            self.recent_status.set_state("idle", "暂无记录")
            self.recent_meta.setText("错误时间：暂无\n来源模块：暂无")
            self.recent_summary.setText("当前没有记录到任务或应用错误。")
            self.recent_detail.clear()
            self.recent_detail.setVisible(False)
            self.btn_view_error.setEnabled(False)
            self.btn_copy_error.setEnabled(False)
            self.btn_clear_error.setEnabled(False)
            return

        current = record.get("state") == "current"
        record_label = "当前错误" if current else "历史错误"
        self.recent_status.set_state("error" if current else "warning", record_label)
        self.recent_meta.setText(
            f"记录类型：{record_label}\n"
            f"错误时间：{record.get('timestamp', '未知')}\n"
            f"来源模块：{record.get('source', '未知模块')}"
        )
        self.recent_summary.setText(
            f"简短说明：{record.get('summary', '任务执行失败')}"
        )
        self.recent_detail.setPlainText(record.get("details", ""))
        self.recent_detail.setVisible(False)
        self.btn_view_error.setEnabled(True)
        self.btn_copy_error.setEnabled(True)
        self.btn_clear_error.setEnabled(True)

    def _show_recent_error_details(self):
        record = diagnostics.load_recent_error()
        if not record:
            self._refresh_recent_error()
            return
        self._show_text_dialog(
            "最近错误详情",
            diagnostics.format_recent_error(record),
        )

    def _copy_recent_error(self):
        record = diagnostics.load_recent_error()
        if not record:
            self._refresh_recent_error()
            return
        QApplication.clipboard().setText(diagnostics.format_recent_error(record))
        QMessageBox.information(self, "最近错误", "错误详情已复制。")

    def _clear_recent_error(self):
        if not diagnostics.load_recent_error():
            self._refresh_recent_error()
            return
        answer = QMessageBox.question(
            self,
            "清除最近错误",
            "只清除程序生成的最近错误记录和启动错误日志，是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            diagnostics.clear_recent_error()
            self._refresh_recent_error()

    @staticmethod
    def _history_status(record):
        status = record.get("status")
        if status == "running" or not record.get("finished_at"):
            return "未完整结束", "warning"
        return {
            "completed": ("已完成", "success"),
            "failed": ("失败", "error"),
            "cancelled": ("已取消", "warning"),
            "interrupted": ("已中断", "warning"),
        }.get(status, ("未知状态", "warning"))

    def _refresh_task_history(self):
        records = task_history.load_history()
        count = len(records)
        self.history_status.set_state("idle" if not records else "success", f"{count} 条记录")
        self.btn_view_history.setEnabled(True)
        if not records:
            self.history_meta.setText("历史记录：0 条\n最近任务：暂无")
            self.history_summary.setText("当前没有任务历史记录。")
            return
        latest = records[0]
        label, state = self._history_status(latest)
        self.history_status.set_state(state, label)
        self.history_meta.setText(
            f"历史记录：{count} 条\n"
            f"最近任务：{latest.get('tool_name', '未知任务')}\n"
            f"最近时间：{latest.get('started_at', '未知')}\n"
            f"最近状态：{label}"
        )
        self.history_summary.setText(
            f"目标：{_compact_text(latest.get('target_summary', ''), 96)}"
        )

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _open_result_file(path):
        target = Path(path)
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _history_output_row(self, path):
        row = QWidget()
        row.setObjectName("transparent")
        content = QVBoxLayout(row)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(4)
        label = QLabel(str(path))
        label.setWordWrap(True)
        content.addWidget(label)
        exists = task_history.output_exists(path)
        state = QLabel("文件存在" if exists else "文件已不存在")
        state.setObjectName("muted")
        action_row = QHBoxLayout()
        action_row.addWidget(state)
        action_row.addStretch(1)
        button = QPushButton("打开结果文件")
        button.setEnabled(exists)
        button.clicked.connect(lambda _=False, p=path: self._open_result_file(p))
        action_row.addWidget(button)
        content.addLayout(action_row)
        return row

    def _build_task_history_dialog(self):
        records = task_history.load_history()
        dialog = QDialog(self)
        dialog.setWindowTitle("任务历史详情")
        self._dialog_size(dialog, 900, 600)
        root = QVBoxLayout(dialog)

        header = QHBoxLayout()
        header.addWidget(QLabel("按开始时间倒序显示；文件状态会在打开详情时重新检查。"))
        header.addStretch(1)
        root.addLayout(header)

        body = QHBoxLayout()
        record_list = QListWidget()
        record_list.setMinimumWidth(270)
        record_list.setWordWrap(True)
        record_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        record_list.setTextElideMode(Qt.ElideRight)
        for record in records:
            status_label, _state = self._history_status(record)
            record_list.addItem(
                f"{record.get('tool_name', '未知任务')} · {status_label}\n"
                f"{_compact_text(record.get('target_summary', ''), 42)}"
            )
        body.addWidget(record_list)

        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        detail_scroll.setFrameShape(QFrame.NoFrame)
        body.addWidget(detail_scroll, 1)
        root.addLayout(body, 1)

        selected = {"record": records[0] if records else None}
        reuse_button = QPushButton("复用参数")
        open_dir_button = QPushButton("打开所在目录")
        clear_button = QPushButton("清空历史")
        clear_button.setObjectName("danger")
        close_button = QPushButton("关闭")
        actions = QHBoxLayout()
        actions.addWidget(reuse_button)
        actions.addWidget(open_dir_button)
        actions.addWidget(clear_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        root.addLayout(actions)

        def render(index):
            record = records[index] if 0 <= index < len(records) else None
            selected["record"] = record
            content = QWidget()
            content.setObjectName("transparent")
            layout = QVBoxLayout(content)
            layout.setContentsMargins(4, 4, 10, 4)
            if record is None:
                layout.addWidget(muted("当前没有任务历史记录。"))
                reuse_button.setEnabled(False)
                open_dir_button.setEnabled(False)
                detail_scroll.setWidget(content)
                return

            status_label, state = self._history_status(record)
            title_row = QHBoxLayout()
            title_row.addWidget(h2(record.get("tool_name", "未知任务")))
            title_row.addStretch(1)
            title_row.addWidget(StatusPill(status_label, state))
            layout.addLayout(title_row)
            layout.addWidget(muted(
                f"任务类型：{record.get('tool_id', '未知')}\n"
                f"开始时间：{record.get('started_at', '未知')}\n"
                f"结束时间：{record.get('finished_at') or '未记录（未完整结束）'}"
            ))
            layout.addWidget(h2("目标摘要"))
            target = QLabel(record.get("target_summary", "暂无"))
            target.setWordWrap(True)
            target.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(target)
            layout.addWidget(h2("输出文件"))
            outputs = record.get("outputs") or []
            if outputs:
                for path in outputs:
                    layout.addWidget(self._history_output_row(path))
            else:
                layout.addWidget(muted("暂无结果文件。"))

            output_dir = Path(record.get("output_dir", ""))
            open_dir_button.setEnabled(task_history.output_exists(output_dir))
            open_dir_button.setToolTip(str(output_dir))
            reuse_button.setEnabled(bool(record.get("reusable")))
            if record.get("status") == "running" or not record.get("finished_at"):
                layout.addWidget(muted("该任务没有结束时间，显示为“未完整结束”，不会误标为成功。"))
            if record.get("status") == "failed" and record.get("error"):
                layout.addWidget(h2("失败详情"))
                error = record["error"]
                error_view = QPlainTextEdit()
                error_view.setReadOnly(True)
                error_view.setMaximumHeight(170)
                error_view.setPlainText(
                    f"时间：{error.get('timestamp', '未知')}\n"
                    f"来源：{error.get('source', '未知')}\n"
                    f"说明：{error.get('summary', '任务执行失败')}\n"
                    f"详情：{error.get('details', '')}"
                )
                layout.addWidget(error_view)
            layout.addStretch(1)
            detail_scroll.setWidget(content)

        def reuse_selected():
            record = selected.get("record")
            if not record or not record.get("reusable"):
                return
            if self.on_task_reuse and self.on_task_reuse(dict(record)):
                dialog.accept()

        def open_selected_dir():
            record = selected.get("record")
            if record:
                open_path(record.get("output_dir", ""))

        def clear_selected_history():
            answer = QMessageBox.question(
                dialog,
                "清空任务历史",
                "只清除任务历史记录，不会删除生成文件、错误记录或设置，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                task_history.clear_history()
                self._refresh_task_history()
                dialog.accept()

        record_list.currentRowChanged.connect(render)
        reuse_button.clicked.connect(reuse_selected)
        open_dir_button.clicked.connect(open_selected_dir)
        clear_button.clicked.connect(clear_selected_history)
        close_button.clicked.connect(dialog.accept)
        if records:
            record_list.setCurrentRow(0)
        else:
            render(-1)
            reuse_button.setEnabled(False)
            open_dir_button.setEnabled(False)
        return dialog

    def _show_task_history(self):
        dialog = self._build_task_history_dialog()
        dialog.exec()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_recent_error()
        self._refresh_task_history()

    def on_save(self):
        theme = self.theme_combo.currentData()
        transport = self.transport_combo.currentData()
        proxy_spec = self.proxy_edit.text().strip()
        out_dir = self.out_row.value()
        changed_net = (transport != self.cfg.get("transport")
                       or proxy_spec != self.cfg.get("proxy_spec", ""))
        self.cfg.update({"theme": theme, "transport": transport,
                         "proxy_spec": proxy_spec, "out_dir": out_dir})
        config.save(self.cfg)
        self._refresh_diagnostics()
        session.configure(proxy_spec=proxy_spec or None, transport=transport,
                          cookie_path=config.COOKIE_FILE, force=changed_net)
        if self.on_theme_change:
            self.on_theme_change(theme)
        QMessageBox.information(self, "设置", "已保存"
                                + ("，网络配置已即时生效" if changed_net else ""))
