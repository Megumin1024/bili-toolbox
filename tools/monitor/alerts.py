# -*- coding: utf-8 -*-
"""纯 Python 的监控提醒规则与单次会话状态。

本模块不依赖 Qt、不发网络请求，也不读取历史文件。调用方需要把每次
采集成功/失败的安全事件传入，并负责把返回的 AlertEvent 交给通知通道。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import re
from typing import Any, Mapping


DEFAULT_MILESTONES = (10000, 50000, 100000, 500000, 1000000)
MAX_INTEGER_DIGITS = 20
MAX_SAFE_INTEGER = 10 ** MAX_INTEGER_DIGITS - 1
MAX_WINDOW_MINUTES = 10080
MIN_SAMPLE_INTERVAL_SECONDS = 5
WINDOW_BOUNDARY_MARGIN_SAMPLES = 2
MAX_RECENT_SAMPLES = (
    math.ceil(MAX_WINDOW_MINUTES * 60 / MIN_SAMPLE_INTERVAL_SECONDS)
    + WINDOW_BOUNDARY_MARGIN_SAMPLES
)


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, number))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def _safe_nonnegative_int(value: Any) -> int | None:
    """只接受最多 20 位十进制数字，避免超长输入触发 Python 限制。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_SAFE_INTEGER else None
    try:
        text = str(value).strip()
    except (TypeError, ValueError, OverflowError):
        return None
    if not re.fullmatch(r"\d{1,20}", text):
        return None
    try:
        number = int(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= MAX_SAFE_INTEGER else None


def _safe_positive_int(value: Any) -> int | None:
    number = _safe_nonnegative_int(value)
    return number if number is not None and number > 0 else None


def parse_milestones(value: Any) -> tuple[int, ...]:
    """解析逗号分隔的正整数，自动去空格、去重并升序。"""
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        try:
            raw_values = str(value or "").split(",")
        except (TypeError, ValueError, OverflowError):
            raw_values = ()
    result = set()
    for raw in raw_values:
        number = _safe_positive_int(raw)
        if number is not None:
            result.add(number)
    return tuple(sorted(result))


def milestones_text(value: Any) -> str:
    values = parse_milestones(value)
    return ",".join(str(number) for number in values)


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = False
    windows_enabled: bool = True
    sound_enabled: bool = False

    milestone_enabled: bool = True
    milestones: tuple[int, ...] = DEFAULT_MILESTONES

    stagnation_enabled: bool = True
    stagnation_window_min: int = 30
    stagnation_max_growth: int = 0

    spike_enabled: bool = True
    spike_window_min: int = 5
    spike_min_absolute: int = 10000
    spike_min_relative_percent: float = 20.0
    spike_cooldown_min: int = 10

    disconnect_enabled: bool = True
    disconnect_failures: int = 3

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "AlertConfig":
        data = raw if isinstance(raw, Mapping) else {}
        milestones = (
            DEFAULT_MILESTONES
            if "milestones" not in data
            else parse_milestones(data.get("milestones"))
        )
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            windows_enabled=_as_bool(data.get("windows_enabled"), True),
            sound_enabled=_as_bool(data.get("sound_enabled"), False),
            milestone_enabled=_as_bool(data.get("milestone_enabled"), True),
            milestones=milestones,
            stagnation_enabled=_as_bool(data.get("stagnation_enabled"), True),
            stagnation_window_min=_as_int(data.get("stagnation_window_min"), 30, 1, 10080),
            stagnation_max_growth=_as_int(data.get("stagnation_max_growth"), 0, 0, 2_147_483_647),
            spike_enabled=_as_bool(data.get("spike_enabled"), True),
            spike_window_min=_as_int(data.get("spike_window_min"), 5, 1, 10080),
            spike_min_absolute=_as_int(data.get("spike_min_absolute"), 10000, 1, 2_147_483_647),
            spike_min_relative_percent=_as_float(
                data.get("spike_min_relative_percent"), 20.0, 0.0, 10000.0),
            spike_cooldown_min=_as_int(data.get("spike_cooldown_min"), 10, 0, 10080),
            disconnect_enabled=_as_bool(data.get("disconnect_enabled"), True),
            disconnect_failures=_as_int(data.get("disconnect_failures"), 3, 1, 100),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "windows_enabled": self.windows_enabled,
            "sound_enabled": self.sound_enabled,
            "milestone_enabled": self.milestone_enabled,
            "milestones": milestones_text(self.milestones),
            "stagnation_enabled": self.stagnation_enabled,
            "stagnation_window_min": self.stagnation_window_min,
            "stagnation_max_growth": self.stagnation_max_growth,
            "spike_enabled": self.spike_enabled,
            "spike_window_min": self.spike_window_min,
            "spike_min_absolute": self.spike_min_absolute,
            "spike_min_relative_percent": self.spike_min_relative_percent,
            "spike_cooldown_min": self.spike_cooldown_min,
            "disconnect_enabled": self.disconnect_enabled,
            "disconnect_failures": self.disconnect_failures,
        }


@dataclass(frozen=True)
class AlertEvent:
    kind: str
    title: str
    message: str
    ts: float


def _count(value: Any) -> int | None:
    return _safe_nonnegative_int(value)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


class _WindowState:
    """为一个固定时间窗口维护 O(1) 的最新边界样本。"""

    def __init__(self, window_min: int):
        self.duration = window_min * 60
        self.pending: deque[tuple[float, int]] = deque(maxlen=MAX_RECENT_SAMPLES)
        self.anchor: tuple[float, int] | None = None

    def reset(self) -> None:
        self.pending.clear()
        self.anchor = None

    def append(self, sample: tuple[float, int]) -> None:
        current_ts = sample[0]
        while self.pending and current_ts - self.pending[0][0] >= self.duration:
            self.anchor = self.pending.popleft()
        self.pending.append(sample)


class AlertSession:
    """一次监控启动对应的提醒状态机。"""

    def __init__(self, config: AlertConfig | Mapping[str, Any] | None = None,
                 session_id: str = ""):
        self.config = config if isinstance(config, AlertConfig) else AlertConfig.from_mapping(config)
        self.session_id = str(session_id or "session")
        self._baseline_set = False
        self._last_ts: float | None = None
        self._last_view: int | None = None
        self._samples: deque[tuple[float, int]] = deque(maxlen=MAX_RECENT_SAMPLES)
        self._window_states = {
            window_min: _WindowState(window_min)
            for window_min in {
                self.config.stagnation_window_min,
                self.config.spike_window_min,
            }
        }
        self._fired_milestones: set[int] = set()
        self._stagnation_active = False
        self._last_spike_ts: float | None = None
        self._disconnect_alerted = False
        self._stopped = False

    @property
    def baseline_set(self) -> bool:
        return self._baseline_set

    @property
    def fired_milestones(self) -> frozenset[int]:
        return frozenset(self._fired_milestones)

    def stop(self) -> None:
        """主动停止后冻结状态，不产生断线或恢复提醒。"""
        self._stopped = True

    def _event(self, kind: str, title: str, message: str, ts: float) -> AlertEvent:
        return AlertEvent(kind=kind, title=title, message=message, ts=ts)

    def _append_sample(self, sample: tuple[float, int]) -> None:
        self._samples.append(sample)
        for state in self._window_states.values():
            state.append(sample)

    def _clear_window_history(self) -> None:
        self._samples.clear()
        for state in self._window_states.values():
            state.reset()
        self._stagnation_active = False
        self._last_spike_ts = None

    def _reset_window_baseline(self, sample_ts: float, count: int) -> None:
        self._clear_window_history()
        self._baseline_set = True
        self._last_ts = sample_ts
        self._last_view = count
        self._append_sample((sample_ts, count))

    def process_sample(self, ts: Any, view: Any) -> list[AlertEvent]:
        if self._stopped or not self.config.enabled:
            return []
        sample_ts = _timestamp(ts)
        count = _count(view)
        if sample_ts is None:
            return []
        if self._last_ts is not None and sample_ts < self._last_ts:
            return []

        events: list[AlertEvent] = []
        recovering = self._disconnect_alerted
        if recovering:
            events.append(self._event(
                "recovered", "监控已恢复", "采集已恢复，提醒规则重新布防。", sample_ts))
            self._disconnect_alerted = False
        if count is None:
            if recovering:
                self._clear_window_history()
                self._baseline_set = False
                self._last_ts = sample_ts
                self._last_view = None
            return events

        previous_view = self._last_view
        if not self._baseline_set:
            self._baseline_set = True
            self._last_ts = sample_ts
            self._last_view = count
            self._append_sample((sample_ts, count))
            # 首条样本只建立基线，不补发之前已经达到的里程碑。
            self._fired_milestones.update(
                milestone for milestone in self.config.milestones if milestone <= count)
            return events

        if previous_view is not None and self.config.milestone_enabled:
            crossed = [
                milestone for milestone in self.config.milestones
                if milestone not in self._fired_milestones
                and previous_view < milestone <= count
            ]
            if crossed:
                self._fired_milestones.update(crossed)
                values = "、".join(str(value) for value in crossed)
                events.append(self._event(
                    "milestone", "播放量里程碑", f"播放量已达到 {values}。", sample_ts))

        if recovering:
            # 恢复样本可以继续判断里程碑，但不能把断线前的窗口带入停滞/突增。
            self._reset_window_baseline(sample_ts, count)
            return events

        # 只接受单调不倒退的时间进入窗口历史；播放量下降仍是有效样本，
        # 这样停滞规则可以把下降视为没有增长，而突增规则单独抑制下降。
        self._append_sample((sample_ts, count))
        if self.config.stagnation_enabled:
            anchor = self._window_anchor(sample_ts, self.config.stagnation_window_min)
            if anchor is not None:
                growth = count - anchor[1]
                if growth > self.config.stagnation_max_growth:
                    self._stagnation_active = False
                elif not self._stagnation_active:
                    self._stagnation_active = True
                    events.append(self._event(
                        "stagnation", "增长停滞",
                        f"最近 {self.config.stagnation_window_min} 分钟播放量增长 {growth}，未超过允许值。",
                        sample_ts))

        if (self.config.spike_enabled and previous_view is not None
                and count >= previous_view):
            anchor = self._window_anchor(sample_ts, self.config.spike_window_min)
            if anchor is not None and anchor[1] > 0:
                absolute_growth = count - anchor[1]
                relative_growth = absolute_growth / anchor[1] * 100
                cooldown = self.config.spike_cooldown_min * 60
                cooldown_ok = (
                    self._last_spike_ts is None
                    or sample_ts - self._last_spike_ts >= cooldown
                )
                if (absolute_growth >= self.config.spike_min_absolute
                        and relative_growth >= self.config.spike_min_relative_percent
                        and cooldown_ok):
                    self._last_spike_ts = sample_ts
                    events.append(self._event(
                        "spike", "播放量异常突增",
                        f"最近 {self.config.spike_window_min} 分钟增长 {absolute_growth}（{relative_growth:.1f}%）。",
                        sample_ts))

        self._last_ts = sample_ts
        self._last_view = count
        return events

    def process_failure(self, ts: Any, consecutive_failures: Any) -> list[AlertEvent]:
        if self._stopped or not self.config.enabled or not self.config.disconnect_enabled:
            return []
        failure_ts = _timestamp(ts)
        if failure_ts is None:
            return []
        failures = _as_int(consecutive_failures, 0, 0, 2_147_483_647)
        if failures < self.config.disconnect_failures or self._disconnect_alerted:
            return []
        self._disconnect_alerted = True
        return [self._event(
            "disconnect", "监控断线",
            f"已连续采集失败 {failures} 次。", failure_ts)]

    def _window_anchor(self, current_ts: float, window_min: int) -> tuple[float, int] | None:
        state = self._window_states.get(window_min)
        return state.anchor if state is not None else None
