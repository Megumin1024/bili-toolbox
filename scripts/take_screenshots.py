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
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

QMessageBox.information = staticmethod(lambda *a, **k: None)

app = QApplication([])
# Offscreen CI images may not inherit the desktop CJK font. Reuse the Windows
# fonts already expected by the app so screenshot checks validate actual glyphs.
available_fonts = set(QFontDatabase.families())
if not available_fonts:
    # The headless Qt image environment can expose no system font database.
    # Load an existing Windows CJK font; this adds no project dependency.
    for font_path in (Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\simsun.ttc")):
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id >= 0:
                available_fonts.update(QFontDatabase.applicationFontFamilies(font_id))
            if available_fonts:
                break
for font_name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimSun"):
    if font_name in available_fonts:
        app.setFont(QFont(font_name, 10))
        break
from app.theme import apply
from core import config, task_history, task_presets
from tools.monitor.alerts import AlertEvent
from tools.monitor.notifications import RecordingNotificationAdapter
from tools.collector.comparison import SessionComparison

cfg = dict(config.load())
cfg.update({
    "out_dir": "D:\\BiliToolbox", "theme": "light",
    # 截图不得弹出系统通知或播放声音。
    "_monitor_notification_adapter_factory": lambda: RecordingNotificationAdapter(),
})
apply(app, "light")

# 历史截图使用临时文件，不污染用户真实配置目录或输出目录。
_history_temp = tempfile.TemporaryDirectory(prefix="bili-toolbox-history-shot-")
task_history.HISTORY_FILE = Path(_history_temp.name) / "task_history.json"
_preset_temp = tempfile.TemporaryDirectory(prefix="bili-toolbox-presets-shot-")
task_presets.PRESETS_FILE = Path(_preset_temp.name) / "task_presets.json"
task_presets.save_presets([])
# 历史详情中的结果文件使用固定的中性示例路径，避免把本机临时目录写进公开截图。
_history_demo_output_root = Path(r"D:\BiliToolbox\导出") / (
    "very-long-output-directory-name-for-history-"
    "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
)
task_history.save_history([])
_history_demo_ready = False
_report_temp = tempfile.TemporaryDirectory(prefix="bili-toolbox-report-shot-")
_report_demo_ready = False

# 截图只验证界面，不加载或调用真实采集流水线。
for module_name in (
    "tools.comments.pipeline", "tools.collector.pipeline", "tools.data_check.pipeline",
):
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
    ("light", 3, (1440, 900), "data-check.png", "idle"),
    ("light", 4, (1440, 900), "report-center.png", "ready"),
    ("dark", 4, (1440, 900), "report-center-dark.png", "ready"),
    ("light", 4, (960, 640), "report-center-compact.png", "ready"),
    ("dark", 3, (1440, 900), "data-check-dark.png", "issues"),
    ("light", 3, (960, 640), "data-check-compact.png", "empty"),
    ("dark", 3, (960, 640), "data-check-compact-dark.png", "issues"),
    ("light", 3, (1440, 900), "data-check-repair-options.png", "repair-options"),
    ("dark", 3, (1440, 900), "data-check-repair-options-dark.png", "repair-options"),
    ("light", 3, (960, 640), "data-check-repair-compact.png", "repair-options"),
    ("dark", 3, (960, 640), "data-check-repair-compact-dark.png", "repair-options"),
    ("light", 3, (1440, 900), "data-check-repair-result.png", "repair-result"),
    ("dark", 3, (1440, 900), "data-check-repair-result-dark.png", "repair-result"),
    # 深色与紧凑窗口验收截图
    ("dark", 0, (1440, 900), "comments-dark.png", "running"),
    ("dark", 1, (1440, 900), "collector-dark.png", "idle"),
    ("dark", 2, (1440, 900), "monitor-dark.png", "running"),
    ("light", 2, (960, 640), "monitor-compact.png", "idle"),
    ("light", 1, (960, 640), "collector-compact.png", "idle"),
    ("dark", 1, (960, 640), "collector-compact-dark.png", "idle"),
    ("light", 5, (1440, 900), "settings.png", "idle"),
    ("dark", 5, (1440, 900), "settings-dark.png", "idle"),
    ("light", 5, (960, 640), "settings-compact.png", "idle"),
    ("dark", 5, (960, 640), "settings-compact-dark.png", "idle"),
    ("light", 5, (1440, 900), "settings-history.png", "history"),
    ("dark", 5, (1440, 900), "settings-history-dark.png", "history"),
    ("dark", 5, (960, 640), "settings-history-compact-dark.png", "history"),
    ("dark", 5, (1440, 900), "settings-history-detail-dark.png", "history-detail"),
    ("light", 0, (960, 640), "comments-compact.png", "idle"),
    ("dark", 2, (960, 640), "monitor-compact-dark.png", "running"),
]

DASHBOARD_SHOTS = [
    ("light", (1440, 900), (1000, 700), "collector-dashboard-light.png", "dashboard"),
    ("dark", (1440, 900), (1000, 700), "collector-dashboard-dark.png", "dashboard"),
    ("light", (960, 640), (960, 640), "collector-dashboard-compact.png", "dashboard"),
    ("dark", (960, 640), (960, 640), "collector-dashboard-compact-dark.png", "dashboard"),
    ("light", (960, 640), (960, 640), "collector-dashboard-empty.png", "dashboard-empty"),
]


def reset_page(page):
    if hasattr(page, "progress_block"):
        page.progress_block.reset()
        page.btn_start.setEnabled(True)
        page.btn_cancel.setEnabled(False)
        page.result_card.hide()
        page.log_panel.clear()
        if hasattr(page, "_clear_dashboard"):
            if getattr(page, "_dashboard_dialog", None) is not None:
                page._dashboard_dialog.close()
            page._clear_dashboard()
        if hasattr(page, "file_list"):
            page.file_list.clear()
    elif hasattr(page, "status_label"):
        page.status_label.set_state("idle", "○ 未启动")
        page.btn_start.setEnabled(True)
        page.btn_stop.setEnabled(False)
        page.log_panel.clear()
        if hasattr(page, "_clear_recent_alerts"):
            page._clear_recent_alerts()
    elif hasattr(page, "status_pill"):
        page._set_status("待机", "idle")
        page.btn_cancel.setEnabled(False)
        page.search_edit.clear()
        page.compare_summary.setText("尚未生成文件对比。")
        page.period_summary.setText("尚未生成时间段对比。")


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
        demo_bvids = [f"BVDemo{i:06d}" for i in range(1, 13)]
        comparison = SessionComparison(demo_bvids)
        first_round = [
            {
                "bvid": bvid,
                "title": ("一个用于展示长标题截断和工具提示的演示视频标题 "
                          if index == 1 else f"演示视频标题 {index}"),
                "owner": f"演示UP主 {index}",
                "view": index * 10000,
                "like": index * 800,
                "fetched_at": 1_700_000_000,
            }
            for index, bvid in enumerate(demo_bvids, 1)
        ]
        second_round = [
            {**value, "view": value["view"] + (index - 6) * 700,
             "like": value["like"] + index * 20,
             "fetched_at": 1_700_003_600}
            for index, value in enumerate(first_round, 1)
        ]
        if state == "dashboard-empty":
            return
        dashboard_state = "tracking" if state == "running" else "completed"
        page._render_dashboard(comparison.publish_round(first_round))
        page._render_dashboard(comparison.publish_round(
            second_round, round_number=2, state=dashboard_state))
    elif page_index == 3:
        page.out_row.set_value(r"D:\BiliToolbox\数据检查报告")
        if state == "idle":
            values = [
                r"D:\BiliToolbox\导出\comments-2026-09-08.jsonl",
                r"D:\BiliToolbox\导出\very-long-video-snapshot-report-name-xxxxxxxxxxxxxxxx.xlsx",
                r"D:\BiliToolbox\导出\archive\nested\local-data.jsonl",
            ]
        elif state in ("issues", "repair-options", "repair-result"):
            values = [
                r"D:\BiliToolbox\导出\comments-with-duplicate-and-blank.jsonl",
                r"D:\BiliToolbox\导出\damaged-report.xlsx",
            ]
        else:
            values = []
        for value in values:
            page.file_list.addItem(value)
            page.file_list.item(page.file_list.count() - 1).setToolTip(value)
        if state in ("issues", "repair-options", "repair-result"):
            page.on_finished({
                "report": r"D:\BiliToolbox\数据检查报告\数据检查报告_20260908_193000.xlsx",
                "dir": r"D:\BiliToolbox\数据检查报告",
                "stats": {"total_issues": 7},
                "source_files": [
                    {"path": value, "sha256": "0" * 64, "size": 1024, "mtime_ns": 1}
                    for value in values
                ],
            })
            page.log_panel.append("已检查 2 个本地文件，发现 7 项问题。")
            page.log_panel.append("其中 1 个文件损坏，已继续检查其他文件。", "warn")
            if state == "repair-result":
                page.repair_progress.set_success("修复完成")
                page.repair_result_card.show_result(
                    "修复副本已生成 ✓",
                    [
                        ("修复副本", r"D:\BiliToolbox\修复副本\comments-with-duplicate-and-blank_修复副本.jsonl"),
                        ("修复清单", r"D:\BiliToolbox\修复副本\数据修复清单_20260908_193100.xlsx"),
                        ("打开输出目录", r"D:\BiliToolbox\修复副本"),
                    ],
                )
                page.log_panel.append("1 个损坏文件已跳过，其他修复副本已安全生成。", "warn")
    elif page_index == 4:
        global _report_demo_ready
        if not _report_demo_ready:
            report_root = Path(_report_temp.name)
            comment_a = report_root / "comments-a.jsonl"
            comment_b = report_root / "comments-b.jsonl"
            comment_a.write_text(
                '{"rpid": 1, "message": "演示评论", "ctime": 1700000000, "like": 12}\n',
                encoding="utf-8",
            )
            comment_b.write_text(
                '{"rpid": 1, "message": "演示评论", "ctime": 1700000000, "like": 18}\n',
                encoding="utf-8",
            )
            page.manual_files = [str(comment_a), str(comment_b)]
            page.refresh_sources()
            _report_demo_ready = True

    elif page_index == 2:
        running = state == "running"
        page.alert_total_check.setChecked(running)
        page.alert_windows_check.setChecked(True)
        page.alert_sound_check.setChecked(running)
        page.alert_milestone_check.setChecked(running)
        page.alert_milestone_edit.setText("10000,50000,100000,500000,1000000")
        page.alert_stagnation_check.setChecked(running)
        page.alert_spike_check.setChecked(running)
        page.alert_disconnect_check.setChecked(running)
        if running:
            page._record_alert(AlertEvent(
                "milestone", "播放量里程碑",
                "播放量已达到 10000、50000、100000。", 1788900000))
            page._record_alert(AlertEvent(
                "recovered", "监控已恢复",
                "采集已恢复，提醒规则重新布防。", 1788900060))
            page._record_alert(AlertEvent(
                "spike", "播放量异常突增",
                "最近 5 分钟增长 15000（25.0%）。", 1788900120))

    if state in ("history", "history-detail") and not _history_demo_ready:
        root = _history_demo_output_root
        records = [
            ("completed", "2026-01-01T12:03:00+08:00", "评论抓取"),
            ("failed", "2026-01-01T12:02:00+08:00", "视频采集"),
            ("cancelled", "2026-01-01T12:01:00+08:00", "评论抓取"),
            ("interrupted", "2026-01-01T12:00:00+08:00", "视频采集"),
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

    if state == "issues":
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
        QTimer.singleShot(220, lambda: shoot_dashboard(0))
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
    if size == (960, 640) and page_index in (1, 2, 3, 4, 5):
        current_page = settings if page_index == 5 else win.pages[page_index]
        page_scroll = current_page.findChild(QScrollArea, "pageScroll")
        if page_scroll is not None:
            if page_index == 5:
                target = settings.history_card if state == "history" else settings.diagnostics_card
            elif page_index == 4:
                target = current_page.tabs
            elif page_index == 1:
                target = current_page.dashboard_open_button
            elif page_index == 2:
                target = current_page.alert_card
            else:
                target = (current_page.repair_card
                          if state in ("repair-options", "repair-result")
                          else current_page.result_card if state == "issues"
                          else current_page.params_card)
            page_scroll.ensureWidgetVisible(target, 0, 8)

    def save():
        if state == "history-detail" and page_index == 5:
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


def shoot_dashboard(index=0):
    if index >= len(DASHBOARD_SHOTS):
        app.quit()
        return

    theme, window_size, dialog_size, filename, state = DASHBOARD_SHOTS[index]
    win._apply_theme(theme)
    settings = win.settings_page
    settings.theme_combo.blockSignals(True)
    settings.theme_combo.setCurrentIndex(
        max(0, settings.theme_combo.findData(theme)))
    settings.theme_combo.blockSignals(False)
    settings.cfg["theme"] = theme
    win.resize(*window_size)
    win.switch(1)
    prepare_demo(1, state)
    page = win.pages[1]
    win.show()
    page._open_dashboard()
    app.processEvents()
    dialog = page._dashboard_dialog
    # The screenshot path intentionally bypasses screen clamping: the dialog
    # component itself must be verified at the requested native dimensions.
    dialog.resize(*dialog_size)
    dialog.show()
    app.processEvents()
    dialog.resize(*dialog_size)
    app.processEvents()

    def save_dashboard():
        target = OUT / filename
        pixmap = dialog.grab()
        actual_size = (pixmap.width(), pixmap.height())
        if actual_size != tuple(dialog_size):
            print(
                f"Screenshot size mismatch for {filename}: "
                f"got {actual_size[0]}x{actual_size[1]}, "
                f"expected {dialog_size[0]}x{dialog_size[1]}",
                file=sys.stderr,
                flush=True,
            )
            dialog.close()
            app.exit(1)
            return
        ok = pixmap.save(str(target))
        print(
            f"{filename}: {actual_size[0]}x{actual_size[1]} "
            f"{'ok' if ok else 'failed'}",
            flush=True,
        )
        dialog.close()
        if not ok:
            print(f"Screenshot failed: {target}", file=sys.stderr, flush=True)
            app.exit(1)
            return
        QTimer.singleShot(180, lambda: shoot_dashboard(index + 1))

    QTimer.singleShot(520, save_dashboard)


QTimer.singleShot(700, shoot)
sys.exit(app.exec())
