# -*- coding: utf-8 -*-
"""任务页基类：头部 + 参数卡片 + 运行控制 + 进度/日志 + 结果卡片。

子类实现 build_params()（构建表单）、collect_params()（校验并返回流水线
参数 dict）、pipeline()（流水线函数引用，签名须接受 progress/cancel）。
"""
from PySide6.QtWidgets import (QHBoxLayout, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from .task_runner import TaskRunner
from .widgets import LogPanel, ProgressBlock, ResultCard, card, h1, h2, muted


class TaskPage(QWidget):
    tool_title = ""
    tool_subtitle = ""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.runner = None
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)

        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(h1(self.tool_title))
        head.addWidget(muted(self.tool_subtitle))
        root.addLayout(head)

        self.params_card, self.params_lay = card()
        self.params_lay.addWidget(h2("参数"))
        self.build_params()
        root.addWidget(self.params_card)

        run_row = QHBoxLayout()
        run_row.setSpacing(10)
        self.btn_start = QPushButton("开始")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.on_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.on_cancel)
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_cancel)
        run_row.addStretch(1)
        self.progress_block = ProgressBlock()
        self.progress_block.setMaximumWidth(460)
        run_row.addWidget(self.progress_block, 1)
        root.addLayout(run_row)

        self.result_card = ResultCard()
        root.addWidget(self.result_card)

        root.addWidget(h2("运行日志"), 0)
        self.log_panel = LogPanel()
        root.addWidget(self.log_panel, 1)

    # ---- 子类接口 ----

    def build_params(self):
        raise NotImplementedError

    def collect_params(self):
        """返回流水线 kwargs；校验失败抛 ValueError（信息展示给用户）。"""
        raise NotImplementedError

    def pipeline(self):
        raise NotImplementedError

    def on_finished(self, result):
        """任务成功后的结果卡片展示；默认空实现。"""
        self.result_card.show_result("完成 ✓", [])

    # ---- 运行控制 ----

    def on_start(self):
        if self.runner is not None and self.runner.isRunning():
            return
        try:
            kwargs = self.collect_params()
        except ValueError as exc:
            QMessageBox.warning(self, "参数有误", str(exc))
            return
        self.log_panel.clear()
        self.result_card.hide()
        self.progress_block.set_busy("准备中…")
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.runner = TaskRunner(self.pipeline(), kwargs)
        self.runner.progress.connect(self._on_progress)
        self.runner.finished_ok.connect(self._on_finished_ok)
        self.runner.failed.connect(self._on_failed)
        self.runner.finished.connect(self._on_thread_finished)
        self.runner.start()

    def on_cancel(self):
        if self.runner is not None:
            self.runner.cancel()
            self.log_panel.append("已请求取消，等待当前步骤完成后停止…")

    def _on_progress(self, kw):
        if kw.get("text"):
            level = kw.get("level")
            if level in ("warn", "error"):
                self.log_panel.append(kw["text"], level)
            else:
                self.log_panel.append(kw["text"])
        if kw.get("done") is not None and kw.get("total"):
            self.progress_block.set_value(kw["done"], kw["total"],
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
            parts.append(f"已抓 {kw['done']:,}" if isinstance(kw["done"], int)
                         else f"已完成 {kw['done']}")
        if kw.get("ok") is not None:
            parts.append(f"成功 {kw['ok']}")
        if kw.get("fail"):
            parts.append(f"失败 {kw['fail']}")
        if parts and not kw.get("total"):
            self.progress_block.set_text("  ".join(parts))

    def _on_finished_ok(self, result):
        self.progress_block.set_busy("完成 ✓")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.on_finished(result)

    def _on_failed(self, msg):
        self.log_panel.append(msg, level="error")
        self.progress_block.set_text("失败（详见日志）")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _on_thread_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
