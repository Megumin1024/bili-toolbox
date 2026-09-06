# -*- coding: utf-8 -*-
"""代理池管理。

- round-robin 轮换；直连作为第一个条目
- 失败计数达到阈值 → 该代理进入冷却期并自动切到下一个
- 成功即清零失败计数
- 支持快照/恢复（供上层在风险事件后回滚状态）
"""
import threading
import time

VALID_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


class ProxyEntry:
    __slots__ = ("url", "failures", "failed_until", "total_ok", "total_fail")

    def __init__(self, url):
        self.url = url                      # None 表示直连
        self.failures = 0
        self.failed_until = 0.0
        self.total_ok = 0
        self.total_fail = 0

    @property
    def cooling(self):
        return time.time() < self.failed_until

    def display(self):
        if self.url is None:
            return "直连"
        # 隐藏代理 URL 中的用户名密码
        rest = self.url.split("@")[-1]
        scheme = self.url.split("://", 1)[0]
        return f"{scheme}://{rest}"


class ProxyPool:
    def __init__(self, spec=None, failure_threshold=2, cooldown_seconds=180.0):
        """spec 示例: "direct,http://u:p@1.2.3.4:8080,socks5://5.6.7.8:1080"，
        逗号分隔；direct/空串表示直连；None 表示仅直连。"""
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.entries = []
        for item in (spec.split(",") if spec else []) or ["direct"]:
            item = item.strip()
            if not item or item.lower() == "direct":
                url = None
            else:
                scheme = item.split("://", 1)[0].lower()
                if scheme not in VALID_SCHEMES:
                    raise ValueError(f"不支持的代理协议: {item}（支持 {VALID_SCHEMES}）")
                url = item
            if not any(e.url == url for e in self.entries):
                self.entries.append(ProxyEntry(url))
        self._idx = 0
        self.rotations = 0
        self.lock = threading.Lock()

    # ---------- 选择 ----------
    def current(self):
        with self.lock:
            return self.entries[self._idx]

    def current_url(self):
        return self.current().url

    def has_alternative(self):
        return len(self.entries) > 1

    def _next_available_index(self):
        n = len(self.entries)
        for step in range(1, n + 1):
            i = (self._idx + step) % n
            if not self.entries[i].cooling:
                return i
        return None  # 全部在冷却

    def rotate(self, reason=""):
        """轮换到下一个可用代理；全部冷却时留在原地并返回 False。"""
        with self.lock:
            nxt = self._next_available_index()
            if nxt is None:
                return False
            changed = nxt != self._idx
            self._idx = nxt
            if changed:
                self.rotations += 1
            return changed

    # ---------- 健康度 ----------
    def mark_success(self):
        with self.lock:
            e = self.entries[self._idx]
            e.failures = 0
            e.total_ok += 1

    def mark_failure(self):
        """失败计数；达阈值则冷却并轮换。返回是否发生了轮换。"""
        with self.lock:
            e = self.entries[self._idx]
            e.failures += 1
            e.total_fail += 1
            if e.failures >= self.failure_threshold:
                e.failed_until = time.time() + self.cooldown_seconds
                e.failures = 0
                nxt = self._next_available_index()
                if nxt is not None and nxt != self._idx:
                    self._idx = nxt
                    self.rotations += 1
                    return True
                # 无处可轮换（单条目或全部冷却）：留在原地，由调用方退避重试
                return False
            return False

    # ---------- 状态 ----------
    def snapshot(self):
        with self.lock:
            return (self._idx, self.rotations,
                    [(e.url, e.failures, e.failed_until) for e in self.entries])

    def restore(self, snap):
        with self.lock:
            self._idx, self.rotations, entries = snap
            for e, (url, failures, failed_until) in zip(self.entries, entries):
                e.url, e.failures, e.failed_until = url, failures, failed_until

    def status(self):
        with self.lock:
            cur = self.entries[self._idx]
            return {
                "current": cur.display(),
                "current_cooling": cur.cooling,
                "size": len(self.entries),
                "rotations": self.rotations,
                "entries": [
                    {"proxy": e.display(), "cooling": e.cooling,
                     "ok": e.total_ok, "fail": e.total_fail}
                    for e in self.entries
                ],
            }
