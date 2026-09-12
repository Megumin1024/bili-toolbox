# -*- coding: utf-8 -*-
"""直播追踪流水线：输入归一 → 单次快照/轮询追踪 → JSONL/Excel + 翻转推送。

框架无关（progress(**kw) / cancel() 注入），GUI 与脚本共用。默认网络通道走
core.session 的四层风控栈（自动过全局闸门，任务预算在 HTTP 层强制）；
fetch / sleeper / budget / notify 均可注入以便离线测试。

轮询礼仪（任务卡硬约束，越界一律强制收敛）：
- 间隔下限 30s、上限 3600s；轮数上限 500；
- 每轮仅 1 次业务请求（Room/get_info）；getRoomPlayInfo 仅在首轮 get_info
  报业务错误（短号/别名 get_info 不认）时作输入归一兜底，最多用一次；
- online 是「人气值」口径，用户可见文案一律写「人气」。
"""
import os
import time
from pathlib import Path

from core.budget import BudgetExhaustedError, TaskBudget
from core.cancel import TaskCancelledError, wait as cancel_wait
from core.transport import BiliApiError

from . import core


def run_pipeline(target, out_dir, mode=core.DEFAULT_MODE,
                 rounds=core.DEFAULT_ROUNDS, interval=core.DEFAULT_INTERVAL,
                 cancel=None, progress=None, open_result=False,
                 max_requests=None, max_minutes=None, budget=None,
                 notify=None, fetch=None, sleeper=None):
    """完整流水线。返回结果 dict（xlsx/jsonl/rows/stats）。

    mode：snapshot=单次快照（1 次请求，Excel 概览一张表）；
          track=轮询追踪（rounds 轮 × interval 秒，每轮追加一条 JSONL，
          全部结束后出 Excel「概览 + 快照明细」两张表）。

    notify 为可注入的推送回调 notify(event_type, title, text) -> bool；
    None 表示关闭推送（状态照样记进快照，只是不外发）。仅当相邻两轮
    live_status 发生翻转时推送恰一条；状态未翻转零推送。

    max_requests / max_minutes 为任务预算（core.budget）；到限按正常完成
    收尾（stopped_reason="budget_reached"），已抓轮次完整落盘。取消优先于
    预算：cancel 与预算同时到期按取消语义。
    """

    def p(**kw):
        if progress:
            progress(**kw)

    room_input = core.parse_room_input(target)
    p(text=f"目标直播间：{room_input}")

    if mode not in ("snapshot", "track"):
        mode = core.DEFAULT_MODE
    interval = max(core.MIN_INTERVAL,
                   min(core.MAX_INTERVAL, float(interval or 0)))
    rounds = (1 if mode == "snapshot"
              else max(1, min(core.MAX_ROUNDS, int(rounds or 1))))

    out_path = Path(out_dir) / f"直播_{room_input}"
    out_path.mkdir(parents=True, exist_ok=True)

    if budget is None and (max_requests is not None or max_minutes is not None):
        budget = TaskBudget(
            max_requests=max_requests,
            max_seconds=(max_minutes * 60) if max_minutes is not None else None)

    if fetch is not None:
        fetch_fn = fetch
    else:
        def fetch_fn(url):
            # cancel 必须透传：HTTP 层退避受 TOTAL_WAIT_BUDGET 约束，拿不到
            # 取消谓词时用户按取消最坏要等满整个退避窗口。budget 同理透传
            # （业务请求数的强制点在 BiliClient._request，本层不重复记账）。
            from core import session          # 延迟导入：本模块要能离线单独导入
            kwargs = {"cancel": lambda: bool(cancel and cancel())}
            if budget is not None:
                kwargs["budget"] = budget
            return session.http_get_json(url, **kwargs)

    sleeper = sleeper or time.sleep
    jsonl_path = out_path / core.jsonl_filename(room_input)
    rows = []
    stats = {"room_input": room_input, "room_id": room_input, "uid": 0,
             "mode": mode, "rounds_requested": rounds, "rounds_done": 0,
             "requests": 0, "resolve_requests": 0, "interval": interval,
             "pushes": 0, "stopped_reason": None, "cancelled": False}

    def _request(url):
        stats["requests"] += 1
        return fetch_fn(url)

    def _resolve_once(room_id):
        """输入归一兜底：getRoomPlayInfo 最多一次；失败时让原始错误冒泡。"""
        try:
            stats["resolve_requests"] += 1
            return core.resolve_room_id(_request, room_id)
        except Exception:  # noqa: BLE001 - 归一失败不掩盖首轮 get_info 的原错
            return 0

    def _snapshot(room_id, rnd):
        """一次业务请求取快照；get_info 报业务错误时先归一短号再重试一次。"""
        try:
            payload = _request(core.get_info_url(room_id))
        except BiliApiError:
            resolved = _resolve_once(room_id)
            if not resolved or resolved == room_id:
                raise
            p(level="warn",
              text=f"房间号 {room_id} 已归一为真实房间号 {resolved}")
            payload = _request(core.get_info_url(resolved))
        row = core.parse_get_info(payload, fallback_room=room_id)
        row["round"] = rnd
        return row

    try:
        prev_status = None
        current_room = room_input
        for rnd in range(1, rounds + 1):
            if cancel is not None and cancel():
                raise TaskCancelledError()
            # 预算边界：到限按正常完成收尾（不是失败、也不是取消）。
            if budget is not None and budget.expired():
                stats["stopped_reason"] = "budget_reached"
                p(level="warn",
                  text=f"已达预算上限（{budget.reason()}），安全停止，"
                       f"保留 {len(rows)} 轮快照")
                break
            row = _snapshot(current_room, rnd)
            current_room = row["room_id"]        # 短号在响应里归一，后续轮用真实号
            stats["room_id"] = row["room_id"]
            stats["uid"] = row["uid"]
            rows.append(row)
            stats["rounds_done"] = len(rows)
            if mode == "track":
                core.append_jsonl(jsonl_path, row)
                if budget is not None:
                    budget.observe_records(1)
                flipped, kind = core.status_transition(
                    prev_status, row["live_status"])
                if flipped:
                    _event_type, _title, _text = core.push_message(
                        row, kind,
                        core.live_status_label(prev_status))
                    if notify is not None and notify(_event_type, _title, _text):
                        stats["pushes"] += 1
                    p(text=f"检测到状态翻转：{row['live_status_label']}，"
                           f"推送已提交")
                prev_status = row["live_status"]
            p(done=rnd, total=rounds,
              text=f"第 {rnd}/{rounds} 轮快照完成"
                   f"（状态：{row['live_status_label']}，人气 {row['online']}）")
            if rnd < rounds and mode == "track":
                # 轮间等待可取消；取消时 cancel_wait 返回 False。
                if not cancel_wait(interval, lambda: bool(cancel and cancel()),
                                   sleep=sleeper):
                    raise TaskCancelledError()
    except TaskCancelledError:
        stats["cancelled"] = True
        p(level="warn", text=f"已取消，保留已抓到的 {len(rows)} 轮快照")
    except BudgetExhaustedError:
        # 兜底：轮内请求被 HTTP 层硬拦时到限同样按正常完成收尾，不冒充失败。
        stats["stopped_reason"] = "budget_reached"
        p(level="warn", text=f"已达预算上限，安全停止，保留 {len(rows)} 轮快照")

    xlsx_path = ""
    if rows:
        xlsx_path = (out_path / (f"直播追踪_{stats['room_id']}.xlsx" if mode == "track"
                                 else f"直播快照_{stats['room_id']}.xlsx"))
        core.export_xlsx(rows, stats, xlsx_path, progress=p)
        p(text=f"完成! Excel: {xlsx_path}")
    if open_result and xlsx_path and os.name == "nt":
        try:
            os.startfile(str(xlsx_path))  # noqa: S606
        except OSError:
            pass
    return {"room_id": stats["room_id"], "uid": stats["uid"], "mode": mode,
            "rows": len(rows), "xlsx": str(xlsx_path),
            "jsonl": str(jsonl_path) if mode == "track" else "",
            "dir": str(out_path), "stats": stats}
