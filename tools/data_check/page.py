# -*- coding: utf-8 -*-
"""本地数据检查页面。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QListWidget,
                               QPushButton)

from app.task_runner import TaskRunner
from app.task_page import TaskPage
from app.widgets import PathRow, ProgressBlock, ResultCard, card, h2, muted
from core import diagnostics, output as output_mod, task_history

from .core import fingerprint_file, validate_inputs
from .pipeline import run_pipeline
from .repair_core import SourceChanged, validate_repair_inputs
from .repair_pipeline import run_repair_pipeline


class DataCheckPage(TaskPage):
    tool_title = "数据检查"
    tool_subtitle = "检查本地 Excel / JSONL，不修改源文件"
    tool_module = "本地数据检查"
    history_enabled = True
    history_tool_id = "data_check"

    def __init__(self, cfg, parent=None):
        self.repair_runner = None
        self._repair_snapshots = []
        self._repair_history_id = None
        self._pending_repair_out_dir = ""
        self._restoring_reusable = False
        super().__init__(cfg, parent)
        self.btn_start.setText("开始检查")
        self.btn_cancel.setText("取消检查")

    def build_post_result_card(self):
        self.repair_card, layout = card(variant="accent")
        layout.addWidget(h2("生成修复副本（可选）"))
        layout.addWidget(muted(
            "仅处理本次检查确认过的文件；源文件变化时会停止，不覆盖任何原文件或既有结果。"
        ))

        options = QHBoxLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(12)
        self.repair_deduplicate = QCheckBox("去除重复记录")
        self.repair_clean_blanks = QCheckBox("清理空白行")
        self.repair_normalize = QCheckBox("规范字段")
        self.repair_merge = QCheckBox("合并兼容文件")
        for checkbox, checked in (
            (self.repair_deduplicate, True),
            (self.repair_clean_blanks, True),
            (self.repair_normalize, True),
            (self.repair_merge, False),
        ):
            checkbox.setChecked(checked)
            checkbox.toggled.connect(self._update_repair_enabled)
            options.addWidget(checkbox)
        options.addStretch(1)
        layout.addLayout(options)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.repair_out_row = PathRow(None, "")
        form.addRow("修复输出目录", self.repair_out_row)
        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        self.btn_repair = QPushButton("生成修复副本")
        self.btn_repair.setObjectName("primary")
        self.btn_repair.clicked.connect(self._start_repair)
        self.btn_repair_cancel = QPushButton("取消修复")
        self.btn_repair_cancel.setObjectName("danger")
        self.btn_repair_cancel.setEnabled(False)
        self.btn_repair_cancel.clicked.connect(self._cancel_repair)
        self.repair_progress = ProgressBlock()
        actions.addWidget(self.btn_repair)
        actions.addWidget(self.btn_repair_cancel)
        actions.addWidget(self.repair_progress, 1)
        layout.addLayout(actions)

        self.repair_result_card = ResultCard()
        layout.addWidget(self.repair_result_card)
        self.repair_card.hide()

        model = self.file_list.model()
        model.rowsInserted.connect(self._invalidate_repair)
        model.rowsRemoved.connect(self._invalidate_repair)
        model.modelReset.connect(self._invalidate_repair)
        model.dataChanged.connect(self._invalidate_repair)
        self.repair_out_row.edit.textChanged.connect(self._on_repair_out_changed)
        return self.repair_card

    def build_params(self):
        self.params_lay.addWidget(QLabel("待检查文件（可多选 .xlsx / .jsonl）"))
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setTextElideMode(Qt.ElideMiddle)
        self.file_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.file_list.setMinimumHeight(96)
        self.file_list.setMaximumHeight(156)
        self.params_lay.addWidget(self.file_list)

        file_actions = QHBoxLayout()
        file_actions.setContentsMargins(0, 0, 0, 0)
        file_actions.setSpacing(8)
        add_button = QPushButton("添加文件")
        add_button.clicked.connect(self._add_files)
        remove_button = QPushButton("移除选中")
        remove_button.setObjectName("flat")
        remove_button.clicked.connect(self._remove_selected)
        clear_button = QPushButton("清空列表")
        clear_button.setObjectName("flat")
        clear_button.clicked.connect(self._clear_files)
        file_actions.addWidget(add_button)
        file_actions.addWidget(remove_button)
        file_actions.addWidget(clear_button)
        file_actions.addStretch(1)
        self.params_lay.addLayout(file_actions)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)
        default_out = str(Path(self.cfg.get("out_dir")
                               or output_mod.default_out_dir()) / "数据检查")
        self.out_row = PathRow(None, default_out)
        form.addRow("报告输出目录", self.out_row)
        self.params_lay.addLayout(form)
        self.params_lay.addWidget(muted(
            "源文件只读，程序不会修改或整理原文件。检查只在本机进行，不重新抓取、不访问网络。"
        ))

    def _add_files(self):
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "选择本地数据文件",
            "",
            "Excel / JSONL (*.xlsx *.jsonl);;Excel (*.xlsx);;JSONL (*.jsonl)",
        )
        existing = {
            str(Path(self.file_list.item(index).text()).resolve(strict=False)).casefold()
            for index in range(self.file_list.count())
        }
        for value in paths:
            path = Path(value).resolve(strict=False)
            identity = str(path).casefold()
            if identity in existing:
                continue
            item = self.file_list.addItem(str(path))
            del item
            self.file_list.item(self.file_list.count() - 1).setToolTip(str(path))
            existing.add(identity)

    def _remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _clear_files(self):
        self.file_list.clear()

    def _invalidate_repair(self, *_args):
        self._repair_snapshots = []
        if hasattr(self, "repair_card"):
            self.repair_card.hide()
            self.repair_result_card.hide()
            self.repair_progress.reset()

    def _on_repair_out_changed(self, *_args):
        if self._restoring_reusable:
            return
        self._pending_repair_out_dir = self.repair_out_row.value()
        self._invalidate_repair()

    def _repair_is_running(self):
        return self.repair_runner is not None and self.repair_runner.isRunning()

    def on_start(self):
        if self._repair_is_running():
            return
        self._invalidate_repair()
        super().on_start()

    def _repair_options(self):
        return {
            "deduplicate": self.repair_deduplicate.isChecked(),
            "clean_blanks": self.repair_clean_blanks.isChecked(),
            "normalize_fields": self.repair_normalize.isChecked(),
            "merge": self.repair_merge.isChecked(),
        }

    def _update_repair_enabled(self, *_args):
        if not hasattr(self, "btn_repair"):
            return
        self.btn_repair.setEnabled(
            bool(self._repair_snapshots)
            and any(self._repair_options().values())
            and not self._repair_is_running()
            and not (self.runner is not None and self.runner.isRunning())
        )

    def _start_repair(self):
        if self._repair_is_running() or (self.runner is not None and self.runner.isRunning()):
            return
        files = [item["path"] for item in self._repair_snapshots]
        options = self._repair_options()
        try:
            validate_repair_inputs(
                files, self.repair_out_row.value(), self._repair_snapshots, options)
        except (ValueError, SourceChanged) as exc:
            self.repair_progress.set_warning(str(exc))
            self.log_panel.append(str(exc), "warn")
            return

        try:
            history = task_history.create_record(
                tool_id="data_check",
                tool_name="数据修复与合并",
                target_summary=f"修复 {len(files)} 个已检查本地文件",
                output_dir=self.repair_out_row.value(),
                reusable_params={
                    "files": files,
                    "out_dir": self.out_row.value(),
                    "repair_out_dir": self.repair_out_row.value(),
                    **options,
                },
            )
            self._repair_history_id = history.record.get("id")
            if not history.persisted:
                self.log_panel.append("修复任务历史写入失败，已继续执行。", "warn")
        except Exception as exc:  # noqa: BLE001 - 历史写入是 best-effort 边界
            self._repair_history_id = None
            self.log_panel.append(
                f"修复任务历史写入失败，已继续执行：{type(exc).__name__}", "warn")
        self.repair_result_card.hide()
        self.repair_progress.set_busy("准备修复…")
        self.btn_repair.setEnabled(False)
        self.btn_repair_cancel.setEnabled(True)
        self.btn_start.setEnabled(False)
        kwargs = {
            "files": files,
            "out_dir": self.repair_out_row.value(),
            "snapshots": list(self._repair_snapshots),
            **options,
        }
        self.repair_runner = TaskRunner(run_repair_pipeline, kwargs)
        self.repair_runner.progress.connect(self._on_repair_progress)
        self.repair_runner.finished_ok.connect(self._on_repair_finished)
        self.repair_runner.failed.connect(self._on_repair_failed)
        self.repair_runner.finished.connect(self._on_repair_thread_finished)
        self.repair_runner.start()

    def _cancel_repair(self):
        if self.repair_runner is not None:
            self.repair_runner.cancel()
            self.repair_progress.set_warning("正在取消…")
            self.log_panel.append("已请求取消修复，正在清理本次临时文件…", "warn")

    def _on_repair_progress(self, values):
        text = values.get("text")
        if text:
            self.log_panel.append(text, values.get("level"))
        if values.get("done") is not None and values.get("total"):
            self.repair_progress.set_value(values["done"], values["total"], text)
        elif text:
            self.repair_progress.set_busy(text)

    def _update_repair_history(self, status, outputs=None, error=None):
        if not self._repair_history_id:
            return
        if not task_history.update_record(
                self._repair_history_id, status, outputs=outputs, error=error):
            self.log_panel.append("修复任务历史更新失败，不影响任务结果。", "warn")

    def _on_repair_finished(self, result):
        stats = result.get("stats", {}) if isinstance(result, dict) else {}
        status = stats.get("status")
        if stats.get("cancelled") or status == "cancelled":
            self.repair_progress.set_warning("已取消")
            self._update_repair_history("cancelled", outputs=[])
            return
        if status == "interrupted":
            reason = stats.get("reason") or "源文件已变化，请重新检查"
            self.repair_progress.set_warning(reason)
            self.log_panel.append(reason, "warn")
            self._update_repair_history("interrupted", outputs=[])
            self._invalidate_repair()
            return
        outputs = list(result.get("copies", [])) + list(result.get("merged", []))
        manifest = result.get("manifest")
        if manifest:
            outputs.append(manifest)
        self.repair_progress.set_success("修复完成")
        self.repair_result_card.show_result(
            "修复副本已生成 ✓",
            [("修复副本", path) for path in result.get("copies", [])]
            + [("合并文件", path) for path in result.get("merged", [])]
            + [("修复清单", manifest), ("打开输出目录", result.get("dir"))],
        )
        skipped = int(stats.get("skipped_files", 0) or 0)
        if skipped:
            self.log_panel.append(f"{skipped} 个文件因损坏或结构复杂已跳过，详见修复清单。", "warn")
        self._update_repair_history("completed", outputs=outputs)

    def _on_repair_failed(self, message):
        error = diagnostics.record_error(
            source="数据修复与合并", summary="任务执行失败", details=message)
        self._update_repair_history("failed", error=error)
        self.repair_progress.set_error("修复失败（详见日志）")
        self.log_panel.append(message, "error")

    def _on_repair_thread_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_repair_cancel.setEnabled(False)
        self._update_repair_enabled()

    def collect_params(self):
        files = [self.file_list.item(index).text()
                 for index in range(self.file_list.count())]
        valid_files, out_dir = validate_inputs(files, self.out_row.value())
        return {"files": [str(path) for path in valid_files], "out_dir": str(out_dir)}

    def pipeline(self):
        return run_pipeline

    def history_target_summary(self, params):
        files = [Path(value).name for value in params.get("files", [])]
        names = "、".join(name for name in files if name)
        return (f"检查 {len(files)} 个本地文件" + (f"：{names}" if names else ""))[:320]

    def history_reusable_params(self, params):
        return {
            "files": list(params.get("files", [])),
            "out_dir": params.get("out_dir", ""),
        }

    def history_output_paths(self, result):
        if not isinstance(result, dict):
            return []
        return [result.get("report")]

    def apply_reusable_params(self, params):
        has_repair_params = any(
            key in params for key in (
                "repair_out_dir", "deduplicate", "clean_blanks",
                "normalize_fields", "merge",
            )
        )
        self._pending_repair_out_dir = (
            str(params.get("repair_out_dir", "")).strip()
            if has_repair_params else ""
        )
        self._restoring_reusable = True
        self.file_list.clear()
        for value in params.get("files", []):
            item = self.file_list.addItem(str(value))
            del item
            self.file_list.item(self.file_list.count() - 1).setToolTip(str(value))
        self.out_row.set_value(params.get("out_dir", ""))
        if has_repair_params and hasattr(self, "repair_out_row"):
            self.repair_out_row.set_value(self._pending_repair_out_dir)
            for key, checkbox in (
                ("deduplicate", self.repair_deduplicate),
                ("clean_blanks", self.repair_clean_blanks),
                ("normalize_fields", self.repair_normalize),
                ("merge", self.repair_merge),
            ):
                if key in params:
                    checkbox.setChecked(bool(params[key]))
        self._restoring_reusable = False
        self.file_list.setFocus()

    def on_finished(self, result):
        stats = result.get("stats", {})
        total = int(stats.get("total_issues", 0) or 0)
        title = "检查完成，未发现问题" if total == 0 else f"检查完成，发现 {total:,} 项问题"
        self.result_card.show_result(
            f"{title} ✓",
            [("检查报告", result.get("report")),
             ("打开报告目录", result.get("dir"))],
        )
        snapshots = result.get("source_files") or []
        if not snapshots:
            files = [self.file_list.item(index).text()
                     for index in range(self.file_list.count())]
            snapshots = [fingerprint_file(path) for path in files]
        self._repair_snapshots = list(snapshots)
        repair_out_dir = self._pending_repair_out_dir or str(
            Path(result.get("dir") or ".") / "修复副本"
        )
        self._restoring_reusable = True
        self.repair_out_row.set_value(repair_out_dir)
        self._restoring_reusable = False
        self.repair_result_card.hide()
        self.repair_progress.reset()
        self.repair_card.show()
        self._update_repair_enabled()

    def _on_cancelled(self, result=None):
        self._invalidate_repair()
        super()._on_cancelled(result)

    def _on_interrupted(self, result=None):
        self._invalidate_repair()
        super()._on_interrupted(result)

    def _on_failed(self, msg):
        self._invalidate_repair()
        super()._on_failed(msg)
