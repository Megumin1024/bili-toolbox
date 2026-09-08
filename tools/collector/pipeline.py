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


def run_pipeline(sources, out_dir, sleep=0.3, monitor=False, interval_min=60,
                 rounds=1, cancel=None, progress=None, open_result=False):
    """流水线。返回结果 dict。"""

    def p(**kw):
        if progress:
            progress(**kw)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snap_path = out / "snapshots.jsonl"

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

    all_rounds = []
    total_rounds = rounds if monitor else 1
    rnd = 0
    snaps, fail = [], []
    while True:
        rnd += 1
        if monitor:
            p(text=f"—— 第 {rnd}/{rounds if rounds else '∞'} 轮采集 ——")
        start_idx = 0
        while True:
            try:
                ok, fail = core.collect_snapshot(
                    bvids[start_idx:], sleep=sleep, progress=p, cancel=cancel,
                    snapshot_path=snap_path)
                break
            except RiskChallengeError as e:
                resume = getattr(e, "resume_index", 0)
                p(level="warn", text=f"触发B站风控挑战(-352)：{e.v_voucher[:28]}…")
                grisk = risk.risk_recovery_flow(e.v_voucher, progress=p)
                if grisk:
                    start_idx += resume
                    p(text=f"恢复成功，从第 {start_idx + 1} 个视频继续")
                    continue
                raise RuntimeError("风控挑战未完成人工验证，采集中止"
                                   "（已完成部分保留在 snapshots.jsonl，重跑可断点续传）")
        all_rounds.append(ok)
        p(text=f"第 {rnd} 轮完成: 成功 {len(ok)}，失败 {len(fail)}"
              + (f"（失败示例: {fail[0][0]} {fail[0][1]}）" if fail else ""))

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
                "ok": len(ok), "attempted": len(bvids),
                "monitor": "是" if monitor else "否",
                "rounds": rnd, "interval_min": interval_min}
        xlsx_path = out / f"视频数据报告_{rnd}轮.xlsx"
        if monitor and rounds and rnd < rounds:
            xlsx_path = out / f"视频数据报告_第{rnd}轮.xlsx"
        p(text="正在生成 Excel…")
        core.export_xlsx(snaps, growth, meta, xlsx_path, progress=p)
        p(text=f"Excel 已更新: {xlsx_path}")

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
            "snapshots": len(snaps), "rounds": rnd, "fail": len(fail)}
