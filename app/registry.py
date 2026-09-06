# -*- coding: utf-8 -*-
"""工具注册表：侧边栏与页面栈由本表自动生成。

新增功能 = 在 tools/ 下新建包（含 spec），并在 tools/__init__.py 的 TOOLS
列表加一行；GUI 无需改动。
"""
from dataclasses import dataclass
from typing import Callable


@dataclass
class ToolSpec:
    id: str          # 工具标识（用于目录命名等）
    name: str        # 侧边栏/页头显示名
    subtitle: str    # 简述（tooltip/备用）
    icon: str        # qtawesome 图标名
    factory: Callable  # factory(cfg: dict) -> QWidget 页面


def all_tools():
    from tools import TOOLS
    return TOOLS
