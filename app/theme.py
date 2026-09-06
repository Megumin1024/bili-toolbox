# -*- coding: utf-8 -*-
"""主题：深色/浅色两套 QSS。B站粉 #FB7299 / 蓝 #00AEEC 作为强调色。"""

DARK = {
    "bg": "#14161b", "sidebar": "#1a1d24", "card": "#1e222b",
    "card_alt": "#242a35", "hover": "#272e3a", "border": "#2b323e",
    "text": "#e9ebf0", "muted": "#98a1ad", "input_bg": "#171b21",
    "log_bg": "#101318", "accent": "#fb7299", "accent_soft": "#3a2530",
    "accent2": "#00aeec", "accent2_soft": "#173240",
    "danger": "#e5484d", "ok": "#4cb782",
}

LIGHT = {
    "bg": "#f3f5f8", "sidebar": "#ffffff", "card": "#ffffff",
    "card_alt": "#eef1f5", "hover": "#e9edf3", "border": "#e0e5ec",
    "text": "#22262e", "muted": "#697382", "input_bg": "#f8fafc",
    "log_bg": "#1b1e24", "accent": "#fb7299", "accent_soft": "#ffe9f0",
    "accent2": "#0087b8", "accent2_soft": "#dff3fb",
    "danger": "#d93036", "ok": "#2f9e5f",
}


def _qss(t):
    return f"""
* {{ outline: none; }}
QWidget {{ background: {t['bg']}; color: {t['text']};
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; font-size: 10pt; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 17pt; font-weight: 600; background: transparent; }}
QLabel#h2 {{ font-size: 12pt; font-weight: 600; background: transparent; }}
QLabel#muted {{ color: {t['muted']}; font-size: 9pt; background: transparent; }}
QLabel#logo {{ font-size: 15pt; font-weight: 700; color: {t['accent']};
  background: transparent; }}
QLabel#status {{ font-size: 10pt; background: transparent; }}

QFrame#sidebar {{ background: {t['sidebar']}; border-right: 1px solid {t['border']}; }}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['border']};
  border-radius: 10px; }}
QFrame#resultCard {{ background: {t['accent2_soft']}; border: 1px solid {t['border']};
  border-radius: 10px; }}
QFrame#line {{ background: {t['border']}; max-height: 1px; border: none; }}

QPushButton#nav {{ text-align: left; padding: 9px 14px; border: none;
  border-radius: 8px; background: transparent; color: {t['muted']};
  font-size: 10.5pt; spacing: 8px; }}
QPushButton#nav:hover {{ background: {t['hover']}; color: {t['text']}; }}
QPushButton#nav:checked {{ background: {t['card_alt']}; color: {t['text']};
  font-weight: 600; }}

QPushButton {{ background: {t['card_alt']}; color: {t['text']};
  border: 1px solid {t['border']}; border-radius: 7px; padding: 7px 18px;
  font-size: 10pt; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:disabled {{ color: {t['muted']}; background: transparent; }}
QPushButton#primary {{ background: {t['accent']}; color: #ffffff;
  border: none; font-weight: 600; padding: 8px 26px; }}
QPushButton#primary:hover {{ background: #ff83a5; }}
QPushButton#primary:disabled {{ background: {t['accent_soft']}; color: {t['muted']}; }}
QPushButton#danger {{ background: transparent; color: {t['danger']};
  border: 1px solid {t['danger']}; }}
QPushButton#danger:hover {{ background: rgba(229,72,77,0.12); }}
QPushButton#flat {{ background: transparent; border: none;
  color: {t['accent2']}; padding: 2px 4px; }}
QPushButton#flat:hover {{ color: {t['text']}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
  background: {t['input_bg']}; border: 1px solid {t['border']};
  border-radius: 7px; padding: 5px 9px; color: {t['text']};
  selection-background-color: {t['accent2']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {t['accent2']}; }}
QLineEdit:disabled, QPlainTextEdit:disabled {{ color: {t['muted']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;
  border-right: 4px solid transparent; border-top: 5px solid {t['muted']};
  margin-right: 8px; }}
QComboBox QAbstractItemView {{ background: {t['card']};
  border: 1px solid {t['border']}; selection-background-color: {t['card_alt']};
  outline: none; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {{ background: transparent; border: none; width: 16px; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ border-left: 4px solid transparent;
  border-right: 4px solid transparent; border-bottom: 5px solid {t['muted']}; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ border-left: 4px solid transparent;
  border-right: 4px solid transparent; border-top: 5px solid {t['muted']}; }}

QProgressBar {{ background: {t['card_alt']}; border: none; border-radius: 4px;
  min-height: 8px; max-height: 8px; }}
QProgressBar::chunk {{ background: {t['accent2']}; border-radius: 4px; }}

QPlainTextEdit#log {{ background: {t['log_bg']}; border: 1px solid {t['border']};
  border-radius: 8px; color: #c9d1d9; font-family: "Consolas", "Microsoft YaHei UI",
  monospace; font-size: 9pt; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px;
  min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['border']}; border-radius: 4px;
  min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QRadioButton, QCheckBox {{ background: transparent; spacing: 6px; }}
QRadioButton::indicator, QCheckBox::indicator {{ width: 15px; height: 15px; }}
QRadioButton::indicator {{ border-radius: 8px; border: 1px solid {t['muted']};
  background: {t['input_bg']}; }}
QRadioButton::indicator:checked {{ border: 4px solid {t['accent2']};
  background: {t['text']}; }}
QCheckBox::indicator {{ border-radius: 4px; border: 1px solid {t['muted']};
  background: {t['input_bg']}; }}
QCheckBox::indicator:checked {{ background: {t['accent2']};
  border: 1px solid {t['accent2']}; }}

QToolTip {{ background: {t['card']}; color: {t['text']};
  border: 1px solid {t['border']}; padding: 4px 8px; }}
QMessageBox {{ background: {t['card']}; }}
QMessageBox QLabel {{ background: transparent; }}
"""


def tokens(theme="dark"):
    return DARK if theme == "dark" else LIGHT


def qss(theme="dark"):
    return _qss(tokens(theme))


def apply(app, theme="dark"):
    app.setStyleSheet(qss(theme))
