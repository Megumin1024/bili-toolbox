# -*- coding: utf-8 -*-
"""通用 Qt 组件：页头、卡片、状态、路径、日志、进度与结果展示。"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)


def h1(text):
    lbl = QLabel(text)
    lbl.setObjectName("h1")
    return lbl


def h2(text):
    lbl = QLabel(text)
    lbl.setObjectName("h2")
    return lbl


def muted(text):
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return lbl


class PageHeader(QFrame):
    """统一页头：模块标签、标题、说明与粉蓝数据星轨。"""

    def __init__(self, title, subtitle, module="功能模块", parent=None):
        super().__init__(parent)
        self.setObjectName("pageHeader")
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(5)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        eyebrow = QLabel("✦ B站数据工具")
        eyebrow.setObjectName("eyebrow")
        chip = QLabel(module)
        chip.setObjectName("moduleChip")
        top.addWidget(eyebrow)
        top.addStretch(1)
        top.addWidget(chip)
        root.addLayout(top)
        root.addWidget(h1(title))
        root.addWidget(muted(subtitle))
        rail = QFrame()
        rail.setObjectName("starRail")
        root.addWidget(rail)


class StatusPill(QLabel):
    """轻量状态徽章；状态值用于 QSS 着色。"""

    def __init__(self, text="待开始", state="idle", parent=None):
        super().__init__(parent)
        self.setObjectName("statusPill")
        self.setAlignment(Qt.AlignCenter)
        self.set_state(state, text)

    def set_state(self, state, text=None):
        self.setProperty("state", state)
        if text is not None:
            self.setText(text)
        style = self.style()
        style.unpolish(self)
        style.polish(self)


def card(margin=14, spacing=10, variant="default"):
    """创建卡片；variant=default/accent，原有无参数调用保持兼容。"""
    f = QFrame()
    f.setObjectName("cardAccent" if variant == "accent" else "card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(margin, margin, margin, margin)
    lay.setSpacing(spacing)
    return f, lay


def open_path(path):
    p = Path(path)
    target = p if p.is_dir() else p.parent
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


class PathRow(QWidget):
    """标签 + 输入框 + 浏览按钮（目录选择）。"""

    def __init__(self, label, value="", parent=None):
        super().__init__(parent)
        self.setObjectName("transparent")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        if label:
            lbl = QLabel(label)
            lbl.setMinimumWidth(72)
            lay.addWidget(lbl)
        self.edit = QLineEdit(value)
        lay.addWidget(self.edit, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择目录", self.value() or ".")
        if d:
            self.edit.setText(d)

    def value(self):
        return self.edit.text().strip()

    def set_value(self, v):
        self.edit.setText(str(v))


class LogPanel(QPlainTextEdit):
    """只读任务通讯面板；超量自动丢弃最旧行。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setPlaceholderText("任务通讯会显示在这里…")

    def append(self, text, level=None):
        stamp = time.strftime("%H:%M:%S")
        if level == "error":
            line = f"[{stamp}] ✕ {text}"
        elif level == "warn":
            line = f"[{stamp}] ◇ {text}"
        else:
            line = f"[{stamp}] · {text}"
        self.appendPlainText(line)


class ProgressBlock(QWidget):
    """进度条 + 状态徽章。busy=不确定进度，set_value=确定进度。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transparent")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.label = StatusPill()
        lay.addWidget(self.bar)
        lay.addWidget(self.label, 0, Qt.AlignRight)
        self.reset()

    def reset(self):
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.label.set_state("idle", "○ 待开始")

    def set_busy(self, text="运行中…"):
        self.bar.setRange(0, 0)
        if text:
            self.label.set_state("running", f"● {text}")

    def set_value(self, done, total=None, text=None):
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(min(100, int(done * 100 / max(total, 1))))
        if text:
            self.label.set_state("running", f"● {text}")

    def set_text(self, text):
        self.label.set_state("running", f"● {text}")

    def set_success(self, text="完成"):
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.label.set_state("success", f"✓ {text}")

    def set_warning(self, text):
        self.label.set_state("warning", f"◇ {text}")

    def set_error(self, text):
        self.bar.setRange(0, 100)
        self.label.set_state("error", f"✕ {text}")


class ResultCard(QFrame):
    """结果卡片：完成后展示产物文件，点击用系统默认程序打开。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resultCard")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 12, 16, 12)
        self.lay.setSpacing(7)
        self.title = h2("完成 ✓")
        self.lay.addWidget(self.title)
        self.hidden_rows = QVBoxLayout()
        self.hidden_rows.setSpacing(4)
        self.lay.addLayout(self.hidden_rows)
        self.hide()

    def clear(self):
        while self.hidden_rows.count():
            item = self.hidden_rows.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def show_result(self, title, files):
        """files: [(标签, 路径), ...]"""
        self.clear()
        self.title.setText(title)
        for label, path in files:
            if not path:
                continue
            row = QWidget()
            row.setObjectName("transparent")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            name = QLabel(label)
            rl.addWidget(name)
            btn = QPushButton(Path(path).name)
            btn.setObjectName("flat")
            btn.setToolTip(path)
            btn.clicked.connect(lambda _=False, p=path: open_path(p))
            rl.addWidget(btn, 1)
            self.hidden_rows.addWidget(row)
        self.show()
