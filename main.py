# -*- coding: utf-8 -*-
"""B站工具箱入口。

GUI 模式: python main.py            （双击 exe 同样进入 GUI）
"""
import sys
import traceback
from pathlib import Path


def _asset(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def main():
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("B站工具箱")
    app.setApplicationDisplayName("B站工具箱")
    icon = _asset("assets/icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    from core import config, output, session

    cfg = config.load()
    if not cfg.get("out_dir"):
        cfg["out_dir"] = str(output.default_out_dir())
    session.configure(proxy_spec=cfg.get("proxy_spec") or None,
                      transport=cfg.get("transport") or "auto",
                      cookie_path=config.COOKIE_FILE)

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
        try:
            (base / "error.log").write_text(traceback.format_exc(),
                                            encoding="utf-8")
        except OSError:
            pass
        raise
