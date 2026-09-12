# -*- coding: utf-8 -*-
"""用户动态流水线：解析 UID → 抓取动态 → Excel。

框架无关（progress(**kw) / cancel() 注入），GUI 与 CLI 共用。网络请求走
core.session 的四层风控栈，因而自动经过全局闸门（限速 + 熔断）。
"""
import os
from pathlib import Path

from core.budget import BudgetExhaustedError, TaskBudget

from . import core


def run_pipeline(target, out_dir, max_pages=core.DEFAULT_MAX_PAGES,
                 sleep=core.DEFAULT_SLEEP, cancel=None, progress=None,
                 open_result=False, max_requests=None, max_minutes=None):
    """完整流水线。返回结果 dict。

    target 可以是纯 UID，也可以是 space.bilibili.com/<uid> 链接。
    取不到动态时抛 core.DynamicsUnavailable（由 TaskRunner 走失败通道）。

    max_requests / max_minutes 为任务预算（core.budget）；None = 该项无上限，
    旧调用行为不变。到限按正常完成收尾：已抓数据照常导出，stats 记
    stopped_reason="budget_reached"。
    """

    def p(**kw):
        if progress:
            progress(**kw)

    uid = core.parse_uid(target)
    p(text=f"目标用户 UID：{uid}")

    out_path = Path(out_dir) / f"动态_{uid}"
    out_path.mkdir(parents=True, exist_ok=True)

    budget = None
    if max_requests is not None or max_minutes is not None:
        # 未提供任何预算参数时保持 budget=None：整条请求链的调用与引入
        # 预算前逐字节一致（旧签名、旧测试不受影响）。
        budget = TaskBudget(
            max_requests=max_requests,
            max_seconds=(max_minutes * 60) if max_minutes is not None else None)
    crawler = core.DynamicsCrawler(
        uid, out_path, max_pages=max_pages, sleep=sleep, progress=p,
        cancel=lambda: bool(cancel and cancel()), budget=budget)
    try:
        stats = crawler.crawl()
    except BudgetExhaustedError:
        # 双保险：正常路径已在爬虫内收尾落盘；这里兜住未预见的传播路径，
        # 保证到限不冒充失败。
        stats = dict(crawler.stats)
        stats["stopped_reason"] = "budget_reached"
        stats["rows"] = len(crawler.rows)
    rows = crawler.rows

    xlsx_path = out_path / f"用户动态_{uid}.xlsx"
    core.export_xlsx(rows, uid, stats, xlsx_path, progress=p)
    p(text=f"完成! Excel: {xlsx_path}")
    if open_result and os.name == "nt":
        try:
            os.startfile(str(xlsx_path))  # noqa: S606
        except OSError:
            pass
    return {"uid": uid, "xlsx": str(xlsx_path), "jsonl": str(crawler.out_path),
            "dir": str(out_path), "rows": len(rows), "stats": stats}
