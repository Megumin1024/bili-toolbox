# -*- coding: utf-8 -*-
"""openpyxl 导出基建：统一样式令牌 / ILLEGAL 字符清洗 / 表头与 KV 行辅助。

评论与采集两套 Excel 报表共用的部分；各工具的报表内容见 tools/*/core.py。
"""
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

FONT_NAME, HEADER_BOLD = "Microsoft YaHei", False
PRIMARY, NEUTRAL_0, NEUTRAL_100 = "1B2A4A", "FFFFFF", "F7F7F5"
NEUTRAL_200, NEUTRAL_600, NEUTRAL_900 = "E9E9E8", "8C8A84", "37352F"

F_TITLE = Font(name=FONT_NAME, size=16, bold=HEADER_BOLD, color=PRIMARY)
F_HEADER = Font(name=FONT_NAME, size=11, bold=HEADER_BOLD, color="FFFFFF")
F_BODY = Font(name=FONT_NAME, size=11, color=NEUTRAL_900)
F_CAPTION = Font(name=FONT_NAME, size=9, color=NEUTRAL_600)
FILL_HEADER = PatternFill("solid", fgColor=PRIMARY)
B_HEADER = Border(bottom=Side(style="thin", color=NEUTRAL_200))
A_HEADER = Alignment(horizontal="center", vertical="center", wrap_text=True)
A_TEXT = Alignment(horizontal="left", vertical="center")


def clean(v):
    """清洗 Excel 非法字符（B站文本常含控制字符，直接写入会抛错）。"""
    if isinstance(v, str):
        return ILLEGAL_CHARACTERS_RE.sub("", v)
    return v


def new_workbook(creator="BiliToolbox"):
    wb = Workbook(write_only=True)
    wb.properties.creator = creator
    return wb


class SheetWriter:
    """write_only 模式的表辅助。ws 属性随切页更新，wc() 写入当前页。"""

    def __init__(self, wb):
        self.wb = wb
        self.ws = None

    def wc(self, value, font=None, fill=None, align=None, border=None):
        c = WriteOnlyCell(self.ws, value=clean(value))
        c.font = font or F_BODY
        if fill:
            c.fill = fill
        c.alignment = align or A_TEXT
        if border:
            c.border = border
        return c

    def title_row(self, ws, text, width):
        ws.append([None] + [self.wc(text, font=F_TITLE)] + [None] * (width - 1))
        ws.row_dimensions[1].height = 15
        ws.row_dimensions[2].height = 32
        ws.append([None])

    def header_row(self, ws, headers):
        ws.append([None] + [self.wc(h, font=F_HEADER, fill=FILL_HEADER,
                                    align=A_HEADER, border=B_HEADER)
                            for h in headers])

    def kv(self, ws, pairs, width=4):
        self.header_row(ws, ["指标", "数值", "说明"])
        for k, v, note in pairs:
            ws.append([None, self.wc(k), self.wc(v), self.wc(note)])
        ws.append([None])
