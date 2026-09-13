# -*- coding: utf-8 -*-
"""直播间状态解析与导出（纯逻辑，网络经注入的 fetch，可离线测试）。

输入归一与 get_info 解析的正本已上提 `core/live.py`（实时监控「直播间
模式」共用同一套归一口径，见该模块 docstring）；本模块 re-export 既有
导入面——`tools.live_room.core` 上的名字一个不少，行为逐字节一致。

- GET xlive/web-room/v2/index/getRoomPlayInfo?room_id=：短号/别名 → 真实
  room_id（仅作 get_info 报业务错误时的输入归一兜底，最多用一次）。

口径（任务卡硬约束）：online 是平台「人气值」，不是精确观看人数——所有
用户可见表头与文案一律写「人气」。轮询礼仪：间隔下限 30s、上限 3600s，
轮数上限 500；每轮仅 1 次业务请求（get_info）。
"""
from __future__ import annotations

import json
from datetime import datetime

from openpyxl.utils import get_column_letter

from core import xlsx as xlsx_mod
from core.live import (GET_INFO_URL, LIVE_STATUS_LABELS, get_info_url,
                       live_status_label, parse_get_info, parse_room_input,
                       status_transition, to_int)

RESOLVE_URL = ("https://api.live.bilibili.com"
               "/xlive/web-room/v2/index/getRoomPlayInfo")

DEFAULT_MODE = "snapshot"
DEFAULT_ROUNDS = 10
MAX_ROUNDS = 500
DEFAULT_INTERVAL = 30
MIN_INTERVAL = 30
MAX_INTERVAL = 3600

DASH = "—"


def _s(value, dash=DASH):
    """用户可见文本里绝不放 None / 空白（参考 danmaku analysis 的 _s）。"""
    if value is None:
        return dash
    text = str(value).strip()
    return text or dash


def resolve_room_id(fetch, room_id):
    """输入归一兜底：getRoomPlayInfo → 真实 room_id（主播 uid 由 get_info 给）。

    仅供 get_info 报业务错误（短号/别名不认）时用一次；解析不出返回 0，
    由调用方决定是否回退原始错误。
    """
    url = f"{RESOLVE_URL}?room_id={int(room_id)}"
    data = (fetch(url) or {}).get("data")
    if not isinstance(data, dict):
        return 0
    return to_int(data.get("room_id"), 0)


def push_message(row, kind, prev_label=""):
    """翻转推送内容：(event_type, title, text)。

    文本只含房间号/标题/固定话术，交给 WebhookAdapter 后还会过
    webhook_payload 的白名单与 sanitize_text 双层兜底。
    """
    room = row.get("room_id")
    title = _s(row.get("title"))
    if kind == "live":
        return ("live_status", "开播提醒",
                f"房间 {room}「{title}」已开播（人气 {row.get('online', 0)}）")
    if kind == "offline":
        return ("live_status", "下播提醒",
                f"房间 {room}「{title}」已下播")
    cur_label = _s(row.get("live_status_label"))
    return ("live_status", "直播状态变化提醒",
            f"房间 {room} 直播状态变化：{_s(prev_label)} → {cur_label}")


# ---------- JSONL / Excel ----------

def jsonl_filename(room_id):
    return f"live_{room_id}.jsonl"


def append_jsonl(path, row):
    """每轮一条快照追加落盘；即时 flush，取消/到限不丢已抓轮次。"""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


_DETAIL_HEADERS = ("轮次", "时间", "直播状态", "标题", "人气", "分区",
                   "父分区", "开播时刻", "房间号", "主播UID", "标签")
_DETAIL_WIDTHS = (6, 20, 10, 50, 10, 14, 14, 20, 12, 12, 30)


def export_xlsx(rows, stats, path, progress=None):
    """快照 → Excel。单次快照只有「概览」一张表；轮询追踪另加「快照明细」。

    rows 为空不出文件（调用方保证）；stats 为 pipeline 的运行统计。
    """
    if progress:
        progress(text="正在生成 Excel…")
    rows = rows or []
    latest = rows[-1] if rows else {}
    mode = stats.get("mode")
    track = mode == "track"

    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)

    ws = wb.create_sheet("概览")
    sw.ws = ws
    label = f"直播间追踪 · 房间 {latest.get('room_id', '')}"
    sw.title_row(ws, label, 4)
    if stats.get("stopped_reason") == "budget_reached":
        note = "已达上限安全停止（请求数/时长预算到限，已抓数据完整保留）"
    elif stats.get("cancelled"):
        note = "任务被中途取消（已抓轮次完整保留）"
    elif track:
        note = "已跑满设定轮数"
    else:
        note = "单次快照完成"
    area = "·".join(part for part in (
        _s(latest.get("parent_area_name")), _s(latest.get("area_name")))
        if part != DASH)
    if track:
        mode_note = (f"轮询追踪 {stats.get('rounds_requested', 0)} 轮 × "
                     f"{stats.get('interval', 0):g} 秒间隔")
    else:
        mode_note = "单次快照"
    sw.kv(ws, [
        ("房间号（真实）", _s(latest.get("room_id")), "接口返回的真实房间号（短号/链接已归一）"),
        ("主播 UID", latest.get("uid") or DASH, ""),
        ("最新标题", _s(latest.get("title")), ""),
        ("当前状态", _s(latest.get("live_status_label")), "0 未开播 / 1 直播中 / 2 轮播"),
        ("人气（最新）", latest.get("online", 0), "平台人气值口径，非精确观看人数"),
        ("分区", area or DASH, ""),
        ("开播时刻", _s(latest.get("live_time")), "接口未下发时显示占位符"),
        ("追踪模式", mode_note, f"推送 {stats.get('pushes', 0)} 次"),
        ("完成情况", note, f"取消={stats.get('cancelled', False)}"),
        ("导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         f"共 {len(rows)} 轮快照"),
    ])
    ws.append([None, sw.wc(
        "口径：「人气」为 B 站接口返回的人气值，不是精确观看人数。"
        "轮询每轮仅 1 次业务请求（get_info），间隔下限 30 秒。",
        font=xlsx_mod.F_CAPTION)])

    if track:
        ws2 = wb.create_sheet("快照明细")
        sw.ws = ws2
        sw.title_row(ws2, label, len(_DETAIL_HEADERS))
        sw.header_row(ws2, list(_DETAIL_HEADERS))
        for r in rows:
            ws2.append([None,
                        sw.wc(r.get("round")),
                        sw.wc(r.get("ts")),
                        sw.wc(_s(r.get("live_status_label"))),
                        sw.wc(_s(r.get("title"))),
                        sw.wc(r.get("online", 0)),
                        sw.wc(_s(r.get("area_name"))),
                        sw.wc(_s(r.get("parent_area_name"))),
                        sw.wc(_s(r.get("live_time"))),
                        sw.wc(r.get("room_id")),
                        sw.wc(r.get("uid") if r.get("uid") else DASH),
                        sw.wc(_s(r.get("tags")))])
        for idx, width in enumerate(_DETAIL_WIDTHS, start=2):
            ws2.column_dimensions[get_column_letter(idx)].width = width
    wb.save(path)
    return path
