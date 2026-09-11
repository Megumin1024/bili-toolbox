# -*- coding: utf-8 -*-
"""弹幕流水线：解析目标 → 查视频元信息 → 逐段抓弹幕 → Excel。

框架无关（progress(**kw) / cancel() 注入），GUI 与 CLI 共用。网络请求走
core.session：JSON 的走 JSON 通道，弹幕字节走二进制通道，两者共用同一套
四层风控栈与全局闸门。

多分P（all_pages=True）时逐P串行抓取，每个分P一个爬取器、一份 jsonl，最后
合成一张 Excel。串行而不是并发：这套代码的全部价值在于不对平台施压，为了
省几分钟去并发打接口，等于把限速设计作废。
"""
import os
from pathlib import Path

from core.cancel import TaskCancelledError, wait as cancel_wait

from . import analysis, core


def run_pipeline(target, out_dir, max_segments=core.DEFAULT_MAX_SEGMENTS,
                 sleep=core.DEFAULT_SLEEP, cancel=None, progress=None,
                 open_result=False, all_pages=False):
    """完整流水线。返回结果 dict。

    target 可以是视频链接、BV 号或 av 号（链接可带 ?p=N 指定分P）。
    all_pages=True 时忽略 ?p=N，抓全部分P。
    全部抓完一条都没有且视频自称有弹幕时抛 core.DanmakuUnavailable，由
    TaskRunner 走失败通道——不交一份"该视频没有弹幕"的假报告。
    """

    def p(**kw):
        if progress:
            progress(**kw)

    bvid, aid, page = core.parse_target(target)
    try:
        meta = core.fetch_video_meta(bvid=bvid, aid=aid, page=page,
                                     cancel=cancel)
    except TaskCancelledError:
        return _cancelled_before_meta(bvid, aid, out_dir)
    label = meta["bvid"] or f"av{meta['aid']}"
    p(text=f"视频：{meta['title'][:40]}")
    p(text=f"{label} · P{meta['page']}/{meta['page_count']} · "
           f"时长 {core.hhmmss(meta['duration'] * 1000)} · cid={meta['cid']}")

    page_count = int(meta.get("page_count") or 1)
    if all_pages and page_count > 1:
        targets = list(range(1, page_count + 1))
        p(text=f"已勾选「抓取全部分P」：将串行抓取 {page_count} 个分P，"
               f"每 P 之间间隔 {sleep}s")
    else:
        targets = [meta["page"]]
        if page_count > 1:
            p(level="warn",
              text=f"该视频有 {page_count} 个分P，本次只抓 P{meta['page']}；"
                   f"如需全抓请勾选「抓取全部分P」")

    out_path = Path(out_dir) / f"弹幕_{label}"
    out_path.mkdir(parents=True, exist_ok=True)

    rows, parts = [], []
    cancelled = False
    for i, n in enumerate(targets):
        # 只拦"下一个分P"：第一个分P无论如何都要走一遍爬取器，由它自己把取消
        # 记成 cancelled 并落盘已抓到的内容——用户按取消也该拿到一份报告。
        if i and cancel and cancel():
            cancelled = True
            break
        try:
            pm = meta if n == meta["page"] else core.fetch_video_meta(
                bvid=bvid, aid=aid, page=n, cancel=cancel)
        except TaskCancelledError:
            cancelled = True
            break
        p(text=f"—— P{n}/{page_count}"
               + (f" {pm.get('part')}" if pm.get("part") else "")
               + f" · 时长 {core.hhmmss(pm['duration'] * 1000)} ——")
        crawler = core.DanmakuCrawler(
            pm["cid"], out_path, duration=pm["duration"],
            max_segments=max_segments, sleep=sleep, progress=p,
            cancel=lambda: bool(cancel and cancel()),
            name=f"{label}_P{n}", page=n, part=pm.get("part") or "")
        st = crawler.crawl()
        rows.extend(crawler.rows)
        parts.append({"page": n, "part": pm.get("part") or "",
                      "duration": pm["duration"], "rows": len(crawler.rows),
                      "segments": st.get("segments", 0),
                      "expected_segments": st.get("expected_segments"),
                      "duplicates": st.get("duplicates", 0),
                      "cancelled": st.get("cancelled", False),
                      "truncated": st.get("truncated", False)})
        if st.get("cancelled"):
            cancelled = True
            break
        # 换分P也是一次新的接口请求，跟段间一样要限速：爬取器只在段与段之间
        # 停顿，上一个分P的收尾请求和下一个分P的首次请求之间没有任何间隔。
        if n != targets[-1] and not cancel_wait(sleep, cancel):
            cancelled = True
            break

    # 循环至少跑一次（targets 恒非空），所以这里 parts 一定非空。
    multi = len(parts) > 1
    stats = _merge_stats(parts)
    stats["cancelled"] = stats["cancelled"] or cancelled
    if not rows and not stats["cancelled"] and meta["claimed_danmaku"] > 0:
        raise core.DanmakuUnavailable(
            f"一条弹幕都没抓到，但视频接口自称有 {meta['claimed_danmaku']:,} 条。"
            "通常是被限流降级——降级响应与「该视频真的没有弹幕」完全一样，"
            "无法区分。请稍后重试，或换一个视频验证接口是否正常。")

    name = (f"弹幕_{label}_全部P" if multi
            else f"弹幕_{label}_P{parts[-1]['page']}")
    # 分析是纯本地计算，跟抓取结果无关，放在导出前——Excel 要把它嵌进去。
    # 这一步不产生任何网络请求，也不改变已经落盘的 jsonl。
    ana = analysis.analyze(rows, meta, parts if multi else None)
    xlsx_path = out_path / f"{name}.xlsx"
    core.export_xlsx(rows, meta, stats, xlsx_path, progress=p,
                     parts=parts if multi else None, sleep=sleep, analysis=ana)

    report_path = out_path / f"{name}_分析报告.md"
    report = ""
    try:
        report_path.write_text(analysis.render_markdown(ana, meta),
                               encoding="utf-8")
        report = str(report_path)
        p(text=f"分析报告: {report_path}")
    except OSError as exc:
        # Excel 此时已经落盘，它才是主产物。报告写不出来只降级提示，不让整个
        # 任务在最后一步翻车——但要说清楚，不能假装成功。report 留空串，
        # 页面据此不显示这一项（空路径会被 Path("") 当成当前目录，是个坑）。
        p(level="warn", text=f"分析报告写入失败（{type(exc).__name__}），"
                             f"Excel 不受影响：{exc}")

    p(text=f"完成! Excel: {xlsx_path}")
    if open_result and os.name == "nt":
        try:
            os.startfile(str(xlsx_path))  # noqa: S606
        except OSError:
            pass
    # 单分P给具体文件，多分P给目录：让"打开"按钮落到用户真正想看的东西上，
    # 而不是每次都要先点进一层。
    jsonl_files = [str(out_path / f"danmaku_{label}_P{pt['page']}.jsonl")
                   for pt in parts]
    return {"label": label, "cid": meta["cid"], "xlsx": str(xlsx_path),
            "report": report,
            "jsonl": jsonl_files[0] if len(jsonl_files) == 1 else str(out_path),
            "jsonl_files": jsonl_files, "dir": str(out_path),
            "rows": len(rows), "stats": stats, "meta": meta, "parts": parts,
            "analysis": ana}


def _cancelled_before_meta(bvid, aid, out_dir):
    """元信息请求阶段取消时，返回正常的取消结果而不是抛到 GUI 失败槽。"""
    label = bvid or f"av{aid}"
    stats = _merge_stats([])
    stats["cancelled"] = True
    return {
        "label": label,
        "cid": None,
        "xlsx": "",
        "report": "",
        "jsonl": "",
        "jsonl_files": [],
        "dir": str(Path(out_dir) / f"弹幕_{label}"),
        "rows": 0,
        "stats": stats,
        "meta": {},
        "parts": [],
        "analysis": {},
    }


def _merge_stats(parts):
    """各分P stats 合并成一份任务级统计，供概览页使用。

    截断/取消是"或"不是"和"：只要有一个分P没抓全，整份导出就不能印"已抓到底"。
    段数与预期段数则是求和——多分P的概览说的是合计口径。
    """
    if not parts:
        return {"segments": 0, "duplicates": 0, "cancelled": False,
                "truncated": False, "expected_segments": None}
    expects = [p["expected_segments"] for p in parts]
    return {
        "segments": sum(p["segments"] for p in parts),
        "duplicates": sum(p["duplicates"] for p in parts),
        "cancelled": any(p["cancelled"] for p in parts),
        "truncated": any(p["truncated"] for p in parts),
        "expected_segments": (sum(e for e in expects if e is not None)
                              if all(e is not None for e in expects) else None),
    }
