# -*- coding: utf-8 -*-
"""运行环境标记。QtWebEngine 必须在 QApplication 创建之前导入，因此由
main.py 最早导入本模块并记录可用性；监控页据此决定内嵌视图或浏览器回退。"""

try:  # noqa: SIM105 - 导入失败是正常分支（未装 PySide6-Addons）
    from PySide6 import QtWebEngineWidgets  # noqa: F401
    WEBENGINE_AVAILABLE = True
except Exception:  # noqa: BLE001
    WEBENGINE_AVAILABLE = False
