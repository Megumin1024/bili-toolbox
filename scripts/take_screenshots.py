# -*- coding: utf-8 -*-
"""生成 README 截图（浅色主题 + 演示数据，无真实目标信息）。开发期工具。"""
import time

import app.env  # noqa: F401
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

QMessageBox.information = staticmethod(lambda *a, **k: None)

app = QApplication([])
from app.theme import apply

apply(app, "light")
from core import config

cfg = dict(config.load())
cfg["out_dir"] = "D:\\BiliToolbox"
from app.main_window import MainWindow

win = MainWindow(cfg)
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1440, 900)
win.show()

# ---- 监控页采集数据去标识：真实拉取后即刻改写为演示数据 ----
import tools.monitor.server as ms

_orig_collect = ms.MonitorServer.collect_once


def fake_collect(self, bvid):
    meta, sample = _orig_collect(self, bvid)
    n = len(self.state.samples)
    meta.update({
        "bvid": "BV1xxxxxxxxx", "aid": 0, "title": "示例视频标题（演示数据）",
        "owner": "UP主", "owner_mid": 0, "pubdate": 1756684800,
        "desc": "", "pic": None, "videos": 2,
        "pages": [{"cid": 1, "page": 1, "part": "P1", "duration": 180},
                  {"cid": 2, "page": 2, "part": "P2", "duration": 120}],
    })
    sample["bvid"] = "BV1xxxxxxxxx"
    sample["view"] = 125000 + n * 53
    sample["like"] = 8764 + n * 3
    sample["coin"] = 3153 + n
    sample["favorite"] = 2087 + n
    sample["share"] = 946 + n
    sample["danmaku"] = 4268 + n * 2
    sample["reply"] = 1188 + n
    sample["online"] = 44 + n * 3
    for idx, op in enumerate(sample.get("online_pages", [])):
        op["part"] = f"P{idx + 1}"
        op["total"] = 44 + n * 3 - idx * 2
        op["count"] = 31 + idx
    return meta, sample


ms.MonitorServer.collect_once = fake_collect

jobs_done = []


def shoot_comments():
    win.switch(0)
    p = win.pages[0]
    p.link_edit.setText("示例：参考视频链接")
    QTimer.singleShot(900, lambda: (win.grab().save("docs/screenshots/comments.png"),
                                    print("comments ok"), shoot_collector()))


def shoot_collector():
    win.switch(1)
    QTimer.singleShot(900, lambda: (win.grab().save("docs/screenshots/collector.png"),
                                    print("collector ok"), shoot_monitor()))


def shoot_monitor():
    win.switch(2)
    p = win.pages[2]
    p.bvid_edit.setText("BV1zi7Y6BEdS")          # 内部用真实 BV 拉取，展示层已去标识
    p.interval_spin.setValue(5)
    p.data_row.set_value("D:\\BiliToolbox\\监控数据")
    p.on_start()
    p.bvid_edit.setText("")
    p.log_panel.clear()
    p.log_panel.append("监控已启动（演示数据），每 5 秒采样一次…")
    p.log_panel.append("仪表盘加载中…")

    def reload_view():
        if p.view is not None:
            p.view.reload()
        QTimer.singleShot(8000, finish)

    def finish():
        p.log_panel.clear()
        p.log_panel.append("已启动监控（演示数据）")
        p.log_panel.append("正常 · 已采样 6 次 · 通道 h2-ja3")
        ok = win.grab().save("docs/screenshots/monitor.png")
        print("monitor ok:", ok)
        app.quit()

    QTimer.singleShot(14000, reload_view)


QTimer.singleShot(1200, shoot_comments)
app.exec()
