# -*- coding: utf-8 -*-
"""B站直播公开数据的输入归一与响应解析（纯逻辑，可离线测试）。

从 tools/live_room/core.py 上提的共享正本：live_room 工具与实时监控的
「直播间模式」共用同一套归一口径，tools/live_room/core.py 对下列名字做
re-export，行为与原实现逐字节一致。

接口口径（游客直连，2026-09-12 探针实证，只读 api.live.bilibili.com 公开数据）：
- GET room/v1/Room/get_info?room_id=：live_status / title / online / 分区 /
  开播时刻 / uid / tags。响应里的 room_id 是**真实房间号**——传短号进来
  时在此归一，零额外请求。

口径：online 是平台「人气值」，不是精确观看人数——所有用户可见表头与
文案一律写「人气」。本模块不依赖 openpyxl、不发网络请求，core/ 内保持
零上层依赖。
"""
from __future__ import annotations

import re
from datetime import datetime

GET_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"

LIVE_STATUS_LABELS = {0: "未开播", 1: "直播中", 2: "轮播"}

_LIVE_LINK_RE = re.compile(r"live\.bilibili\.com/(\d{1,20})", re.I)


def parse_room_input(text):
    """直播间号（含短号，纯数字）或 live.bilibili.com 链接 → 房间号。"""
    s = (text or "").strip().strip("\"'“”")
    if not s:
        raise ValueError("请输入直播间号或直播间链接")
    m = _LIVE_LINK_RE.search(s)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d{1,20}", s):
        return int(s)
    raise ValueError(f"无法识别的直播间号：{s[:60]}")


def to_int(value, default=0):
    """B 站的计数/时间戳常以字符串下发，直接当整数用会炸。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def get_info_url(room_id):
    return f"{GET_INFO_URL}?room_id={int(room_id)}"


def live_status_label(status):
    return LIVE_STATUS_LABELS.get(status) or f"未知({status})"


def _fmt_live_time(raw):
    """开播时刻：接口给 0/空串表示未开播；纯数字按时间戳格式化。"""
    text = str(raw or "").strip()
    if not text or text == "0":
        return ""
    if re.fullmatch(r"\d{1,12}", text):
        try:
            return datetime.fromtimestamp(int(text)).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return text
    return text


def parse_get_info(payload, fallback_room=0):
    """get_info 响应 → 快照行。字段缺失一律降级为空/0，绝不抛异常。

    data.room_id 是**真实房间号**：输入为短号/别名时在此归一（fallback_room
    仅在响应缺失该字段时兜底）。
    """
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        data = {}
    status = to_int(data.get("live_status"), -1)
    live_time_raw = str(data.get("live_time") or "").strip()
    return {
        "room_id": to_int(data.get("room_id"), fallback_room),
        "uid": to_int(data.get("uid"), 0),
        "live_status": status,
        "live_status_label": live_status_label(status),
        "title": str(data.get("title") or "").strip(),
        "online": to_int(data.get("online"), 0),
        "parent_area_name": str(data.get("parent_area_name") or "").strip(),
        "area_name": str(data.get("area_name") or "").strip(),
        "live_time": _fmt_live_time(live_time_raw),
        "live_time_raw": live_time_raw,
        "tags": str(data.get("tags") or "").strip(),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def status_transition(prev, cur):
    """状态翻转判定：prev 为 None（首轮只建基线）或两者相等 → 未翻转。

    返回 (flipped, kind)；kind：live=开播 / offline=下播 / change=其它变化。
    """
    if prev is None or cur is None or cur == prev:
        return False, ""
    kind = {(0, 1): "live", (1, 0): "offline"}.get((prev, cur), "change")
    return True, kind
