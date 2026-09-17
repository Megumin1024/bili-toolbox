# -*- coding: utf-8 -*-
"""关系分析任务编排：本地解析 → 集合运算 → 动态工作表 Excel。

TaskRunner 注入 progress/cancel；cancel 只在解析循环间隙与写表前后检查，
取消走正常完成路径（stats.cancelled=True），已解析内容不落半截 Excel。
"""
from datetime import datetime

from core import xlsx as xlsx_mod
from core.xlsx_metadata import (
    FieldDefinition,
    QualityItem,
    make_metadata,
    primary_key_quality,
    write_metadata_sheets,
)
from core.xlsx_presentation import (
    TableLayout,
    append_sheet_directory,
    configure_table,
    finish_table,
    wrap_cell,
)

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
    progress(text="正在生成 Excel…")
    try:
        _write_excel(final_path, rosters, analysis,
                     before_replace=lambda: _raise_if_cancelled(cancel))
    except Cancelled:
        return _cancelled_result(rosters)

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


def _raise_if_cancelled(cancel):
    if cancel():
        raise Cancelled("export")


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


def _state_cell(kind, label):
    return xlsx_mod.cell_value(None, kind, note=label)


def _typed_cell(raw, kind, label):
    if raw is None:
        return _state_cell(xlsx_mod.CellKind.MISSING, f"{label}缺失")
    return xlsx_mod.checked_cell_value(raw, kind, note=f"{label}格式异常")


def _time_cell(raw, label="时间"):
    if raw is None:
        # 现有报告约定用破折号显示无法解析的时间；这里保留该文案，
        # 可靠解析出的时间才使用真正的 Excel datetime。
        return _typed_cell("—", xlsx_mod.CellKind.TEXT, label)
    return _typed_cell(raw, xlsx_mod.CellKind.DATETIME, label)


def _write_list_sheet(wb, sw, name, title, rows, has_time, caption):
    """清单/集合结果表的公共形态：mid、昵称、[时间]，按 mid 升序。"""
    ws = wb.create_sheet(name)
    sw.ws = ws
    headers = ["mid", "昵称"] + (["时间"] if has_time else [])
    _set_widths(ws, [14, 22, 22][:len(headers)])
    layout = TableLayout(3, 2, len(headers) + 1)
    configure_table(ws, layout)
    sw.title_row(ws, title, len(headers) + 1)
    sw.header_row(ws, headers)
    for row in rows:
        cells = [_typed_cell(row["mid"], xlsx_mod.CellKind.ID, "mid"),
                 wrap_cell(sw, _typed_cell(_s(row["name"]), xlsx_mod.CellKind.TEXT, "昵称"))]
        if has_time:
            cells.append(_time_cell(row["time"]))
        sw.append([None] + cells)
    sw.append([None])
    sw.append([None, sw.wc(caption, font=xlsx_mod.F_CAPTION)])
    finish_table(ws, layout, len(rows))


def _write_excel(path, rosters, analysis, before_replace=None):
    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)

    # ---- 汇总（必有） ----
    ws = wb.create_sheet("汇总")
    sw.ws = ws
    _set_widths(ws, [24, 18, 56])
    sw.title_row(ws, "关系分析汇总", 4)
    sw.kv(ws, _summary_pairs(rosters, analysis))
    sw.append([None, sw.wc(DECLARATION, font=xlsx_mod.F_CAPTION)])

    # ---- 清单与集合表（按主时点动态裁剪） ----
    fans_roster = rosters.get(analysis["primary_fans_key"])
    follows_roster = rosters.get(analysis["primary_follows_key"])
    directory_entries = []
    if fans_roster is not None:
        directory_entries.append(("粉丝清单", "粉丝清单"))
    if follows_roster is not None:
        directory_entries.append(("关注清单", "关注清单"))
    if analysis["mutual_rows"] is not None:
        directory_entries.append(("互相关注", "互相关注"))
    if analysis["only_fans_rows"] is not None:
        directory_entries.append(("仅粉丝", "仅粉丝"))
    if analysis["only_follows_rows"] is not None:
        directory_entries.append(("仅关注", "仅关注"))
    if analysis["diff_added_rows"] is not None:
        directory_entries.append(("粉丝差异", "粉丝差异"))
    if analysis["monthly"] is not None:
        directory_entries.append(("按月新增分布", "按月新增分布"))
    directory_entries.extend((("数据质量", "数据质量"), ("字段说明", "字段说明")))
    append_sheet_directory(sw, ws, directory_entries)
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
        diff_layout = TableLayout(3, 2, 5)
        configure_table(ws, diff_layout)
        sw.title_row(
            ws,
            f"粉丝差异 · 时点 2 相对时点 1（新增 {len(added):,} / 取关 {len(removed):,}）",
            5)
        sw.header_row(ws, ["状态", "mid", "昵称", "时间"])
        for status, rows in (("新增", added), ("取关", removed)):
            for row in rows:
                sw.append([None, _typed_cell(status, xlsx_mod.CellKind.TEXT, "状态"),
                           _typed_cell(row["mid"], xlsx_mod.CellKind.ID, "mid"),
                           wrap_cell(sw, _typed_cell(_s(row["name"]), xlsx_mod.CellKind.TEXT, "昵称")),
                           _time_cell(row["time"])])
        finish_table(ws, diff_layout, len(added) + len(removed))
        sw.append([None])
        sw.append([None, sw.wc(
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
        monthly_layout = TableLayout(3, 2, 4)
        configure_table(ws, monthly_layout)
        sw.title_row(ws, "按月新增分布（时点 1 粉丝的关注时间）", 4)
        sw.header_row(ws, ["月份", "粉丝人数", "占比"])
        for month, count in monthly:
            sw.append([None, _typed_cell(month, xlsx_mod.CellKind.TEXT, "月份"),
                       _typed_cell(count, xlsx_mod.CellKind.INTEGER, "粉丝人数"),
                       _typed_cell(count / t1_total, xlsx_mod.CellKind.PERCENT, "占比")])
        finish_table(ws, monthly_layout, len(monthly))
        sw.append([None])
        note = ("口径：按主时点为时点 1 的粉丝清单中各用户的关注时间按月统计，"
                "反映粉丝的累积进入节奏。")
        if unparsed:
            note += f"另有 {unparsed:,} 人时间无法解析，未计入。"
        if not monthly:
            note = "时点 1 粉丝清单存在时间列，但没有可解析的时间值。"
        sw.append([None, sw.wc(note, font=xlsx_mod.F_CAPTION)])

    total_rows = sum(item.data_rows for item in rosters.values())
    bad_rows = sum(item.bad_rows for item in rosters.values())
    duplicates = sum(item.duplicates for item in rosters.values())
    def q(item, raw, kind, unit, note, presentation_state=None):
        if raw is None:
            value = xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE, note=note)
        else:
            value = xlsx_mod.checked_cell_value(raw, kind, note=note)
        return QualityItem("本地输入", item, value, unit, note, presentation_state)
    quality = [
        q("输入文件数", len(rosters), xlsx_mod.CellKind.INTEGER, "个", "成功解析的本地输入文件"),
        q("读到的数据行总数", total_rows, xlsx_mod.CellKind.INTEGER, "行", "含坏行和重复行"),
        q("有效写入行数", sum(len(item.rows) for item in rosters.values()), xlsx_mod.CellKind.INTEGER, "行", "按 mid 去重后的有效行"),
        q("坏行数量", bad_rows, xlsx_mod.CellKind.INTEGER, "行", "缺 mid 或 mid 非数字"),
        q("重复数量", duplicates, xlsx_mod.CellKind.INTEGER, "行", "同一 mid 保留首次出现"),
        q("无法解析时间数量", analysis["monthly_unparsed"], xlsx_mod.CellKind.INTEGER, "行", "按月分布中真实未解析的关注时间数量"),
        q("接口声称数量", None, xlsx_mod.CellKind.INTEGER, "条", "本地输入不适用"),
        q("覆盖率", None, xlsx_mod.CellKind.PERCENT, "状态", "本地输入不适用"),
        q("是否取消", False, xlsx_mod.CellKind.BOOLEAN, "状态", "本地任务已完成写表"),
        q("是否部分成功", bool(sum(len(item.rows) for item in rosters.values())) and bool(
            bad_rows or duplicates or analysis["monthly_unparsed"]),
          xlsx_mod.CellKind.BOOLEAN, "状态", "存在有效输出且有坏行、重复或时间未解析", "warning"),
    ]
    quality.extend(primary_key_quality(
        "主键", "mid", denominator=total_rows,
        missing=None, invalid=None, duplicates=duplicates,
        dedup_discarded=duplicates, remaining_conflicts=0,
        source_note="Roster 只保留 mid 归一化后的结构化统计；缺失/非法未拆分时不适用",
    ))
    fields = []
    def add(sheet, display, stable, dtype, metric, missing="缺失或无法解析"):
        fields.append(FieldDefinition(sheet, display, stable, dtype, "", "是", "本地用户文件/关系运算", metric,
                                      xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE), missing))
    summary_labels = [label for label, _value, _note in _summary_pairs(rosters, analysis)]
    for label in summary_labels:
        add("汇总", label, f"summary.{label}", "文本/数值", "汇总表稳定指标项")
    list_specs = []
    fans_roster = rosters.get(analysis["primary_fans_key"])
    follows_roster = rosters.get(analysis["primary_follows_key"])
    if fans_roster is not None:
        list_specs.append(("粉丝清单", fans_roster.has_time))
    if follows_roster is not None:
        list_specs.append(("关注清单", follows_roster.has_time))
    if analysis["mutual_rows"] is not None:
        list_specs.append(("互相关注", fans_roster.has_time))
    if analysis["only_fans_rows"] is not None:
        list_specs.append(("仅粉丝", fans_roster.has_time))
    if analysis["only_follows_rows"] is not None:
        list_specs.append(("仅关注", follows_roster.has_time))
    for sheet, has_time in list_specs:
        add(sheet, "mid", "mid", "ID", "按 mid 集合运算")
        add(sheet, "昵称", "name", "文本", "源文件昵称")
        if has_time:
            add(sheet, "时间", "time", "日期时间", "源文件时间解析；失败显示占位符")
    if analysis["diff_added_rows"] is not None:
        for display, stable, dtype, metric in (
            ("状态", "status", "文本", "新增/取关"), ("mid", "mid", "ID", "集合差异主键"),
            ("昵称", "name", "文本", "主时点昵称"), ("时间", "time", "日期时间", "主时点时间"),
        ):
            add("粉丝差异", display, stable, dtype, metric)
    if analysis["monthly"] is not None:
        for display, stable, dtype, metric in (
            ("月份", "month", "文本", "按时间月份聚合"),
            ("粉丝人数", "count", "整数", "按月计数"),
            ("占比", "ratio", "百分比", "相对时点1粉丝总数"),
        ):
            add("按月新增分布", display, stable, dtype, metric)
    metadata = make_metadata(
        tool="关系分析", report_type="关系分析", parameters={
            "输入文件数量": xlsx_mod.cell_value(len(rosters), xlsx_mod.CellKind.INTEGER),
            "输入槽位": xlsx_mod.cell_value("、".join(ROSTER_LABELS[key] for key in rosters), xlsx_mod.CellKind.TEXT),
        }, parameter_allowlist=("输入文件数量", "输入槽位"), quality_items=quality, fields=fields)
    write_metadata_sheets(wb, metadata)
    xlsx_mod.save_workbook_atomic(wb, path, before_replace=before_replace)


def _summary_pairs(rosters, analysis):
    pairs = []
    for key in ROSTER_KEYS:
        label = ROSTER_LABELS[key]
        roster = rosters.get(key)
        if roster is None:
            pairs.append((label, _typed_cell("未提供", xlsx_mod.CellKind.TEXT, label), "留空"))
            continue
        note = f"文件：{roster.source_name}"
        if roster.has_time:
            note += "；含时间列"
        pairs.append((label, _typed_cell(roster.summary_note(), xlsx_mod.CellKind.TEXT, label), note))

    pairs.append((
        "主时点",
        _typed_cell(analysis["primary_fans_label"] or "—", xlsx_mod.CellKind.TEXT, "主时点"),
        "清单表与互关/仅粉丝/仅关注按主时点计算；时点 2 仅用于差异。",
    ))

    if analysis["mutual_rows"] is not None:
        pairs.append(("互相关注", _typed_cell(len(analysis["mutual_rows"]),
                                               xlsx_mod.CellKind.INTEGER, "互相关注"),
                      "既是粉丝也是关注（按 mid）。"))
        pairs.append(("仅粉丝", _typed_cell(len(analysis["only_fans_rows"]),
                                             xlsx_mod.CellKind.INTEGER, "仅粉丝"),
                      "是粉丝但不在关注清单里。"))
        pairs.append(("仅关注", _typed_cell(len(analysis["only_follows_rows"]),
                                              xlsx_mod.CellKind.INTEGER, "仅关注"),
                      "在关注清单里但不是粉丝。"))
    else:
        pairs.append(("互相关注 / 仅粉丝 / 仅关注",
                      _typed_cell("未计算", xlsx_mod.CellKind.TEXT, "集合运算"),
                      "需要同时提供粉丝与关注清单（主时点）。"))

    if analysis["diff_added_rows"] is not None:
        pairs.append(("快照差异 · 新增", _typed_cell(len(analysis["diff_added_rows"]),
                                                     xlsx_mod.CellKind.INTEGER, "新增"),
                      "时点 2 粉丝相对时点 1 粉丝"))
        pairs.append(("快照差异 · 取关", _typed_cell(len(analysis["diff_removed_rows"]),
                                                     xlsx_mod.CellKind.INTEGER, "取关"),
                      "时点 1 有、时点 2 没有"))
        pairs.append(("快照差异 · 未变", _typed_cell(analysis["diff_unchanged"],
                                                     xlsx_mod.CellKind.INTEGER, "未变"),
                      "两个时点都在"))
    else:
        pairs.append(("快照差异", _typed_cell("未计算", xlsx_mod.CellKind.TEXT, "快照差异"),
                      "需要同时提供时点 1 与时点 2 的粉丝清单。"))

    if analysis["monthly"] is not None:
        pairs.append(("按月新增分布", _typed_cell("已输出", xlsx_mod.CellKind.TEXT, "按月新增分布"),
                      "按时点 1 粉丝的关注时间按月统计。"))
    elif rosters.get("fans_t1") is None:
        pairs.append(("按月新增分布", _typed_cell("未输出", xlsx_mod.CellKind.TEXT, "按月新增分布"),
                      "时点 1 粉丝清单未提供。"))
    else:
        pairs.append(("按月新增分布", _typed_cell("未输出", xlsx_mod.CellKind.TEXT, "按月新增分布"),
                      "时点 1 粉丝清单没有时间列。"))

    bad_total = sum(item.bad_rows for item in rosters.values())
    dup_total = sum(item.duplicates for item in rosters.values())
    pairs.append(("坏行跳过合计", _typed_cell(bad_total, xlsx_mod.CellKind.INTEGER, "坏行跳过合计"),
                  "缺 mid 或 mid 非数字的行已跳过，不中断任务。"))
    pairs.append(("mid 去重合计", _typed_cell(dup_total, xlsx_mod.CellKind.INTEGER, "mid去重合计"),
                  "同一 mid 只保留首次出现。"))
    pairs.append(("生成时间", _typed_cell(
        datetime.now(xlsx_mod.ASIA_SHANGHAI).replace(tzinfo=None),
        xlsx_mod.CellKind.DATETIME, "生成时间"),
                  "纯本地处理，无网络请求。"))
    return pairs
