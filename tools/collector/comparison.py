# -*- coding: utf-8 -*-
"""当前采集任务内的多视频对比聚合。

本模块只处理调用方已经取得的快照，不发起网络请求，也不读取历史文件。
流水线在一个完整轮次结束后提交“本轮新增记录”，因此这里的首条快照
天然属于当前任务会话，不会把旧 snapshots.jsonl 当成本次基线。
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from typing import Any, Iterable


MAX_DECIMAL_DIGITS = 20
MAX_DECIMAL_VALUE = 10 ** MAX_DECIMAL_DIGITS
MAX_NUMERIC_TEXT_LENGTH = 128


METRICS = {
    "view": "播放总量",
    "delta_view": "播放增量",
    "view_per_hour": "播放/小时",
    "engagement_rate": "点赞互动率",
}
NUMERIC_FIELDS = frozenset({"view", "delta_view", "view_per_hour", "like",
                            "delta_like", "engagement_rate", "fetched_at"})

STATE_LABELS = {
    "waiting_first": "等待首轮",
    "only_one_round": "仅有一轮",
    "tracking": "追踪中",
    "cancelled": "已取消",
    "partial_failure": "部分失败",
    "completed": "已完成",
}


def _decimal_within_limit(value: Decimal) -> bool:
    """限制数值的有效数字和整数数量级，避免生成巨型 int/float。"""
    if not value.is_finite():
        return False
    if value.is_zero():
        return True
    try:
        digits = value.as_tuple().digits
        # adjusted() 是最高位的十进制位置；>= 20 意味着整数部分至少 21 位。
        return len(digits) <= MAX_DECIMAL_DIGITS and value.copy_abs().adjusted() < MAX_DECIMAL_DIGITS
    except (ValueError, OverflowError, InvalidOperation):
        return False


def _decimal_to_number(value: Decimal) -> int | float | None:
    if not _decimal_within_limit(value):
        return None
    if value == value.to_integral_value():
        try:
            result = int(value)
        except (ValueError, OverflowError):
            return None
        return result
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    # 非零 Decimal 下溢成 0.0 时也视为异常输入，避免伪造时间/计数。
    if not math.isfinite(result) or (result == 0.0 and not value.is_zero()):
        return None
    return result


def _numeric_text_within_cheap_limit(text: str) -> bool:
    """在 Decimal 解析前挡住超长文本和明显超界的有效数字。"""
    if len(text) > MAX_NUMERIC_TEXT_LENGTH:
        return False
    mantissa, _separator, exponent = text.lower().partition("e")

    def significant_digit_count(part: str) -> int:
        digits = "".join(char for char in part if "0" <= char <= "9")
        return len(digits.lstrip("0"))

    return (significant_digit_count(mantissa) <= MAX_DECIMAL_DIGITS
            and significant_digit_count(exponent) <= MAX_DECIMAL_DIGITS)


def numeric_value(value: Any) -> int | float | None:
    """安全转换计数/Unix 秒；最多接受 20 位十进制有效数字。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if abs(value) < MAX_DECIMAL_VALUE else None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        try:
            return _decimal_to_number(Decimal(str(value)))
        except (InvalidOperation, ValueError, OverflowError):
            return None
    if isinstance(value, Decimal):
        return _decimal_to_number(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "")
    if not text:
        return None
    if not _numeric_text_within_cheap_limit(text):
        return None
    try:
        return _decimal_to_number(Decimal(text))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _safe_rate(delta: int | float | None, first_time: Any, last_time: Any) -> float | None:
    if delta is None:
        return None
    first = numeric_value(first_time)
    last = numeric_value(last_time)
    if first is None or last is None:
        return None
    try:
        hours = (Decimal(str(last)) - Decimal(str(first))) / Decimal("3600")
        if hours <= 0:
            return None
        result = float(Decimal(str(delta)) / hours)
    except (InvalidOperation, OverflowError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def _difference(first: Any, last: Any) -> int | float | None:
    first_value = numeric_value(first)
    last_value = numeric_value(last)
    if first_value is None or last_value is None:
        return None
    return numeric_value(last_value - first_value)


def _latest_text(records: list[dict[str, Any]], field: str) -> str:
    for record in reversed(records):
        value = record.get(field)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _sortable(value: Any) -> Decimal | str:
    number = numeric_value(value)
    if number is not None:
        try:
            return Decimal(str(number))
        except InvalidOperation:
            pass
    return str(value or "").casefold()


def sort_rows(rows: Iterable[dict[str, Any]], field: str, descending: bool = True) -> list[dict[str, Any]]:
    """按字段排序，缺失值始终在后；同值按 BV 号稳定排序。"""
    values = list(rows)

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        left_value, right_value = left.get(field), right.get(field)
        left_missing = left_value is None or (isinstance(left_value, str) and not left_value.strip())
        right_missing = right_value is None or (isinstance(right_value, str) and not right_value.strip())
        left_number = numeric_value(left_value)
        right_number = numeric_value(right_value)
        if field in NUMERIC_FIELDS:
            left_missing = left_missing or (left_value is not None and left_number is None)
            right_missing = right_missing or (right_value is not None and right_number is None)
        if left_missing != right_missing:
            return 1 if left_missing else -1
        if not left_missing:
            if left_number is not None and right_number is not None:
                if left_number != right_number:
                    result = -1 if left_number > right_number else 1
                    return result if descending else -result
            else:
                left_text, right_text = str(left_value).casefold(), str(right_value).casefold()
                if left_text != right_text:
                    result = -1 if left_text < right_text else 1
                    return result if not descending else -result
        left_bvid = str(left.get("bvid") or "").casefold()
        right_bvid = str(right.get("bvid") or "").casefold()
        return -1 if left_bvid < right_bvid else (1 if left_bvid > right_bvid else 0)

    return sorted(values, key=cmp_to_key(compare))


class SessionComparison:
    """只保存当前任务会话中已经提交的完整轮次。"""

    def __init__(self, bvids: Iterable[str] = ()):
        self.reset(bvids)

    def reset(self, bvids: Iterable[str] = ()) -> None:
        self.bvids = tuple(dict.fromkeys(str(value) for value in bvids if str(value)))
        self._records: dict[str, list[dict[str, Any]]] = {bvid: [] for bvid in self.bvids}
        self._round_status: dict[str, str] = {bvid: "等待首轮" for bvid in self.bvids}
        self.rounds = 0
        self.state = "waiting_first"

    def initial_payload(self, state: str = "waiting_first") -> dict[str, Any]:
        self.state = state
        rows = [self._row(bvid) for bvid in self.bvids]
        return self._payload(rows)

    def publish_round(self, records: Iterable[dict[str, Any]], failed: Iterable[str] = (),
                      round_number: int = 1, state: str = "only_one_round") -> dict[str, Any]:
        """提交一个已经确认完成尝试的轮次；调用方不得传入半轮数据。"""
        by_bvid: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            bvid = str(record.get("bvid") or "")
            if bvid in self._records:
                by_bvid[bvid] = dict(record)
        failed_set = {str(value) for value in failed}
        for bvid in self.bvids:
            if bvid in by_bvid:
                self._records[bvid].append(by_bvid[bvid])
                self._round_status[bvid] = "本轮成功"
            elif bvid in failed_set or bvid not in by_bvid:
                self._round_status[bvid] = "本轮失败"
        self.rounds = max(self.rounds, int(round_number))
        self.state = state
        return self._payload([self._row(bvid) for bvid in self.bvids])

    def _row(self, bvid: str) -> dict[str, Any]:
        records = self._records.get(bvid, [])
        latest = records[-1] if records else {}
        first = records[0] if records else {}
        view = numeric_value(latest.get("view"))
        like = numeric_value(latest.get("like"))
        delta_view = _difference(first.get("view"), latest.get("view")) if len(records) >= 2 else None
        delta_like = _difference(first.get("like"), latest.get("like")) if len(records) >= 2 else None
        rate = _safe_rate(delta_view, first.get("fetched_at"), latest.get("fetched_at")) \
            if len(records) >= 2 else None
        engagement = None
        if view is not None and view != 0 and like is not None:
            try:
                engagement = float(Decimal(str(like)) / Decimal(str(view)) * Decimal("100"))
            except (InvalidOperation, OverflowError, ValueError, ZeroDivisionError):
                engagement = None
            if engagement is not None and not math.isfinite(engagement):
                engagement = None
        fetched_at = numeric_value(latest.get("fetched_at"))
        status = self._round_status.get(bvid, "等待首轮")
        if not records and self.rounds:
            status = "暂无成功数据" if status == "等待首轮" else status
        return {
            "bvid": bvid,
            "title": _latest_text(records, "title"),
            "owner": _latest_text(records, "owner"),
            "status": status,
            "view": view,
            "delta_view": delta_view,
            "view_per_hour": rate,
            "like": like,
            "delta_like": delta_like,
            "engagement_rate": engagement if engagement is not None else None,
            "fetched_at": fetched_at,
            "sample_count": len(records),
        }

    def _payload(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        views = [row["view"] for row in rows if row.get("view") is not None]
        delta_views = [row["delta_view"] for row in rows if row.get("delta_view") is not None]
        rates = [row["view_per_hour"] for row in rows if row.get("view_per_hour") is not None]
        total_view = sum(views) if views else None
        total_delta = sum(delta_views) if delta_views else None
        highest = (sorted(
            (row for row in rows if row.get("view_per_hour") is not None),
            key=lambda row: (-float(row["view_per_hour"]), str(row.get("bvid") or "").casefold()),
        )[0] if rates else None)
        return {
            "state": self.state,
            "state_text": STATE_LABELS.get(self.state, self.state),
            "rounds": self.rounds,
            "video_count": len(self.bvids),
            "rows": rows,
            "summary": {
                "video_count": len(self.bvids),
                "total_view": total_view,
                "total_delta_view": total_delta,
                "highest_rate_bvid": highest.get("bvid") if highest else None,
                "highest_rate": highest.get("view_per_hour") if highest else None,
            },
        }
