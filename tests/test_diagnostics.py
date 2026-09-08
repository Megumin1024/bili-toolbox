# -*- coding: utf-8 -*-
"""环境诊断与最近错误的无网络单元测试。"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core import config, diagnostics, output


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        diagnostics._last_error = None
        diagnostics._last_error_ts = 0.0

    def tearDown(self):
        diagnostics._last_error = None
        diagnostics._last_error_ts = 0.0

    def test_missing_dependency_returns_clear_failure(self):
        def importer(_name):
            raise ImportError("dependency is missing")

        item = diagnostics._import_check("openpyxl", "openpyxl", importer)
        self.assertEqual(item.status, diagnostics.STATUS_ERROR)
        self.assertIn("依赖无法导入", item.summary)
        self.assertIn("ImportError", item.details)

    def test_collect_diagnostics_uses_injected_importer_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "config.json").write_text("{}", encoding="utf-8")
            output_dir = root / "output"
            output_dir.mkdir()
            static_dir = root / "tools" / "monitor" / "static"
            static_dir.mkdir(parents=True)
            (static_dir / "index.html").write_text("<html/>", encoding="utf-8")

            def importer(_name):
                return types.SimpleNamespace(__version__="test")

            with patch.object(config, "CONFIG_DIR", config_dir), \
                    patch.object(config, "CONFIG_FILE", config_dir / "config.json"), \
                    patch.object(config, "COOKIE_FILE", config_dir / "session.json"), \
                    patch.object(diagnostics, "RECENT_ERROR_FILE",
                                 config_dir / "recent_error.json"), \
                    patch.object(sys, "_MEIPASS", str(root), create=True), \
                    patch.object(output, "default_out_dir",
                                 return_value=output_dir):
                items = diagnostics.collect_diagnostics(
                    {"out_dir": str(output_dir)}, importer=importer
                )

            self.assertTrue(items)
            self.assertTrue(all(item.status == diagnostics.STATUS_OK for item in items))

    def test_damaged_config_returns_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            with patch.object(config, "CONFIG_FILE", path):
                item = diagnostics._config_file_check()
        self.assertEqual(item.status, diagnostics.STATUS_ERROR)
        self.assertIn("JSON", item.summary)

    def test_missing_and_unwritable_output_directory_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = diagnostics._directory_check(
                "默认输出目录", root / "missing", diagnostics.STATUS_WARNING,
                diagnostics.STATUS_WARNING, "输出目录"
            )
            self.assertEqual(missing.status, diagnostics.STATUS_WARNING)
            self.assertIn("尚未创建", missing.summary)

            existing = root / "existing"
            existing.mkdir()
            with patch.object(diagnostics, "_probe_write",
                              return_value=(False, "permission denied")):
                unwritable = diagnostics._directory_check(
                    "默认输出目录", existing, diagnostics.STATUS_WARNING,
                    diagnostics.STATUS_WARNING, "输出目录"
                )
        self.assertEqual(unwritable.status, diagnostics.STATUS_WARNING)
        self.assertIn("不可写", unwritable.summary)

    def test_sanitize_text_redacts_secrets_urls_ips_and_spaced_paths(self):
        raw = (
            "Cookie=secret-cookie; SESSDATA=secret-sess; bili_jct=csrf; "
            "access_token=access-secret; refresh_token=refresh-secret; "
            "Authorization: Bearer auth-secret; proxy_username=proxy-user; "
            "proxy_password=proxy-pass; 代理密码：代理密钥; "
            "https://example.com/api?access_token=url-secret; "
            "192.168.1.10; C:\\Project\\BiliToolbox\\Temp User\\out; "
            "Python 3.14.6"
        )
        safe = diagnostics.sanitize_text(raw)
        for secret in (
            "secret-cookie", "secret-sess", "csrf", "access-secret",
            "refresh-secret", "auth-secret", "proxy-user", "proxy-pass",
            "代理密钥", "url-secret", "192.168.1.10",
            "C:\\Project\\BiliToolbox\\Temp User\\out",
        ):
            self.assertNotIn(secret, safe)
        self.assertIn("3.14.6", safe)
        self.assertIn("[已脱敏]", safe)
        self.assertIn("[网络地址已脱敏]", safe)
        self.assertIn("[本地路径已脱敏]", safe)

    def test_sanitize_text_redacts_json_and_dict_repr_secrets(self):
        raw = (
            'json={"Cookie": "json-cookie", "access_token": "json-access", '
            '"refresh_token": "json-refresh", "proxy_password": "json-pass"} '
            "dict={'Cookie': 'dict-cookie', 'SESSDATA': 'dict-sess', "
            "'bili_jct': 'dict-jct', 'proxy_username': 'dict-user'} "
            "代理用户名：中文代理用户 "
            "Authorization: Bearer header-token"
        )
        safe = diagnostics.sanitize_text(raw)
        for secret in (
            "json-cookie", "json-access", "json-refresh", "json-pass",
            "dict-cookie", "dict-sess", "dict-jct", "dict-user",
            "header-token", "中文代理用户",
        ):
            self.assertNotIn(secret, safe)
        self.assertIn('"Cookie": "[已脱敏]"', safe)
        self.assertIn("'SESSDATA': '[已脱敏]'", safe)

    def test_format_diagnostics_is_redacted_by_default(self):
        item = diagnostics.DiagnosticItem(
            "输出目录", diagnostics.STATUS_WARNING, "目录存在",
            "路径：C:\\Project\\BiliToolbox\\Temp User\\导出",
            "proxy_password=secret-pass",
        )
        report = diagnostics.format_diagnostics([item])
        self.assertNotIn("C:\\Project", report)
        self.assertNotIn("secret-pass", report)
        self.assertIn("输出目录", report)
        self.assertIn("警告", report)

    def test_recent_error_is_saved_sanitized_and_loaded_as_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "config"
            base_dir = root / "base"
            with patch.object(config, "CONFIG_DIR", config_dir), \
                    patch.object(diagnostics, "RECENT_ERROR_FILE",
                                 config_dir / "recent_error.json"), \
                    patch.object(output, "app_base_dir", return_value=base_dir):
                diagnostics.record_error(
                    "评论任务", "任务失败",
                    "Cookie=secret https://example.com 10.0.0.1 "
                    '{"access_token": "persist-token", '
                    "'proxy_password': 'persist-pass'}",
                )
                saved = (config_dir / "recent_error.json").read_text(encoding="utf-8")
                diagnostics._last_error = None
                diagnostics._last_error_ts = 0.0
                loaded = diagnostics.load_recent_error()

        self.assertNotIn("secret", saved)
        self.assertNotIn("example.com", saved)
        self.assertNotIn("10.0.0.1", saved)
        self.assertNotIn("persist-token", saved)
        self.assertNotIn("persist-pass", saved)
        self.assertEqual(loaded.get("state"), "history")

    def test_startup_error_is_marked_history(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "error.log").write_text(
                "Traceback\nC:\\Project\\BiliToolbox\\main.py",
                encoding="utf-8",
            )
            with patch.object(output, "app_base_dir", return_value=base), \
                    patch.object(diagnostics, "RECENT_ERROR_FILE",
                                 base / "recent_error.json"):
                record = diagnostics.load_recent_error()
        self.assertEqual(record.get("state"), "history")
        self.assertEqual(record.get("source"), "应用启动（历史）")

    def test_clear_recent_error_only_removes_program_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "config"
            base = root / "base"
            config_dir.mkdir()
            base.mkdir()
            recent = config_dir / "recent_error.json"
            startup = base / "error.log"
            recent.write_text(json.dumps({"details": "x"}), encoding="utf-8")
            startup.write_text("old", encoding="utf-8")
            other = root / "keep.txt"
            other.write_text("keep", encoding="utf-8")
            with patch.object(config, "CONFIG_DIR", config_dir), \
                    patch.object(diagnostics, "RECENT_ERROR_FILE", recent), \
                    patch.object(output, "app_base_dir", return_value=base):
                self.assertTrue(diagnostics.clear_recent_error())
            self.assertFalse(recent.exists())
            self.assertFalse(startup.exists())
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
