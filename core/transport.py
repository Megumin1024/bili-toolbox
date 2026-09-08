# -*- coding: utf-8 -*-
"""传输通道：
- Ja3H2Transport   curl_cffi(chrome impersonation) —— 真实 Chrome JA3/JA4 TLS
                   指纹 + HTTP/2。impersonate 目标按会话身份的 UA 大版本探测选定。
- UrllibTransport  回退：纯标准库 HTTP/1.1 + 指纹状态生成的完整浏览器头。

配套能力：会话级指纹随机化（fingerprint.BrowserIdentity）、cookie 持久化
（CookieStore，跨重启复用 buvid）、代理健康检查、错误分类（风控/限流/网络）。
"""
import http.cookiejar
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .activation import gen_uuid_infoc

HOME_URL = "https://www.bilibili.com/"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
TICKET_URL = ("https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
              "?key_id=ec02&hexsign={hexsign}&context%5Bts%5D={ts}&csrf=")
RISK_HTTP_STATUS = (412, 403)
RISK_API_CODES = (-352, -412, 412)
RATE_LIMIT_HTTP_STATUS = (429,)
RATE_LIMIT_API_CODES = (-799, -502)
CANDIDATE_IMPERSONATE = ["chrome131", "chrome124", "chrome120", "chrome116", "chrome110"]
COOKIE_MAX_AGE = 14 * 86400  # 持久化 cookie 复用窗口

_PROBED_TARGETS = None
_PROBE_LOCK = threading.Lock()

LOG = print  # 由上层替换为 GUI 日志回调


def _log(msg):
    try:
        LOG(msg)
    except Exception:  # noqa: BLE001
        pass


class TransportError(Exception):
    """网络/协议层错误。"""


class RiskBlocked(TransportError):
    """风控信号：HTTP 412/403 或 API code -352/-412。"""


class RiskVoucher(RiskBlocked):
    """风控信号且响应携带 v_voucher，可走人工验证（-352 恢复流程）。"""

    def __init__(self, message, v_voucher):
        super().__init__(message)
        self.v_voucher = v_voucher


class BiliRateLimitError(TransportError):
    """限流信号：HTTP 429 或 API code -799/-502。"""


class BiliConnectionError(TransportError):
    """连接类错误（DNS/超时/重置）。"""


class BiliApiError(TransportError):
    """API 返回非 0 code（非风控/限流类）。"""


def describe_non_json(status, body_limit=200):
    """把非 JSON 响应压成一行，便于日志排障。"""
    return f"非 JSON 响应（HTTP {status}）"


def _major_to_target(major):
    """UA 主版本 → 最近的可用 impersonate 目标（向下就近，无则 'chrome' 最新）。"""
    available = [int(t.replace("chrome", "")) for t in _probe_targets() if t != "chrome"]
    exact = f"chrome{major}"
    if exact in available:
        return exact
    lower = [m for m in available if m <= major]
    return f"chrome{max(lower)}" if lower else "chrome"


def _probe_targets():
    """探测 curl_cffi 实际支持的 impersonate 目标（进程内缓存）。"""
    global _PROBED_TARGETS
    with _PROBE_LOCK:
        if _PROBED_TARGETS is not None:
            return _PROBED_TARGETS
        try:
            from curl_cffi import requests as cr
        except ImportError:
            _PROBED_TARGETS = []
            return _PROBED_TARGETS
        ok = []
        for target in CANDIDATE_IMPERSONATE:
            try:
                s = cr.Session(impersonate=target)
                s.close()
                ok.append(target)
            except Exception:  # noqa: BLE001 - 不支持的目标在构造期即报错
                continue
        if not ok:
            try:
                s = cr.Session(impersonate="chrome")
                s.close()
                ok.append("chrome")
            except Exception:  # noqa: BLE001
                pass
        _PROBED_TARGETS = ok
        return _PROBED_TARGETS


class CookieStore:
    """cookie 按指纹身份键控落盘，跨重启复用 buvid。

    文件结构: {"<device_id>": {"saved_at": ts, "cookies": {...}, "activated": bool}}
    """

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()

    def _read_all(self):
        if not self.path:
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                # 兼容旧版单身份格式：顶层非 dict 的键（saved_at/cookies）直接剔除
                return {k: v for k, v in data.items() if isinstance(v, dict)}
            return {}
        except (OSError, ValueError):
            return {}

    def _write_all(self, data):
        if not self.path:
            return
        # 顺手清理过期身份条目（saved_at 超过 2×复用窗口即删除，防无限膨胀）
        deadline = time.time() - COOKIE_MAX_AGE * 2
        data = {k: v for k, v in data.items()
                if not (isinstance(v, dict) and v.get("saved_at", 0) < deadline)}
        with self._lock:
            try:
                parent = Path(self.path).parent
                parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
            except OSError:
                pass

    def load(self, device_id):
        """取该身份的持久化 cookie；超过复用窗口返回空。"""
        if not device_id:
            return {}
        entry = self._read_all().get(device_id, {})
        if time.time() - entry.get("saved_at", 0) > COOKIE_MAX_AGE:
            return {}
        return entry.get("cookies", {})

    def meta(self, device_id):
        """取该身份的元信息（如 activated 标记），不受复用窗口限制。"""
        if not device_id:
            return {}
        return {k: v for k, v in self._read_all().get(device_id, {}).items()
                if k not in ("cookies", "saved_at")}

    def save(self, device_id, cookies, **meta):
        if not device_id:
            return
        data = self._read_all()
        entry = data.get(device_id, {})
        entry.update(meta)
        entry["saved_at"] = time.time()
        if cookies:
            entry["cookies"] = dict(cookies)
        data[device_id] = entry
        self._write_all(data)


def bootstrap_credentials(transport, identity=None, cookie_store=None, force=False):
    """凭证引导（P0/P1）：spi 领取 buvid 对（失败自生成 buvid3 兜底）→ bili_ticket
    签发 → 首页补 b_nut → 全部落到会话与身份键控存储。返回是否确认有 buvid3。

    24 小时内已引导过的身份直接跳过（force=True 强制重跑）。
    """
    now = time.time()
    meta = cookie_store.meta(identity.device_id) if (cookie_store and identity) else {}
    if identity and not force and now - meta.get("cred_ok_at", 0) < 86400:
        return bool(cookie_store.load(identity.device_id).get("buvid3"))

    cookies = {}
    try:
        spi = transport.get_json(SPI_URL).get("data") or {}
        if spi.get("b_3"):
            cookies["buvid3"], cookies["buvid4"] = spi["b_3"], spi["b_4"]
    except Exception as exc:  # noqa: BLE001
        _log(f"[cred] spi 获取失败({getattr(transport, 'name', '?')}): {exc}，"
             f"自生成 buvid3 兜底")
    if not cookies.get("buvid3"):
        cookies["buvid3"] = gen_uuid_infoc()  # 格式合法即可用；经 ExClimbWuzhi 激活后更稳
    try:
        ts = int(time.time())
        hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
        r = transport.post_json(TICKET_URL.format(ts=ts, hexsign=hexsign),
                                json_body="{}",
                                headers={"Content-Type": "application/json"})
        ticket = (r.get("data") or {}).get("ticket")
        if ticket:
            cookies["bili_ticket"] = ticket
            cookies["bili_ticket_expires"] = str(ts + 259200)
    except Exception as exc:  # noqa: BLE001
        _log(f"[cred] bili_ticket 签发失败: {exc}")
    try:
        transport.get_json(HOME_URL)  # 首页补 b_nut 等基础 cookie
    except Exception:  # noqa: BLE001
        pass
    transport.set_cookies(cookies)
    if cookie_store and identity:
        saved = cookie_store.load(identity.device_id)
        saved.update(cookies)
        cookie_store.save(identity.device_id, saved, cred_ok_at=int(now))
    return bool(cookies.get("buvid3"))


class BaseTransport:
    name = "base"
    desc = ""

    def get_json(self, url):
        raise NotImplementedError

    def warmup(self, force=False):
        """首页预热获取风控 cookie。返回是否确认拿到 buvid3。"""
        raise NotImplementedError

    def healthcheck(self, timeout=5):
        """代理连通性检查。"""
        raise NotImplementedError

    def close(self):
        pass

    @staticmethod
    def _classify_json(data):
        code = data.get("code")
        if code in RISK_API_CODES:
            d = data.get("data")
            vv = d.get("v_voucher") if isinstance(d, dict) else None
            if vv:
                raise RiskVoucher(f"API code={code}（v_voucher 已取得，可人工验证恢复）", vv)
            raise RiskBlocked(f"API code={code}"
                              "（无v_voucher，检查cookie/wbi）")
        if code in RATE_LIMIT_API_CODES:
            raise BiliRateLimitError(f"API code={code}")
        if code != 0:
            raise BiliApiError(f"API code={code} msg={data.get('message')}")
        return data


class Ja3H2Transport(BaseTransport):
    """第3层主通道：Chrome 指纹级模拟（curl_cffi / curl-impersonate）。"""
    name = "h2-ja3"
    desc = "curl_cffi chrome 指纹（JA3/JA4 + HTTP/2+）"

    def __init__(self, identity=None, proxy_url=None, cookie_store=None):
        try:
            from curl_cffi import requests as cr
        except ImportError as exc:
            raise RuntimeError("curl_cffi 未安装（pip install curl_cffi）") from exc
        self._cr = cr
        self.identity = identity
        self.cookie_store = cookie_store
        kw = {"impersonate": "chrome", "timeout": 15}
        if identity:
            if identity.impersonate is None:
                identity.impersonate = _major_to_target(identity.chrome_major)
            kw["impersonate"] = identity.impersonate
        if proxy_url:
            kw["proxies"] = {"http": proxy_url, "https": proxy_url}
        self.session = cr.Session(**kw)
        saved = cookie_store.load(identity.device_id) if (cookie_store and identity) else {}
        if saved:
            self.session.cookies.update(saved)
        self._last_warm = time.time() if saved.get("buvid3") else 0.0
        self._identity = identity
        self._cookie_store = cookie_store

    @property
    def _biz_headers(self):
        """指纹身份生成的业务头（impersonate 自带 UA/sec-ch-ua 等浏览器头）。"""
        if self.identity:
            h = dict(self.identity.browser_headers)
            # 与 TLS 指纹成套的字段交给 impersonate，避免头/指纹矛盾
            for k in ("user-agent", "sec-ch-ua", "sec-ch-ua-mobile",
                      "sec-ch-ua-platform", "cookie"):
                h.pop(k, None)
            return h
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }

    def warmup(self, force=False):
        if not force and time.time() - self._last_warm < 300:
            return True
        if self._identity and self._cookie_store:
            ok = bootstrap_credentials(self, self._identity, self._cookie_store,
                                       force=force)
        else:
            try:
                resp = self.session.get(HOME_URL,
                                        headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                                        timeout=15)
                ok = resp.status_code == 200 and bool(self.session.cookies.get("buvid3"))
            except Exception:  # noqa: BLE001 - 预热失败不致命，接口请求仍可尝试
                ok = False
        if ok:
            self._last_warm = time.time()
        return ok

    def set_cookies(self, cookies):
        self.session.cookies.update(cookies)

    def get_cookies(self):
        try:
            return dict(self.session.cookies)
        except Exception:  # noqa: BLE001
            return {}

    def post_form(self, url, fields, headers=None):
        """表单 POST（gaia 极验 register/validate 用）；返回解析后的 JSON dict。"""
        extra = dict(self._biz_headers)
        extra.update(headers or {})
        try:
            resp = self.session.post(url, data=fields, headers=extra, timeout=15)
        except Exception as exc:  # noqa: BLE001
            raise BiliConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code in RISK_HTTP_STATUS:
            raise RiskBlocked(f"HTTP {resp.status_code}")
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(describe_non_json(resp.status_code)) from exc

    def post_json(self, url, json_body, headers=None, cookies=None):
        extra = dict(headers or {})
        if cookies:
            extra["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            resp = self.session.post(url, data=json_body.encode("utf-8"),
                                     headers=extra, timeout=15)
        except Exception as exc:  # noqa: BLE001
            raise BiliConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code in RISK_HTTP_STATUS:
            raise RiskBlocked(f"HTTP {resp.status_code}")
        if resp.status_code in RATE_LIMIT_HTTP_STATUS:
            raise BiliRateLimitError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(describe_non_json(resp.status_code)) from exc
        return self._classify_json(data)

    def get_json(self, url):
        try:
            resp = self.session.get(url, headers=self._biz_headers, timeout=15)
        except Exception as exc:  # noqa: BLE001 - curl 层异常统一转 TransportError
            raise BiliConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code in RISK_HTTP_STATUS:
            raise RiskBlocked(f"HTTP {resp.status_code}")
        if resp.status_code in RATE_LIMIT_HTTP_STATUS:
            raise BiliRateLimitError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - 风控页常返回 HTML
            raise TransportError(describe_non_json(resp.status_code)) from exc
        return self._classify_json(data)

    def healthcheck(self, timeout=5):
        try:
            return self.session.get(HOME_URL, timeout=timeout).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass


class UrllibTransport(BaseTransport):
    """回退通道：标准库 HTTP/1.1 + 指纹状态生成的完整浏览器头（第1层）。"""
    name = "urllib"
    desc = "HTTP/1.1 指纹状态头 + cookie 预热"

    def __init__(self, identity=None, proxy_url=None, cookie_store=None):
        if proxy_url and proxy_url.startswith("socks"):
            raise RuntimeError("urllib 通道不支持 socks 代理，请使用 h2-ja3 通道")
        self.identity = identity
        self.cookie_store = cookie_store
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}))
        self.opener = urllib.request.build_opener(*handlers)
        self._last_warm = 0.0
        self._lock = threading.Lock()
        saved = cookie_store.load(identity.device_id) if (cookie_store and identity) else {}
        if saved:
            self._seed_jar(saved)
            self._last_warm = time.time() if saved.get("buvid3") else 0.0

    def _headers(self):
        if self.identity:
            return dict(self.identity.browser_headers)
        return {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Priority": "u=1, i",
        }

    def _seed_jar(self, cookies):
        for name, value in cookies.items():
            c = http.cookiejar.Cookie(0, name, value, None, False,
                                      "www.bilibili.com", True, True, "/", False,
                                      False, None, False, None, None, {})
            self.jar.set_cookie(c)

    def _drain_jar(self):
        return {c.name: c.value for c in self.jar}

    def warmup(self, force=False):
        with self._lock:
            if not force and time.time() - self._last_warm < 300:
                return True
            if self.identity and self.cookie_store:
                ok = bootstrap_credentials(self, self.identity, self.cookie_store,
                                           force=force)
            else:
                try:
                    req = urllib.request.Request(HOME_URL, headers={
                        "User-Agent": self._headers()["user-agent"],
                        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                                   "image/avif,image/webp,*/*;q=0.8"),
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    })
                    self.opener.open(req, timeout=15).read(65536)
                except Exception:  # noqa: BLE001
                    pass  # 预热失败不致命
                ok = any(c.name == "buvid3" for c in self.jar)
            if ok:
                self._last_warm = time.time()
            return ok

    def set_cookies(self, cookies):
        self._seed_jar(cookies)

    def get_cookies(self):
        return self._drain_jar()

    def post_form(self, url, fields, headers=None):
        """表单 POST（gaia 极验 register/validate 用）；返回解析后的 JSON dict。"""
        extra = dict(self._headers())
        extra.update(headers or {})
        extra["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=extra)
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in RISK_HTTP_STATUS:
                raise RiskBlocked(f"HTTP {exc.code}") from exc
            if exc.code in RATE_LIMIT_HTTP_STATUS:
                raise BiliRateLimitError(f"HTTP {exc.code}") from exc
            raise BiliConnectionError(f"HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise BiliConnectionError(f"{type(exc).__name__}: {exc}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise TransportError(describe_non_json(200)) from exc

    def post_json(self, url, json_body, headers=None, cookies=None):
        extra = dict(self._headers())
        extra.update(headers or {})
        extra["Content-Type"] = extra.get("Content-Type", "application/json")
        if cookies:
            extra["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req = urllib.request.Request(url, data=json_body.encode("utf-8"), headers=extra)
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in RISK_HTTP_STATUS:
                raise RiskBlocked(f"HTTP {exc.code}") from exc
            if exc.code in RATE_LIMIT_HTTP_STATUS:
                raise BiliRateLimitError(f"HTTP {exc.code}") from exc
            raise BiliConnectionError(f"HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise BiliConnectionError(f"{type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise TransportError(describe_non_json(200)) from exc
        return self._classify_json(data)

    def get_json(self, url):
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self.opener.open(req, timeout=15) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in RISK_HTTP_STATUS:
                raise RiskBlocked(f"HTTP {exc.code}") from exc
            if exc.code in RATE_LIMIT_HTTP_STATUS:
                raise BiliRateLimitError(f"HTTP {exc.code}") from exc
            raise BiliConnectionError(f"HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise BiliConnectionError(f"{type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise TransportError(describe_non_json(200)) from exc
        return self._classify_json(data)

    def healthcheck(self, timeout=5):
        try:
            req = urllib.request.Request(HOME_URL, headers={"User-Agent": self._headers()["user-agent"]})
            return self.opener.open(req, timeout=timeout).status == 200
        except Exception:  # noqa: BLE001
            return False


def build_transport(preferred, proxy_url=None, identity=None, cookie_store=None):
    """按首选构造通道；preferred=auto 时按 h2-ja3 → urllib 顺序降级。"""
    order = [preferred] if preferred != "auto" else ["h2-ja3", "urllib"]
    last_err = None
    for name in order:
        try:
            if name == "h2-ja3":
                t = Ja3H2Transport(identity, proxy_url, cookie_store)
            elif name == "urllib":
                t = UrllibTransport(identity, proxy_url, cookie_store)
            else:
                raise ValueError(f"未知通道: {name}（可选 auto/h2-ja3/urllib）")
            return t
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"无法建立任何传输通道: {last_err}")
