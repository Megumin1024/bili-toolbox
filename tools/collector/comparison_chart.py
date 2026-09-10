# -*- coding: utf-8 -*-
"""视频采集页内部使用的轻量 QPainter 水平排名图。"""
from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget


class ComparisonChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setObjectName("collectorComparisonChart")
        self._title = "暂无可绘制数据"
        self._items: list[tuple[str, float]] = []

    def set_items(self, title: str, items: list[tuple[Any, Any]]) -> None:
        self._title = str(title or "")
        self._items = []
        for label, value in items:
            if len(self._items) >= 10:
                break
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(number):
                continue
            self._items.append((str(label), number))
        self.update()

    def clear_chart(self, text="暂无可绘制数据"):
        self._title = str(text)
        self._items = []
        self.update()

    @staticmethod
    def _format_value(value: float) -> str:
        if value == 0:
            return "0"
        if value.is_integer() and abs(value) < 1e20:
            text = f"{value:,.0f}"
        else:
            text = f"{value:,.2f}"
        return text if len(text) <= 14 else f"{value:.3g}"

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width = max(1, self.width())
        height = max(1, self.height())
        title_rect = QRectF(6, 2, width - 12, 18)
        painter.setPen(self.palette().text().color())
        title_metrics = QFontMetrics(painter.font())
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         title_metrics.elidedText(self._title, Qt.ElideRight, max(1, width - 12)))
        if not self._items:
            painter.setPen(self.palette().mid().color())
            painter.drawText(QRectF(6, 28, width - 12, max(1, height - 28)),
                             Qt.AlignCenter, "暂无可绘制数据")
            painter.end()
            return

        label_width = min(150, max(72, int(width * 0.27)))
        value_width = min(72, max(46, int(width * 0.16)))
        plot = QRectF(label_width, 28,
                      max(1, width - label_width - value_width - 10),
                      max(1, height - 36))
        values = [value for _label, value in self._items]
        minimum, maximum = min(values), max(values)
        if minimum == maximum:
            if minimum == 0:
                minimum, maximum = -1.0, 1.0
            elif minimum > 0:
                minimum, maximum = 0.0, minimum * 1.15
            else:
                minimum, maximum = minimum * 1.15, 0.0
        else:
            minimum, maximum = min(0.0, minimum), max(0.0, maximum)
        span = maximum - minimum
        if not math.isfinite(span) or span <= 0:
            painter.end()
            return

        zero_x = plot.left() + (-minimum) / span * plot.width()
        painter.setPen(QPen(self.palette().mid().color(), 1, Qt.DashLine))
        painter.drawLine(zero_x, plot.top(), zero_x, plot.bottom())

        row_height = plot.height() / max(1, len(self._items))
        bar_height = max(4.0, min(18.0, row_height * 0.62))
        positive = self.palette().highlight().color()
        negative = positive.darker(130)
        label_metrics = QFontMetrics(painter.font())
        for index, (label, value) in enumerate(self._items):
            center_y = plot.top() + row_height * (index + 0.5)
            label_rect = QRectF(2, center_y - row_height / 2, label_width - 8, row_height)
            painter.setPen(self.palette().text().color())
            painter.drawText(label_rect, Qt.AlignRight | Qt.AlignVCenter,
                             label_metrics.elidedText(label, Qt.ElideLeft,
                                                      max(1, int(label_rect.width()))))

            value_x = plot.left() + (value - minimum) / span * plot.width()
            left, right = sorted((zero_x, value_x))
            bar_rect = QRectF(left, center_y - bar_height / 2,
                              max(0.0, right - left), bar_height)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(positive if value >= 0 else negative))
            painter.drawRect(bar_rect)

            painter.setPen(self.palette().text().color())
            painter.drawText(QRectF(plot.right() + 4, center_y - row_height / 2,
                                    value_width, row_height),
                             Qt.AlignLeft | Qt.AlignVCenter, self._format_value(value))
        painter.end()
