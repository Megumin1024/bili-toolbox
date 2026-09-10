# -*- coding: utf-8 -*-
"""本地任务参数预设的独立存储与隐私边界。"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from . import config, task_history


SCHEMA_VERSION = 1
MAX_PRESETS = 100
MAX_NAME_LENGTH = 40
PRESETS_FILE = config.CONFIG_DIR / "task_presets.json"


class PresetNameConflict(ValueError):
    """同一工具下已经存在同名预设。"""


class PresetStorageError(RuntimeError):
    """预设文件无法原子写入。"""


def preset_path():
    """返回当前预设文件路径，供界面和测试使用。"""
    return Path(PRESETS_FILE)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _time_key(preset):
    try:
        return datetime.fromisoformat(str(preset.get("updated_at", ""))).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _validate_name(name):
    value = str(name or "").strip()
    if not value:
        raise ValueError("预设名称不能为空")
    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"预设名称最多 {MAX_NAME_LENGTH} 个字符")
    return value


def _normalize_preset(record):
    if not isinstance(record, dict):
        return None
    required = {"id", "name", "tool_id", "tool_name", "created_at", "updated_at", "params"}
    if not required.issubset(record):
        return None
    if not all(isinstance(record.get(key), str) for key in (
        "id", "name", "tool_id", "tool_name", "created_at", "updated_at",
    )):
        return None
    if not record["id"] or not record["tool_id"] or not record["tool_name"]:
        return None
    try:
        name = _validate_name(record["name"])
    except ValueError:
        return None
    params = record.get("params")
    if not isinstance(params, dict):
        return None
    safe_params, reusable = task_history.prepare_reusable_params(params)
    if not reusable:
        return None
    return {
        "id": record["id"],
        "name": name,
        "tool_id": record["tool_id"],
        "tool_name": record["tool_name"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "params": safe_params,
    }


def _normalize_records(records):
    normalized = []
    seen_ids = set()
    seen_names = set()
    for raw in list(records or []):
        preset = _normalize_preset(raw)
        name_key = (preset["tool_id"], preset["name"]) if preset else None
        if (preset is None or preset["id"] in seen_ids
                or name_key in seen_names):
            continue
        seen_ids.add(preset["id"])
        seen_names.add(name_key)
        normalized.append(preset)
    normalized.sort(key=_time_key, reverse=True)
    return normalized[:MAX_PRESETS]


def load_presets(tool_id=None):
    """读取可用预设；文件或单条记录损坏时只返回可用部分。"""
    path = preset_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return []
    raw_presets = payload.get("presets")
    if not isinstance(raw_presets, list):
        return []
    presets = _normalize_records(raw_presets)
    if tool_id is not None:
        presets = [preset for preset in presets if preset["tool_id"] == str(tool_id)]
    return presets


def save_presets(records):
    """用同目录临时文件和 replace 原子写入；失败时保留原文件。"""
    path = preset_path()
    temp_path = None
    try:
        safe_records = _normalize_records(records)
        payload = {"schema_version": SCHEMA_VERSION, "presets": safe_records}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".task-presets-",
            suffix=".tmp", dir=str(path.parent), delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        return True
    except (OSError, TypeError, ValueError):
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def create_or_update_preset(name, tool_id, tool_name, params, overwrite=False):
    """创建预设；同工具同名时只有显式 overwrite 才会替换。"""
    clean_name = _validate_name(name)
    clean_tool_id = str(tool_id or "").strip()
    clean_tool_name = str(tool_name or "").strip()
    if not clean_tool_id or not clean_tool_name:
        raise ValueError("预设缺少工具信息")
    safe_params, reusable = task_history.prepare_reusable_params(params or {})
    if not reusable:
        raise ValueError(
            "参数包含 Cookie、Token、Authorization、Bearer、密码或代理凭据，未保存预设"
        )

    records = load_presets()
    existing = next(
        (preset for preset in records
         if preset["tool_id"] == clean_tool_id and preset["name"] == clean_name),
        None,
    )
    if existing is not None and not overwrite:
        raise PresetNameConflict("同一工具下已经存在同名预设")

    timestamp = now_iso()
    record = {
        "id": existing["id"] if existing else uuid.uuid4().hex,
        "name": clean_name,
        "tool_id": clean_tool_id,
        "tool_name": clean_tool_name,
        "created_at": existing["created_at"] if existing else timestamp,
        "updated_at": timestamp,
        "params": safe_params,
    }
    if existing is None:
        records.append(record)
    else:
        records = [record if preset["id"] == existing["id"] else preset for preset in records]
    if not save_presets(records):
        raise PresetStorageError("预设文件写入失败，原文件未改变")
    return record


def delete_preset(preset_id):
    """只删除指定预设记录，不触碰任务历史、输出或其他配置。"""
    records = load_presets()
    remaining = [preset for preset in records if preset["id"] != str(preset_id)]
    if len(remaining) == len(records):
        return False
    if not save_presets(remaining):
        raise PresetStorageError("预设文件写入失败，未删除预设")
    return True
