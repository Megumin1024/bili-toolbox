# -*- coding: utf-8 -*-
"""统一 HTTP 会话门面：所有工具的 JSON/表单请求都走 BiliClient 四层风控栈。

L1 完整浏览器头 + L2 凭证预热/buvid激活 + L3 JA3 指纹双通道(h2-ja3→urllib
自动降级) + L4 代理池，由 BiliClient 编排（代理×指纹绑定、cookie 持久化、
风控/限流退避重试）。本模块维护进程级默认客户端，并在其上提供：

- http_get_json()：统一 JSON GET，-352+v_voucher 时抛 RiskChallengeError
  （人工恢复流程入口，见 risk.py）
- post_form()：表单 POST（gaia 极验端点用，继承当前通道 cookie）
- grpc_metadata() / ensure_ready()：把预热产物（buvid3/buvid4/bili_ticket）
  注入 gRPC metadata，供评论 gRPC 通道伪装
"""
import threading
import time
import urllib.parse

from .client import BiliClient
from .proxy import ProxyPool
from .risk import RiskChallengeError
from .transport import (BiliApiError, BiliConnectionError, BiliRateLimitError,
                        RiskBlocked, RiskVoucher, TransportError, LOG as _TLOG)

UA_APP = "Mozilla/5.0 BiliDroid/8.16.0 (bbcall@126.com)"

# 必须是可重入锁：get_client() 会在持锁状态下调用 configure()，而 configure()
# 也要拿这把锁。用普通 Lock 会让「冷启动直接调 get_client()」永久自锁——没有
# 异常、没有超时、CPU 也不转，表现为进程静默挂起。
# GUI 碰不到（main.py 启动时已 configure，_CLIENT 非空，走不到那条分支），
# 但脚本 / CLI / 集成测试只要把 http_get_json 当进程里第一个 session 调用就中招。
_LOCK = threading.RLock()
_CLIENT = None
_CONF = {}

_VTOKEN = None


def log(msg):
    """默认日志（session.configure 可替换为 GUI 日志回调）。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def configure(proxy_spec=None, transport="auto", cookie_path=None, log_fn=None,
              force=False):
    """（重）配置进程级默认客户端。配置未变化时复用现有实例。

    任务开始前调用；GUI 在应用启动与设置保存时调用。
    """
    global _CLIENT, _CONF
    log_fn = log_fn or log
    import core.transport as _t
    _t.LOG = log_fn
    conf = {"proxy_spec": proxy_spec or "",
            "transport": transport or "auto",
            "cookie_path": str(cookie_path) if cookie_path else None}
    old = None
    with _LOCK:
        if _CLIENT is not None and not force and conf == _CONF:
            return _CLIENT
        old = _CLIENT
        pool = ProxyPool(conf["proxy_spec"] or None)
        client = BiliClient(pool, preferred_transport=conf["transport"],
                            cookie_path=conf["cookie_path"], log=log_fn)
        _CLIENT, _CONF = client, conf
    if old is not None:
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass
    return client


def get_client():
    with _LOCK:
        if _CLIENT is None:
            configure()
            return _CLIENT
        return _CLIENT


# ---------------- gaia_vtoken（人工验证通行证） ----------------

def set_gaia_vtoken(grisk_id):
    """保存通行证：后续所有 HTTP 请求自动附带 gaia_vtoken 参数与 cookie。"""
    global _VTOKEN
    _VTOKEN = grisk_id
    try:
        t = get_client()._transport
        if t is not None:
            t.set_cookies({"x-bili-gaia-vtoken": grisk_id})
    except Exception:  # noqa: BLE001
        pass


def _attach_vtoken(url):
    if _VTOKEN:
        sep = "&" if "?" in url else "?"
        return url + sep + "gaia_vtoken=" + urllib.parse.quote(_VTOKEN)
    return url


# ---------------- 统一请求入口 ----------------

def http_get_json(url, retries=3, cancel=None):
    """统一 JSON GET（四层风控栈）。-352+voucher → RiskChallengeError。

    其余风控/限流信号由 BiliClient 内部按策略重试，最终失败抛
    RiskBlocked / BiliRateLimitError / TransportError。

    cancel 为取消谓词（如 threading.Event.is_set）：为真时请求立即中止、
    退避等待被打断，抛 core.cancel.TaskCancelledError，不计入任何失败统计。
    """
    url = _attach_vtoken(url)
    try:
        return get_client().fetch_json(url, retries=retries, cancel=cancel)
    except RiskVoucher as exc:
        raise RiskChallengeError(exc.v_voucher) from exc


def http_get_bytes(url, retries=3, cancel=None):
    """统一原始字节 GET（四层风控栈），给二进制接口用（如弹幕 protobuf）。

    失败语义与 http_get_json 一致：重试/退避/闸门/统计全套相同，只是成功时
    返回 bytes 而非解析后的 dict。响应体若其实是风控 JSON，在传输层就已被
    识别并抛错，不会当成"取到了 0 条数据"。
    """
    url = _attach_vtoken(url)
    try:
        return get_client().fetch_bytes(url, retries=retries, cancel=cancel)
    except RiskVoucher as exc:
        raise RiskChallengeError(exc.v_voucher) from exc


def post_form(url, fields, retries=2):
    """表单 POST（继承当前通道会话 cookie），返回解析后的 JSON dict。"""
    client = get_client()
    last = None
    for _ in range(max(1, retries)):
        try:
            t = client._get_transport()
            return t.post_form(url, fields)
        except RiskVoucher as exc:
            raise RiskChallengeError(exc.v_voucher) from exc
        except TransportError as exc:
            last = exc
            client._invalidate_transport()
    raise last


def ensure_ready():
    """预热凭证（spi buvid 对 + bili_ticket + buvid 激活，24h 缓存）。

    返回当前会话 cookie 串；失败不致命（返回空串，指纹通道仍可用）。
    """
    client = get_client()
    try:
        client._get_transport()
    except Exception as exc:  # noqa: BLE001
        log(f"[cred] 凭证预热失败（不阻塞，接口请求仍会尝试）: {exc}")
    return client.cookie_header()


def grpc_metadata(ua_app=UA_APP):
    """gRPC metadata：App UA + 预热 cookie 串 + Referer（伪装增强项）。"""
    md = [("user-agent", ua_app)]
    cookie = ensure_ready()
    if cookie:
        md.append(("cookie", cookie))
    md.append(("referer", "https://www.bilibili.com/"))
    return md


def info():
    """当前会话状态（设置页/调试展示用）。"""
    try:
        return get_client().info()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
