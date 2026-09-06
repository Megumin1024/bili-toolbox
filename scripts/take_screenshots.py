# -*- coding: utf-8 -*-
"""生成 README 截图（浅色主题 + 演示状态，无真实目标信息）。开发期工具。

运行（窗口会实际显示几秒钟）: python scripts/take_screenshots.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
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
win.resize(1440, 900)
win.show()


def shoot_comments():
    win.switch(0)
    win.pages[0].link_edit.setText("示例：参考视频链接")
    QTimer.singleShot(900, lambda: (win.grab().save("docs/screenshots/comments.png"),
                                    print("comments ok", flush=True), shoot_collector()))


def shoot_collector():
    win.switch(1)
    QTimer.singleShot(900, lambda: (win.grab().save("docs/screenshots/collector.png"),
                                    print("collector ok", flush=True), shoot_monitor()))


def shoot_monitor():
    win.switch(2)
    p = win.pages[2]
    # 摆拍运行态：不启动真实服务
    p.status_label.setText("运行中 · http://127.0.0.1:58194/")
    p.btn_start.setEnabled(False)
    p.btn_stop.setEnabled(True)
    p.log_panel.append("监控已启动（演示数据）")
    p.log_panel.append("正常 · 已采样 6 次 · 通道 h2-ja3")
    p.log_panel.append("采集成功 播放=125474 点赞=8788 正在看=71 | 通道=h2-ja3 33ms")

    def do():
        ok = win.grab().save("docs/screenshots/monitor.png")
        print("monitor ok:", ok, flush=True)
        app.quit()

    QTimer.singleShot(900, do)


QTimer.singleShot(1200, shoot_comments)
app.exec()
