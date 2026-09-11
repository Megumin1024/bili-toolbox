# -*- coding: utf-8 -*-
"""工具注册表：新功能 = 新建 tools/<name>/ 包（page+pipeline+core），
并在下方 TOOLS 加一行，侧边栏与页面自动生成。"""
from app.registry import ToolSpec
from .collector.page import CollectorPage
from .comments.page import CommentsPage
from .danmaku.page import DanmakuPage
from .data_check.page import DataCheckPage
from .monitor.page import MonitorPage
from .report_center.page import ReportCenterPage
from .user_dynamics.page import UserDynamicsPage

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
    ToolSpec(id="data_check", name="数据检查",
             subtitle="检查本地 Excel / JSONL，不修改源文件",
             icon="fa5s.clipboard-check", factory=DataCheckPage),
    ToolSpec(id="report_center", name="报告中心",
             subtitle="浏览、对比和导出本地评论/快照/监控历史",
             icon="fa5s.file-alt", factory=ReportCenterPage),
    ToolSpec(id="user_dynamics", name="用户动态",
             subtitle="输入 UID 抓取该用户的公开动态 → Excel",
             icon="fa5s.user-circle", factory=UserDynamicsPage),
    ToolSpec(id="danmaku", name="弹幕抓取",
              subtitle="输入视频链接 · 抓取弹幕 → 热词/高频弹幕/热点分钟 + 密度分布 Excel（无需登录）",
              icon="fa5s.comment-dots", factory=DanmakuPage),
]
