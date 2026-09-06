# -*- coding: utf-8 -*-
"""生成 README 截图（浅色主题 + 演示数据，无真实目标信息）。开发期工具。

用法：
    set BILITOOLBOX_SHOT_BVID=<任意真实视频BV号>
    python scripts/take_screenshots.py     # 窗口会实际显示约 40 秒

评论/采集页由本进程直接抓取；监控页因 WebEngine 与 win.grab() 同用会崩溃，
故窗口保持显示，由外部脚本按 ready 文件里的窗口矩形做屏幕抓取，抓完后
本进程自动退出。
"""
import os
import sys
import time

import app.env  # noqa: F401  # QtWebEngine 必须先于 QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

SHOT_BVID = os.environ.get("BILITOOLBOX_SHOT_BVID")
if not SHOT_BVID:
    raise SystemExit("请先设置 BILITOOLBOX_SHOT_BVID（任意真实视频 BV 号）")

READY_FILE = os.path.abspath("data/shot_ready.txt")

app = QApplication([])
from app.theme import apply

apply(app, "light")
from core import config

cfg = dict(config.load())
cfg["out_dir"] = "D:\\BiliToolbox"
from app.main_window import MainWindow

win = MainWindow(cfg)
win.resize(1440, 900)
win.show()

# ---- 监控采集数据去标识：真实拉取后即刻改写为演示数据 ----
import tools.monitor.server as ms

_orig_collect = ms.MonitorServer.collect_once


def fake_collect(self, bvid):
    meta, sample = _orig_collect(self, bvid)
    n = len(self.state.samples)
    meta.update({"bvid": "BV1xxxxxxxxx", "aid": 0, "title": "示例视频标题（演示数据）",
                 "owner": "UP主", "owner_mid": 0, "pubdate": 1756684800, "desc": "",
                 "pic": None, "videos": 2, "duration": 300,
                 "pages": [{"cid": 1, "page": 1, "part": "P1", "duration": 180},
                           {"cid": 2, "page": 2, "part": "P2", "duration": 120}]})
    sample["bvid"] = "BV1xxxxxxxxx"
    sample["view"] = 125000 + n * 53
    sample["like"] = 8764 + n * 3
    sample["coin"] = 3153 + n
    sample["favorite"] = 2087 + n
    sample["share"] = 946 + n
    sample["danmaku"] = 4268 + n * 2
    sample["reply"] = 1188 + n
    sample["online"] = 44 + n * 3
    ops = sample.get("online_pages", [])[:2]
    for idx, op in enumerate(ops):
        op["part"] = f"P{idx + 1}"
        op["total"] = 44 + n * 3 - idx * 2
        op["count"] = 31 + idx
    sample["online_pages"] = ops
    return meta, sample


ms.MonitorServer.collect_once = fake_collect


def shoot_comments():
    win.switch(0)
    win.pages[0].link_edit.setText("示例：参考视频链接")
    QTimer.singleShot(900, lambda: (win.grab().save("docs/screenshots/comments.png"),
                                    print("comments ok", flush=True), shoot_collector()))


def shoot_collector():
    win.switch(1)
    QTimer.singleShot(900, lambda: (win.grab().save("docs/screenshots/collector.png"),
                                    print("collector ok", flush=True), prep_monitor()))


def prep_monitor():
    """准备监控页：启动演示数据服务并刷新视图，然后写 ready 文件供外部抓屏。"""
    win.switch(2)
    p = win.pages[2]
    p.bvid_edit.setText(SHOT_BVID)
    p.interval_spin.setValue(5)
    p.data_row.set_value("D:\\BiliToolbox\\监控数据")
    p.on_start()
    p.bvid_edit.setText("")
    p.log_panel.clear()
    p.log_panel.append("监控已启动（演示数据）")

    def reload_view():
        if p.view is not None:
            p.view.reload()
        QTimer.singleShot(9000, write_ready)

    def write_ready():
        p.log_panel.clear()
        p.log_panel.append("监控已启动（演示数据）")
        p.log_panel.append("正常 · 已采样 6 次 · 通道 h2-ja3")
        g = win.frameGeometry()
        info = f"{os.getpid()} {g.x()} {g.y()} {g.width()} {g.height()} {win.devicePixelRatioF()}"
        with open(READY_FILE, "w", encoding="utf-8") as fh:
            fh.write(info)
        print("ready:", info, flush=True)

    QTimer.singleShot(14000, reload_view)


QTimer.singleShot(1200, shoot_comments)
app.exec()
