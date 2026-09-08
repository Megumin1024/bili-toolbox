# -*- coding: utf-8 -*-
"""工具注册表：新功能 = 新建 tools/<name>/ 包（page+pipeline+core），
并在下方 TOOLS 加一行，侧边栏与页面自动生成。"""
from app.registry import ToolSpec
from .collector.page import CollectorPage
from .comments.page import CommentsPage
from .monitor.page import MonitorPage

TOOLS = [
    ToolSpec(id="comments", name="评论抓取",
             subtitle="动态/视频全量评论 → 分析 → Excel",
             icon="fa5s.comments", factory=CommentsPage),
    ToolSpec(id="collector", name="视频采集",
             subtitle="批量视频公开数据快照/追踪 → Excel",
             icon="fa5s.chart-bar", factory=CollectorPage),
    ToolSpec(id="monitor", name="实时监控",
             subtitle="视频实时数据仪表盘",
             icon="fa5s.broadcast-tower", factory=MonitorPage),
]
