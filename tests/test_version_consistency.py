# -*- coding: utf-8 -*-
"""验证发布版本只从 core.__version__ 派生。"""

import inspect
import unittest

import core
from app import main_window, settings_page


class VersionConsistencyTests(unittest.TestCase):
    EXPECTED_RELEASE_VERSION = "1.0.3"

    def test_core_version_is_the_expected_release_version(self):
        self.assertEqual(core.__version__, self.EXPECTED_RELEASE_VERSION)

    def test_settings_version_is_derived_from_core_version(self):
        self.assertEqual(settings_page.VERSION, f"v{core.__version__}")
        self.assertEqual(main_window.VERSION, settings_page.VERSION)

    def test_main_window_and_about_page_use_shared_version_symbol(self):
        main_source = inspect.getsource(main_window)
        settings_source = inspect.getsource(settings_page)

        self.assertIn("from .settings_page import SettingsPage, VERSION", main_source)
        self.assertIn('foot = QLabel(f"{VERSION}\\n仅处理公开数据")', main_source)
        self.assertIn('f"B站工具箱 {VERSION} ·', settings_source)
        self.assertNotRegex(main_source, r"v1\.0\.[0-9]+")
        self.assertNotRegex(settings_source, r"v1\.0\.[0-9]+")


if __name__ == "__main__":
    unittest.main()
