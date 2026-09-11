# -*- coding: utf-8 -*-
"""用户动态流水线：解析 UID → 抓取动态 → Excel。

框架无关（progress(**kw) / cancel() 注入），GUI 与 CLI 共用。网络请求走
core.session 的四层风控栈，因而自动经过全局闸门（限速 + 熔断）。
"""
import os
from pathlib import Path

from . import core


def run_pipeline(target, out_dir, max_pages=core.DEFAULT_MAX_PAGES,
                 sleep=core.DEFAULT_SLEEP, cancel=None, progress=None,
                 open_result=False):
    """完整流水线。返回结果 dict。

    target 可以是纯 UID，也可以是 space.bilibili.com/<uid> 链接。
    取不到动态时抛 core.DynamicsUnavailable（由 TaskRunner 走失败通道）。
    """

    def p(**kw):
        if progress:
            progress(**kw)

    uid = core.parse_uid(target)
    p(text=f"目标用户 UID：{uid}")

    out_path = Path(out_dir) / f"动态_{uid}"
    out_path.mkdir(parents=True, exist_ok=True)

    crawler = core.DynamicsCrawler(
        uid, out_path, max_pages=max_pages, sleep=sleep, progress=p,
        cancel=lambda: bool(cancel and cancel()))
    stats = crawler.crawl()
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
