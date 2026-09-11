# -*- coding: utf-8 -*-
"""默认导出目录：绝不能落在程序自己的安装目录里。

2026-09-11 的教训：打包版的默认导出目录是 `<exe 目录>/导出`，而构建命令
`pyinstaller --noconfirm` 会清空整个产物目录——重建一次就把用户数据删了。
装到 Program Files 之类不可写的位置时，行为还会随安装位置漂移。

这里锁三件事：
1. 打包态一律写用户目录，不管安装目录能不能写；
2. 旧配置里指向安装目录的路径要被重新解析，用户自己挑的路径不许动；
3. 任何页面都不许再用 `or "."` 兜底——那会把导出落到当前工作目录。
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import output


@contextlib.contextmanager
def frozen_at(exe_dir):
    """模拟 PyInstaller 打包后的进程：sys.frozen + sys.executable 指向 exe。"""
    with patch.object(sys, "frozen", True, create=True), \
            patch.object(sys, "executable", str(Path(exe_dir) / "B站工具箱.exe")):
        yield


class DefaultOutDirTests(unittest.TestCase):
    def test_frozen_uses_the_user_dir_not_the_install_dir(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                base = output.app_base_dir()
                self.assertEqual(output.default_out_dir(), output.user_out_dir())
                self.assertNotEqual(output.default_out_dir(), base / "导出")

    def test_frozen_ignores_writability_of_the_install_dir(self):
        """安装目录可写也不行——会被重建清空，跟不可写一样是坏去处。"""
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp), patch.object(os, "access", return_value=True):
                self.assertEqual(output.default_out_dir(), output.user_out_dir())

    def test_source_mode_still_uses_the_repo_root(self):
        """源码运行维持现状，开发和测试看到的东西不变。"""
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            fake_base = Path(tmp)
            with patch.object(sys, "frozen", False, create=True), \
                    patch.object(output, "app_base_dir", return_value=fake_base), \
                    patch.object(os, "access", return_value=True):
                self.assertEqual(output.default_out_dir(), fake_base / "导出")

    def test_unwritable_base_in_source_mode_falls_back(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with patch.object(sys, "frozen", False, create=True), \
                    patch.object(output, "app_base_dir", return_value=Path(tmp)), \
                    patch.object(os, "access", return_value=False):
                self.assertEqual(output.default_out_dir(), output.user_out_dir())

    def test_env_override_beats_everything(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            mine = str(Path(tmp) / "我的导出")
            with frozen_at(Path(tmp)), \
                    patch.dict(os.environ, {"BILITOOLBOX_OUT": mine}):
                self.assertEqual(output.default_out_dir(), Path(mine))


class IsInsideAppDirTests(unittest.TestCase):
    def test_install_dir_itself_counts(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                self.assertTrue(output.is_inside_app_dir(output.app_base_dir()))

    def test_old_default_subdir_counts(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                self.assertTrue(
                    output.is_inside_app_dir(output.app_base_dir() / "导出"))

    def test_outside_path_does_not_count(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as outside, \
                tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                self.assertFalse(output.is_inside_app_dir(outside))

    def test_empty_value_does_not_count(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                self.assertFalse(output.is_inside_app_dir(""))
                self.assertFalse(output.is_inside_app_dir(None))

    def test_different_drive_does_not_explode(self):
        """Windows 上跨盘符 commonpath 会抛 ValueError，不能让它冒出去。"""
        with frozen_at(Path("C:/fake/base")):
            self.assertFalse(output.is_inside_app_dir("Z:/other/导出"))

    def test_case_difference_is_not_fooled(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                upper = str(output.app_base_dir()).upper()
                self.assertTrue(output.is_inside_app_dir(upper + "\\导出"))


class MigrateOutDirTests(unittest.TestCase):
    def test_old_install_dir_default_is_re_resolved(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                old = str(output.app_base_dir() / "导出")
                self.assertEqual(output.migrate_out_dir(old),
                                 str(output.user_out_dir()))

    def test_install_dir_itself_is_re_resolved(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                self.assertEqual(
                    output.migrate_out_dir(str(output.app_base_dir())),
                    str(output.user_out_dir()))

    def test_user_chosen_path_is_left_alone(self):
        """用户自己挑到别处的路径不许替他改。"""
        with tempfile.TemporaryDirectory(prefix="mine") as mine, \
                tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                self.assertEqual(output.migrate_out_dir(mine), mine)

    def test_empty_value_resolves_to_the_default(self):
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                for empty in ("", None, "   "):
                    self.assertEqual(output.migrate_out_dir(empty),
                                     str(output.user_out_dir()))

    def test_already_migrated_value_is_a_fixed_point(self):
        """迁移要幂等：跑第二次不能再变。否则每次启动配置都在漂。"""
        with tempfile.TemporaryDirectory(prefix="outdir_") as tmp:
            with frozen_at(tmp):
                once = output.migrate_out_dir("")
                self.assertEqual(output.migrate_out_dir(once), once)

    def test_source_mode_value_stays_put(self):
        """源码态的仓库内路径就是当前默认值，迁移不该改变它。"""
        with patch.object(sys, "frozen", False, create=True), \
                patch.object(os, "access", return_value=True):
            current = str(output.default_out_dir())
            self.assertEqual(output.migrate_out_dir(current), current)


class NoPageFallsBackToCwdTests(unittest.TestCase):
    """`(cfg.get("out_dir") or ".") + "/xxx"` 这种写法会把导出落到工作目录。

    它现在被 main.py 填好的配置兜住了，但那是巧合不是设计——一旦配置为空
    （换台机器、配置损坏、CLI 调用），输出就会掉进当前目录。这里把这类写法
    从源码里钉死，顺带确认每个页面确实走了 default_out_dir()。
    """

    PAGES = ("tools/collector/page.py", "tools/comments/page.py",
             "tools/danmaku/page.py", "tools/data_check/page.py",
             "tools/user_dynamics/page.py", "tools/report_center/page.py")

    @staticmethod
    def _root():
        return Path(__file__).resolve().parents[1]

    def test_no_page_uses_the_bare_dot_out_dir_fallback(self):
        for rel in self.PAGES:
            source = (self._root() / rel).read_text(encoding="utf-8")
            self.assertNotIn('out_dir") or "."', source,
                             f"{rel} 又用回了 or \".\" 兜底，输出会落到工作目录")

    def test_every_page_resolves_through_default_out_dir(self):
        for rel in self.PAGES:
            source = (self._root() / rel).read_text(encoding="utf-8")
            self.assertIn("default_out_dir", source,
                          f"{rel} 没有走 core.output.default_out_dir()")

    def test_main_migrates_the_stored_out_dir(self):
        """启动时的迁移必须在 main.py 里接着，否则旧配置永远不生效。"""
        source = (self._root() / "main.py").read_text(encoding="utf-8")
        self.assertIn("output.migrate_out_dir(", source)


if __name__ == "__main__":
    unittest.main()
