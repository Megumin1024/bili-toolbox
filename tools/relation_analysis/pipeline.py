# -*- coding: utf-8 -*-
"""关系分析任务编排：本地解析 → 集合运算 → 动态工作表 Excel。

TaskRunner 注入 progress/cancel；cancel 只在解析循环间隙与写表前后检查，
取消走正常完成路径（stats.cancelled=True），已解析内容不落半截 Excel
（先写临时文件，确认未取消后 os.replace 原子替换）。
"""
import os
from datetime import datetime

from core import xlsx as xlsx_mod

from .core import Cancelled, _s, compute_analysis, format_time, load_roster, validate_inputs

ROSTER_KEYS = ("fans_t1", "follows_t1", "fans_t2", "follows_t2")
ROSTER_LABELS = {
    "fans_t1": "时点 1 · 粉丝清单",
    "follows_t1": "时点 1 · 关注清单",
    "fans_t2": "时点 2 · 粉丝清单",
    "follows_t2": "时点 2 · 关注清单",
}
SLOT_KEYS = {"fans_t1": "时点 1", "follows_t1": "时点 1",
             "fans_t2": "时点 2", "follows_t2": "时点 2"}

DECLARATION = ("数据来源声明：本工具仅处理你合法取得的本地文件，不发起任何网络请求；"
               "结果只保存在本机输出目录。")


def run_pipeline(fans_t1="", follows_t1="", fans_t2="", follows_t2="", out_dir="",
                 progress=None, cancel=None, **kwargs):
    """TaskRunner 注入 progress/cancel 后调用的本地分析入口。"""
    progress = progress or (lambda **kw: None)
    cancel = cancel or (lambda: False)
    paths, out_path = validate_inputs(fans_t1, follows_t1, fans_t2, follows_t2, out_dir)
    rosters = {}
    ordered = [key for key in ROSTER_KEYS if key in paths]
    total = len(ordered)
    for index, key in enumerate(ordered, 1):
        label = ROSTER_LABELS[key]
        if cancel():
            return _cancelled_result(rosters)
        try:
            roster = load_roster(paths[key], label, cancel=cancel, progress=progress)
        except Cancelled:
            return _cancelled_result(rosters)
        rosters[key] = roster
        # 文本由 load_roster 汇报（开始/完成各一条），这里只推进度条
        progress(done=index, total=total)
    if cancel():
        return _cancelled_result(rosters)

    analysis = compute_analysis(rosters)
    final_path = _reserve_output_path(out_path)
    temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    progress(text="正在生成 Excel…")
    try:
        _write_excel(temp_path, rosters, analysis)
        if cancel():
            raise Cancelled("export")
        os.replace(str(temp_path), str(final_path))
    except Cancelled:
        temp_path.unlink(missing_ok=True)
        return _cancelled_result(rosters)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    stats = _build_stats(rosters, analysis)
    progress(text=f"分析完成：{final_path.name}", done=total, total=total)
    return {
        "excel": str(final_path),
        "dir": str(out_path),
        "stats": stats,
    }


def _cancelled_result(rosters):
    return {
        "excel": "",
        "dir": "",
        "stats": {
            "cancelled": True,
            "file_count": len(rosters),
            "row_count": sum(len(item.rows) for item in rosters.values()),
        },
    }


def _build_stats(rosters, analysis):
    stats = {
        "cancelled": False,
        "file_count": len(rosters),
        "row_count": sum(len(item.rows) for item in rosters.values()),
        "bad_rows": sum(item.bad_rows for item in rosters.values()),
        "duplicates": sum(item.duplicates for item in rosters.values()),
        "mutual": None, "only_fans": None, "only_follows": None,
        "added": None, "removed": None, "unchanged": None,
        "monthly": analysis["monthly"] is not None,
    }
    if analysis["mutual_rows"] is not None:
        stats["mutual"] = len(analysis["mutual_rows"])
        stats["only_fans"] = len(analysis["only_fans_rows"])
        stats["only_follows"] = len(analysis["only_follows_rows"])
    if analysis["diff_added_rows"] is not None:
        stats["added"] = len(analysis["diff_added_rows"])
        stats["removed"] = len(analysis["diff_removed_rows"])
        stats["unchanged"] = analysis["diff_unchanged"]
    return stats


def _reserve_output_path(out_path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = out_path / f"关系分析_{stamp}.xlsx"
    counter = 1
    while candidate.exists():
        candidate = out_path / f"关系分析_{stamp}_{counter}.xlsx"
        counter += 1
    return candidate


def _set_widths(ws, widths):
    for offset, width in enumerate(widths, start=2):
        ws.column_dimensions[chr(ord("B") + offset - 2)].width = width


def _write_list_sheet(wb, sw, name, title, rows, has_time, caption):
    """清单/集合结果表的公共形态：mid、昵称、[时间]，按 mid 升序。"""
    ws = wb.create_sheet(name)
    sw.ws = ws
    headers = ["mid", "昵称"] + (["时间"] if has_time else [])
    _set_widths(ws, [14, 22, 22][:len(headers)])
    sw.title_row(ws, title, len(headers) + 1)
    sw.header_row(ws, headers)
    for row in rows:
        cells = [sw.wc(row["mid"]), sw.wc(_s(row["name"]))]
        if has_time:
            cells.append(sw.wc(format_time(row["time"])))
        ws.append([None] + cells)
    ws.append([None])
    ws.append([None, sw.wc(caption, font=xlsx_mod.F_CAPTION)])


def _write_excel(path, rosters, analysis):
    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)

    # ---- 汇总（必有） ----
    ws = wb.create_sheet("汇总")
    sw.ws = ws
    _set_widths(ws, [24, 18, 56])
    sw.title_row(ws, "关系分析汇总", 4)
    sw.kv(ws, _summary_pairs(rosters, analysis))
    ws.append([None, sw.wc(DECLARATION, font=xlsx_mod.F_CAPTION)])

    # ---- 清单与集合表（按主时点动态裁剪） ----
    fans_roster = rosters.get(analysis["primary_fans_key"])
    follows_roster = rosters.get(analysis["primary_follows_key"])
    if fans_roster is not None:
        _write_list_sheet(
            wb, sw, "粉丝清单",
            f"粉丝清单 · {analysis['primary_fans_label']}（{len(fans_roster.rows):,} 人）",
            sorted(fans_roster.rows, key=lambda row: row["mid"]),
            fans_roster.has_time,
            "按 mid 升序；时间无法解析时显示 —；原文件其他列已丢弃。")
    if follows_roster is not None:
        _write_list_sheet(
            wb, sw, "关注清单",
            f"关注清单 · {analysis['primary_follows_label']}（{len(follows_roster.rows):,} 人）",
            sorted(follows_roster.rows, key=lambda row: row["mid"]),
            follows_roster.has_time,
            "按 mid 升序；时间无法解析时显示 —；原文件其他列已丢弃。")
    if analysis["mutual_rows"] is not None:
        _write_list_sheet(
            wb, sw, "互相关注",
            f"互相关注（{len(analysis['mutual_rows']):,} 人）",
            analysis["mutual_rows"], fans_roster.has_time,
            "口径：粉丝 ∩ 关注（按 mid，主时点）；昵称与时间取自主时点粉丝清单。")
    if analysis["only_fans_rows"] is not None:
        _write_list_sheet(
            wb, sw, "仅粉丝",
            f"仅粉丝（{len(analysis['only_fans_rows']):,} 人）",
            analysis["only_fans_rows"], fans_roster.has_time,
            "口径：粉丝 − 关注（按 mid，主时点）。")
    if analysis["only_follows_rows"] is not None:
        _write_list_sheet(
            wb, sw, "仅关注",
            f"仅关注（{len(analysis['only_follows_rows']):,} 人）",
            analysis["only_follows_rows"], follows_roster.has_time,
            "口径：关注 − 粉丝（按 mid，主时点）。")

    # ---- 粉丝差异（两份粉丝清单齐备时） ----
    if analysis["diff_added_rows"] is not None:
        added = analysis["diff_added_rows"]
        removed = analysis["diff_removed_rows"]
        ws = wb.create_sheet("粉丝差异")
        sw.ws = ws
        _set_widths(ws, [10, 14, 22, 22])
        sw.title_row(
            ws,
            f"粉丝差异 · 时点 2 相对时点 1（新增 {len(added):,} / 取关 {len(removed):,}）",
            5)
        sw.header_row(ws, ["状态", "mid", "昵称", "时间"])
        for status, rows in (("新增", added), ("取关", removed)):
            for row in rows:
                ws.append([None, sw.wc(status), sw.wc(row["mid"]),
                           sw.wc(_s(row["name"])), sw.wc(format_time(row["time"]))])
        ws.append([None])
        ws.append([None, sw.wc(
            "口径：新增 = 时点 2 有、时点 1 没有；取关 = 时点 1 有、时点 2 没有。"
            "时间为该用户在对应时点清单中的关注时间，解析失败显示 —。",
            font=xlsx_mod.F_CAPTION)])

    # ---- 按月新增分布（时点 1 粉丝清单存在时间列时） ----
    if analysis["monthly"] is not None:
        monthly = analysis["monthly"]
        unparsed = analysis["monthly_unparsed"]
        t1_total = len(rosters["fans_t1"].rows) or 1
        ws = wb.create_sheet("按月新增分布")
        sw.ws = ws
        _set_widths(ws, [14, 14, 14])
        sw.title_row(ws, "按月新增分布（时点 1 粉丝的关注时间）", 4)
        sw.header_row(ws, ["月份", "粉丝人数", "占比"])
        for month, count in monthly:
            ws.append([None, sw.wc(month), sw.wc(f"{count:,}"),
                       sw.wc(f"{count / t1_total * 100:.1f}%")])
        ws.append([None])
        note = ("口径：按主时点为时点 1 的粉丝清单中各用户的关注时间按月统计，"
                "反映粉丝的累积进入节奏。")
        if unparsed:
            note += f"另有 {unparsed:,} 人时间无法解析，未计入。"
        if not monthly:
            note = "时点 1 粉丝清单存在时间列，但没有可解析的时间值。"
        ws.append([None, sw.wc(note, font=xlsx_mod.F_CAPTION)])

    wb.save(str(path))


def _summary_pairs(rosters, analysis):
    pairs = []
    for key in ROSTER_KEYS:
        label = ROSTER_LABELS[key]
        roster = rosters.get(key)
        if roster is None:
            pairs.append((label, "未提供", "留空"))
            continue
        note = f"文件：{roster.source_name}"
        if roster.has_time:
            note += "；含时间列"
        pairs.append((label, roster.summary_note(), note))

    pairs.append((
        "主时点",
        analysis["primary_fans_label"] or "—",
        "清单表与互关/仅粉丝/仅关注按主时点计算；时点 2 仅用于差异。",
    ))

    if analysis["mutual_rows"] is not None:
        pairs.append(("互相关注", f"{len(analysis['mutual_rows']):,}",
                      "既是粉丝也是关注（按 mid）。"))
        pairs.append(("仅粉丝", f"{len(analysis['only_fans_rows']):,}",
                      "是粉丝但不在关注清单里。"))
        pairs.append(("仅关注", f"{len(analysis['only_follows_rows']):,}",
                      "在关注清单里但不是粉丝。"))
    else:
        pairs.append(("互相关注 / 仅粉丝 / 仅关注", "未计算",
                      "需要同时提供粉丝与关注清单（主时点）。"))

    if analysis["diff_added_rows"] is not None:
        pairs.append(("快照差异 · 新增", f"{len(analysis['diff_added_rows']):,}",
                      "时点 2 粉丝相对时点 1 粉丝"))
        pairs.append(("快照差异 · 取关", f"{len(analysis['diff_removed_rows']):,}",
                      "时点 1 有、时点 2 没有"))
        pairs.append(("快照差异 · 未变", f"{analysis['diff_unchanged']:,}",
                      "两个时点都在"))
    else:
        pairs.append(("快照差异", "未计算",
                      "需要同时提供时点 1 与时点 2 的粉丝清单。"))

    if analysis["monthly"] is not None:
        pairs.append(("按月新增分布", "已输出",
                      "按时点 1 粉丝的关注时间按月统计。"))
    elif rosters.get("fans_t1") is None:
        pairs.append(("按月新增分布", "未输出", "时点 1 粉丝清单未提供。"))
    else:
        pairs.append(("按月新增分布", "未输出", "时点 1 粉丝清单没有时间列。"))

    bad_total = sum(item.bad_rows for item in rosters.values())
    dup_total = sum(item.duplicates for item in rosters.values())
    pairs.append(("坏行跳过合计", f"{bad_total:,}",
                  "缺 mid 或 mid 非数字的行已跳过，不中断任务。"))
    pairs.append(("mid 去重合计", f"{dup_total:,}", "同一 mid 只保留首次出现。"))
    pairs.append(("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "纯本地处理，无网络请求。"))
    return pairs
