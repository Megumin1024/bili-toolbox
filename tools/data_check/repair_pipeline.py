# -*- coding: utf-8 -*-
"""数据修复与合并任务编排。"""
from .repair_core import run_repair


def run_repair_pipeline(files, out_dir, snapshots, progress=None, cancel=None, **options):
    """适配 TaskRunner 的 progress/cancel 注入协议。"""
    return run_repair(
        files, out_dir, snapshots,
        progress=progress, cancel=cancel, **options,
    )
