# -*- coding: utf-8 -*-
"""本地环境诊断与最近错误记录。

诊断只访问当前 Python 进程、文件系统和可导入模块，不启动网络请求、监控服务
或任务流水线。最近错误记录是独立的 best-effort JSON 文件，不改变现有配置格式。
"""
from __future__ import annotations

import importlib
import json
import os
import platform
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import config, output


STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

STATUS_LABELS = {
    STATUS_OK: "正常",
    STATUS_WARNING: "警告",
    STATUS_ERROR: "失败",
}

RECENT_ERROR_FILE = config.CONFIG_DIR / "recent_error.json"
_last_error = None
_last_error_ts = 0.0


@dataclass(frozen=True)
class DiagnosticItem:
    """一项可展示的诊断结果。"""

    name: str
    status: str
    summary: str
    details: str
    suggestion: str

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, "警告")

    def to_dict(self):
        return asdict(self)


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _display_path(path):
    try:
        return str(Path(path).expanduser())
    except (OSError, TypeError, ValueError):
        return str(path)


def output_dir(cfg=None):
    """返回设置页和诊断共用的默认输出目录。"""
    values = cfg or {}
    return Path(values.get("out_dir") or output.default_out_dir())


def _startup_error_file():
    """返回程序自己使用的启动错误文件。"""
    return Path(output.app_base_dir()) / "error.log"


def _nearest_existing_parent(path):
    current = Path(path)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() else None


def _probe_write(path):
    """在目录内创建并立即清理一个临时文件，实际验证写权限。"""
    probe = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".bili-toolbox-check-",
            suffix=".tmp", dir=str(path), delete=False,
        ) as handle:
            handle.write("ok")
            probe = Path(handle.name)
        return True, ""
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def _directory_check(name, path, missing_status, unwritable_status, purpose):
    path = Path(path)
    if not path.exists():
        parent = _nearest_existing_parent(path.parent)
        if parent is None:
            return DiagnosticItem(
                name, STATUS_ERROR,
                f"{purpose}不存在且无法定位可用父目录。",
                _display_path(path),
                "请检查路径设置，或选择一个存在且可写的目录。",
            )
        can_write, error = _probe_write(parent) if parent.is_dir() else (False, "父路径不是目录")
        if can_write:
            return DiagnosticItem(
                name, missing_status,
                f"{purpose}尚未创建，父目录可写。",
                _display_path(path),
                "保存设置或运行任务时会按需创建目录。",
            )
        return DiagnosticItem(
            name, unwritable_status,
            f"{purpose}不存在，且父目录不可写。",
            f"路径：{_display_path(path)}；原因：{error}",
            "请选择可写目录，或修改目录权限。",
        )

    if not path.is_dir():
        return DiagnosticItem(
            name, STATUS_ERROR,
            f"{purpose}路径不是目录。",
            _display_path(path),
            "请改用目录路径，不要指向同名文件。",
        )

    try:
        readable = os.access(path, os.R_OK)
    except OSError:
        readable = False
    if not readable:
        return DiagnosticItem(
            name, STATUS_ERROR,
            f"{purpose}不可读取。",
            _display_path(path),
            "请检查目录权限或选择其他目录。",
        )

    can_write, error = _probe_write(path)
    if not can_write:
        return DiagnosticItem(
            name, unwritable_status,
            f"{purpose}不可写。",
            f"路径：{_display_path(path)}；原因：{error}",
            "请检查目录权限，或在设置中选择其他目录。",
        )
    return DiagnosticItem(
        name, STATUS_OK,
        f"{purpose}存在且可读写。",
        _display_path(path),
        "无需处理。",
    )


def _import_check(name, module_name, importer):
    try:
        module = importer(module_name)
    except Exception as exc:  # 诊断边界：把导入失败展示给用户，不影响应用主流程
        return DiagnosticItem(
            name, STATUS_ERROR,
            "依赖无法导入。",
            sanitize_text(f"{type(exc).__name__}: {exc}"),
            "使用当前 Python 安装 requirements.txt 中对应的依赖后重新检查。",
        )
    version = getattr(module, "__version__", None)
    detail = "导入成功"
    if version:
        detail += f"，版本 {version}"
    return DiagnosticItem(name, STATUS_OK, "依赖可以正常导入。", detail, "无需处理。")


def _config_file_check():
    path = config.CONFIG_FILE
    if not path.exists():
        return DiagnosticItem(
            "主配置文件", STATUS_WARNING,
            "配置文件尚未生成，当前使用默认配置。",
            _display_path(path),
            "保存一次设置即可生成配置文件。",
        )
    if not path.is_file():
        return DiagnosticItem(
            "主配置文件", STATUS_ERROR,
            "配置路径不是文件。",
            _display_path(path),
            "请将该路径恢复为普通 JSON 文件，或备份后移除冲突路径。",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return DiagnosticItem(
            "主配置文件", STATUS_ERROR,
            "配置文件无法读取或 JSON 格式损坏。",
            sanitize_text(f"路径：{path}\n原因：{type(exc).__name__}: {exc}"),
            "备份后修复或移除 config.json；设置页会继续使用默认值。",
        )
    if not isinstance(data, dict):
        return DiagnosticItem(
            "主配置文件", STATUS_ERROR,
            "配置文件内容不是 JSON 对象。",
            _display_path(path),
            "请恢复为键值形式的 JSON 配置文件。",
        )
    return DiagnosticItem(
        "主配置文件", STATUS_OK, "配置文件可以读取。", _display_path(path), "无需处理。"
    )


def _session_file_check():
    path = config.COOKIE_FILE
    if not path.exists():
        return DiagnosticItem(
            "会话配置文件（可选）", STATUS_OK,
            "尚未生成会话文件，首次运行属于正常情况。",
            _display_path(path),
            "无需处理；不会在诊断页面显示会话内容。",
        )
    try:
        raw = path.read_text(encoding="utf-8")
        json.loads(raw)
    except (OSError, ValueError) as exc:
        return DiagnosticItem(
            "会话配置文件（可选）", STATUS_WARNING,
            "会话文件无法读取或格式异常。",
            sanitize_text(f"路径：{path}\n原因：{type(exc).__name__}: {exc}"),
            "如遇到登录或风控问题，可备份后让程序重新生成会话文件。",
        )
    return DiagnosticItem(
        "会话配置文件（可选）", STATUS_OK,
        "会话文件可以读取，内容不会展示。",
        _display_path(path),
        "无需处理。",
    )


def _static_check():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    static_dir = base / "tools" / "monitor" / "static"
    index = static_dir / "index.html"
    if not static_dir.is_dir():
        return DiagnosticItem(
            "监控静态资源", STATUS_ERROR,
            "监控静态资源目录不存在。",
            _display_path(static_dir),
            "检查源码目录或重新打包，确保 tools/monitor/static 被收录。",
        )
    try:
        files = [item for item in static_dir.iterdir() if item.is_file()]
        index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return DiagnosticItem(
            "监控静态资源", STATUS_ERROR,
            "监控静态资源无法读取。",
            sanitize_text(f"路径：{static_dir}\n原因：{type(exc).__name__}: {exc}"),
            "检查 static/index.html 是否存在且可读，或重新打包资源。",
        )
    if not index.is_file() or not files:
        return DiagnosticItem(
            "监控静态资源", STATUS_ERROR,
            "监控静态资源不完整。",
            f"目录：{_display_path(static_dir)}；文件数：{len(files)}",
            "确保 tools/monitor/static/index.html 存在并重新检查。",
        )
    return DiagnosticItem(
        "监控静态资源", STATUS_OK,
        "监控静态资源目录和入口文件可以读取。",
        f"目录：{_display_path(static_dir)}；文件数：{len(files)}",
        "无需处理。",
    )


def _runtime_mode_checks():
    frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return [
            DiagnosticItem(
                "运行模式", STATUS_OK,
                "当前为源码运行模式。",
                f"Python：{_display_path(sys.executable)}",
                "无需处理。",
            ),
            DiagnosticItem(
                "PyInstaller 资源", STATUS_OK,
                "源码模式不需要 _MEIPASS 资源目录。",
                "未启用 PyInstaller 打包运行。",
                "打包后可再次检查 EXE 资源。",
            ),
        ]

    meipass = getattr(sys, "_MEIPASS", None)
    base = Path(meipass) if meipass else None
    if base is None or not base.is_dir():
        return [
            DiagnosticItem(
                "运行模式", STATUS_OK,
                "当前为 PyInstaller 打包运行模式。",
                f"EXE：{_display_path(sys.executable)}",
                "无需处理。",
            ),
            DiagnosticItem(
                "PyInstaller 资源", STATUS_ERROR,
                "_MEIPASS 资源目录无法定位。",
                f"_MEIPASS：{_display_path(meipass or '')}",
                "请重新打包并检查启动时的资源目录。",
            ),
        ]

    missing = [name for name in ("PySide6", "shiboken6") if not (base / name).is_dir()]
    if missing:
        detail = f"_MEIPASS：{_display_path(base)}；缺少：{', '.join(missing)}"
        status = STATUS_ERROR
        summary = "PyInstaller 资源目录存在，但 Qt 运行资源不完整。"
        suggestion = "重新打包并确保 PySide6、shiboken6 资源被收录。"
    else:
        detail = f"_MEIPASS：{_display_path(base)}；PySide6、shiboken6 目录均存在。"
        status = STATUS_OK
        summary = "PyInstaller 资源可以定位。"
        suggestion = "无需处理。"
    return [
        DiagnosticItem(
            "运行模式", STATUS_OK,
            "当前为 PyInstaller 打包运行模式。",
            f"EXE：{_display_path(sys.executable)}",
            "无需处理。",
        ),
        DiagnosticItem("PyInstaller 资源", status, summary, detail, suggestion),
    ]


def collect_diagnostics(cfg=None, importer=None):
    """执行所有本地诊断，返回 DiagnosticItem 列表。"""
    importer = importer or importlib.import_module
    executable = _display_path(sys.executable)
    version = platform.python_version()
    items = [
        DiagnosticItem(
            "Python 解释器", STATUS_OK,
            f"Python {version} 正在运行。",
            f"路径：{executable}\n版本：{sys.version.split()[0]}",
            "无需处理。",
        )
    ]

    implementation = platform.python_implementation()
    lowered = executable.lower()
    suspicious = any(marker in lowered for marker in ("windowsapps", "libreoffice"))
    if implementation != "CPython":
        items.append(DiagnosticItem(
            "官方 Python", STATUS_WARNING,
            f"当前实现为 {implementation}，无法确认是官方 CPython。",
            executable,
            "建议使用官方 CPython 安装运行工具箱。",
        ))
    elif suspicious:
        items.append(DiagnosticItem(
            "官方 Python", STATUS_WARNING,
            "当前是 CPython，但路径看起来不是推荐的官方 Python 安装。",
            executable,
            "请优先使用官方 CPython，避免 WindowsApps 或 LibreOffice 内置 Python。",
        ))
    else:
        items.append(DiagnosticItem(
            "官方 Python", STATUS_OK,
            "当前为 CPython，路径未发现已知替代运行时标记。",
            executable,
            "无需处理。",
        ))

    dependencies = [
        ("PySide6", "PySide6"),
        ("qtawesome", "qtawesome"),
        ("curl_cffi", "curl_cffi"),
        ("grpc", "grpc"),
        ("protobuf", "google.protobuf"),
        ("openpyxl", "openpyxl"),
    ]
    items.extend(_import_check(name, module, importer) for name, module in dependencies)
    items.append(_directory_check(
        "配置目录", config.CONFIG_DIR, STATUS_WARNING, STATUS_ERROR, "配置目录"
    ))
    items.append(_directory_check(
        "默认输出目录", output_dir(cfg), STATUS_WARNING, STATUS_WARNING, "输出目录"
    ))
    items.append(_static_check())
    items.extend(_runtime_mode_checks())
    items.append(_config_file_check())
    items.append(_session_file_check())
    return items


def format_diagnostics(items, redact=True):
    """将诊断结果格式化为可复制文本，默认脱敏。"""
    formatter = sanitize_text if redact else str
    lines = [
        "B站工具箱环境诊断",
        f"检查时间：{_now_iso()}",
        "",
    ]
    for item in items:
        lines.extend([
            f"[{item.status_label}] {item.name}",
            f"说明：{formatter(item.summary)}",
            f"详情：{formatter(item.details)}",
            f"建议：{formatter(item.suggestion)}",
            "",
        ])
    return "\n".join(lines).rstrip()


_SECRET_KEY_PATTERN = (
    r"(?:set-cookie|access_token|refresh_token|authorization|sessdata|"
    r"bili_jct|proxy[_ -]?(?:username|user|password)|cookie|bearer|"
    r"token|csrf|password|代理用户名|代理密码)"
)
_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    rf"(?P<key_quote>['\"]?)(?P<key>{_SECRET_KEY_PATTERN})(?P=key_quote)"
    r"(?P<separator>\s*[:=：]\s*)"
    r"(?:"
    r"(?P<value_quote>['\"])(?P<quoted_value>.*?)(?P=value_quote)"
    r"|(?P<bare_value>(?:bearer\s+)?[^\s,;，；\r\n'\"}]+)"
    r")"
)
_URL_RE = re.compile(r"(?i)\b(?:https?|socks5?)://[^\s<>\"']+")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)\b[A-Z]:\\[^\r\n,;，；<>\"']+|\\\\[^\r\n,;，；<>\"']+"
)


def sanitize_text(value):
    """脱敏错误详情，不展示凭据、完整 URL、IP 或本地完整路径。"""
    text = str(value or "")[:16000]

    def redact_secret(match):
        key_quote = match.group("key_quote")
        value_quote = match.group("value_quote") or ""
        return (
            f"{key_quote}{match.group('key')}{key_quote}"
            f"{match.group('separator')}{value_quote}[已脱敏]{value_quote}"
        )

    text = _SECRET_RE.sub(redact_secret, text)
    text = _URL_RE.sub("[网络地址已脱敏]", text)
    text = _IP_RE.sub("[地址已脱敏]", text)
    text = _WINDOWS_PATH_RE.sub("[本地路径已脱敏]", text)
    return text


def _error_record(timestamp, source, summary, details, state="history"):
    return {
        "timestamp": timestamp,
        "source": sanitize_text(source),
        "summary": sanitize_text(summary),
        "details": sanitize_text(details),
        "state": state,
    }


def record_error(source, summary, details, timestamp=None):
    """记录最近一次任务错误；持久化失败不影响原任务错误处理。"""
    global _last_error, _last_error_ts
    record = _error_record(
        timestamp or _now_iso(), source, summary, details, state="current"
    )
    _last_error = record
    _last_error_ts = datetime.now().timestamp()

    temp_path = None
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".recent-error-",
            suffix=".tmp", dir=str(config.CONFIG_DIR), delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(record, handle, ensure_ascii=False, indent=2)
        temp_path.replace(RECENT_ERROR_FILE)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return record


def _read_persisted_error():
    if not RECENT_ERROR_FILE.is_file():
        return None
    try:
        data = json.loads(RECENT_ERROR_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("details"):
        return None
    return _error_record(
        data.get("timestamp") or _now_iso(),
        data.get("source") or "未知模块",
        data.get("summary") or "任务执行失败",
        data.get("details"),
        state="history",
    )


def _read_startup_error():
    path = _startup_error_file()
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except (OSError, UnicodeError):
        return None
    if not raw.strip():
        return None
    return _error_record(
        stamp, "应用启动（历史）", "历史启动错误", raw, state="history"
    )


def clear_startup_error():
    """只清理程序自己生成的启动错误文件。"""
    path = _startup_error_file()
    if path.name != "error.log":
        return False
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def clear_recent_error():
    """清理最近错误记录和程序自己的启动错误文件。"""
    global _last_error, _last_error_ts
    cleared = False
    path = Path(RECENT_ERROR_FILE)
    if path.name == "recent_error.json":
        try:
            if path.is_file():
                path.unlink()
                cleared = True
        except OSError:
            pass
    cleared = clear_startup_error() or cleared
    _last_error = None
    _last_error_ts = 0.0
    return cleared


def _record_time(record):
    try:
        return datetime.fromisoformat(record["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0


def load_recent_error():
    """读取当前进程、任务记录和启动 error.log 中时间最新的一项。"""
    candidates = []
    if _last_error is not None:
        candidates.append((_last_error_ts, _last_error))
    persisted = _read_persisted_error()
    if persisted is not None:
        try:
            mtime = RECENT_ERROR_FILE.stat().st_mtime
        except OSError:
            mtime = _record_time(persisted)
        candidates.append((mtime, persisted))
    startup = _read_startup_error()
    if startup is not None:
        candidates.append((_record_time(startup), startup))
    if not candidates:
        return None
    return dict(max(candidates, key=lambda item: item[0])[1])


def format_recent_error(record):
    if not record:
        return "暂无错误记录"
    state = "当前任务错误" if record.get("state") == "current" else "历史错误"
    return "\n".join([
        f"记录类型：{state}",
        f"错误时间：{record.get('timestamp', '未知')}",
        f"来源模块：{sanitize_text(record.get('source', '未知模块'))}",
        f"说明：{sanitize_text(record.get('summary', '任务执行失败'))}",
        "详细信息：",
        sanitize_text(record.get("details", "")),
    ])
