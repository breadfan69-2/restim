from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QProgressBar


class BatteryProgressBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 100)
        self.setTextVisible(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(60, 30)
        self.setToolTip("Battery state of charge")
        self.setValue(0)

    def _style_for_value(self, value):
        if value > 60:
            color = "#4CAF50"
        elif value > 30:
            color = "#FFC107"
        else:
            color = "#F44336"

        return f"""
        QProgressBar {{
            border: 3px solid #333;
            border-radius: 8px;
            background-color: #ddd;
            text-align: center;
            font-weight: bold;
            margin-right: 4px;
        }}

        QProgressBar::chunk {{
            background-color: {color};
            border-radius: 5px;
        }}
        """

    def setValue(self, value):
        super().setValue(int(value))
        self.setStyleSheet(self._style_for_value(int(value)))

    def paintEvent(self, event):
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cap_width = 4
        cap_height = self.height() // 2
        cap_x = self.width() - 5
        cap_y = (self.height() - cap_height) // 2

        painter.setBrush(QColor("#333"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(cap_x, cap_y, cap_width, cap_height)
