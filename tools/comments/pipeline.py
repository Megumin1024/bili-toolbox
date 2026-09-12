# -*- coding: utf-8 -*-
"""评论抓取流水线：解析链接 → 元信息 → 全量抓取 → 分析 → Excel。

框架无关（progress(**kw)/cancel() 注入），GUI 与 CLI 共用；HTTP 元信息请求
走 core.session 风控栈，gRPC metadata 注入预热 cookie。

max_requests / max_minutes 为任务预算（core.budget.TaskBudget，每次任务新
建），强制点与记账都在 gRPC 通道入口（tools.comments.core.Crawler._call），
与 core.client 的 attempt==0 同一口径；None = 该项无上限，旧调用行为不变。
到限按正常完成收尾：已抓评论照常分析导出，stats 记
stopped_reason="budget_reached"，断点保留，重跑从断点续传。链接解析、动态
元信息与 buvid/bili_ticket 预热合计至多几条请求，且入口 links.*（core/）不
暴露 budget 参数，故不记账——预算管住的是高速率的 gRPC 评论通道。
"""
import json
import os
from pathlib import Path

from core import links, risk, session
from core.budget import TaskBudget
from core.risk import RiskChallengeError
from core.session import http_get_json  # noqa: F401  兼容旧引用

from . import core


def _raise_on_crawl_error(stats):
    """把评论核心的真实运行异常交给 TaskRunner 的失败通道。"""
    if not isinstance(stats, dict):
        return
    if stats.get("status") == "error" or stats.get("error"):
        raise RuntimeError(stats.get("error") or "评论抓取阶段发生运行异常")


def run_pipeline(url, out_dir, sleep=0.2, max_pages=0, use_tls_grpc=False,
                 cancel=None, progress=None, open_result=False,
                 max_requests=None, max_minutes=None):
    """完整流水线。返回结果 dict。"""

    def p(**kw):
        if progress:
            progress(**kw)

    budget = None
    if max_requests is not None or max_minutes is not None:
        # 未提供任何预算参数时保持 budget=None：整条请求链的调用与引入预算前
        # 逐字节一致（旧签名、旧测试不受影响）。
        budget = TaskBudget(
            max_requests=max_requests,
            max_seconds=(max_minutes * 60) if max_minutes is not None else None)

    def parse_with_recovery():
        try:
            return links.parse_link(url, cancel=cancel)
        except RiskChallengeError as e:
            p(level="warn", text=f"触发B站风控挑战(-352)：{e.v_voucher[:28]}…")
            if not risk.risk_recovery_flow(e.v_voucher, progress=p):
                raise
            return links.parse_link(url, cancel=cancel)

    p(text="解析链接…")
    kind, oid, info = parse_with_recovery()
    p(text=f"识别为{'动态' if kind == 'dynamic' else '视频'}: {oid}")

    if kind == "dynamic":
        try:
            meta = links.get_dynamic_meta(oid, cancel=cancel)
        except RiskChallengeError as e:
            p(level="warn", text=f"触发B站风控挑战(-352)：{e.v_voucher[:28]}…")
            if not risk.risk_recovery_flow(e.v_voucher, progress=p):
                raise
            meta = links.get_dynamic_meta(oid, cancel=cancel)
        except Exception as e:  # noqa: BLE001 - 元信息失败不阻塞抓取
            p(level="warn", text=f"动态元信息获取失败({e})，使用基础信息")
            meta = {"title": f"动态{oid}", "author": "", "pub_ts": None,
                    "claimed_comment_count": None}
        meta.setdefault("claimed_comment_count", None)
        rtype = 17
    else:
        meta = {"title": info.get("title", f"av{oid}"),
                "author": info.get("owner", ""),
                "pub_ts": info.get("pubdate"),
                "claimed_comment_count": info.get("claimed_comment_count")}
        rtype = 1

    p(text="预热风控凭证（buvid 激活 + bili_ticket，24h 缓存）…")
    metadata = session.grpc_metadata()

    out_path = Path(out_dir) / (f"dyn_{oid}" if kind == "dynamic" else f"av_{oid}")
    channel_desc = "Chrome-TLS gRPC（实验）" if use_tls_grpc else "gRPC 游客通道"
    p(text=f"开始抓取评论（{channel_desc}，oid={oid}）…")
    crawler = core.Crawler(oid, rtype, out_path, sleep=sleep, max_pages=max_pages,
                           progress=p, cancel=lambda: bool(cancel and cancel()),
                           metadata=metadata, use_tls_grpc=use_tls_grpc,
                           budget=budget)
    stats = crawler.crawl()
    _raise_on_crawl_error(stats)
    rows = []
    seen = set()
    with open(crawler.out_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                if r.get("rpid") not in seen:
                    seen.add(r["rpid"])
                    rows.append(r)
            except ValueError:
                continue
    p(text=f"抓取完成: 共 {len(rows):,} 条（抓取请求 {stats['pages']} 页"
           f"{'，已取消/限页' if stats.get('aborted') else ''}"
           f"{'，已达上限安全停止' if stats.get('stopped_reason') == 'budget_reached' else ''}）"
           f"，正在分析…")

    report, kpi = core.analyze(rows, meta)
    report_path = Path(out_path) / "分析报告.md"
    report_path.write_text(report, encoding="utf-8")

    p(text="正在生成 Excel…")
    xlsx_path = Path(out_path) / f"评论分析_{oid}.xlsx"
    core.export_xlsx(rows, meta, kpi, xlsx_path, progress=lambda **kw: p(**kw))
    p(text=f"完成! Excel: {xlsx_path}")
    if open_result and os.name == "nt":
        try:
            os.startfile(str(xlsx_path))  # noqa: S606
        except OSError:
            pass
    return {"xlsx": str(xlsx_path), "report": str(report_path),
            "jsonl": str(crawler.out_path), "dir": str(out_path),
            "rows": len(rows), "stats": stats}
