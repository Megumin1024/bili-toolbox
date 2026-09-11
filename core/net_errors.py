# -*- coding: utf-8 -*-
"""网络错误分类内核：把 HTTP 状态、API code、底层异常归一为 ErrorKind。

本模块是错误分类的单一权威来源：
- transport 层用它决定抛哪一种异常；
- 后续的重试/退避策略用它决定"该不该重试、要不要换通道"。

硬约束（保持可离线测试）：
- 纯函数、无 I/O、无副作用：不发起请求、不读写文件、不 sleep、不读时钟。
- 只依赖标准库，不导入 core 内其它模块，避免与 transport 形成循环依赖。
"""
from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    """一次网络交互失败的性质。"""

    RISK = "risk"                  # 风控拦截：HTTP 412/403，或 API -352/-412/412
    RISK_VOUCHER = "risk_voucher"  # 风控且响应带 v_voucher，可走人工验证恢复
    RATE_LIMIT = "rate_limit"      # 平台限流：HTTP 429，或 API -799/-502
    API_ERROR = "api_error"        # 业务非零 code（非风控/限流），属调用方语义错误
    HTTP_ERROR = "http_error"      # 其它非 2xx 状态
    NON_JSON = "non_json"          # 响应体无法解析为 JSON（风控页常返回 HTML）
    TIMEOUT = "timeout"            # 请求超时
    CONNECTION = "connection"      # DNS/拒绝/重置/TLS/代理等连接类错误
    UNKNOWN = "unknown"            # 有错误但无法归类


# 单一权威定义：transport 层从这里导入，不再各自维护一份。
RISK_HTTP_STATUS = (412, 403)
RISK_API_CODES = (-352, -412, 412)
RATE_LIMIT_HTTP_STATUS = (429,)
RATE_LIMIT_API_CODES = (-799, -502)

# 可自动重试的性质。注意：RISK/RISK_VOUCHER 的"重试"必须伴随重新预热与轮换，
# 不能盲目重复同一请求；RATE_LIMIT 必须长退避。真正的重试编排在后续任务实现，
# 本模块只回答"值不值得重试"。
_RETRYABLE = frozenset({
    ErrorKind.RISK,
    ErrorKind.RISK_VOUCHER,
    ErrorKind.RATE_LIMIT,
    ErrorKind.NON_JSON,
    ErrorKind.TIMEOUT,
    ErrorKind.CONNECTION,
})

# 按异常类名判定，避免导入 curl_cffi / urllib / socket 造成耦合。
_TIMEOUT_EXC_NAMES = frozenset({
    "TimeoutError", "Timeout", "ReadTimeout", "ConnectTimeout",
    "BiliTimeoutError", "socket.timeout",
})
_CONNECTION_EXC_NAMES = frozenset({
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "ConnectionRefusedError", "RemoteDisconnected", "IncompleteRead",
    "SSLError", "ProxyError", "URLError", "BiliConnectionError",
    # 通用传输错误（含非 JSON 响应）归为连接类，保住指数退避语义。
    "TransportError",
})

_DESCRIPTIONS = {
    ErrorKind.RISK: "风控拦截",
    ErrorKind.RISK_VOUCHER: "风控挑战（可人工验证恢复）",
    ErrorKind.RATE_LIMIT: "平台限流",
    ErrorKind.API_ERROR: "接口业务错误",
    ErrorKind.HTTP_ERROR: "HTTP 错误",
    ErrorKind.NON_JSON: "非 JSON 响应",
    ErrorKind.TIMEOUT: "请求超时",
    ErrorKind.CONNECTION: "连接失败",
    ErrorKind.UNKNOWN: "未知网络错误",
}


def _classify_api_code(payload):
    """API 业务 code → ErrorKind；code == 0（业务成功）返回 None。"""
    code = payload.get("code")
    if code in RISK_API_CODES:
        data = payload.get("data")
        if isinstance(data, dict) and data.get("v_voucher"):
            return ErrorKind.RISK_VOUCHER
        return ErrorKind.RISK
    if code in RATE_LIMIT_API_CODES:
        return ErrorKind.RATE_LIMIT
    if code != 0:
        return ErrorKind.API_ERROR
    return None


def _classify_http_status(status):
    """HTTP 状态 → ErrorKind；2xx 或未知返回 None。"""
    if status is None:
        return None
    if status in RISK_HTTP_STATUS:
        return ErrorKind.RISK
    if status in RATE_LIMIT_HTTP_STATUS:
        return ErrorKind.RATE_LIMIT
    if 200 <= status < 300:
        return None
    return ErrorKind.HTTP_ERROR


def _classify_exception(exc):
    name = type(exc).__name__
    if name in _TIMEOUT_EXC_NAMES or "timeout" in name.lower():
        return ErrorKind.TIMEOUT
    if name in _CONNECTION_EXC_NAMES:
        return ErrorKind.CONNECTION
    return ErrorKind.UNKNOWN


def classify(http_status=None, payload=None, exc=None):
    """把一次失败归一为 ErrorKind；没有可判定的错误时返回 None。

    - http_status: int 或 None
    - payload: 已解析的响应 dict，或无法解析时的原始 body 字符串
    - exc: 捕获到的底层异常（用于区分超时/连接类）

    判定优先级：API 业务 code > HTTP 风控/限流状态 > 非 JSON > 底层异常 > 其它状态。
    风控/限流状态优先于 body 解析结果——412 返回 HTML 风控页时应判 RISK，而非 NON_JSON。
    """
    if isinstance(payload, dict) and "code" in payload:
        kind = _classify_api_code(payload)
        if kind is not None:
            return kind
        return _classify_http_status(http_status)

    status_kind = _classify_http_status(http_status)
    if status_kind in (ErrorKind.RISK, ErrorKind.RATE_LIMIT):
        return status_kind
    if isinstance(payload, str):
        return ErrorKind.NON_JSON
    if exc is not None:
        return _classify_exception(exc)
    if status_kind is not None:
        return status_kind
    return None


def is_retryable(kind):
    """该性质是否默认值得自动重试。不表达退避方式与是否换通道。"""
    return kind in _RETRYABLE


def describe(kind, http_status=None, detail=None):
    """一行中文描述，供日志与用户提示复用。"""
    text = _DESCRIPTIONS.get(kind, "未知网络错误")
    if http_status is not None:
        text += f"（HTTP {http_status}）"
    if detail:
        text += f": {detail}"
    return text
