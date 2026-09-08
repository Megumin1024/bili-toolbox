# -*- coding: utf-8 -*-
"""生成 README 与界面验收截图，不连接真实目标或启动监控服务。

覆盖：深色 / 浅色、待机 / 运行中、1440×900 / 960×640。
运行（窗口会显示数秒）: python scripts/take_screenshots.py
"""
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

QMessageBox.information = staticmethod(lambda *a, **k: None)

app = QApplication([])
from app.theme import apply
from core import config, task_history

cfg = dict(config.load())
cfg.update({"out_dir": "D:\\BiliToolbox", "theme": "light"})
apply(app, "light")

# 历史截图使用临时文件，不污染用户真实配置目录或输出目录。
_history_temp = tempfile.TemporaryDirectory(prefix="bili-toolbox-history-shot-")
task_history.HISTORY_FILE = Path(_history_temp.name) / "task_history.json"
task_history.save_history([])
_history_demo_ready = False

# 截图只验证界面，不加载或调用真实采集流水线。
for module_name in ("tools.comments.pipeline", "tools.collector.pipeline"):
    stub = types.ModuleType(module_name)
    stub.run_pipeline = lambda **_kwargs: None
    sys.modules[module_name] = stub

from app.main_window import MainWindow

win = MainWindow(cfg)
OUT.mkdir(parents=True, exist_ok=True)

SHOTS = [
    # README 主截图：浅色 1440×900，覆盖待机与运行态
    ("light", 0, (1440, 900), "comments.png", "idle"),
    ("light", 1, (1440, 900), "collector.png", "running"),
    ("light", 2, (1440, 900), "monitor.png", "running"),
    # 深色与紧凑窗口验收截图
    ("dark", 0, (1440, 900), "comments-dark.png", "running"),
    ("dark", 1, (1440, 900), "collector-dark.png", "idle"),
    ("dark", 2, (1440, 900), "monitor-dark.png", "running"),
    ("light", 3, (1440, 900), "settings.png", "idle"),
    ("dark", 3, (1440, 900), "settings-dark.png", "idle"),
    ("light", 3, (960, 640), "settings-compact.png", "idle"),
    ("dark", 3, (960, 640), "settings-compact-dark.png", "idle"),
    ("light", 3, (1440, 900), "settings-history.png", "history"),
    ("dark", 3, (1440, 900), "settings-history-dark.png", "history"),
    ("dark", 3, (960, 640), "settings-history-compact-dark.png", "history"),
    ("dark", 3, (1440, 900), "settings-history-detail-dark.png", "history-detail"),
    ("light", 0, (960, 640), "comments-compact.png", "idle"),
    ("dark", 2, (960, 640), "monitor-compact-dark.png", "running"),
]


def reset_page(page):
    if hasattr(page, "progress_block"):
        page.progress_block.reset()
        page.btn_start.setEnabled(True)
        page.btn_cancel.setEnabled(False)
        page.result_card.hide()
        page.log_panel.clear()
    elif hasattr(page, "status_label"):
        page.status_label.set_state("idle", "○ 未启动")
        page.btn_start.setEnabled(True)
        page.btn_stop.setEnabled(False)
        page.log_panel.clear()


def prepare_demo(page_index, state):
    global _history_demo_ready
    page = win.pages[page_index]
    reset_page(page)

    if page_index == 0:
        page.link_edit.setText("https://www.bilibili.com/video/BV1DemoData01")
    elif page_index == 1:
        page.src_edit.setPlainText(
            "https://www.bilibili.com/video/BV1DemoData01\n"
            "https://space.bilibili.com/10086/favlist?fid=2026")

    if state in ("history", "history-detail") and not _history_demo_ready:
        root = Path(_history_temp.name) / (
            "very-long-output-directory-name-for-history-"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        )
        records = [
            ("completed", "2026-09-08T12:03:00+08:00", "评论抓取"),
            ("failed", "2026-09-08T12:02:00+08:00", "视频采集"),
            ("cancelled", "2026-09-08T12:01:00+08:00", "评论抓取"),
            ("interrupted", "2026-09-08T12:00:00+08:00", "视频采集"),
        ]
        for status, started_at, tool_name in records:
            is_comments = tool_name == "评论抓取"
            created = task_history.create_record(
                "comments" if is_comments else "collector",
                tool_name,
                started_at=started_at,
                target_summary=(
                    "www.bilibili.com/video/"
                    "BV1LongHistoryTargetxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                    if is_comments else
                    "www.bilibili.com/video/BV1CollectorHistoryLongTarget"
                ),
                output_dir=str(root),
                reusable_params=({"url": "BV1LongHistoryTarget"} if is_comments else {
                    "sources": ["BV1CollectorHistoryLongTarget"],
                }),
            )
            error = {"details": "演示失败：公开数据读取失败"} if status == "failed" else None
            outputs = [str(root / ("history-result-" + "x" * 80 + ".xlsx"))]
            task_history.update_record(
                created.record["id"], status,
                outputs=outputs if status == "completed" else [],
                error=error,
            )
        _history_demo_ready = True
        win.settings_page._refresh_task_history()

    if state not in ("running", "history", "history-detail"):
        return

    if state in ("history", "history-detail"):
        return

    if hasattr(page, "progress_block"):
        page.progress_block.set_value(42, 100, "正在读取公开数据 · 42%")
        page.btn_start.setEnabled(False)
        page.btn_cancel.setEnabled(True)
        page.log_panel.append("任务已启动（界面演示，不发起网络请求）")
        page.log_panel.append("已完成参数校验，正在整理公开数据")
        page.log_panel.append("进度 42 / 100 · 输出结构保持不变")
    else:
        page.status_label.set_state(
            "running", "● 运行中 · http://127.0.0.1:58194/")
        page.btn_start.setEnabled(False)
        page.btn_stop.setEnabled(True)
        page.log_panel.append("监控已启动（界面演示，不发起网络请求）")
        page.log_panel.append("正常 · 已采样 6 次 · 本地仪表盘已就绪")
        page.log_panel.append("播放=125474 · 点赞=8788 · 正在看=71")


def shoot(index=0):
    if index >= len(SHOTS):
        app.quit()
        return

    theme, page_index, size, filename, state = SHOTS[index]
    win._apply_theme(theme)
    settings = win.settings_page
    settings.theme_combo.blockSignals(True)
    settings.theme_combo.setCurrentIndex(
        max(0, settings.theme_combo.findData(theme)))
    settings.theme_combo.blockSignals(False)
    settings.cfg["theme"] = theme
    win.resize(*size)
    win.switch(page_index)
    prepare_demo(page_index, state)
    win.show()
    if page_index == 3 and size == (960, 640):
        page_scroll = settings.findChild(QScrollArea, "pageScroll")
        if page_scroll is not None:
            target = settings.history_card if state == "history" else settings.diagnostics_card
            page_scroll.ensureWidgetVisible(target, 0, 8)

    def save():
        if state == "history-detail" and page_index == 3:
            dialog = win.settings_page._build_task_history_dialog()
            dialog.show()
            app.processEvents()
            target = OUT / filename
            pixmap = dialog.grab()
            if pixmap.width() != size[0] or pixmap.height() != size[1]:
                pixmap = pixmap.scaled(
                    size[0], size[1], Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                pixmap.setDevicePixelRatio(1.0)
            ok = pixmap.save(str(target))
            dialog.close()
            print(f"{filename}: {'ok' if ok else 'failed'}", flush=True)
            if not ok:
                print(f"Screenshot failed: {target}", file=sys.stderr, flush=True)
                app.exit(1)
                return
            QTimer.singleShot(180, lambda: shoot(index + 1))
            return
        target = OUT / filename
        pixmap = win.grab()
        if pixmap.width() != size[0] or pixmap.height() != size[1]:
            pixmap = pixmap.scaled(
                size[0], size[1], Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            pixmap.setDevicePixelRatio(1.0)
        ok = pixmap.save(str(target))
        print(f"{filename}: {'ok' if ok else 'failed'}", flush=True)
        if not ok:
            print(f"Screenshot failed: {target}", file=sys.stderr, flush=True)
            app.exit(1)
            return
        QTimer.singleShot(180, lambda: shoot(index + 1))

    QTimer.singleShot(480, save)


QTimer.singleShot(700, shoot)
sys.exit(app.exec())
