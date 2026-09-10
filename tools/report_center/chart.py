# -*- coding: utf-8 -*-
"""不引入第三方依赖的轻量 QPainter 图表。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPen, QBrush, QFontMetrics
from PySide6.QtWidgets import QWidget


class ReportChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setObjectName("reportChart")
        self._series: dict[str, list[tuple[Any, float]]] = {}
        self._labels: list[str] = []
        self._empty_text = "暂无可绘制数据"

    def set_series(self, series: dict[str, list[tuple[Any, float]]], labels=None):
        self._series = {
            str(name): [(label, float(value)) for label, value in values
                        if value is not None]
            for name, values in (series or {}).items()
        }
        self._labels = [str(item) for item in (labels or [])]
        self.update()

    def clear_chart(self, text="暂无可绘制数据"):
        self._series = {}
        self._labels = []
        self._empty_text = text
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(42, 16, -18, -30)
        colors = [self.palette().highlight().color(), self.palette().link().color(),
                  self.palette().brightText().color()]
        if not any(self._series.values()):
            painter.setPen(self.palette().mid().color())
            painter.drawText(QRectF(self.rect()), Qt.AlignCenter, self._empty_text)
            return
        all_values = [value for values in self._series.values() for _label, value in values]
        minimum, maximum = min(all_values), max(all_values)
        if minimum == maximum:
            minimum -= 1
            maximum += 1
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.right(), rect.bottom()))
        painter.drawLine(QPointF(rect.left(), rect.top()), QPointF(rect.left(), rect.bottom()))
        painter.setPen(self.palette().mid().color())
        fm = QFontMetrics(painter.font())
        painter.drawText(QRectF(2, rect.top() - 8, 35, 18), Qt.AlignRight, f"{maximum:g}")
        painter.drawText(QRectF(2, rect.bottom() - 10, 35, 18), Qt.AlignRight, f"{minimum:g}")
        width = max(1.0, rect.width())
        for index, (name, values) in enumerate(self._series.items()):
            if not values:
                continue
            pen = QPen(colors[index % len(colors)], 2)
            painter.setPen(pen)
            points = []
            count = max(1, len(values) - 1)
            for point_index, (_label, value) in enumerate(values):
                x = rect.left() + width * point_index / count
                y = rect.bottom() - (value - minimum) / (maximum - minimum) * rect.height()
                points.append(QPointF(x, y))
            for point_index in range(1, len(points)):
                painter.drawLine(points[point_index - 1], points[point_index])
            painter.setBrush(QBrush(colors[index % len(colors)]))
            for point in points:
                painter.drawEllipse(point, 3, 3)
        painter.setPen(self.palette().text().color())
        names = "  ·  ".join(self._series.keys())
        painter.drawText(QRectF(rect.left(), rect.bottom() + 8, rect.width(), 18), Qt.AlignLeft, names)
