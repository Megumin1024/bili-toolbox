# -*- coding: utf-8 -*-
"""文本脱敏内核：把可能外泄的凭据/地址/路径从任意文本里抹掉。

放在独立模块而非 core.diagnostics：网络层（core.client）、GUI 出口
（app.task_runner）与监控接口（tools.monitor.server）都要用它，
而这些位置不该依赖诊断报告模块——那会把依赖方向拧成环。

约束：纯函数、无 I/O、无副作用，只依赖标准库。
脱敏是**粗粒度**的：命中的 URL/IP/路径整段打码，不做部分保留——
宁可多糊，不可漏出。
"""
from __future__ import annotations

import re

# 需要整段抹掉的敏感键名。命中即「键 + 分隔符 + [已脱敏]」。
_SECRET_KEY_PATTERN = (
    r"(?:set-cookie|access_token|refresh_token|authorization|sessdata|"
    r"bili_jct|proxy[_ -]?(?:username|user|password)|cookie|bearer|"
    r"token|csrf|password|代理用户名|代理密码|"
    # 网络层与 B 站风控链路会带上的其它凭据 / 签名 / 设备标识
    r"v_voucher|voucher|access[_ -]?key|buvid[0-9a-z_]*|w_rid|wts|"
    r"gaia_vtoken|grisk[0-9a-z_]*|bili_ticket|_uuid)"
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
# 无 scheme 的 user:pass@host —— 代理串混进异常文本时常见此形态，
# 上面的 _URL_RE 抓不到，_IP_RE 又只会糊掉主机部分，账号密码会原样留下。
_USERINFO_RE = re.compile(
    r"(?<![A-Za-z0-9_@/])"
    r"[^\s:/@]{1,64}:[^\s:/@]{1,64}@"
    r"(?=[A-Za-z0-9.\-]+(?::\d{1,5})?)"
)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)\b[A-Z]:\\[^\r\n,;，；<>\"']+|\\\\[^\r\n,;，；<>\"']+"
)

_MAX_LENGTH = 16000


def sanitize_text(value):
    """脱敏错误详情，不展示凭据、完整 URL、IP 或本地完整路径。"""
    text = str(value or "")[:_MAX_LENGTH]

    def redact_secret(match):
        key_quote = match.group("key_quote")
        value_quote = match.group("value_quote") or ""
        return (
            f"{key_quote}{match.group('key')}{key_quote}"
            f"{match.group('separator')}{value_quote}[已脱敏]{value_quote}"
        )

    # 必须排在最前：否则 proxyuser:proxypass@host 会先被 _SECRET_RE 当成
    # 「键 proxyuser + 值 proxypass@host」，用户名以键的形式留了下来。
    text = _USERINFO_RE.sub("[凭据已脱敏]@", text)
    text = _SECRET_RE.sub(redact_secret, text)
    text = _URL_RE.sub("[网络地址已脱敏]", text)
    text = _IP_RE.sub("[地址已脱敏]", text)
    text = _WINDOWS_PATH_RE.sub("[本地路径已脱敏]", text)
    return text
