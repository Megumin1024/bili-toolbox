# -*- coding: utf-8 -*-
"""应用配置持久化（JSON）。Windows 存 %APPDATA%/BiliToolbox/config.json。"""
import json
import os
import tempfile
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "BiliToolbox"
CONFIG_FILE = CONFIG_DIR / "config.json"
COOKIE_FILE = CONFIG_DIR / "session_cookies.json"  # 风控会话 cookie 持久化

DEFAULTS = {
    "out_dir": "",        # 空 = 自动（core.output.default_out_dir）
    "theme": "dark",      # dark | light
    "proxy_spec": "",     # 例: "direct,socks5://127.0.0.1:7890,http://u:p@1.2.3.4:8080"
    "transport": "auto",  # auto | h2-ja3 | urllib
}


def atomic_write_text(path, text):
    """在目标文件同目录写临时文件，再用原子替换提交内容。

    调用方决定如何处理 OSError；本函数只保证失败时尽量清理本轮临时文件，
    不会主动打开或截断旧文件。
    """
    target = Path(path)
    temp_path = None
    fd = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp",
            dir=str(target.parent))
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def load():
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        current = load()
        current.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        atomic_write_text(
            CONFIG_FILE,
            json.dumps(current, ensure_ascii=False, indent=2),
        )
    except OSError:
        pass
