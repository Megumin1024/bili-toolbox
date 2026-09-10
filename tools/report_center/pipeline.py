# -*- coding: utf-8 -*-
"""报告中心的本地后台函数和独立 Qt Worker。

这里不导入 TaskRunner，也不导入任何采集/监控网络模块。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot

from . import core


def run_refresh(history_records=None, manual_files=None, manual_dirs=None,
                monitor_roots=None, cancel=None, progress=None):
    sources = core.discover_sources(
        history_records=history_records,
        manual_files=manual_files,
        manual_dirs=manual_dirs,
        monitor_roots=monitor_roots,
        cancel=cancel,
        progress=progress,
    )
    if cancel and cancel():
        return {"cancelled": True, "sources": []}
    for index, source in enumerate(sources, 1):
        if cancel and cancel():
            return {"cancelled": True, "sources": sources[:index - 1]}
        if progress:
            progress(text=f"已读取 {index}/{len(sources)}：{source.file_name}", done=index, total=len(sources))
    return {"cancelled": False, "sources": sources, "count": len(sources)}


def run_search(sources, query, cancel=None, progress=None):
    values = list(sources)
    match_modes = core.search_source_matches(values, query, cancel=cancel, progress=progress)
    if cancel and cancel():
        return {"cancelled": True, "sources": [], "match_modes": {}}
    matched = [source for source in values if source.path.casefold() in match_modes]
    return {"cancelled": False, "sources": matched, "count": len(matched), "match_modes": match_modes}


def run_file_comparison(source_a, source_b, row_limit=500, cancel=None, progress=None):
    try:
        result = core.compare_sources(source_a, source_b, row_limit=row_limit,
                                      cancel=cancel, progress=progress)
    except core.ComparisonCancelled:
        return {"cancelled": True}
    if cancel and cancel():
        return {"cancelled": True}
    return {"cancelled": False, "comparison": result}


def run_period_comparison(source, period_a, period_b, metric, cancel=None, progress=None):
    try:
        result = core.compare_periods(source, period_a, period_b, metric,
                                      cancel=cancel, progress=progress)
    except core.ComparisonCancelled:
        return {"cancelled": True}
    if cancel and cancel():
        return {"cancelled": True}
    return {"cancelled": False, "comparison": result}


def run_export(kind, payload, out_dir, fmt, cancel=None, progress=None):
    if cancel and cancel():
        return {"cancelled": True}
    if kind == "filtered":
        path = core.export_filtered(payload["source"], out_dir, fmt, query=payload.get("query", ""),
                                    progress=progress, cancel=cancel)
    elif kind == "file_comparison":
        path = core.export_file_comparison(payload["comparison"], out_dir, fmt)
    elif kind == "period_comparison":
        path = core.export_period_comparison(payload["comparison"], out_dir, fmt)
    else:
        raise ValueError(f"未知导出类型：{kind}")
    return {"cancelled": False, "path": str(path)}


class LocalWorker(QObject):
    """只执行本地函数；Worker 自己持有取消事件。"""

    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, function: Callable[..., Any], kwargs: dict[str, Any] | None = None):
        super().__init__()
        self.function = function
        self.kwargs = dict(kwargs or {})
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    @Slot()
    def run(self):
        values = dict(self.kwargs)
        values.setdefault("cancel", self.cancel_event.is_set)
        values.setdefault("progress", lambda **kw: self.progress.emit(kw))
        try:
            self.finished.emit(self.function(**values))
        except Exception as exc:  # noqa: BLE001 - 单项本地任务边界
            self.failed.emit(f"{type(exc).__name__}: {exc}")
