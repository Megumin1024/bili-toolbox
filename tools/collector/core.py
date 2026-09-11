# -*- coding: utf-8 -*-
"""采集核心：快照采集 + 分析 + Excel。

HTTP 请求统一走 core.session 风控栈；来源展开（收藏夹/合集/系列/txt）见
core.links.expand_source；Excel 基于 core.xlsx 共享基建。
"""
import json
import random
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from core import session
from core.cancel import TaskCancelledError, wait as wait_or_cancel
from core.risk import RiskChallengeError  # noqa: F401  供 pipeline 引用
from core import xlsx as xlsx_mod

try:
    from openpyxl.utils import get_column_letter
    HAVE_XLSX = True
except ImportError:  # noqa: F841
    HAVE_XLSX = False


# ============================ 采集 ============================

def fetch_view(bvid, cancel=None):
    d = session.http_get_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", cancel=cancel)
    if d.get("code") != 0:
        raise ValueError(f"{bvid}: code={d.get('code')} {d.get('message')}")
    v = d["data"]
    st = v.get("stat") or {}
    return {
        "bvid": v.get("bvid"),
        "aid": v.get("aid"),
        "title": v.get("title", ""),
        "owner": (v.get("owner") or {}).get("name", ""),
        "owner_mid": (v.get("owner") or {}).get("mid"),
        "tname": v.get("tname", ""),
        "pubdate": v.get("pubdate"),
        "duration": v.get("duration"),
        "view": st.get("view"), "danmaku": st.get("danmaku"), "reply": st.get("reply"),
        "favorite": st.get("favorite"), "coin": st.get("coin"),
        "share": st.get("share"), "like": st.get("like"),
        "fetched_at": int(time.time()),
    }


def collect_snapshot(bvids, sleep=0.3, progress=None, cancel=None, snapshot_path=None):
    """逐视频采集一份快照；返回 (成功列表, 失败列表[(bvid, err)])。

    cancel 会一路透传到请求内部：不仅在本循环间生效，也能打断 BiliClient 的
    退避等待。取消属主动行为，不计入失败列表——干净收尾并返回已采到的部分。
    """
    ok, fail = [], []
    total = len(bvids)
    for i, bv in enumerate(bvids, 1):
        if cancel and cancel():
            break
        try:
            snap = fetch_view(bv, cancel=cancel)
            ok.append(snap)
            if snapshot_path:
                with open(snapshot_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        except RiskChallengeError as e:
            e.resume_index = i - 1  # 断点：下一轮从此视频续采（0-based）
            raise
        except TaskCancelledError:
            break                   # 退避途中被取消：不是失败，直接收尾
        except Exception as e:  # noqa: BLE001
            fail.append((bv, str(e)[:80]))
        if progress:
            progress(done=i, total=total, ok=len(ok), fail=len(fail),
                     text=f"采集 {i}/{total}: {bv}（成功{len(ok)} 失败{len(fail)}）")
        if not wait_or_cancel(sleep + random.random() * 0.15, cancel):
            break                   # 视频间隔也可被打断
    return ok, fail


# ============================ 分析 ============================

def analyze_snapshot(snaps):
    n = len(snaps)
    views = sorted((s["view"] or 0) for s in snaps)
    eng = sorted(((s["like"] or 0) / s["view"] * 100 if s["view"] else 0) for s in snaps)
    days = Counter(datetime.fromtimestamp(s["pubdate"]).strftime("%Y") for s in snaps
                   if s.get("pubdate"))
    kpi = {
        "count": n, "total_view": sum(views),
        "median_view": views[len(views) // 2] if views else 0,
        "median_engage": f"{eng[len(eng) // 2]:.2f}%" if eng else "-",
        "top_view": snaps[0] if snaps else None,
        "years": sorted(days.items()),
    }
    return kpi


def analyze_growth(snapshots_by_bvid):
    """监控模式：每个 bvid 首末快照对比 → 增速榜。"""
    growth = []
    for bvid, snaps in snapshots_by_bvid.items():
        if len(snaps) < 2:
            continue
        first, last = snaps[0], snaps[-1]
        dt_h = max((last["fetched_at"] - first["fetched_at"]) / 3600, 1 / 60)
        growth.append({
            "bvid": bvid, "title": last.get("title", ""), "owner": last.get("owner", ""),
            "first_view": first.get("view") or 0, "last_view": last.get("view") or 0,
            "d_view": (last.get("view") or 0) - (first.get("view") or 0),
            "d_like": (last.get("like") or 0) - (first.get("like") or 0),
            "d_coin": (last.get("coin") or 0) - (first.get("coin") or 0),
            "d_fav": (last.get("favorite") or 0) - (first.get("favorite") or 0),
            "hours": round(dt_h, 2),
            "view_per_hour": round(((last.get("view") or 0) - (first.get("view") or 0)) / dt_h, 1),
        })
    growth.sort(key=lambda g: -g["view_per_hour"])
    return growth


# ============================ Excel 导出 ============================

def export_xlsx(snaps, growth, meta, out_path, progress=None):
    if not HAVE_XLSX:
        raise RuntimeError("缺少 openpyxl（pip install openpyxl）")
    n = len(snaps)
    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)

    views = sorted((s["view"] or 0) for s in snaps)
    eng = sorted(((s["like"] or 0) / s["view"] * 100 if s["view"] else 0) for s in snaps)
    total_view = sum(views)

    # Sheet1 概览
    ws = wb.create_sheet("采集概览")
    sw.ws = ws
    sw.title_row(ws, f"视频批量采集报告 | {meta.get('title', '')} 共{n}个视频", 6)
    sw.kv(ws, [
        ("采集时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), meta.get("source", "")),
        ("视频数量", n, f"成功率 {meta.get('ok', n)}/{meta.get('attempted', n)}"),
        ("播放总量", f"{total_view:,}", f"中位数 {views[len(views) // 2] if views else 0:,}"),
        ("互动率中位", f"{eng[len(eng) // 2]:.2f}%" if eng else "-", "点赞/播放"),
        ("监控模式", meta.get("monitor", "否"),
         f"轮次 {meta.get('rounds', 1)}，间隔 {meta.get('interval_min', '-')} 分钟"),
        ("增速榜第一", (growth[0]["title"][:30] if growth else "-"),
         f"{growth[0]['view_per_hour']:,}/小时" if growth else ""),
    ])
    ws.append([None, sw.wc("口径：游客通道公开数据采集；播放量等计数为B站页面口径。",
                           font=xlsx_mod.F_CAPTION)])

    # Sheet2 视频总表（按播放降序）
    ws2 = wb.create_sheet("视频总表")
    sw.ws = ws2
    headers = ["排名", "BV号", "标题", "UP主", "分区", "时长(秒)", "发布时间",
               "播放", "弹幕", "评论", "点赞", "投币", "收藏", "分享"]
    ws2.column_dimensions["A"].width = 3
    for i, h in enumerate(headers):
        ws2.column_dimensions[get_column_letter(i + 2)].width = {
            "排名": 6, "BV号": 14, "标题": 46, "UP主": 18, "分区": 12, "时长(秒)": 9,
            "发布时间": 17, "播放": 10, "弹幕": 9, "评论": 9, "点赞": 10,
            "投币": 9, "收藏": 9, "分享": 9}.get(h, 10)
    ws2.append([None] + [sw.wc(h, font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER,
                               align=xlsx_mod.A_HEADER, border=xlsx_mod.B_HEADER)
                         for h in headers])
    ws2.freeze_panes = "C2"
    ws2.auto_filter.ref = f"B1:{get_column_letter(len(headers) + 1)}{n + 1}"
    for i, s in enumerate(sorted(snaps, key=lambda x: -(x["view"] or 0))):
        if progress and i % 5000 == 0:
            progress(text=f"写入 Excel {i}/{n}")
        ws2.append([None, i + 1, s.get("bvid"), xlsx_mod.clean(s.get("title", "")),
                    xlsx_mod.clean(s.get("owner", "")), xlsx_mod.clean(s.get("tname", "")),
                    s.get("duration"),
                    datetime.fromtimestamp(s["pubdate"]).strftime("%Y-%m-%d %H:%M")
                    if s.get("pubdate") else "",
                    s.get("view") or 0, s.get("danmaku") or 0, s.get("reply") or 0,
                    s.get("like") or 0, s.get("coin") or 0, s.get("favorite") or 0,
                    s.get("share") or 0])

    # Sheet3 增速榜（监控模式）
    ws3 = wb.create_sheet("增速榜")
    sw.ws = ws3
    sw.title_row(ws3, "增速榜（监控期增量，按播放/小时降序）", 9)
    sw.header_row(ws3, ["排名", "BV号", "标题", "UP主", "期初播放", "期末播放",
                        "播放增量", "点赞增量", "播放/小时"])
    for i, g in enumerate(growth or []):
        ws3.append([None, i + 1, g["bvid"], xlsx_mod.clean(g["title"]),
                    xlsx_mod.clean(g["owner"]),
                    g["first_view"], g["last_view"], g["d_view"], g["d_like"],
                    g["view_per_hour"]])
    if not growth:
        ws3.append([None, sw.wc("单次快照模式无增速数据（使用定时追踪模式可采集时序增量）",
                                font=xlsx_mod.F_CAPTION)])
    wb.save(str(out_path))
