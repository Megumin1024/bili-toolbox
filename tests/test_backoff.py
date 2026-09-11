# -*- coding: utf-8 -*-
"""退避策略内核的离线单元测试。

全部离线：不 sleep、不读真实时钟（now 与随机源均注入）。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from core.backoff import (JITTER_RATIO, MAX_SINGLE_WAIT, backoff_seconds,
                          parse_retry_after)
from core.net_errors import ErrorKind


class ParseRetryAfterTests(unittest.TestCase):
    def test_missing_or_blank(self):
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after(""))
        self.assertIsNone(parse_retry_after("   "))

    def test_seconds(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after(" 30 "), 30.0)

    def test_non_positive_seconds_clamp_to_zero(self):
        self.assertEqual(parse_retry_after("0"), 0.0)
        self.assertEqual(parse_retry_after("-15"), 0.0)

    def test_clamped_to_max_single_wait(self):
        self.assertEqual(parse_retry_after("99999"), MAX_SINGLE_WAIT)

    def test_http_date_in_future(self):
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        later = now + timedelta(seconds=45)
        self.assertAlmostEqual(
            parse_retry_after(format_datetime(later), now=now), 45.0, places=3)

    def test_http_date_in_past(self):
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        earlier = now - timedelta(seconds=45)
        self.assertEqual(parse_retry_after(format_datetime(earlier), now=now), 0.0)

    def test_http_date_without_timezone_treated_as_utc(self):
        # 不带时区的 HTTP-date（如 "Wed, 21 Oct 2015 07:28:30"）按 UTC 解释。
        now = datetime(2015, 10, 21, 7, 28, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            parse_retry_after("Wed, 21 Oct 2015 07:28:30", now=now), 30.0, places=3)

    def test_unparsable(self):
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after("Wed, 32 Oct 2015 07:28:00 GMT"))


class BackoffSecondsTests(unittest.TestCase):
    def test_non_retryable_kinds_wait_zero(self):
        for kind in (ErrorKind.API_ERROR, ErrorKind.HTTP_ERROR, ErrorKind.UNKNOWN):
            self.assertEqual(backoff_seconds(kind, 0, rand=lambda: 0.0), 0.0, kind)

    def test_transport_errors_grow_exponentially(self):
        base = [backoff_seconds(ErrorKind.CONNECTION, a, rand=lambda: 0.0)
                for a in range(4)]
        self.assertEqual(base, [1.0, 2.0, 4.0, 8.0])

    def test_transport_base_is_capped(self):
        self.assertEqual(
            backoff_seconds(ErrorKind.TIMEOUT, 5, rand=lambda: 0.0), 8.0)

    def test_risk_uses_standard_backoff(self):
        self.assertEqual(
            backoff_seconds(ErrorKind.RISK, 1, rand=lambda: 0.0), 2.0)

    def test_rate_limit_backs_off_harder_and_is_capped(self):
        values = [backoff_seconds(ErrorKind.RATE_LIMIT, a, rand=lambda: 0.0)
                  for a in range(6)]
        self.assertEqual(values, [4.0, 8.0, 16.0, 30.0, 30.0, 30.0])

    def test_retry_after_takes_priority(self):
        self.assertEqual(
            backoff_seconds(ErrorKind.RATE_LIMIT, 4, retry_after=12.0,
                            rand=lambda: 0.0),
            12.0)
        # Retry-After 同样压过风控/连接类的基础退避。
        self.assertEqual(
            backoff_seconds(ErrorKind.RISK, 0, retry_after=7.0, rand=lambda: 0.0),
            7.0)

    def test_jitter_stays_within_ratio(self):
        _R = JITTER_RATIO
        low = backoff_seconds(ErrorKind.RATE_LIMIT, 0, rand=lambda: 0.0)
        high = backoff_seconds(ErrorKind.RATE_LIMIT, 0, rand=lambda: 1.0)
        self.assertEqual(low, 4.0)
        self.assertAlmostEqual(high, 4.0 * (1 + _R))

    def test_single_wait_is_clamped(self):
        self.assertEqual(
            backoff_seconds(ErrorKind.RATE_LIMIT, 0, retry_after=10_000.0,
                            rand=lambda: 1.0),
            MAX_SINGLE_WAIT)

    def test_zero_retry_after_keeps_zero(self):
        self.assertEqual(
            backoff_seconds(ErrorKind.RATE_LIMIT, 0, retry_after=0.0,
                            rand=lambda: 1.0),
            0.0)


if __name__ == "__main__":
    unittest.main()
