# -*- coding: utf-8 -*-
"""直播追踪页：房间输入 → 快照/轮询参数 + 开播提醒推送（与监控页共享配置）。"""
import functools
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox,
                               QFormLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QSpinBox)

from app.task_page import TaskPage
from app.widgets import PathRow, muted
from core import config as app_config
from core import output as output_mod
from core.budget import (DEFAULT_MAX_MINUTES, DEFAULT_MAX_REQUESTS,
                         MAX_MINUTES_LIMIT, MAX_REQUESTS_LIMIT)
from core.notify import WEBHOOK_FORMATS, WebhookAdapter

from . import core
from .pipeline import run_pipeline

SAMPLE_TARGET = "房间号或直播间链接：6 · live.bilibili.com/6"


class LiveRoomPage(TaskPage):
    tool_title = "直播追踪"
    tool_subtitle = "输入直播间号/链接 → 快照/轮询追踪 + 开播提醒 → Excel（游客可用）"
    tool_module = "直播分析"
    history_enabled = True
    history_tool_id = "live_room"
    preset_enabled = True

    webhook_log = Signal(str)

    def __init__(self, cfg, parent=None):
        self.webhook_adapter = None
        super().__init__(cfg, parent)
        # 推送线程不能直接触碰 Qt 控件；日志经信号排队切回 GUI 线程。
        self.webhook_log.connect(self._log)

    # ---------- 参数区 ----------

    def build_params(self):
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(10)

        self.room_edit = QLineEdit()
        self.room_edit.setPlaceholderText(SAMPLE_TARGET)
        form.addRow("直播间", self.room_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("单次快照", "snapshot")
        self.mode_combo.addItem("轮询追踪", "track")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("模式", self.mode_combo)

        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, core.MAX_ROUNDS)
        self.rounds_spin.setValue(core.DEFAULT_ROUNDS)
        self.rounds_spin.setSuffix(" 轮")
        form.addRow("追踪轮数", self.rounds_spin)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(core.MIN_INTERVAL, core.MAX_INTERVAL)
        self.interval_spin.setValue(core.DEFAULT_INTERVAL)
        self.interval_spin.setSuffix(" 秒")
        form.addRow("轮询间隔", self.interval_spin)

        default_out = str(Path(self.cfg.get("out_dir")
                               or output_mod.default_out_dir()) / "直播追踪")
        self.out_row = PathRow(None, default_out)
        form.addRow("输出目录", self.out_row)

        self.max_requests_edit = QLineEdit(str(DEFAULT_MAX_REQUESTS))
        self.max_requests_edit.setPlaceholderText(
            f"本次任务最多发多少次请求（1–{MAX_REQUESTS_LIMIT:,}），到限安全停止")
        form.addRow("请求数上限", self.max_requests_edit)

        self.max_minutes_edit = QLineEdit(str(DEFAULT_MAX_MINUTES))
        self.max_minutes_edit.setPlaceholderText(
            f"任务最长运行多少分钟（1–{MAX_MINUTES_LIMIT:,}），到限安全停止")
        form.addRow("时长上限(分钟)", self.max_minutes_edit)

        self.auto_open = QCheckBox("完成后自动打开 Excel")
        self.auto_open.setChecked(True)
        form.addRow("", self.auto_open)

        # 开播提醒推送独占一行（复选框 + 格式 + URL + 测试按钮），与实时监控
        # 页同款交互、共享同一 config 字段（webhook_url / webhook_format），
        # 两页改一处全局生效。
        webhook_row = QHBoxLayout()
        webhook_row.setSpacing(8)
        self.push_check = QCheckBox("开播提醒推送")
        self.push_check.setChecked(False)
        self.webhook_format_combo = QComboBox()
        for val, label in (("json", "通用 JSON"), ("serverchan", "Server酱")):
            self.webhook_format_combo.addItem(label, val)
        # 先设初值再连信号：初始化阶段不得触发保存（此时 URL 控件还不存在）。
        self.webhook_format_combo.setCurrentIndex(max(
            0, self.webhook_format_combo.findData(
                str(self.cfg.get("webhook_format") or "json"))))
        self.webhook_format_combo.currentIndexChanged.connect(
            self._on_webhook_format_changed)
        self.webhook_url_edit = QLineEdit()
        self.webhook_url_edit.setPlaceholderText(self._webhook_placeholder())
        self.webhook_url_edit.setText(str(self.cfg.get("webhook_url") or ""))
        self.webhook_url_edit.editingFinished.connect(self._save_webhook_url)
        self.btn_test_webhook = QPushButton("发送测试")
        self.btn_test_webhook.setObjectName("flat")
        self.btn_test_webhook.clicked.connect(self.on_test_webhook)
        webhook_row.addWidget(self.push_check)
        webhook_row.addWidget(self.webhook_format_combo)
        webhook_row.addWidget(self.webhook_url_edit, 1)
        webhook_row.addWidget(self.btn_test_webhook)

        self.params_lay.addLayout(form)
        self.params_lay.addLayout(webhook_row)
        self.params_lay.addWidget(muted(
            "「人气」为 B 站接口返回的人气值口径，不是精确观看人数。"
            "轮询每轮仅 1 次业务请求，间隔下限 30 秒。\n"
            "开播提醒：仅当相邻两轮直播状态翻转（开播/下播）时推送一条，"
            "每小时最多 10 条；与实时监控共用同一 Webhook 配置，"
            "SendKey 与 URL 只保存在本机、不进入任务历史与预设。"))

        # spin 的 sizeHint 按取值上限位数预留宽度（监控页踩过的 960 裁切教训），
        # 这里上限位数小，去按钮 + 显式下限进一步兜底。
        for spin, min_w in ((self.rounds_spin, 110),
                            (self.interval_spin, 110)):
            spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
            spin.setMinimumWidth(min_w)

        self._on_mode_changed()
        self.room_edit.setFocus()

    def _on_mode_changed(self, _index=None):
        track = self.mode_combo.currentData() == "track"
        self.rounds_spin.setEnabled(track)
        self.interval_spin.setEnabled(track)

    @staticmethod
    def _parse_int(text, label, default, lo, hi):
        raw = (text or "").strip()
        if not raw:
            return default
        try:
            value = int(float(raw))
        except ValueError:
            raise ValueError(f"{label}需为数字")
        if not lo <= value <= hi:
            raise ValueError(f"{label}需在 {lo}~{hi} 之间")
        return value

    def _read(self):
        target = self.room_edit.text().strip()
        if not target:
            raise ValueError("请先输入直播间号或直播间链接")
        core.parse_room_input(target)      # 提前报错，别等任务跑起来才失败
        out_dir = self.out_row.value()
        if not out_dir:
            raise ValueError("请设置输出目录")
        mode = self.mode_combo.currentData()
        if mode not in ("snapshot", "track"):
            mode = "snapshot"
        max_requests = self._parse_int(
            self.max_requests_edit.text(), "请求数上限",
            DEFAULT_MAX_REQUESTS, 1, MAX_REQUESTS_LIMIT)
        max_minutes = self._parse_int(
            self.max_minutes_edit.text(), "时长上限",
            DEFAULT_MAX_MINUTES, 1, MAX_MINUTES_LIMIT)
        return (target, out_dir, mode, self.rounds_spin.value(),
                self.interval_spin.value(), max_requests, max_minutes)

    def collect_params(self):
        (target, out_dir, mode, rounds, interval,
         max_requests, max_minutes) = self._read()
        return {"target": target, "out_dir": out_dir, "mode": mode,
                "rounds": rounds, "interval": interval,
                "open_result": self.auto_open.isChecked(),
                "max_requests": max_requests, "max_minutes": max_minutes}

    def collect_preset_params(self):
        return self.collect_params()

    def apply_reusable_params(self, params):
        self.room_edit.setText(str(params.get("target", "")))
        self.out_row.set_value(params.get("out_dir", ""))
        mode = params.get("mode", "snapshot")
        index = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(index if index >= 0 else 0)
        try:
            self.rounds_spin.setValue(int(params.get("rounds", core.DEFAULT_ROUNDS)))
        except (TypeError, ValueError):
            self.rounds_spin.setValue(core.DEFAULT_ROUNDS)
        try:
            self.interval_spin.setValue(
                int(params.get("interval", core.DEFAULT_INTERVAL)))
        except (TypeError, ValueError):
            self.interval_spin.setValue(core.DEFAULT_INTERVAL)
        self.max_requests_edit.setText(
            str(params.get("max_requests", DEFAULT_MAX_REQUESTS)))
        self.max_minutes_edit.setText(
            str(params.get("max_minutes", DEFAULT_MAX_MINUTES)))
        self.auto_open.setChecked(bool(params.get("open_result", True)))
        self.room_edit.setFocus()

    def apply_preset_params(self, params):
        self.apply_reusable_params(params)

    def pipeline(self):
        # on_start 在 GUI 线程取好推送回调再交给任务线程；URL/格式不进
        # collect_params，因此不落入任务历史与预设。
        notify = None
        if self.push_check.isChecked():
            adapter = self._ensure_webhook_adapter()
            if adapter is not None:
                notify = adapter.send
        return functools.partial(run_pipeline, notify=notify)

    # ---------- 任务历史 ----------

    def history_target_summary(self, params):
        try:
            return f"直播间 {core.parse_room_input(params.get('target', ''))}"
        except ValueError:
            return "直播追踪（未识别房间）"

    def history_reusable_params(self, params):
        return {
            "target": params.get("target", ""),
            "out_dir": params.get("out_dir", ""),
            "mode": params.get("mode", "snapshot"),
            "rounds": params.get("rounds", core.DEFAULT_ROUNDS),
            "interval": params.get("interval", core.DEFAULT_INTERVAL),
            "max_requests": params.get("max_requests", DEFAULT_MAX_REQUESTS),
            "max_minutes": params.get("max_minutes", DEFAULT_MAX_MINUTES),
            "open_result": bool(params.get("open_result", True)),
        }

    def history_output_paths(self, result):
        if not isinstance(result, dict):
            return []
        return [result.get(key) for key in ("xlsx", "jsonl") if result.get(key)]

    def on_finished(self, result):
        stats = result.get("stats") or {}
        if stats.get("stopped_reason") == "budget_reached":
            tail = "（已达上限安全停止）"
        else:
            tail = ""
        if result.get("mode") == "track":
            headline = f"完成 ✓ 共 {result['rows']:,} 轮快照{tail}"
        else:
            headline = f"完成 ✓ 已保存房间 {result.get('room_id', '')} 的快照{tail}"
        files = []
        if result.get("xlsx"):
            files.append(("Excel 报告", result["xlsx"]))
        if result.get("jsonl"):
            files.append(("原始数据(jsonl)", result["jsonl"]))
        files.append(("打开输出目录", result["dir"]))
        self.result_card.show_result(headline, files)

    # ---------- 开播提醒推送（与监控页同边界：URL 只落 config.json） ----------

    def _log(self, msg):
        self.log_panel.append(str(msg))

    def _new_webhook_adapter(self):
        return WebhookAdapter(
            url_getter=self.webhook_url_edit.text,
            format_getter=self._webhook_format_value,
            log=self.webhook_log.emit,
        )

    def _ensure_webhook_adapter(self):
        """懒创建并跨任务保留：频控预算不随任务重启而清零。"""
        if self.webhook_adapter is None:
            try:
                self.webhook_adapter = self._new_webhook_adapter()
            except Exception as exc:  # noqa: BLE001 - 通道失败不得影响页面
                self._log(f"Webhook 适配器创建失败（{type(exc).__name__}）。")
                self.webhook_adapter = None
        return self.webhook_adapter

    def _save_webhook_url(self):
        """URL 只落 config.json 最小字段，不进历史/预设（与监控页同边界）。"""
        url = self.webhook_url_edit.text().strip()
        if self.cfg.get("webhook_url") == url:
            return
        self.cfg["webhook_url"] = url
        app_config.save({"webhook_url": url})

    def _webhook_format_value(self) -> str:
        """下拉当前格式；未知值回退通用 JSON（config 层只做键白名单）。"""
        data = self.webhook_format_combo.currentData()
        return data if data in WEBHOOK_FORMATS else "json"

    def _webhook_placeholder(self) -> str:
        if self._webhook_format_value() == "serverchan":
            return "填 SendKey 或 .send 完整链接（仅保存在本机配置文件）"
        return "https://…（接收端 URL，仅保存在本机配置文件）"

    def _on_webhook_format_changed(self):
        self.webhook_url_edit.setPlaceholderText(self._webhook_placeholder())
        self._save_webhook_format()

    def _save_webhook_format(self):
        """格式与 webhook_url 同模式：最小字段、只落 config.json。"""
        value = self._webhook_format_value()
        if self.cfg.get("webhook_format") == value:
            return
        self.cfg["webhook_format"] = value
        app_config.save({"webhook_format": value})

    def on_test_webhook(self):
        if not self.push_check.isChecked():
            self._log("推送测试未发送：请先勾选「开播提醒推送」。")
            return
        self._save_webhook_url()
        adapter = self._ensure_webhook_adapter()
        if adapter is not None and adapter.send(
                "test", "开播提醒测试",
                "这是一次测试推送，不会启动或改变任务。"):
            self._log("推送测试已提交，发送结果见后续日志。")
