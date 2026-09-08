# -*- coding: utf-8 -*-
"""清爽轻二次元主题：深色/浅色两套 QSS 与共享视觉令牌。"""

DARK = {
    "bg": "#0F1324", "sidebar": "#12172A", "card": "#171C31",
    "card_alt": "#202740", "hover": "#252D49", "border": "#303A60",
    "border_soft": "#242D4B", "text": "#EAF0FF", "heading": "#FFFFFF",
    "muted": "#94A1C3", "input_bg": "#11172A", "log_bg": "#0A0E1A",
    "terminal_text": "#C9D7F5", "accent": "#FB7299", "accent_hover": "#FF8DAE",
    "accent_soft": "#352039", "accent2": "#55C7F3", "accent2_soft": "#173449",
    "danger": "#FF667A", "danger_soft": "#3A1E2C", "ok": "#69D7B0",
    "ok_soft": "#173A36", "warning": "#F6C86B", "warning_soft": "#3A3120",
}

LIGHT = {
    "bg": "#F7F4FA", "sidebar": "#FFFCFF", "card": "#FFFFFF",
    "card_alt": "#F3EEF8", "hover": "#F0E9F6", "border": "#E4DCEF",
    "border_soft": "#EEE8F4", "text": "#303348", "heading": "#202238",
    "muted": "#737A96", "input_bg": "#FBFAFD", "log_bg": "#111626",
    "terminal_text": "#D9E4FF", "accent": "#FB7299", "accent_hover": "#FF5F8F",
    "accent_soft": "#FFE6EF", "accent2": "#199BCB", "accent2_soft": "#E1F5FC",
    "danger": "#E34E63", "danger_soft": "#FFE8EC", "ok": "#2EA879",
    "ok_soft": "#E0F7EF", "warning": "#B87916", "warning_soft": "#FFF3D8",
}


def _qss(t):
    return f"""
* {{ outline: none; }}
QWidget {{ background: {t['bg']}; color: {t['text']};
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; font-size: 10pt; }}
QWidget#transparent {{ background: transparent; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-family: "Microsoft YaHei UI Semibold", "Microsoft YaHei UI";
  font-size: 18pt; font-weight: 600; color: {t['heading']}; background: transparent; }}
QLabel#h2 {{ font-family: "Microsoft YaHei UI Semibold", "Microsoft YaHei UI";
  font-size: 11.5pt; font-weight: 600; color: {t['heading']}; background: transparent; }}
QLabel#muted {{ color: {t['muted']}; font-size: 9pt; background: transparent; }}
QLabel#eyebrow {{ color: {t['accent2']}; font-family: "Segoe UI Variable", "Segoe UI";
  font-size: 8pt; font-weight: 700; background: transparent; }}
QLabel#brandBadge {{ color: {t['accent']}; font-size: 18pt; font-weight: 700;
  min-width: 34px; max-width: 34px; background: {t['accent_soft']};
  border: 1px solid {t['accent']}; border-radius: 11px; padding: 3px; }}
QLabel#brandTitle {{ color: {t['heading']}; font-size: 13.5pt; font-weight: 700;
  background: transparent; }}
QLabel#brandCaption {{ color: {t['muted']}; font-family: "Segoe UI Variable", "Segoe UI";
  font-size: 8pt; background: transparent; }}
QLabel#status {{ font-size: 10pt; background: transparent; }}
QLabel#moduleChip {{ color: {t['accent2']}; background: {t['accent2_soft']};
  border: 1px solid {t['border']}; border-radius: 8px; padding: 3px 9px;
  font-family: "Segoe UI Variable", "Segoe UI"; font-size: 8pt; font-weight: 600; }}
QLabel#statusPill {{ color: {t['muted']}; background: {t['card_alt']};
  border: 1px solid {t['border']}; border-radius: 9px; padding: 4px 10px;
  font-size: 9pt; }}
QLabel#statusPill[state="running"] {{ color: {t['accent2']}; background: {t['accent2_soft']}; }}
QLabel#statusPill[state="success"] {{ color: {t['ok']}; background: {t['ok_soft']}; }}
QLabel#statusPill[state="warning"] {{ color: {t['warning']}; background: {t['warning_soft']}; }}
QLabel#statusPill[state="error"] {{ color: {t['danger']}; background: {t['danger_soft']}; }}

QFrame#sidebar {{ background: {t['sidebar']}; border-right: 1px solid {t['border_soft']}; }}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['border_soft']};
  border-radius: 13px; }}
QFrame#cardAccent {{ background: {t['card']}; border: 1px solid {t['border']};
  border-left: 3px solid {t['accent2']}; border-radius: 13px; }}
QFrame#pageHeader {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
  stop:0 {t['card']}, stop:0.72 {t['card']}, stop:1 {t['accent_soft']});
  border: 1px solid {t['border_soft']}; border-radius: 14px; }}
QFrame#starRail {{ min-height: 3px; max-height: 3px;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
  stop:0 {t['accent']}, stop:0.55 {t['accent2']}, stop:1 transparent);
  border: none; border-radius: 1px; }}
QFrame#actionBar {{ background: {t['card']}; border: 1px solid {t['border_soft']};
  border-radius: 12px; }}
QFrame#resultCard {{ background: {t['ok_soft']}; border: 1px solid {t['ok']};
  border-left: 3px solid {t['ok']}; border-radius: 12px; }}
QFrame#line {{ background: {t['border']}; max-height: 1px; border: none; }}

QPushButton#nav {{ text-align: left; padding: 10px 13px; border: 1px solid transparent;
  border-radius: 9px; background: transparent; color: {t['muted']};
  font-size: 10.5pt; spacing: 8px; }}
QPushButton#nav:hover {{ background: {t['hover']}; color: {t['heading']};
  border-color: {t['border_soft']}; }}
QPushButton#nav:checked {{ background: {t['card_alt']}; color: {t['heading']};
  border-left: 3px solid {t['accent']}; font-weight: 600; }}

QPushButton {{ background: {t['card_alt']}; color: {t['text']};
  border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 18px;
  font-size: 10pt; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:disabled {{ color: {t['muted']}; background: transparent; }}
QPushButton#primary {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
  stop:0 {t['accent']}, stop:1 {t['accent_hover']}); color: #ffffff;
  border: none; font-weight: 600; padding: 8px 26px; }}
QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
QPushButton#primary:disabled {{ background: {t['accent_soft']}; color: {t['muted']}; }}
QPushButton#danger {{ background: transparent; color: {t['danger']};
  border: 1px solid {t['danger']}; }}
QPushButton#danger:hover {{ background: {t['danger_soft']}; }}
QPushButton#flat {{ background: transparent; border: none;
  color: {t['accent2']}; padding: 2px 4px; }}
QPushButton#flat:hover {{ color: {t['text']}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
  background: {t['input_bg']}; border: 1px solid {t['border']};
  border-radius: 8px; padding: 6px 10px; color: {t['text']};
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
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
  stop:0 {t['accent2']}, stop:1 {t['accent']}); border-radius: 4px; }}

QPlainTextEdit#log {{ background: {t['log_bg']}; border: 1px solid {t['border']};
  border-radius: 11px; color: {t['terminal_text']};
  font-family: "Consolas", "Microsoft YaHei UI", monospace; font-size: 9pt;
  padding: 8px; selection-background-color: {t['accent_soft']}; }}

QGroupBox {{ background: {t['input_bg']}; border: 1px solid {t['border_soft']};
  border-radius: 10px; margin-top: 10px; padding: 12px 10px 8px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
  left: 12px; padding: 0 6px; color: {t['accent2']}; font-weight: 600; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

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
