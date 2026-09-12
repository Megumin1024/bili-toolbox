# -*- coding: utf-8 -*-
"""关系分析纯计算内核：本地 CSV/JSON 清单解析 + 集合运算。

本工具**零网络**：只处理用户自行取得的本地文件，不 import 任何网络模块。
解析规则（任务卡定稿）：
- CSV 首行表头，编码依次尝试 utf-8-sig → utf-8 → gbk；
- JSON 为对象数组，键名走同一套同义映射；
- mid：mid/uid/id；昵称：uname/昵称/name/nickname；
  时间：mtime/ptime/ctime/follow_time/关注时间；
- 时间值支持 epoch 秒与 YYYY-MM-DD[ HH:MM:SS]，解析失败置空不丢行；
- 坏行（缺 mid、mid 非数字）跳过并计数；按 mid 去重保留首次出现；
- 上限：单文件 ≤ 50MB 且 ≤ 1,000,000 行，超限明确报错，不做部分处理。
"""
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ROWS = 1_000_000
# 大文件解析循环的取消/进度检查间隙：逐行查太浪费，8192 行一次足够及时
_CANCEL_GAP = 8192

MID_KEYS = ("mid", "uid", "id")
UNAME_KEYS = ("uname", "昵称", "name", "nickname")
TIME_KEYS = ("mtime", "ptime", "ctime", "follow_time", "关注时间")

DASH = "—"
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


class Cancelled(Exception):
    """解析循环中检测到取消信号；由 pipeline 捕获并返回正常取消路径。"""


def _s(value, dash=DASH):
    """用户可见文本里绝不放 None / 空白。"""
    if value is None:
        return dash
    text = str(value).strip()
    return text or dash


def _norm_key(name):
    return str(name or "").strip().lower()


def _to_mid(value):
    """mid 必须是整数；其余（空、非数字、bool）一律 None（记坏行）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None


def _from_epoch(value):
    try:
        return datetime.fromtimestamp(value)
    except (OSError, OverflowError, ValueError):
        return None


def parse_time_value(value):
    """epoch 秒（int/float/数字串）或 YYYY-MM-DD[ HH:MM:SS]；失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return _from_epoch(float(text))
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_time(value):
    """时间列渲染：datetime → 文本；空值交给 _s() 兜底。"""
    if value is None:
        return _s(None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Roster:
    """一份解析后的清单：已按 mid 去重，坏行只计数不占位。"""

    label: str
    source_name: str = ""
    rows: list = field(default_factory=list)   # {"mid": int, "name": str, "time": datetime|None}
    data_rows: int = 0                          # 读到的数据行总数（含坏行/重复）
    has_time: bool = False                      # 来源是否存在时间列
    bad_rows: int = 0
    duplicates: int = 0

    def summary_note(self):
        parts = [f"{len(self.rows):,} 人"]
        if self.bad_rows:
            parts.append(f"坏行 {self.bad_rows:,}")
        if self.duplicates:
            parts.append(f"去重 {self.duplicates:,}")
        return " · ".join(parts)


def _find_column(columns, keys):
    for key in keys:
        for index, name in enumerate(columns):
            if name == key:
                return index
    return None


def _decode_text(data, label):
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{label}：无法识别文件编码（已尝试 UTF-8 / GBK）")


def _row_time(cells, time_idx):
    if time_idx is None or time_idx >= len(cells):
        return None
    return parse_time_value(cells[time_idx])


def _load_csv(text, label, cancel):
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        raise ValueError(f"{label}：文件为空（没有表头行）")
    columns = [_norm_key(cell) for cell in header]
    mid_idx = _find_column(columns, MID_KEYS)
    if mid_idx is None:
        raise ValueError(
            f"{label}：表头缺少 mid 列（可用列名：{' / '.join(MID_KEYS)}）")
    name_idx = _find_column(columns, UNAME_KEYS)
    time_idx = _find_column(columns, TIME_KEYS)
    roster = Roster(label=label, has_time=time_idx is not None)
    seen = set()
    for cells in reader:
        roster.data_rows += 1
        if roster.data_rows > MAX_ROWS:
            raise ValueError(
                f"{label}：数据行超过 {MAX_ROWS:,} 行上限，不做部分处理")
        if roster.data_rows % _CANCEL_GAP == 0 and cancel():
            raise Cancelled(label)
        if not cells or all(not str(cell).strip() for cell in cells):
            continue
        mid = _to_mid(cells[mid_idx] if mid_idx < len(cells) else None)
        if mid is None:
            roster.bad_rows += 1
            continue
        if mid in seen:
            roster.duplicates += 1
            continue
        seen.add(mid)
        name = ""
        if name_idx is not None and name_idx < len(cells):
            name = str(cells[name_idx] or "").strip()
        roster.rows.append({
            "mid": mid,
            "name": name,
            "time": _row_time(cells, time_idx),
        })
    return roster


def _load_json(text, label, cancel):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}：JSON 解析失败：{exc}") from None
    if not isinstance(payload, list):
        raise ValueError(f"{label}：JSON 应为对象数组（顶层不是数组）")
    if len(payload) > MAX_ROWS:
        raise ValueError(
            f"{label}：数据行超过 {MAX_ROWS:,} 行上限，不做部分处理")
    roster = Roster(label=label)
    saw_mid_key = False
    seen = set()
    for index, element in enumerate(payload, 1):
        roster.data_rows += 1
        if index % _CANCEL_GAP == 0 and cancel():
            raise Cancelled(label)
        if not isinstance(element, dict):
            roster.bad_rows += 1
            continue
        norm_map = {_norm_key(key): value for key, value in element.items()}
        mid = None
        for key in MID_KEYS:
            if key in norm_map:
                saw_mid_key = True
                mid = _to_mid(norm_map[key])
                break
        if any(key in norm_map for key in TIME_KEYS):
            roster.has_time = True
        if mid is None:
            roster.bad_rows += 1
            continue
        if mid in seen:
            roster.duplicates += 1
            continue
        seen.add(mid)
        name = ""
        for key in UNAME_KEYS:
            if key in norm_map:
                value = norm_map[key]
                name = "" if value is None else str(value).strip()
                break
        time_value = None
        for key in TIME_KEYS:
            if key in norm_map:
                time_value = parse_time_value(norm_map[key])
                break
        roster.rows.append({"mid": mid, "name": name, "time": time_value})
    if payload and not saw_mid_key:
        raise ValueError(
            f"{label}：JSON 对象缺少 mid 键（可用键名：{' / '.join(MID_KEYS)}）")
    return roster


def load_roster(path, label, cancel=None, progress=None):
    """读取并解析一份清单文件；编码回退、列名映射、去重、上限与坏行规则见模块注释。"""
    cancel = cancel or (lambda: False)
    progress = progress or (lambda **kw: None)
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"{label}：文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MB 上限"
            f"（{size:,} 字节），不做部分处理")
    if cancel():
        raise Cancelled(label)
    progress(text=f"正在解析 {label}：{path.name}")
    data = path.read_bytes()
    text = _decode_text(data, label)
    if path.suffix.lower() == ".json":
        roster = _load_json(text, label, cancel)
    else:
        roster = _load_csv(text, label, cancel)
    roster.source_name = path.name
    progress(text=f"{label}：{roster.summary_note()}")
    return roster


def validate_inputs(fans_t1, follows_t1, fans_t2, follows_t2, out_dir):
    """校验页面参数：至少一个文件、扩展名、存在性、输出目录。"""
    slots = (
        ("fans_t1", "时点 1 · 粉丝清单", fans_t1),
        ("follows_t1", "时点 1 · 关注清单", follows_t1),
        ("fans_t2", "时点 2 · 粉丝清单", fans_t2),
        ("follows_t2", "时点 2 · 关注清单", follows_t2),
    )
    paths = {}
    for key, label, raw in slots:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.exists() or not path.is_file():
            raise ValueError(f"{label}：文件不存在或不是普通文件（{path.name or text}）")
        if path.suffix.lower() not in {".csv", ".json"}:
            raise ValueError(f"{label}：只支持 .csv 和 .json 文件（扩展名不区分大小写）")
        paths[key] = path.resolve(strict=False)
    if not paths:
        raise ValueError("请至少选择一个清单文件（时点 2 可留空，做单时点分析）")
    out_text = str(out_dir or "").strip()
    if not out_text:
        raise ValueError("请设置输出目录")
    target = Path(out_text).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise NotADirectoryError(str(target))
    except OSError as exc:
        raise ValueError(f"输出目录不可用：{_s(exc)}") from None
    return paths, target.resolve(strict=False)


def compute_analysis(rosters):
    """集合运算 + 快照差异 + 按月分布。

    主时点：优先时点 1；只提供时点 2 时以时点 2 为主（清单表与互关/仅粉丝/
    仅关注按主时点计算）。快照差异固定为「时点 2 粉丝相对时点 1 粉丝」，
    需两份粉丝清单齐备。按月分布仅当时点 1 粉丝清单存在时间列时输出。
    """
    fans1 = rosters.get("fans_t1")
    follows1 = rosters.get("follows_t1")
    fans2 = rosters.get("fans_t2")
    follows2 = rosters.get("follows_t2")

    fans_key = ("fans_t1" if fans1 is not None
                else "fans_t2" if fans2 is not None else None)
    follows_key = ("follows_t1" if follows1 is not None
                   else "follows_t2" if follows2 is not None else None)
    result = {
        "primary_fans_key": fans_key,
        "primary_follows_key": follows_key,
        "primary_fans_label": {"fans_t1": "时点 1", "fans_t2": "时点 2"}.get(fans_key, ""),
        "primary_follows_label": {
            "follows_t1": "时点 1", "follows_t2": "时点 2"}.get(follows_key, ""),
        "mutual_rows": None,
        "only_fans_rows": None,
        "only_follows_rows": None,
        "diff_added_rows": None,
        "diff_removed_rows": None,
        "diff_unchanged": None,
        "monthly": None,
        "monthly_unparsed": 0,
    }

    fans_roster = rosters.get(fans_key)
    follows_roster = rosters.get(follows_key)
    fans_by_mid = {row["mid"]: row for row in fans_roster.rows} if fans_roster else {}
    follows_by_mid = {row["mid"]: row for row in follows_roster.rows} if follows_roster else {}
    if fans_roster is not None and follows_roster is not None:
        result["mutual_rows"] = [
            fans_by_mid[mid] for mid in sorted(fans_by_mid.keys() & follows_by_mid.keys())]
        result["only_fans_rows"] = [
            fans_by_mid[mid] for mid in sorted(fans_by_mid.keys() - follows_by_mid.keys())]
        result["only_follows_rows"] = [
            follows_by_mid[mid] for mid in sorted(follows_by_mid.keys() - fans_by_mid.keys())]

    if fans1 is not None and fans2 is not None:
        t1_mids = {row["mid"] for row in fans1.rows}
        t2_mids = {row["mid"] for row in fans2.rows}
        t1_by_mid = {row["mid"]: row for row in fans1.rows}
        t2_by_mid = {row["mid"]: row for row in fans2.rows}
        result["diff_added_rows"] = [t2_by_mid[mid] for mid in sorted(t2_mids - t1_mids)]
        result["diff_removed_rows"] = [t1_by_mid[mid] for mid in sorted(t1_mids - t2_mids)]
        result["diff_unchanged"] = len(t1_mids & t2_mids)

    if fans1 is not None and fans1.has_time:
        counter = {}
        unparsed = 0
        for row in fans1.rows:
            time_value = row["time"]
            if time_value is None:
                unparsed += 1
                continue
            month = time_value.strftime("%Y-%m")
            counter[month] = counter.get(month, 0) + 1
        result["monthly"] = sorted(counter.items())
        result["monthly_unparsed"] = unparsed
    return result
