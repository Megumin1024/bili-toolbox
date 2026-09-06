# -*- coding: utf-8 -*-
"""通用 Qt 组件：卡片、标题、路径选择行、日志面板、进度区、结果卡片。"""
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


def card(margin=14, spacing=10):
    f = QFrame()
    f.setObjectName("card")
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
    """只读日志面板；超量自动丢弃最旧行。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)

    def append(self, text, level=None):
        stamp = time.strftime("%H:%M:%S")
        if level == "error":
            line = f"[{stamp}] ✗ {text}"
        elif level == "warn":
            line = f"[{stamp}] ⚠ {text}"
        else:
            line = f"[{stamp}] {text}"
        self.appendPlainText(line)


class ProgressBlock(QWidget):
    """进度条 + 状态文本。busy=转圈模式；set_value 启用确定模式。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.bar = QProgressBar()
        self.label = QLabel("待开始")
        self.label.setObjectName("status")
        lay.addWidget(self.bar)
        lay.addWidget(self.label)
        self.reset()

    def reset(self):
        self.bar.setRange(0, 0)
        self.bar.setValue(0)
        self.label.setText("待开始")

    def set_busy(self, text="运行中…"):
        self.bar.setRange(0, 0)
        if text:
            self.label.setText(text)

    def set_value(self, done, total=None, text=None):
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(min(100, int(done * 100 / max(total, 1))))
        if text:
            self.label.setText(text)

    def set_text(self, text):
        self.label.setText(text)


class ResultCard(QFrame):
    """结果卡片：完成后展示产物文件，点击用系统默认程序打开。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resultCard")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(14, 10, 14, 10)
        self.lay.setSpacing(6)
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
