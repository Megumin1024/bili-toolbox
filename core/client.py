# -*- coding: utf-8 -*-
"""统一请求层：代理×指纹绑定(第1/3层) + cookie预热/持久化/激活(第2层) + 代理池(第4层)。

每个代理出口绑定一套独立的随机指纹身份（BrowserIdentity），互不串联；
cookie/激活状态按身份 deviceId 键控持久化。

重试策略：
- 风控信号（412/403/-352/-412）→ 强制重新预热 cookie；有备用代理则轮换
- 限流信号（429/-799/-502）→ 更长退避；有备用代理则轮换
- 网络/传输错误 → 计入代理失败数，达阈值自动冷却并轮换；否则指数退避重试
- API 非 0 code（非风控）→ 直接抛出，由采集层处理
- 所有退避遵守 Retry-After、带抖动，并受 TOTAL_WAIT_BUDGET 总预算约束；
  cancel 谓词可在等待中打断（分段轮询，约 0.25s 粒度）
"""
import threading
import time

from .activation import activate_via_transport
from .backoff import backoff_seconds
from .cancel import TaskCancelledError, wait as wait_or_cancel
from .fingerprint import BrowserIdentity
from .gate import RequestGate, shared_gate
from .net_errors import ErrorKind, classify
from .proxy import ProxyPool, redact_url
from .redact import sanitize_text
from .transport import (BiliApiError, BiliRateLimitError, CookieStore,
                        RiskBlocked, TransportError, build_transport)

RETRY_ATTEMPTS = 3
# 一次 fetch_json 内所有退避等待的累计上限：超过即停止重试并抛最后一个异常，
# 防止重试叠加大退避把一次调用拖到分钟级。
TOTAL_WAIT_BUDGET = 90.0


class BiliClient:
    def __init__(self, pool: ProxyPool, preferred_transport="auto", cookie_path=None,
                 log=None, clock=None, sleep=None, cancel=None, gate=None):
        self.pool = pool
        self.preferred = preferred_transport
        self.cookie_store = CookieStore(cookie_path) if cookie_path else None
        # 日志出口强制脱敏：本层抛出的异常可能带代理 URL / 底层库异常原文，
        # 而 log 会流进 GUI 日志面板与 stdout，不能指望每个调用方自己清洗。
        raw_log = log or (lambda msg: print(msg, flush=True))
        self.log = lambda msg: raw_log(sanitize_text(msg))
        # 可注入依赖：clock 用于延迟统计，sleep 用于退避等待，cancel 为默认取消谓词。
        # 三者都可替换，网络层因此可以完全离线测试。
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._cancel = cancel or (lambda: False)
        self._identities = {}      # 代理出口 -> BrowserIdentity（代理×指纹绑定）
        # 全局限速 + 熔断。生产路径共用进程级闸门，让 session 单例与监控自建
        # client 共享同一份背压；显式注入了 clock/sleep 的调用方（测试、离线
        # 复现）默认拿到独立闸门，避免把状态漏进全局。
        if gate is not None:
            self.gate = gate
        elif clock is None and sleep is None:
            self.gate = shared_gate()
        else:
            self.gate = RequestGate(clock=self._clock, sleep=self._sleep)
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
                # 只回显脱敏后的出口（key 含账号密码，不得进异常消息）；
                # 用 key 而非 current_display()——mark_failure 可能已轮换到下一个出口。
                raise TransportError(
                    f"代理健康检查失败: {redact_url(key)}")
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

    # ---------- 取消与退避 ----------
    def _cancelled(self, cancel=None):
        predicate = cancel if cancel is not None else self._cancel
        return bool(predicate())

    def _wait(self, seconds, cancel=None):
        """可中断的分段等待；被取消返回 False。

        切片逻辑与 core.cancel.wait 共用一份实现，调用方注入的 sleep 一并透传。
        """
        return wait_or_cancel(seconds, lambda: self._cancelled(cancel),
                              sleep=self._sleep)

    # ---------- 对外 ----------
    def fetch_json(self, url, retries=RETRY_ATTEMPTS, cancel=None):
        """请求 JSON 并解析。重试/风控/统计策略见 _request。"""
        return self._request(url, "get_json", retries, cancel)

    def fetch_bytes(self, url, retries=RETRY_ATTEMPTS, cancel=None):
        """请求原始字节（二进制接口，如弹幕 protobuf 分段）。

        除了不解析响应体，闸门、重试、退避、统计与探针回报与 fetch_json 完全同一
        段代码——二进制通道不另开一条简化版风控路径，否则风控信号会被当数据吞掉。
        """
        return self._request(url, "get_bytes", retries, cancel)

    def _request(self, url, method, retries, cancel):
        """共享请求循环：两种通道在这里只差一行。

        - 发请求前先过全局闸门：全局限速 + 熔断冷却（见 core.gate）；
        - 退避遵守 Retry-After、带抖动，并受 TOTAL_WAIT_BUDGET 总预算约束；
        - cancel 为真值时立即抛出 TaskCancelledError，不计入任何失败统计。
        """
        last_exc = None
        waited = 0.0
        for attempt in range(retries):
            if self._cancelled(cancel):
                raise TaskCancelledError()
            if not self.gate.acquire(cancel, on_wait=self._log_gate_wait):
                raise TaskCancelledError()
            # transport 与 reported 必须同时在此初始化：
            #   - transport 为 None 表示失败发生在"拿到通道"之前（代理健康检查、
            #     通道构建），这条路径过去完全绕过了重试/统计/轮换；
            #   - reported 用来保证闸门探针有且只有一次回报（见 finally）。
            transport = None
            reported = False
            try:
                transport = self._get_transport()
                started = self._clock()
                data = getattr(transport, method)(url)
                self.stats["requests"] += 1
                self.stats["last_latency_ms"] = int((self._clock() - started) * 1000)
                self.stats["last_transport"] = transport.name
                self.pool.mark_success()
                self.gate.record_success()
                reported = True
                return data
            except BiliApiError:
                # 业务非零 code（如视频已删除、参数非法）属调用方语义错误：
                # 直接抛出，不重试、不计 net_errors、不触达代理冷却，
                # 也不作为"平台在拦我们"的证据。探针由 finally 释放。
                raise
            except RiskBlocked as exc:
                last_exc = exc
                self.stats["risk_events"] += 1
                self.gate.record_block(f"risk:{exc}")
                reported = True
                self.log(f"[risk] 风控拦截({transport.name}): {exc}，重新预热并"
                         f"{'轮换代理' if self.pool.has_alternative() else '重试'}")
                transport.warmup(force=True)
                self._invalidate_transport()          # 重建通道（新会话/新 cookie）
                wait = backoff_seconds(ErrorKind.RISK, attempt)
                if not self.pool.rotate(f"risk:{exc}") and self.pool.has_alternative():
                    wait = max(wait, 2.0)             # 全部冷却：等冷却起步再试
            except BiliRateLimitError as exc:
                last_exc = exc
                self.stats["rate_limit_events"] += 1
                self.gate.record_block(f"ratelimit:{exc}")
                reported = True
                rotated = self.pool.mark_failure()
                if rotated or (self.pool.has_alternative() and attempt >= 1):
                    self.pool.rotate(f"ratelimit:{exc}")
                    self._invalidate_transport()
                retry_after = getattr(exc, "retry_after", None)
                if retry_after:
                    self.log(f"[rate] 限流({transport.name}): {exc}，"
                             f"遵守 Retry-After {retry_after:.0f}s")
                else:
                    self.log(f"[rate] 限流({transport.name}): {exc}，加倍退避")
                wait = backoff_seconds(ErrorKind.RATE_LIMIT, attempt,
                                       retry_after=retry_after)
            except TransportError as exc:
                last_exc = exc
                self.stats["net_errors"] += 1
                if transport is None:
                    # 失败在通道构建/健康检查阶段：_get_transport 内部已经调用过
                    # pool.mark_failure()（并按池策略自行轮换），这里再记一次会让
                    # 失败计数翻倍——半个阈值就能把出口打进冷却。所以只保留常规
                    # 路径的"第二次仍失败就换出口"，计数交给池自己。
                    self._invalidate_transport()   # 清掉已 close 的旧通道引用
                    if self.pool.has_alternative() and attempt >= 1:
                        self.pool.rotate(f"transport:{exc}")
                    self.log(f"[net] 通道不可用({exc})，退避重试")
                else:
                    rotated = self.pool.mark_failure()
                    if rotated or (self.pool.has_alternative() and attempt >= 1):
                        self.pool.rotate(f"error:{exc}")
                        self._invalidate_transport()
                    self.log(f"[net] 传输失败({transport.name}): {exc}，退避重试")
                wait = backoff_seconds(classify(exc=exc), attempt)
            finally:
                if not reported:
                    # 闸门探针必须无条件落地。半开时 acquire() 会占住唯一的探针
                    # 名额，若这条路径没回报任何成败，探针就悬空到 probe_timeout，
                    # 期间全进程的 acquire() 都在"等待半开探针结果"里空转——
                    # 一次健康检查失败就能造成 90 秒的静默停摆。
                    self.gate.record_neutral()

            if attempt + 1 >= retries:
                break                                  # 最后一次失败不再空等
            if waited + wait > TOTAL_WAIT_BUDGET:
                self.log(f"[retry] 累计退避将超过 {TOTAL_WAIT_BUDGET:.0f}s 预算，停止重试")
                break
            if not self._wait(wait, cancel):
                raise TaskCancelledError()
            waited += wait
        raise last_exc

    def _log_gate_wait(self, seconds, reason):
        """闸门即将暂停时把它写进日志——否则任务会静默卡住。"""
        self.log(f"[gate] {reason}，暂停 {seconds:.0f}s（全局限速/熔断）")

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
            "gate": self.gate.status(),
            "proxy": self.pool.status(),
            "fingerprint": current.summary() if current else None,
            "impersonate": (current.impersonate if current else None) or "auto(未定)",
            "cookies_persisted": bool(self.cookie_store and current and
                                      self.cookie_store.load(current.device_id)),
            "identities": identities,
        }

    def close(self):
        self._invalidate_transport()
