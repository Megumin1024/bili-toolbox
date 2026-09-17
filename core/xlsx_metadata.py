# -*- coding: utf-8 -*-
"""第一方 XLSX 报告的共享元数据契约。

本模块只负责把调用方已经确认的质量统计、字段定义和白名单参数摘要写入
标准工作表。它不访问网络、不读取配置，也不根据表头推断业务口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import re
from typing import Iterable, Mapping, Sequence

from . import __version__ as APP_VERSION
from .task_history import contains_sensitive_text
from .xlsx import (
    ASIA_SHANGHAI,
    CellKind,
    CellValue,
    SheetWriter,
    cell_value,
    checked_cell_value,
    clean_text,
)
from .xlsx_presentation import apply_quality_conditional_formats, wrap_cell


_EXPORT_SENDKEY_RE = re.compile(
    r"(?i)(?<![a-z0-9_])send[\s_-]*key\s*[:=：]\s*[^\s,;，；]+"
)
_EXPORT_SENDKEY_KEY_RE = re.compile(r"(?i)^send[\s_-]*key$")


def contains_export_sensitive_text(value: object) -> bool:
    """识别 XLSX 导出边界的敏感值，包括 SendKey 变体及嵌套值。"""
    if contains_sensitive_text(value):
        return True
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _EXPORT_SENDKEY_KEY_RE.fullmatch(str(key)):
                return True
            if contains_export_sensitive_text(key) or contains_export_sensitive_text(item):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_export_sensitive_text(item) for item in value)
    return bool(_EXPORT_SENDKEY_RE.search(str(value))) if value is not None else False


XLSX_SCHEMA_VERSION = 1
XLSX_METADATA_MARKER = "BiliToolbox:XLSX_METADATA:v1"
QUALITY_SHEET = "数据质量"
FIELDS_SHEET = "字段说明"
TIMEZONE_LABEL = "Asia/Shanghai / UTC+08:00"
QUALITY_HEADERS = ("类别", "项目", "数值", "单位或状态", "说明")
FIELD_HEADERS = (
    "工作表",
    "显示列名",
    "稳定字段名",
    "数据类型",
    "单位",
    "可为空",
    "字段来源",
    "统计口径",
    "示例值",
    "缺失值含义",
)

_MAX_DECLARED_COUNT = 999_999_999_999_999


@dataclass(frozen=True)
class QualityItem:
    category: str
    item: str
    value: CellValue
    unit_or_status: str
    note: str
    presentation_state: str | None = None


@dataclass(frozen=True)
class FieldDefinition:
    worksheet: str
    display_name: str
    stable_name: str
    data_type: str
    unit: str
    nullable: str
    source: str
    metric: str
    example: CellValue
    missing_meaning: str


@dataclass(frozen=True)
class WorkbookMetadata:
    tool: str
    report_type: str
    generated_at: datetime
    timezone: str
    parameter_summary: tuple[tuple[str, CellValue], ...]
    quality_items: tuple[QualityItem, ...]
    fields: tuple[FieldDefinition, ...]


def percent_cell(numerator, denominator, *, note="分母为 0，覆盖率不适用"):
    """把显式分子/分母转换为 Excel 百分比；分母为 0 时返回不适用。"""
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        raise TypeError("百分比的分子和分母不能是 bool")
    numerator_value = checked_cell_value(numerator, CellKind.DECIMAL)
    denominator_value = checked_cell_value(denominator, CellKind.DECIMAL)
    if numerator_value.kind is not CellKind.DECIMAL:
        raise ValueError("百分比的分子格式非法")
    if denominator_value.kind is not CellKind.DECIMAL:
        raise ValueError("百分比的分母格式非法")
    try:
        numerator_decimal = Decimal(str(numerator_value.value))
        denominator_decimal = Decimal(str(denominator_value.value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TypeError("百分比的分子和分母必须是数字") from exc
    if not numerator_decimal.is_finite() or not denominator_decimal.is_finite():
        raise ValueError("百分比不接受 NaN 或 Infinity")
    if denominator_decimal == 0:
        return cell_value(None, CellKind.NOT_APPLICABLE, note=note)
    ratio = numerator_decimal / denominator_decimal
    if not ratio.is_finite():
        raise ValueError("百分比结果不是有限数值")
    # core.xlsx 的共享数值契约最多允许 15 位有效数字。循环小数先按
    # 有效数字舍入，不能把 1/3 直接交给 SheetWriter。
    try:
        if ratio:
            quantum = Decimal(1).scaleb(ratio.adjusted() - 14)
            ratio = ratio.quantize(quantum, rounding=ROUND_HALF_EVEN)
        result = checked_cell_value(
            ratio, CellKind.PERCENT, number_format="0.0%"
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("百分比结果超出 Excel 可写范围") from exc
    if result.kind is not CellKind.PERCENT:
        raise ValueError("百分比结果超出 Excel 可写范围")
    return result


def quality_state(raw, kind, *, note):
    """把上游可观测值转换成质量表单元格；缺失不伪造为 0。"""
    if raw is None:
        return cell_value(None, CellKind.NOT_APPLICABLE, note=note)
    return checked_cell_value(raw, kind, note=note)


def primary_key_quality(
    category: str,
    key_label: str,
    *,
    denominator,
    missing,
    invalid,
    duplicates,
    dedup_discarded,
    remaining_conflicts,
    source_note: str,
) -> tuple[QualityItem, ...]:
    """生成所有第一方主键必须具备的六项质量指标。

    参数全部来自生产管线或调用方显式传入的结构化统计；本函数不读取工作表、
    不扫描输出内容，也不把未提供的计数猜成 0。
    """
    def count_item(suffix, raw):
        return QualityItem(
            category,
            f"{key_label}{suffix}",
            quality_state(raw, CellKind.INTEGER, note=f"{source_note}；未传入时不适用"),
            "条",
            source_note,
        )

    missing_rate = percent_cell(
        missing, denominator,
        note=f"缺失率分子={missing}、分母={denominator}；分母为 0 或统计未返回时不适用",
    ) if missing is not None and denominator is not None else cell_value(
        None, CellKind.NOT_APPLICABLE, note=f"缺失率分子/分母未返回；{source_note}",
    )
    return (
        count_item("缺失数量", missing),
        QualityItem(category, f"{key_label}缺失率", missing_rate, "百分比",
                    f"分子=缺失数量，分母=主键候选记录数；{source_note}"),
        count_item("非法数量", invalid),
        count_item("检测到的重复数量", duplicates),
        count_item("去重丢弃数量", dedup_discarded),
        count_item("输出剩余冲突数量", remaining_conflicts),
    )


def build_parameter_summary(
    values: Mapping[str, CellValue],
    allowlist: Sequence[str],
) -> tuple[tuple[str, CellValue], ...]:
    """按调用方显式白名单构造参数摘要。

    allowlist 是业务层的白名单；未列出的键不能被写入。缺少的可选键不自动
    补值，调用方如需表达未返回/不适用，必须显式传入对应 CellValue。
    """
    allowed = tuple(str(key) for key in allowlist)
    if len(set(allowed)) != len(allowed):
        raise ValueError("参数摘要白名单不能包含重复键")
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError("参数摘要包含未白名单字段")
    result = []
    for key in allowed:
        if key not in values:
            continue
        value = values[key]
        if not isinstance(value, CellValue):
            raise TypeError("参数摘要值必须是 CellValue")
        if contains_export_sensitive_text({key: value.value}):
            # 参数键已由业务层显式白名单确认，但值仍可能来自文件名或用户输入。
            # 这类值不能让整个报告失败，也不能把原文带入错误、批注或 XML。
            result.append((clean_text(key), cell_value(
                None, CellKind.NOT_APPLICABLE, note="已排除敏感值")))
        else:
            result.append((clean_text(key), value))
    return tuple(result)


def make_metadata(
    *,
    tool: str,
    report_type: str,
    parameters: Mapping[str, CellValue],
    parameter_allowlist: Sequence[str],
    quality_items: Iterable[QualityItem],
    fields: Iterable[FieldDefinition],
    generated_at: datetime | None = None,
    timezone_label: str = TIMEZONE_LABEL,
) -> WorkbookMetadata:
    """由业务层用显式白名单创建一份工作簿元数据快照。"""
    return WorkbookMetadata(
        tool=tool,
        report_type=report_type,
        generated_at=generated_at or datetime.now(ASIA_SHANGHAI),
        timezone=timezone_label,
        parameter_summary=build_parameter_summary(parameters, parameter_allowlist),
        quality_items=tuple(quality_items),
        fields=tuple(fields),
    )


def is_standard_metadata_sheet(sheet_name: str, rows: Iterable[Iterable[object]]) -> bool:
    """仅以固定表名和精确标记识别标准元数据表。"""
    if sheet_name not in (QUALITY_SHEET, FIELDS_SHEET):
        return False
    return any(
        cell == XLSX_METADATA_MARKER
        for row in rows
        for cell in row
    )


def is_complete_metadata_workbook(
    sheets: Mapping[str, Iterable[Iterable[object]]],
) -> bool:
    """只有两张固定名称工作表都带精确标记时才识别为标准元数据。"""
    return all(
        name in sheets and is_standard_metadata_sheet(name, sheets[name])
        for name in (QUALITY_SHEET, FIELDS_SHEET)
    )


def classify_declared_count(raw, *, present: bool) -> tuple[str, int | None]:
    """严格区分接口未返回、合法非负整数和声明格式异常。"""
    if not present or raw is None:
        return "not_returned", None
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw < 0
        or raw > _MAX_DECLARED_COUNT
    ):
        return "invalid", None
    return "valid", raw


def _safe_text(value, label):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{label} 必须是文本")
    if contains_export_sensitive_text(value):
        raise ValueError(f"{label} 包含敏感信息")
    return clean_text(value)


def _validate_cell_value(value, label):
    if not isinstance(value, CellValue):
        raise TypeError(f"{label} 必须是 CellValue")
    if isinstance(value.value, str) and contains_export_sensitive_text(value.value):
        raise ValueError(f"{label} 包含敏感信息，已拒绝写入")
    if value.note and contains_export_sensitive_text(value.note):
        raise ValueError(f"{label} 说明包含敏感信息，已拒绝写入")
    return value


def _generation_pairs(metadata: WorkbookMetadata):
    generated_at = metadata.generated_at
    if not isinstance(generated_at, datetime):
        raise TypeError("生成时间必须是 datetime")
    try:
        local_time = generated_at.astimezone(ASIA_SHANGHAI).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("生成时间无法转换为 Asia/Shanghai") from exc
    return (
        ("元数据标记", cell_value(XLSX_METADATA_MARKER, CellKind.TEXT)),
        ("Excel schema 版本", cell_value(XLSX_SCHEMA_VERSION, CellKind.INTEGER)),
        ("应用版本", cell_value(APP_VERSION, CellKind.TEXT)),
        ("生成时间", cell_value(local_time, CellKind.DATETIME)),
        ("时区", cell_value(_safe_text(metadata.timezone, "时区"), CellKind.TEXT)),
        ("生产工具", cell_value(_safe_text(metadata.tool, "生产工具"), CellKind.TEXT)),
        ("报告类型", cell_value(_safe_text(metadata.report_type, "报告类型"), CellKind.TEXT)),
    )


def _write_generation_info(writer: SheetWriter, worksheet, metadata: WorkbookMetadata):
    writer.title_row(worksheet, worksheet.title, max(len(QUALITY_HEADERS), len(FIELD_HEADERS)))
    for key, value in _generation_pairs(metadata):
        writer.append([None, writer.wc(key), value])
    if metadata.parameter_summary:
        writer.append([None, writer.wc("参数摘要"), None])
        for key, value in metadata.parameter_summary:
            writer.append([
                None,
                writer.wc(_safe_text(key, "参数键")),
                _validate_cell_value(value, "参数值"),
            ])
    writer.append([None])
    # title_row() contributes two rows; the seven fixed generation pairs then
    # occupy seven rows.  Parameter rows are explicit and this return value is
    # used to attach formatting to the real quality table, not to guessed rows.
    return 2 + 7 + (1 + len(metadata.parameter_summary) + 1
                    if metadata.parameter_summary else 0) + 1


def _validate_metadata(metadata: WorkbookMetadata):
    if not isinstance(metadata, WorkbookMetadata):
        raise TypeError("metadata 必须是 WorkbookMetadata")
    if not isinstance(metadata.parameter_summary, tuple):
        raise TypeError("参数摘要必须是 tuple")
    for key, value in metadata.parameter_summary:
        _safe_text(key, "参数键")
        _validate_cell_value(value, "参数值")
    for item in metadata.quality_items:
        if not isinstance(item, QualityItem):
            raise TypeError("质量项必须是 QualityItem")
        _safe_text(item.category, "质量类别")
        _safe_text(item.item, "质量项目")
        _validate_cell_value(item.value, "质量值")
        _safe_text(item.unit_or_status, "质量单位或状态")
        _safe_text(item.note, "质量说明")
        if item.presentation_state not in (None, "warning", "error", "stop"):
            raise ValueError("质量展示状态必须是 warning、error 或 stop")
    for field in metadata.fields:
        if not isinstance(field, FieldDefinition):
            raise TypeError("字段定义必须是 FieldDefinition")
        for value, label in (
            (field.worksheet, "字段工作表"),
            (field.display_name, "显示列名"),
            (field.stable_name, "稳定字段名"),
            (field.data_type, "字段类型"),
            (field.unit, "字段单位"),
            (field.nullable, "字段可为空"),
            (field.source, "字段来源"),
            (field.metric, "字段口径"),
            (field.missing_meaning, "缺失值含义"),
        ):
            _safe_text(value, label)
        _validate_cell_value(field.example, "字段示例")


def write_metadata_sheets(workbook, metadata: WorkbookMetadata) -> None:
    """在 workbook 当前末尾追加标准的两张元数据表。"""
    try:
        _validate_metadata(metadata)
        existing = set(getattr(workbook, "sheetnames", ()))
        if QUALITY_SHEET in existing or FIELDS_SHEET in existing:
            raise ValueError("工作簿已存在标准元数据工作表，拒绝覆盖")

        writer = SheetWriter(workbook)
        quality = workbook.create_sheet(QUALITY_SHEET)
        quality_header_before = _write_generation_info(writer, quality, metadata)
        writer.header_row(quality, QUALITY_HEADERS)
        for item in metadata.quality_items:
            writer.append([
                None,
                writer.wc(_safe_text(item.category, "质量类别")),
                writer.wc(_safe_text(item.item, "质量项目")),
                _validate_cell_value(item.value, "质量值"),
                writer.wc(_safe_text(item.unit_or_status, "质量单位或状态")),
                wrap_cell(writer, _safe_text(item.note, "质量说明")),
            ])
        apply_quality_conditional_formats(
            quality,
            quality_header_before + 2,
            metadata.quality_items,
        )

        fields = workbook.create_sheet(FIELDS_SHEET)
        _write_generation_info(writer, fields, metadata)
        writer.header_row(fields, FIELD_HEADERS)
        for field in metadata.fields:
            writer.append([
                None,
                writer.wc(_safe_text(field.worksheet, "字段工作表")),
                writer.wc(_safe_text(field.display_name, "显示列名")),
                writer.wc(_safe_text(field.stable_name, "稳定字段名")),
                writer.wc(_safe_text(field.data_type, "字段类型")),
                writer.wc(_safe_text(field.unit, "字段单位")),
                writer.wc(_safe_text(field.nullable, "字段可为空")),
                writer.wc(_safe_text(field.source, "字段来源")),
                wrap_cell(writer, _safe_text(field.metric, "字段口径")),
                _validate_cell_value(field.example, "字段示例"),
                wrap_cell(writer, _safe_text(field.missing_meaning, "缺失值含义")),
            ])
    except BaseException:
        # write_only workbook 在异常后必须主动关闭，否则 openpyxl 的临时 writer
        # 可能一直持有句柄，导致 Windows 无法清理临时文件。
        try:
            workbook.close()
        except Exception:
            pass
        raise
