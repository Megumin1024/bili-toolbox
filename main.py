# -*- coding: utf-8 -*-
"""B站工具箱入口。

GUI 模式: python main.py            （双击 exe 同样进入 GUI）
"""
import sys
import traceback
import os
from pathlib import Path


_FROZEN_DLL_HANDLES = []


def _prepare_frozen_dll_search():
    """让 Windows 在打包版启动时能找到 Qt/Shiboken 的依赖 DLL。"""
    if not getattr(sys, "frozen", False):
        return

    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    candidates = [base, base / "PySide6", base / "shiboken6"]
    existing = [str(path) for path in candidates if path.is_dir()]

    # PATH 兼容旧版 Windows；add_dll_directory 是 Windows 10+ 的明确搜索路径。
    if existing:
        os.environ["PATH"] = os.pathsep.join(existing + [os.environ.get("PATH", "")])
    if hasattr(os, "add_dll_directory"):
        for path in existing:
            try:
                _FROZEN_DLL_HANDLES.append(os.add_dll_directory(path))
            except OSError:
                pass


def _asset(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def main():
    _prepare_frozen_dll_search()
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("B站工具箱")
    app.setApplicationDisplayName("B站工具箱")
    icon = _asset("assets/icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    from core import config, diagnostics, output, session

    cfg = config.load()
    # 旧版打包会把 <exe 目录>/导出 写进配置；那个位置会被重装/重建清空，
    # 按"未设置"重新解析。用户自己挑到别处的路径不动。
    cfg["out_dir"] = output.migrate_out_dir(cfg.get("out_dir"))
    session.configure(proxy_spec=cfg.get("proxy_spec") or None,
                      transport=cfg.get("transport") or "auto",
                      cookie_path=config.COOKIE_FILE)

    # 走到这里说明应用已完成启动前初始化；旧启动错误不再显示为当前错误。
    diagnostics.clear_startup_error()
    from app.main_window import MainWindow
    from app.theme import apply
    apply(app, cfg.get("theme") or "dark")
    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent)
        raw_error = traceback.format_exc()
        try:
            from core.diagnostics import sanitize_text
            error_text = sanitize_text(raw_error)
        except Exception:  # 启动依赖损坏时也不把原始敏感信息写入日志
            error_text = "应用启动失败，详细信息无法安全记录。"
        try:
            (base / "error.log").write_text(error_text,
                                            encoding="utf-8")
        except OSError:
            pass
        raise
