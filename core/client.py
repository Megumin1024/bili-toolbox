# -*- coding: utf-8 -*-
"""统一请求层：代理×指纹绑定(第1/3层) + cookie预热/持久化/激活(第2层) + 代理池(第4层)。

每个代理出口绑定一套独立的随机指纹身份（BrowserIdentity），互不串联；
cookie/激活状态按身份 deviceId 键控持久化。

重试策略：
- 风控信号（412/403/-352/-412）→ 强制重新预热 cookie；有备用代理则轮换
- 限流信号（429/-799/-502）→ 更长退避；有备用代理则轮换
- 网络/传输错误 → 计入代理失败数，达阈值自动冷却并轮换；否则指数退避重试
- API 非 0 code（非风控）→ 直接抛出，由采集层处理
"""
import threading
import time

from .activation import activate_via_transport
from .fingerprint import BrowserIdentity
from .proxy import ProxyPool
from .transport import (BiliApiError, BiliRateLimitError, CookieStore,
                        RiskBlocked, TransportError, build_transport)

RETRY_ATTEMPTS = 3


class BiliClient:
    def __init__(self, pool: ProxyPool, preferred_transport="auto", cookie_path=None,
                 log=None):
        self.pool = pool
        self.preferred = preferred_transport
        self.cookie_store = CookieStore(cookie_path) if cookie_path else None
        self.log = log or (lambda msg: print(msg, flush=True))
        self._identities = {}      # 代理出口 -> BrowserIdentity（代理×指纹绑定）
        self._transport = None
        self._transport_key = object()
        self.lock = threading.Lock()
        self.stats = {
            "requests": 0, "risk_events": 0, "rate_limit_events": 0,
            "net_errors": 0, "warmups": 0, "healthchecks": 0, "activations": 0,
            "last_latency_ms": 0, "last_transport": "-",
        }

    # ---------- 代理×指纹绑定 ----------
    def _get_identity(self, proxy_key):
        """同一代理出口复用同一套指纹身份，不同出口互不串联。"""
        ident = self._identities.get(proxy_key)
        if ident is None:
            ident = BrowserIdentity()
            self._identities[proxy_key] = ident
        return ident

    # ---------- 通道管理 ----------
    def _get_transport(self):
        key = self.pool.current_url()
        with self.lock:
            if self._transport is not None and self._transport_key == key:
                return self._transport
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
        identity = self._get_identity(key)
        t = build_transport(self.preferred, key, identity=identity,
                            cookie_store=self.cookie_store)
        self.stats["warmups"] += 1
        t.warmup()  # 持久化 cookie 新鲜时秒回
        if key:  # 代理场景做连通性健康检查
            self.stats["healthchecks"] += 1
            if not t.healthcheck():
                t.close()
                self.pool.mark_failure()
                raise TransportError(f"代理健康检查失败: {key}")
        self._try_activate(t, identity)
        with self.lock:
            self._transport = t
            self._transport_key = key
        return t

    def _try_activate(self, transport, identity):
        """buvid 主动激活：每身份仅一次（激活标记持久化）；失败不阻塞采集。"""
        if not self.cookie_store:
            return
        if self.cookie_store.meta(identity.device_id).get("activated"):
            return
        result = activate_via_transport(transport, identity, self.cookie_store)
        self.stats["activations"] += 1
        mark = "已激活" if result.get("activated") else f"未激活({result.get('detail')})"
        self.log(f"[activate] 身份 {identity.device_id[:8]}… 经 {transport.name}: {mark}")

    def _invalidate_transport(self):
        with self.lock:
            if self._transport is not None:
                try:
                    self._transport.close()
                except Exception:  # noqa: BLE001
                    pass
            self._transport = None

    def cookie_header(self):
        """当前通道会话的 cookie 串（gRPC metadata 注入用）；无会话返回空串。"""
        t = self._transport
        if t is None:
            return ""
        try:
            cookies = t.get_cookies()
        except Exception:  # noqa: BLE001
            return ""
        return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)

    # ---------- 对外 ----------
    def fetch_json(self, url, retries=RETRY_ATTEMPTS):
        """请求 JSON；风控/限流/网络错误按策略重试，最终失败抛最后一个异常。"""
        last_exc = None
        for attempt in range(retries):
            transport = self._get_transport()
            started = time.time()
            try:
                data = transport.get_json(url)
                self.stats["requests"] += 1
                self.stats["last_latency_ms"] = int((time.time() - started) * 1000)
                self.stats["last_transport"] = transport.name
                self.pool.mark_success()
                return data
            except RiskBlocked as exc:
                last_exc = exc
                self.stats["risk_events"] += 1
                self.log(f"[risk] 风控拦截({transport.name}): {exc}，重新预热并"
                         f"{'轮换代理' if self.pool.has_alternative() else '重试'}")
                transport.warmup(force=True)
                self._invalidate_transport()          # 重建通道（新会话/新 cookie）
                if not self.pool.rotate(f"risk:{exc}") and self.pool.has_alternative():
                    time.sleep(2)                     # 全部冷却：等冷却结束再试
            except BiliRateLimitError as exc:
                last_exc = exc
                self.stats["rate_limit_events"] += 1
                rotated = self.pool.mark_failure()
                if rotated or (self.pool.has_alternative() and attempt >= 1):
                    self.pool.rotate(f"ratelimit:{exc}")
                    self._invalidate_transport()
                self.log(f"[rate] 限流({transport.name}): {exc}，加倍退避")
                time.sleep(min(4 * (2 ** attempt), 30))
                continue
            except TransportError as exc:
                last_exc = exc
                self.stats["net_errors"] += 1
                rotated = self.pool.mark_failure()
                if rotated or (self.pool.has_alternative() and attempt >= 1):
                    self.pool.rotate(f"error:{exc}")
                    self._invalidate_transport()
                self.log(f"[net] 传输失败({transport.name}): {exc}，退避重试")
            time.sleep(min(2 ** attempt, 8))
        raise last_exc

    def info(self):
        with self.lock:
            stats = dict(self.stats)
        current_key = self.pool.current_url()
        current = self._identities.get(current_key)
        identities = []
        for key, ident in self._identities.items():
            activated = bool(self.cookie_store and
                             self.cookie_store.meta(ident.device_id).get("activated"))
            identities.append({
                "proxy": "直连" if key is None else key.split("@")[-1],
                "device_id": ident.device_id,
                "chrome_major": ident.chrome_major,
                "impersonate": ident.impersonate,
                "activated": activated,
                "current": key == current_key,
            })
        return {
            "transport_preferred": self.preferred,
            "transport_active": stats.get("last_transport", "-"),
            "stats": stats,
            "proxy": self.pool.status(),
            "fingerprint": current.summary() if current else None,
            "impersonate": (current.impersonate if current else None) or "auto(未定)",
            "cookies_persisted": bool(self.cookie_store and current and
                                      self.cookie_store.load(current.device_id)),
            "identities": identities,
        }

    def close(self):
        self._invalidate_transport()
