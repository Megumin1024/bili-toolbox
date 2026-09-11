# -*- coding: utf-8 -*-
"""默认输出目录解析。

导出是**用户数据**，不能落在程序自己的安装目录里：那里会被重装/升级/重建
（PyInstaller --noconfirm 直接清空整个产物目录）抹掉，装到 Program Files 之类
不可写的位置还会让行为随安装位置漂移。所以打包后一律写用户目录。

源码运行仍写仓库根下的 导出/，开发和测试的现状不变。
"""
import os
import sys
from pathlib import Path


def app_base_dir():
    """应用基准目录：打包后=exe 所在目录；源码运行=仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_out_dir():
    """用户目录下的导出根，打包后唯一的默认去处。"""
    return Path.home() / "BiliToolbox" / "导出"


def default_out_dir():
    env = os.environ.get("BILITOOLBOX_OUT")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        # 注：这个分支跟 onefile 的临时解压目录无关——本应用用的是 onedir，
        # sys._MEIPASS 就是 _internal，不存在"退出即丢"的问题。选用户目录的
        # 唯一理由是上面的第一条。
        return user_out_dir()
    base = app_base_dir()
    try:
        writable = os.access(base, os.W_OK)
    except OSError:
        writable = False
    if writable:
        return base / "导出"
    return user_out_dir()


def is_inside_app_dir(value):
    """value 是否落在程序自己的安装目录内（含该目录本身）。"""
    raw = str(value or "").strip()
    if not raw:
        return False
    base = os.path.normcase(str(app_base_dir()))
    try:
        target = os.path.normcase(str(Path(raw).resolve()))
    except OSError:
        return False
    try:
        return os.path.commonpath([target, base]) == base
    except ValueError:      # 不同盘符
        return False


def migrate_out_dir(value):
    """旧配置里的导出目录若落在安装目录内，视为未设置并重新解析。

    打包版曾经把 <exe 目录>/导出 写进 config.json。那个位置现在指向要被清空的
    目录，所以按"没设置"处理。**只重解析安装目录内的路径**——用户自己挑到别处
    的路径一律原样保留，不替人做决定。只改配置，不搬动任何已有文件。
    """
    raw = str(value or "").strip()
    if not raw or is_inside_app_dir(raw):
        return str(default_out_dir())
    return raw
