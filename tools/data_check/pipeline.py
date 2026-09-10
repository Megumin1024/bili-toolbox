# -*- coding: utf-8 -*-
"""数据检查任务编排。"""
from .core import run_check


def run_pipeline(files, out_dir, progress=None, cancel=None, **kwargs):
    """TaskRunner 注入 progress/cancel 后调用的本地检查入口。"""
    allowed = {"max_issue_details"}
    options = {key: value for key, value in kwargs.items() if key in allowed}
    return run_check(files, out_dir, progress=progress, cancel=cancel, **options)
