# -*- coding: utf-8 -*-
"""任务历史的本地、脱敏、best-effort 存储。"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import config, diagnostics


SCHEMA_VERSION = 1
MAX_RECORDS = 100
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
HISTORY_FILE = config.CONFIG_DIR / "task_history.json"

_REQUIRED_FIELDS = {
    "id", "tool_id", "tool_name", "started_at", "finished_at", "status",
    "target_summary", "output_dir", "outputs", "reusable_params", "reusable",
    "error",
}
_ALLOWED_STATUSES = {"running", *TERMINAL_STATUSES}
_SENSITIVE_KEY_RE = re.compile(
    r"(?:cookie|sessdata|bili[_ -]?jct|token|authorization|bearer|csrf|"
    r"password|passwd|secret|api[_ -]?key|access[_ -]?key|"
    r"proxy[_ -]?(?:account|user(?:name)?|pass(?:word)?))",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEYS = _SENSITIVE_KEY_RE
_SENSITIVE_TEXT_RE = re.compile(
    r"(?ix)"
    r"(?<![a-z0-9_])['\"]?(?:cookie|sessdata|bili[_ -]?jct|"
    r"authorization|bearer|token|access[_ -]?token|refresh[_ -]?token|csrf|"
    r"password|passwd|secret|api[_ -]?key|"
    r"proxy[_ -]?(?:account|user(?:name)?|pass(?:word)?))['\"]?"
    r"\s*(?:=|:|：)\s*"
    r"(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|"
    r"(?:bearer\s+)?[^\s,;，；\]\[()<>}\"']+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)(?<![a-z0-9_])bearer[ \t]+\S+")
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")


@dataclass(frozen=True)
class HistoryWriteResult:
    """返回记录及本次写盘是否成功；写盘失败仍保留内存中的记录。"""

    record: dict
    persisted: bool


def history_path():
    """返回当前历史文件路径，供界面和测试使用。"""
    return Path(HISTORY_FILE)


def output_exists(path):
    """在界面显示时重新检查结果文件是否仍存在。"""
    return Path(path).exists()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _time_key(record):
    try:
        return datetime.fromisoformat(str(record.get("started_at", ""))).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _safe_scalar(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("参数包含不可保存的对象")


def _sensitive_url(value):
    """检查 URL 用户信息、查询凭据和代理凭据，不记录原值。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if _SENSITIVE_QUERY_KEYS.search(key):
            return True
    return False


def _key_is_sensitive(key, proxy_context=False):
    text = str(key or "")
    if _SENSITIVE_KEY_RE.search(text):
        return True
    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return proxy_context and normalized in {
        "account", "user", "username", "pass", "password", "passwd",
    }


def contains_sensitive_text(value, proxy_context=False):
    """递归检查待落盘值，覆盖键名、普通文本、URL 和嵌套容器。"""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _key_is_sensitive(key_text, proxy_context):
                return True
            if contains_sensitive_text(
                child,
                proxy_context or key_text.strip().lower() == "proxy",
            ):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_text(item, proxy_context) for item in value)
    if not isinstance(value, str):
        return False
    return bool(
        _SENSITIVE_TEXT_RE.search(value)
        or _BEARER_TOKEN_RE.search(value)
        or _sensitive_url(value)
    )


def _prepare_value(value, key=None, proxy_context=False):
    if key is not None and _key_is_sensitive(key, proxy_context):
        raise ValueError("参数字段可能包含敏感信息")
    if isinstance(value, dict):
        return {
            str(child_key): _prepare_value(
                child_value,
                child_key,
                proxy_context or str(child_key).strip().lower() == "proxy",
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_prepare_value(item) for item in value]
    scalar = _safe_scalar(value)
    if isinstance(scalar, str) and contains_sensitive_text(scalar):
        raise ValueError("参数 URL 可能包含凭据或敏感查询参数")
    return scalar


def _redact_persistent_text(value):
    """先走现有错误脱敏，再兜底移除本模块识别到的敏感表达式。"""
    text = diagnostics.sanitize_text(value)
    text = _SENSITIVE_TEXT_RE.sub("[已脱敏]", text)
    text = _BEARER_TOKEN_RE.sub("[已脱敏]", text)

    def redact_url(match):
        return "[网络地址已脱敏]" if _sensitive_url(match.group(0)) else match.group(0)

    return _URL_RE.sub(redact_url, text)


def prepare_reusable_params(params):
    """只接受页面白名单产生的 JSON 值；不安全时整组参数不落盘。"""
    try:
        safe = _prepare_value(dict(params or {}))
        json.dumps(safe, ensure_ascii=False)
    except (TypeError, ValueError):
        return {}, False
    return safe, True


def _sanitize_error(error):
    if not error:
        return None
    if isinstance(error, dict):
        return {
            "timestamp": _redact_persistent_text(error.get("timestamp", "")),
            "source": _redact_persistent_text(error.get("source", "未知模块")),
            "summary": _redact_persistent_text(error.get("summary", "任务执行失败")),
            "details": _redact_persistent_text(error.get("details", "")),
        }
    return {
        "timestamp": now_iso(),
        "source": "任务执行",
        "summary": "任务执行失败",
        "details": _redact_persistent_text(error),
    }


def _safe_output_dir(value):
    text = str(value or "")
    return "" if contains_sensitive_text(text) else text


def _safe_output_paths(outputs):
    if not isinstance(outputs, (list, tuple)):
        return []
    safe = []
    for path in outputs:
        text = str(path or "")
        if text and not contains_sensitive_text(text):
            safe.append(text)
    return safe


def _prepare_record_for_disk(record):
    """生成只含固定字段、且已通过最终隐私边界的记录。"""
    if not isinstance(record, dict):
        return None
    safe_params, reusable = ({}, False)
    if record.get("reusable"):
        safe_params, reusable = prepare_reusable_params(record.get("reusable_params"))
    safe_record = {
        "id": _redact_persistent_text(record.get("id", "")),
        "tool_id": _redact_persistent_text(record.get("tool_id", "")),
        "tool_name": _redact_persistent_text(record.get("tool_name", "")),
        "started_at": _redact_persistent_text(record.get("started_at", "")),
        "finished_at": (
            None if record.get("finished_at") is None
            else _redact_persistent_text(record.get("finished_at"))
        ),
        "status": _redact_persistent_text(record.get("status", "running")),
        "target_summary": _redact_persistent_text(record.get("target_summary", "")),
        "output_dir": _safe_output_dir(record.get("output_dir", "")),
        "outputs": _safe_output_paths(record.get("outputs", [])),
        "reusable_params": safe_params,
        "reusable": reusable,
        "error": _sanitize_error(record.get("error")),
    }
    return None if contains_sensitive_text(safe_record) else safe_record


def _normalize_record(record):
    if not isinstance(record, dict) or not _REQUIRED_FIELDS.issubset(record):
        return None
    if not isinstance(record.get("id"), str) or not record["id"]:
        return None
    if record.get("status") not in _ALLOWED_STATUSES:
        return None
    if not isinstance(record.get("started_at"), str):
        return None
    if record.get("finished_at") is not None and not isinstance(record.get("finished_at"), str):
        return None
    if not isinstance(record.get("outputs"), list):
        return None
    if not isinstance(record.get("reusable_params"), dict):
        return None
    if not isinstance(record.get("reusable"), bool):
        return None
    return _prepare_record_for_disk(record)


def load_history():
    """读取可用记录；损坏文件或坏记录只得到空/部分历史，不阻塞启动。"""
    path = history_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return []
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        return []
    records = []
    for raw in raw_records:
        record = _normalize_record(raw)
        if record is not None:
            records.append(record)
    records.sort(key=_time_key, reverse=True)
    return records[:MAX_RECORDS]


def save_history(records):
    """用同目录临时文件和 replace 原子写入；失败返回 False。"""
    path = history_path()
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_records = []
        for record in list(records)[:MAX_RECORDS]:
            safe_record = _prepare_record_for_disk(record)
            if safe_record is not None:
                safe_records.append(safe_record)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "records": safe_records,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".task-history-",
            suffix=".tmp", dir=str(path.parent), delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
        temp_path.replace(path)
        return True
    except (OSError, TypeError, ValueError):
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def create_record(tool_id, tool_name, started_at=None, target_summary="",
                  output_dir="", reusable_params=None):
    """创建 running 记录；不安全白名单参数会使记录不可复用。"""
    safe_params, reusable = prepare_reusable_params(reusable_params or {})
    record = {
        "id": uuid.uuid4().hex,
        "tool_id": str(tool_id or ""),
        "tool_name": str(tool_name or ""),
        "started_at": started_at or now_iso(),
        "finished_at": None,
        "status": "running",
        "target_summary": diagnostics.sanitize_text(target_summary),
        "output_dir": _safe_output_dir(output_dir),
        "outputs": [],
        "reusable_params": safe_params,
        "reusable": reusable,
        "error": None,
    }
    records = load_history()
    records.insert(0, record)
    persisted = save_history(records)
    return HistoryWriteResult(record=record, persisted=persisted)


def update_record(record_id, status, finished_at=None, outputs=None, error=None):
    """更新同一条记录；取消/中断显式清空错误字段。"""
    records = load_history()
    for record in records:
        if record.get("id") != record_id:
            continue
        record["status"] = status if status in _ALLOWED_STATUSES else "failed"
        if record["status"] in TERMINAL_STATUSES:
            record["finished_at"] = finished_at or now_iso()
        if outputs is not None:
            record["outputs"] = _safe_output_paths(outputs)
        record["error"] = _sanitize_error(error) if record["status"] == "failed" else None
        return save_history(records)
    return False


def clear_history():
    """只删除程序自己的 task_history.json。"""
    path = history_path()
    if path.name != "task_history.json":
        return False
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    return False
