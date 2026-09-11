# -*- coding: utf-8 -*-
"""WBI 签名的离线测试。

分两类断言：
- **金标准值**：把算法结果钉死，防止以后误改（回归保护）；
- **结构性断言**：置换确实用了那张表、参数确实排序、过滤字符确实被剔除。

签名相对 B 站线上是否**正确**，离线测不了——那由实测覆盖（见 tools/user_dynamics）。
本文件里所有 WBI 密钥都是合成的，不含任何真实账号数据。
全程离线：不打网络、不读文件。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.transport import BiliApiError
from core.wbi import (MIXIN_KEY_ENC_TAB, WbiKeyCache, WbiKeyError, extract_key,
                      fetch_keys, keys_from_nav, mixin_key, sign_params,
                      signed_url)

IMG_KEY = "0123456789abcdef0123456789abcdef"
SUB_KEY = "fedcba9876543210fedcba9876543210"

# 由上面这对合成密钥算出的金标准（改动算法会立刻在这里变红）
GOLDEN_MIXIN = "1022a87ffdaf532cb45ee953dce8c96d"
GOLDEN_SIGN = ("mid=208259&ps=5&wts=1700000000"
               "&w_rid=414c77c8657ce3db1f51f979d55d0fb4")

NAV_OK = {"code": 0, "message": "0", "data": {
    "isLogin": True,
    "wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png"}}}
# 游客态：code=-101，但密钥就在同一个响应里——这正是要保住的那份数据
NAV_GUEST = {"code": -101, "message": "账号未登录", "data": {
    "isLogin": False,
    "wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png"}}}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ExtractKeyTests(unittest.TestCase):
    def test_strips_host_directory_and_extension(self):
        self.assertEqual(
            extract_key("https://i0.hdslb.com/bfs/wbi/abcd1234.png"), "abcd1234")

    def test_ignores_query_and_fragment(self):
        self.assertEqual(extract_key("https://x.com/a/b/key.png?t=1#f"), "key")

    def test_name_without_dot_is_kept_whole(self):
        self.assertEqual(extract_key("https://x.com/wbi/abcdef"), "abcdef")

    def test_missing_key_raises(self):
        with self.assertRaises(WbiKeyError):
            extract_key("https://x.com/wbi/")


class MixinKeyTests(unittest.TestCase):
    def test_matches_golden_value(self):
        self.assertEqual(mixin_key(IMG_KEY, SUB_KEY), GOLDEN_MIXIN)

    def test_is_exactly_32_chars(self):
        self.assertEqual(len(mixin_key(IMG_KEY, SUB_KEY)), 32)

    def test_uses_the_permutation_table(self):
        """换掉置换表结果必须变——否则说明表根本没生效。"""
        orig = IMG_KEY + SUB_KEY
        baseline = mixin_key(IMG_KEY, SUB_KEY)
        swapped = "".join(orig[i] for i in reversed(MIXIN_KEY_ENC_TAB))[:32]
        self.assertNotEqual(baseline, swapped)
        self.assertEqual(baseline, "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32])

    def test_short_keys_raise_instead_of_index_error(self):
        with self.assertRaises(WbiKeyError):
            mixin_key("short", "also-short")


class SignParamsTests(unittest.TestCase):
    def test_matches_golden_value(self):
        self.assertEqual(sign_params({"mid": 208259, "ps": 5},
                                     IMG_KEY, SUB_KEY, wts=1700000000),
                         GOLDEN_SIGN)

    def test_is_reproducible_for_a_fixed_wts(self):
        a = sign_params({"mid": 1}, IMG_KEY, SUB_KEY, wts=1700000000)
        b = sign_params({"mid": 1}, IMG_KEY, SUB_KEY, wts=1700000000)
        self.assertEqual(a, b)

    def test_keys_are_sorted_so_dict_order_does_not_matter(self):
        a = sign_params({"mid": 1, "offset": "x", "platform": "web"},
                        IMG_KEY, SUB_KEY, wts=1700000000)
        b = sign_params({"platform": "web", "offset": "x", "mid": 1},
                        IMG_KEY, SUB_KEY, wts=1700000000)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("mid=1&offset=x&platform=web&wts="))

    def test_filter_chars_are_stripped_like_the_frontend(self):
        got = sign_params({"k": "a!b'c(d)e*f"}, IMG_KEY, SUB_KEY, wts=1700000000)
        self.assertIn("k=abcdef", got)

    def test_wts_defaults_to_now(self):
        got = sign_params({"mid": 1}, IMG_KEY, SUB_KEY)
        wts = int(dict(p.split("=", 1) for p in got.split("&"))["wts"])
        self.assertGreater(wts, 1_600_000_000)

    def test_w_rid_is_md5_hex(self):
        got = sign_params({"mid": 1}, IMG_KEY, SUB_KEY, wts=1700000000)
        rid = got.rsplit("w_rid=", 1)[1]
        self.assertEqual(len(rid), 32)
        int(rid, 16)  # 非十六进制会在这里炸

    def test_signed_url_appends_query(self):
        url = signed_url("https://api.example.invalid/x", {"mid": 1},
                         IMG_KEY, SUB_KEY, wts=1700000000)
        self.assertTrue(url.startswith("https://api.example.invalid/x?"))
        self.assertIn("w_rid=", url)


class KeysFromNavTests(unittest.TestCase):
    def test_reads_keys_when_logged_in(self):
        self.assertEqual(keys_from_nav(NAV_OK), (IMG_KEY, SUB_KEY))

    def test_reads_keys_from_guest_response(self):
        """游客态 nav 返回 -101，但密钥仍在——这是纯游客工具唯一的签名来源。"""
        self.assertEqual(keys_from_nav(NAV_GUEST), (IMG_KEY, SUB_KEY))

    def test_missing_wbi_img_raises(self):
        with self.assertRaises(WbiKeyError):
            keys_from_nav({"code": -352, "data": {}})

    def test_empty_payload_raises(self):
        with self.assertRaises(WbiKeyError):
            keys_from_nav(None)


class FetchKeysTests(unittest.TestCase):
    """fetch_keys 必须能顺着 BiliApiError 把 -101 里的密钥捞回来。

    这条正是 core.transport 让 BiliApiError 携带 payload 的唯一理由。
    """

    def test_recovers_keys_hidden_inside_api_error(self):
        with patch("core.session.http_get_json",
                   side_effect=BiliApiError("API code=-101 msg=账号未登录",
                                            payload=NAV_GUEST)):
            self.assertEqual(fetch_keys(), (IMG_KEY, SUB_KEY))

    def test_api_error_without_payload_becomes_key_error(self):
        with patch("core.session.http_get_json",
                   side_effect=BiliApiError("API code=-101", payload=None)):
            with self.assertRaises(WbiKeyError):
                fetch_keys()

    def test_success_path_returns_keys(self):
        with patch("core.session.http_get_json", return_value=NAV_OK):
            self.assertEqual(fetch_keys(), (IMG_KEY, SUB_KEY))

    def test_cancel_is_forwarded_to_the_http_layer(self):
        """取消谓词必须透传到 HTTP 层。

        取密钥会退避重试；不透传的话，用户在等待中按取消要等满 fetch_json 的
        总退避预算（TOTAL_WAIT_BUDGET）才有反应。
        """
        seen = {}

        def fake(url, retries=2, cancel=None):
            seen["cancel"] = cancel
            return NAV_OK

        def sentinel():
            return True

        with patch("core.session.http_get_json", side_effect=fake):
            self.assertEqual(fetch_keys(cancel=sentinel), (IMG_KEY, SUB_KEY))
        self.assertIs(seen["cancel"], sentinel,
                      "传下去的必须是调用方给的谓词本身，不能另造一个")


class KeyCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = []

        def fetch():
            self.calls.append(1)
            return (IMG_KEY, SUB_KEY)

        self.cache = WbiKeyCache(fetch=fetch, ttl=100.0, clock=self.clock)

    def test_fetches_once_within_ttl(self):
        self.cache.get()
        self.cache.get()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.cache.fetches, 1)

    def test_refetches_after_ttl_expires(self):
        self.cache.get()
        self.clock.now += 101.0
        self.cache.get()
        self.assertEqual(len(self.calls), 2)

    def test_ttl_boundary_is_exclusive(self):
        self.cache.get()
        self.clock.now += 100.0
        self.cache.get()
        self.assertEqual(len(self.calls), 2, "正好到期应重取")

    def test_force_bypasses_cache(self):
        self.cache.get()
        self.cache.get(force=True)
        self.assertEqual(len(self.calls), 2)

    def test_invalidate_forces_next_fetch(self):
        self.cache.get()
        self.cache.invalidate()
        self.cache.get()
        self.assertEqual(len(self.calls), 2)

    def test_returns_tuple(self):
        self.assertEqual(self.cache.get(), (IMG_KEY, SUB_KEY))

    def test_default_fetch_forwards_cancel(self):
        """不注入 fetch 时，cancel 要一路传到默认取密钥实现。

        这条防的是"构造参数收了 cancel 却没接上"——加了参数不等于接上了线。
        """
        def sentinel():
            return False

        with patch("core.wbi.fetch_keys", return_value=(IMG_KEY, SUB_KEY)) as m:
            WbiKeyCache(cancel=sentinel).get()
        m.assert_called_once_with(cancel=sentinel)


if __name__ == "__main__":
    unittest.main()
