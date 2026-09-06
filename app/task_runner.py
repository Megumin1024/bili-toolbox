# -*- coding: utf-8 -*-
"""任务运行器：把框架无关的流水线函数（progress(**kw)/cancel() 注入协议）
包进 QThread，经信号回主线程刷 UI。"""
import threading

from PySide6.QtCore import QThread, Signal


class TaskRunner(QThread):
    progress = Signal(dict)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, kwargs=None, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._kwargs = dict(kwargs or {})
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        self._kwargs.setdefault("progress",
                                lambda **kw: self.progress.emit(kw))
        self._kwargs.setdefault("cancel", self.cancel_event.is_set)
        try:
            result = self._fn(**self._kwargs)
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001 - 统一兜底进 GUI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
