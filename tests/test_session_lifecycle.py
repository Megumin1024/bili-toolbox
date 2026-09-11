# -*- coding: utf-8 -*-
"""core.session 进程级生命周期的回归测试。

这里只钉一件事：**冷启动时 get_client() 不能自锁**。

为什么值得单开一个文件、还动用了子进程：
- 缺陷形态是"永久挂起"——没有异常、没有超时、CPU 不转。它在 GUI 里永远
  复现不出来（main.py 启动时已 configure，_CLIENT 非空，绕过了那条分支），
  只有脚本 / CLI / 集成测试把 http_get_json 当进程里第一个 session 调用时才现身。
- 正因为"挂起"是失败形态，测试**必须跑在独立进程里**：若用线程在本进程内
  验证，一旦回归，那个线程会把 _LOCK 永久占住，把后面的测试一起拖死——
  用挂死去测挂死，等于没有守卫。
"""
from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与实测复现脚本逐字一致：不预先 configure()，直接调 get_client()。
COLD_START = """
import sys
sys.path.insert(0, r"{root}")
from core import session
session.get_client()
print("COLD_START_OK")
"""

# 故意把锁还原成不可重入版，模拟"有人把 RLock 改回 Lock"。
# 这个片段**应该**挂住——用来证明上面的守卫不是摆设。
REGRESSION = """
import sys, threading
sys.path.insert(0, r"{root}")
from core import session
session._LOCK = threading.Lock()
session.get_client()
print("SHOULD_NOT_REACH")
"""

# 死锁是"永不返回"，冷启动实测约 1.5s；8s 足以区分两者，又不至于让
# 元测试每次跑全量都白等 20 秒。
REGRESSION_TIMEOUT = 8.0


def _run(code, timeout):
    return subprocess.run(
        [sys.executable, "-c", code.format(root=ROOT)],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))


class ColdStartTests(unittest.TestCase):
    def test_get_client_does_not_deadlock_on_cold_start(self):
        """_LOCK 必须可重入。

        曾经的缺陷：_LOCK 是 threading.Lock；get_client() 持锁后调用 configure()，
        而 configure() 又去拿同一把锁 → 自我死锁。修复前此处的表现是进程一直
        挂到 timeout，而不是断言失败。
        """
        proc = _run(COLD_START, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"子进程异常退出：\n{proc.stderr[-2000:]}")
        self.assertIn("COLD_START_OK", proc.stdout)

    def test_cold_start_is_fast_not_merely_survivable(self):
        """不只要求"最终能返回"，还要求它不是靠超时兜住的。

        冷启动只是建对象，不该有任何等待；真出现"差一点就锁死"的退化（比如
        锁被别的线程短暂占住），这里会比超时先炸。
        """
        started = time.monotonic()
        _run(COLD_START, timeout=60)
        self.assertLess(time.monotonic() - started, 20.0,
                        "冷启动耗时异常，检查是否有争用或阻塞")

    def test_the_guard_actually_catches_the_regression(self):
        """元测试：把锁换回普通 Lock，守卫必须报超时。

        没有这一条，上面两个测试有可能因为"根本没测到那条路径"而永远绿。
        """
        with self.assertRaises(subprocess.TimeoutExpired):
            _run(REGRESSION, timeout=REGRESSION_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
