# -*- coding: utf-8 -*-
"""应用配置持久化（JSON）。Windows 存 %APPDATA%/BiliToolbox/config.json。"""
import json
import os
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
        CONFIG_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass
