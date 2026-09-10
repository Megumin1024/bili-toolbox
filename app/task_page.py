# -*- coding: utf-8 -*-
"""任务页基类：页头 + 参数 + 运行控制 + 结果 + 任务控制台。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QInputDialog,
                               QScrollArea, QVBoxLayout, QWidget)

from core import diagnostics, task_history, task_presets

from .task_runner import TaskRunner
from .widgets import LogPanel, PageHeader, ProgressBlock, ResultCard, card, h2


class PresetBar(QFrame):
    """任务参数卡顶部的紧凑本地预设栏。"""

    def __init__(self, tool_id, tool_name, collect_params, apply_params, parent=None):
        super().__init__(parent)
        self.tool_id = str(tool_id)
        self.tool_name = str(tool_name)
        self._collect_params = collect_params
        self._apply_params = apply_params
        self.setObjectName("presetBar")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 2)
        row.setSpacing(6)
        row.addWidget(QLabel("任务预设"))

        self.combo = QComboBox()
        self.combo.setMinimumWidth(150)
        self.combo.currentIndexChanged.connect(self._update_actions)
        row.addWidget(self.combo, 1)

        self.btn_apply = QPushButton("应用预设")
        self.btn_apply.clicked.connect(self._on_apply)
        row.addWidget(self.btn_apply)
        self.btn_save = QPushButton("保存当前参数")
        self.btn_save.clicked.connect(self._on_save)
        row.addWidget(self.btn_save)
        self.btn_delete = QPushButton("删除预设")
        self.btn_delete.clicked.connect(self._on_delete)
        row.addWidget(self.btn_delete)
        self.refresh()

    def _selected(self):
        value = self.combo.currentData()
        return value if isinstance(value, dict) else None

    def _update_actions(self, _index=None):
        enabled = self._selected() is not None
        self.btn_apply.setEnabled(enabled)
        self.btn_delete.setEnabled(enabled)

    def refresh(self, selected_id=None):
        current = selected_id
        if current is None:
            selected = self._selected()
            current = selected.get("id") if selected else None
        presets = task_presets.load_presets(self.tool_id)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("选择任务预设", None)
        selected_index = 0
        for index, preset in enumerate(presets, start=1):
            self.combo.addItem(preset["name"], preset)
            if preset["id"] == current:
                selected_index = index
        self.combo.setCurrentIndex(selected_index)
        self.combo.blockSignals(False)
        self._update_actions()

    def _on_apply(self):
        preset = self._selected()
        if preset is None:
            return
        try:
            self._apply_params(dict(preset.get("params") or {}))
        except (TypeError, ValueError):
            QMessageBox.warning(self, "预设应用失败", "预设参数无法回填当前页面。")

    def _on_save(self):
        name, accepted = QInputDialog.getText(self, "保存任务预设", "预设名称：")
        if not accepted:
            return
        try:
            record = task_presets.create_or_update_preset(
                name, self.tool_id, self.tool_name, self._collect_params(),
            )
        except task_presets.PresetNameConflict:
            answer = QMessageBox.question(
                self,
                "预设已存在",
                "同一工具下已经存在同名预设，是否覆盖？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            try:
                record = task_presets.create_or_update_preset(
                    name, self.tool_id, self.tool_name, self._collect_params(),
                    overwrite=True,
                )
            except (ValueError, task_presets.PresetStorageError) as exc:
                QMessageBox.warning(self, "预设未保存", str(exc))
                return
        except (ValueError, task_presets.PresetStorageError) as exc:
            QMessageBox.warning(self, "预设未保存", str(exc))
            return
        self.refresh(record["id"])

    def _on_delete(self):
        preset = self._selected()
        if preset is None:
            return
        answer = QMessageBox.question(
            self,
            "删除任务预设",
            f"确定删除预设“{preset['name']}”？\n只删除本地预设，不影响任务历史。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            task_presets.delete_preset(preset["id"])
        except task_presets.PresetStorageError as exc:
            QMessageBox.warning(self, "预设未删除", str(exc))
            return
        self.refresh()


class TaskPage(QWidget):
    tool_title = ""
    tool_subtitle = ""
    tool_module = "任务流程"
    history_enabled = False
    history_tool_id = ""
    preset_enabled = False

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.runner = None
        self._cancel_requested = False
        self._history_record_id = None
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
            self.tool_title,
            self.tool_subtitle,
            self.tool_module,
        ))

        self.params_card, self.params_lay = card(variant="accent")
        self.params_lay.addWidget(h2("任务参数"))
        preset_bar = self.build_preset_bar()
        if preset_bar is not None:
            self.params_lay.addWidget(preset_bar)
        self.build_params()
        root.addWidget(self.params_card)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        run_row = QHBoxLayout(action_bar)
        run_row.setContentsMargins(14, 12, 14, 12)
        run_row.setSpacing(10)

        self.btn_start = QPushButton("开始任务")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.on_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.on_cancel)
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_cancel)
        run_row.addSpacing(6)
        self.progress_block = ProgressBlock()
        run_row.addWidget(self.progress_block, 1)
        root.addWidget(action_bar)

        self.result_card = ResultCard()
        root.addWidget(self.result_card)

        post_result_card = self.build_post_result_card()
        if post_result_card is not None:
            root.addWidget(post_result_card)

        console_header = QHBoxLayout()
        console_header.setContentsMargins(2, 2, 2, 0)
        console_header.addWidget(h2("任务控制台"))
        console_header.addStretch(1)
        console_chip = QLabel("任务日志")
        console_chip.setObjectName("moduleChip")
        console_header.addWidget(console_chip)
        root.addLayout(console_header)

        self.log_panel = LogPanel()
        self.log_panel.setMinimumHeight(180)
        root.addWidget(self.log_panel)
        root.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ---- 子类接口 ----

    def build_params(self):
        raise NotImplementedError

    def build_preset_bar(self):
        if not self.preset_enabled:
            return None
        self.preset_bar = PresetBar(
            self.history_tool_id,
            self.tool_title,
            self.collect_preset_params,
            self.apply_preset_params,
        )
        return self.preset_bar

    def collect_preset_params(self):
        raise NotImplementedError

    def apply_preset_params(self, params):
        raise NotImplementedError

    def collect_params(self):
        """返回流水线 kwargs；校验失败抛 ValueError（信息展示给用户）。"""
        raise NotImplementedError

    def pipeline(self):
        raise NotImplementedError

    def on_finished(self, result):
        """任务成功后的结果卡片展示；默认空实现。"""
        self.result_card.show_result("完成 ✓", [])

    def build_post_result_card(self):
        """可选的结果后扩展区域；普通任务页默认不占用布局空间。"""
        return None

    # ---- 任务历史钩子 ----

    def history_target_summary(self, params):
        """返回可安全写入历史的目标摘要；具体页面必须显式实现。"""
        return self.tool_title or self.tool_module

    def history_reusable_params(self, params):
        """返回页面白名单参数；默认不允许复用。"""
        return {}

    def history_output_paths(self, result):
        """返回结果文件路径；具体页面必须显式实现。"""
        return []

    def apply_reusable_params(self, params):
        """将历史参数填回页面，不得在此启动任务。"""
        raise NotImplementedError

    def _begin_task_history(self, params):
        if not self.history_enabled:
            return
        try:
            write_result = task_history.create_record(
                tool_id=self.history_tool_id,
                tool_name=self.tool_title,
                target_summary=self.history_target_summary(params),
                output_dir=params.get("out_dir", ""),
                reusable_params=self.history_reusable_params(params),
            )
            self._history_record_id = write_result.record.get("id")
            if not write_result.persisted:
                self.log_panel.append("任务历史写入失败，已继续执行任务。", "warn")
        except Exception as exc:  # noqa: BLE001 - 历史是 best-effort 边界
            self._history_record_id = None
            self.log_panel.append(
                f"任务历史写入失败，已继续执行任务：{type(exc).__name__}", "warn"
            )

    def _update_task_history(self, status, result=None, error=None):
        record_id = getattr(self, "_history_record_id", None)
        if not record_id:
            return
        try:
            outputs = None
            if status in {"completed", "cancelled", "interrupted"} and isinstance(result, dict):
                outputs = self.history_output_paths(result)
            persisted = task_history.update_record(
                record_id, status, outputs=outputs, error=error
            )
            if not persisted:
                self.log_panel.append("任务历史更新失败，不影响任务结果。", "warn")
        except Exception as exc:  # noqa: BLE001 - 历史是 best-effort 边界
            self.log_panel.append(
                f"任务历史更新失败，不影响任务结果：{type(exc).__name__}", "warn"
            )

    # ---- 运行控制 ----

    def on_start(self):
        if self.runner is not None and self.runner.isRunning():
            return
        try:
            kwargs = self.collect_params()
        except ValueError as exc:
            QMessageBox.warning(self, "参数有误", str(exc))
            self.progress_block.set_error("参数有误")
            return
        self.log_panel.clear()
        self.result_card.hide()
        self._cancel_requested = False
        self.progress_block.set_busy("准备中…")
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._begin_task_history(kwargs)
        self.runner = TaskRunner(self.pipeline(), kwargs)
        self.runner.progress.connect(self._on_progress)
        self.runner.finished_ok.connect(self._on_finished_ok)
        self.runner.failed.connect(self._on_failed)
        self.runner.finished.connect(self._on_thread_finished)
        self.runner.start()

    def on_cancel(self):
        if self.runner is not None:
            self._cancel_requested = True
            self.runner.cancel()
            self.progress_block.set_warning("正在取消，请等待当前步骤结束…")
            self.log_panel.append("已请求取消，等待当前步骤完成后停止…", "warn")

    def _on_progress(self, kw):
        if kw.get("text"):
            level = kw.get("level")
            if level in ("warn", "error"):
                self.log_panel.append(kw["text"], level)
            else:
                self.log_panel.append(kw["text"])
        if kw.get("done") is not None and kw.get("total"):
            self.progress_block.set_value(
                kw["done"], kw["total"],
                kw.get("text") or self.progress_block.label.text())
        elif kw.get("text"):
            self.progress_block.set_busy(kw["text"])
        for key in ("pages", "phase", "root_idx", "round", "ok", "fail"):
            if kw.get(key) is not None:
                break
        else:
            return
        parts = []
        if kw.get("phase"):
            parts.append(f"阶段: {kw['phase']}")
        if kw.get("pages"):
            parts.append(f"请求 {kw['pages']} 页")
        if kw.get("done") is not None:
            parts.append(
                f"已抓 {kw['done']:,}" if isinstance(kw["done"], int)
                else f"已完成 {kw['done']}")
        if kw.get("ok") is not None:
            parts.append(f"成功 {kw['ok']}")
        if kw.get("fail"):
            parts.append(f"失败 {kw['fail']}")
        if parts and not kw.get("total"):
            self.progress_block.set_text("  ".join(parts))

    def _on_finished_ok(self, result):
        stats = result.get("stats") if isinstance(result, dict) else {}
        stats = stats if isinstance(stats, dict) else {}
        stats_error = stats.get("status") == "error" or bool(stats.get("error"))
        if stats_error:
            self._on_failed(stats.get("error") or "任务运行异常")
            return
        stats_cancelled = stats.get("cancelled") is True
        if self._cancel_requested or stats_cancelled:
            self._on_cancelled(result)
            return
        stats_interrupted = (
            stats.get("status") == "interrupted"
            or (stats.get("aborted") is True and not stats_cancelled)
        )
        if stats_interrupted:
            self._on_interrupted(result)
            return
        self.progress_block.set_success("任务完成")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        TaskPage._update_task_history(self, "completed", result=result)
        self.on_finished(result)

    def _on_cancelled(self, result=None):
        """处理流水线明确返回的取消状态，不写入最近错误。"""
        self.progress_block.set_warning("已取消")
        self.log_panel.append("任务已取消。", "warn")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        TaskPage._update_task_history(self, "cancelled", result=result)

    def _on_interrupted(self, result=None):
        """处理限页等可恢复中断，不写入最近错误。"""
        self.progress_block.set_warning("已中断，可继续")
        self.log_panel.append("任务已中断，断点已保存，可再次运行继续。", "warn")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        TaskPage._update_task_history(self, "interrupted", result=result)

    def _on_failed(self, msg):
        error_record = diagnostics.record_error(
            source=self.tool_module or self.tool_title or type(self).__name__,
            summary="任务执行失败",
            details=msg,
        )
        TaskPage._update_task_history(self, "failed", error=error_record)
        self.log_panel.append(msg, level="error")
        self.progress_block.set_error("任务失败（详见日志）")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _on_thread_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
