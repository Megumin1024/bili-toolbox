# -*- coding: utf-8 -*-
"""采集流水线：来源展开 → 轮次采集 → 分析 → Excel。

框架无关（progress(**kw)/cancel() 注入），GUI 与 CLI 共用；HTTP 请求走
core.session 风控栈，支持快照断点续传与 -352 人工恢复后续采。
"""
import json
import os
import time
from pathlib import Path

from core import links, risk, session
from core.risk import RiskChallengeError

from . import core
from .comparison import SessionComparison


def _file_size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _read_jsonl_since(path, offset):
    """读取指定字节位置之后新增的完整 JSONL 行。"""
    if not path.exists() or _file_size(path) < offset:
        return []
    records = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def run_pipeline(sources, out_dir, sleep=0.3, monitor=False, interval_min=60,
                 rounds=1, cancel=None, progress=None, open_result=False):
    """流水线。返回结果 dict。"""

    def p(**kw):
        if progress:
            progress(**kw)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snap_path = out / "snapshots.jsonl"
    task_start_offset = _file_size(snap_path)

    p(text="预热风控凭证（buvid 激活 + bili_ticket，24h 缓存）…")
    session.ensure_ready()

    p(text="展开采集来源…")
    bvids, notes = [], {}
    for line in sources:
        for bv, note in links.expand_source(line, progress=p):
            if bv not in notes:
                bvids.append(bv)
                notes[bv] = note
    if not bvids:
        raise ValueError("没有解析到任何视频，请检查输入")
    p(text=f"来源展开完成: 共 {len(bvids)} 个视频（去重后）")

    comparison = SessionComparison(bvids)
    dashboard = comparison.initial_payload()
    p(dashboard_roster=list(bvids), dashboard=dashboard)

    total_rounds = rounds if monitor else 1
    rnd = 0
    snaps = []
    last_round_failed = set()
    while True:
        rnd += 1
        if monitor:
            p(text=f"—— 第 {rnd}/{rounds if rounds else '∞'} 轮采集 ——")
            if rnd > 1:
                p(dashboard_status="tracking")
        # 该偏移属于当前轮次。风控恢复时只改变 start_idx，不能重置它。
        round_start_offset = task_start_offset if rnd == 1 else _file_size(snap_path)
        round_attempted = set()
        round_success = set()
        round_failed = set()
        start_idx = 0
        while True:
            progress_ok = 0
            progress_fail = 0
            call_start_idx = start_idx

            def round_progress(**values):
                nonlocal progress_ok, progress_fail
                done = values.get("done")
                if isinstance(done, int) and done > 0:
                    index = call_start_idx + done - 1
                    if 0 <= index < len(bvids):
                        bvid = bvids[index]
                        round_attempted.add(bvid)
                        current_ok = values.get("ok")
                        current_fail = values.get("fail")
                        if isinstance(current_ok, int) and current_ok > progress_ok:
                            round_success.add(bvid)
                            round_failed.discard(bvid)
                        elif isinstance(current_fail, int) and current_fail > progress_fail:
                            round_success.discard(bvid)
                            round_failed.add(bvid)
                        if isinstance(current_ok, int):
                            progress_ok = current_ok
                        if isinstance(current_fail, int):
                            progress_fail = current_fail
                p(**values)

            try:
                call_ok, call_fail = core.collect_snapshot(
                    bvids[start_idx:], sleep=sleep, progress=round_progress, cancel=cancel,
                    snapshot_path=snap_path)
                for record in call_ok:
                    bvid = str(record.get("bvid") or "") if isinstance(record, dict) else ""
                    if bvid:
                        round_success.add(bvid)
                        round_failed.discard(bvid)
                        round_attempted.add(bvid)
                for item in call_fail:
                    if item:
                        bvid = str(item[0])
                        round_failed.add(bvid)
                        round_success.discard(bvid)
                        round_attempted.add(bvid)
                break
            except RiskChallengeError as e:
                resume = getattr(e, "resume_index", 0)
                p(level="warn", text="触发B站风控挑战(-352)，进入人工恢复流程")
                grisk = risk.risk_recovery_flow(e.v_voucher, progress=p)
                if grisk:
                    start_idx += resume
                    p(text=f"恢复成功，从第 {start_idx + 1} 个视频继续")
                    continue
                raise RuntimeError("风控挑战未完成人工验证，采集中止"
                                   "（已完成部分保留在 snapshots.jsonl，重跑可断点续传）")
        round_records = _read_jsonl_since(snap_path, round_start_offset)
        for record in round_records:
            bvid = str(record.get("bvid") or "") if isinstance(record, dict) else ""
            if bvid:
                round_attempted.add(bvid)
                round_success.add(bvid)
                round_failed.discard(bvid)
        round_complete = all(bvid in round_attempted for bvid in bvids)
        last_round_failed = set(round_failed)
        if round_complete:
            p(text=f"第 {rnd} 轮完成: 成功 {len(round_success)}，失败 {len(round_failed)}"
                  + (f"（失败 {len(round_failed)} 个）" if round_failed else ""))
            final_round = not monitor or (rounds and rnd >= rounds)
            if round_failed:
                dashboard_state = "partial_failure"
            elif final_round:
                dashboard_state = "completed"
            elif rnd == 1:
                dashboard_state = "only_one_round"
            else:
                dashboard_state = "tracking"
            dashboard = comparison.publish_round(
                round_records,
                failed=round_failed,
                round_number=rnd,
                state=dashboard_state,
            )
            p(dashboard=dashboard)
        else:
            p(text="本轮未完成，已取消；已写入快照保留，但看板不更新", level="warn")

        # 重建每视频快照序列（跨轮次，按时间排序）
        by_bvid = {}
        with open(snap_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    s = json.loads(line)
                    by_bvid.setdefault(s["bvid"], []).append(s)
                except ValueError:
                    continue
        for v in by_bvid.values():
            v.sort(key=lambda s: s["fetched_at"])
        snaps = [v[-1] for v in by_bvid.values() if v]
        growth = core.analyze_growth(by_bvid)

        meta = {"title": f"{len(bvids)} 个视频", "source": "; ".join(sources)[:120],
                "ok": len(round_success), "attempted": len(bvids),
                "monitor": "是" if monitor else "否",
                "rounds": rnd, "interval_min": interval_min}
        xlsx_path = out / f"视频数据报告_{rnd}轮.xlsx"
        if monitor and rounds and rnd < rounds:
            xlsx_path = out / f"视频数据报告_第{rnd}轮.xlsx"
        p(text="正在生成 Excel…")
        core.export_xlsx(snaps, growth, meta, xlsx_path, progress=p)
        p(text="Excel 已更新")

        if not monitor or (rounds and rnd >= rounds):
            break
        if cancel and cancel():
            p(text="已取消")
            break
        # 可中断的等待
        deadline = time.time() + interval_min * 60
        while time.time() < deadline:
            if cancel and cancel():
                p(text="已取消")
                break
            time.sleep(5)
        if cancel and cancel():
            break

    if open_result and os.name == "nt":
        try:
            os.startfile(str(xlsx_path))  # noqa: S606
        except OSError:
            pass
    return {"xlsx": str(xlsx_path), "dir": str(out), "videos": len(bvids),
            "snapshots": len(snaps), "rounds": rnd, "fail": len(last_round_failed),
            "dashboard": dashboard}
