# -*- coding: utf-8 -*-
"""默认输出目录解析。

PyInstaller onefile 下 sys._MEIPASS 指向临时解压目录，导出文件会随退出
丢失——打包后一律用 exe 所在目录；目录不可写（如 Program Files）时退回
用户目录。
"""
import os
import sys
from pathlib import Path


def app_base_dir():
    """应用基准目录：打包后=exe 所在目录；源码运行=仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_out_dir():
    env = os.environ.get("BILITOOLBOX_OUT")
    if env:
        return Path(env)
    base = app_base_dir()
    try:
        writable = os.access(base, os.W_OK)
    except OSError:
        writable = False
    if writable:
        return base / "导出"
    return Path.home() / "BiliToolbox" / "导出"
