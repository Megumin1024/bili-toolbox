# -*- coding: utf-8 -*-
"""WBI 签名：B 站 space / 动态等接口的 w_rid 签名参数。

**为什么必须签名**：实测（游客态、同一 uid、同一时刻）不带签名的
`/x/polymer/web-dynamic/v1/feed/space` 会返回 `code=0` 但 **items 为空**——
它不报错、不提示，只是静默地什么都不给。同一请求带上签名立刻返回 12 条。
所以签名不是"可选增强"，是能不能拿到数据的分水岭。

签名算法：
1. 从 nav 接口取 img_url / sub_url，各取路径末段文件名（去扩展名）作为密钥
2. mixin_key = 按固定置换表重排 (img_key + sub_key)，取前 32 位
3. 参数加 wts（秒级时间戳）→ 按 key 排序 → 剔除值里的 !'()* 字符 → urlencode
4. w_rid = md5(query + mixin_key)

密钥会轮换，但取密钥要多一次 nav 请求，因此带 TTL 缓存；clock 可注入，
离线测试无需真等。本模块自身不发请求、不读写文件：取密钥的函数由调用方注入
（默认实现走 core.session 的四层风控通道）。
"""
from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# 密钥缓存的默认存活时长（秒）。密钥按天轮换，取一次很便宜但也没必要每请求都取。
DEFAULT_KEYS_TTL = 6 * 3600.0

# mixin_key 的置换表。这是 B 站前端的固定常量，不是可推导的东西。
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

MIXIN_KEY_LEN = 32
_FILTER_CHARS = "!'()*"


class WbiKeyError(RuntimeError):
    """取不到 WBI 密钥（被风控拦截、或响应结构不符预期）。"""


# ---------- 纯函数部分（可离线测试） ----------

def extract_key(url):
    """从 wbi_img 的 URL 取密钥：路径末段文件名去掉扩展名。"""
    path = str(url or "").split("?", 1)[0].split("#", 1)[0]
    name = path.rsplit("/", 1)[-1]
    key = name.split(".", 1)[0]
    if not key:
        raise WbiKeyError("wbi_img URL 里取不到密钥")
    return key


def mixin_key(img_key, sub_key):
    """按置换表把 img_key + sub_key 重排，取前 32 位。"""
    orig = f"{img_key}{sub_key}"
    if len(orig) < max(MIXIN_KEY_ENC_TAB) + 1:
        raise WbiKeyError(
            f"密钥长度不足，无法置换（img={len(img_key)} sub={len(sub_key)}）")
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:MIXIN_KEY_LEN]


def sign_params(params, img_key, sub_key, wts=None):
    """返回带 wts 与 w_rid 的查询串。

    wts 可注入，使签名结果在测试里可复现（否则同一参数每天得到的 w_rid 都不同）。
    """
    cleaned = {k: "".join(c for c in str(v) if c not in _FILTER_CHARS)
               for k, v in params.items()}
    cleaned["wts"] = int(time.time()) if wts is None else int(wts)
    query = urllib.parse.urlencode(sorted(cleaned.items()))
    rid = hashlib.md5(
        (query + mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={rid}"


def signed_url(base, params, img_key, sub_key, wts=None):
    return f"{base}?{sign_params(params, img_key, sub_key, wts=wts)}"


def keys_from_nav(payload):
    """从 nav 响应体取 (img_key, sub_key)。

    兼容 code=-101：游客态下 nav 报"账号未登录"，但 wbi_img 仍在同一响应里，
    这正是纯游客工具赖以签名的那份密钥。所以这里只看 data，不看 code。
    """
    data = (payload or {}).get("data") or {}
    wbi = data.get("wbi_img") or {}
    img_url, sub_url = wbi.get("img_url"), wbi.get("sub_url")
    if not img_url or not sub_url:
        raise WbiKeyError("nav 响应里没有 wbi_img，可能已被风控拦截")
    return extract_key(img_url), extract_key(sub_url)


# ---------- 取密钥（网络部分，走项目通道） ----------

def fetch_keys(cancel=None):
    """走 core.session 的四层风控通道取密钥。

    nav 在游客态返回 -101，属**正常状态**而非故障；transport 层已把该响应体挂在
    BiliApiError.payload 上，这里顺着异常把密钥捞回来，不把 -101 当失败。

    cancel 为取消谓词，透传给 HTTP 层：不透传的话，用户在取密钥的网络退避里
    按取消，要等满 fetch_json 的总退避预算（TOTAL_WAIT_BUDGET）才有反应。
    """
    from . import session                      # 延迟导入：本模块需可离线单独导入
    from .transport import BiliApiError

    try:
        payload = session.http_get_json(NAV_URL, retries=2, cancel=cancel)
    except BiliApiError as exc:
        if exc.payload is None:
            raise WbiKeyError(f"取 WBI 密钥失败：{exc}") from exc
        payload = exc.payload
    return keys_from_nav(payload)


class WbiKeyCache:
    """WBI 密钥的 TTL 缓存。线程安全；fetch / clock / cancel 均可注入。"""

    def __init__(self, fetch=None, ttl=DEFAULT_KEYS_TTL, clock=None, cancel=None):
        # cancel 只作用于默认取密钥实现；调用方自带 fetch 时由它自行处理取消。
        self._fetch = fetch or (lambda: fetch_keys(cancel=cancel))
        self._ttl = max(0.0, float(ttl))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._keys = None
        self._fetched_at = 0.0
        self.fetches = 0

    def get(self, force=False):
        """取 (img_key, sub_key)；缓存有效则复用。"""
        with self._lock:
            fresh = (self._keys is not None
                     and self._clock() - self._fetched_at < self._ttl)
            if fresh and not force:
                return self._keys
        # 网络调用放在锁外：别让一次慢请求把其他线程串行化。并发重复取无伤大雅。
        keys = self._fetch()
        with self._lock:
            self._keys = tuple(keys)
            self._fetched_at = self._clock()
            self.fetches += 1
            return self._keys

    def invalidate(self):
        """密钥失效（如签名被拒）时主动丢弃，下次取新的。"""
        with self._lock:
            self._keys = None
            self._fetched_at = 0.0
