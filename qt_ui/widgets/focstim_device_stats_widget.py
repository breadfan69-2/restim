import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QVBoxLayout

from qt_ui.widgets.fourphase_widget_stereographic import COLOR_A, COLOR_B, COLOR_C, COLOR_D


class FocStimDeviceStatsWidget(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Device stats", parent)

        self.transformer_max = 0.0
        self.voltage_max = 0.0

        root = QVBoxLayout(self)

        usage_label = QLabel("Device usage")
        usage_label.setToolTip(
            "How much of the device capabilities are used. Output power is limited to 100%.\n"
            "To reduce transformer usage: use higher carrier frequency.\n"
            "To reduce voltage usage: use lower carrier frequency.\n"
            "Better electrodes will significantly reduce both."
        )
        root.addWidget(usage_label)

        usage_grid = QGridLayout()
        usage_grid.addWidget(QLabel("Transformer:"), 0, 0)
        self.label_transformer = QLabel("0%")
        usage_grid.addWidget(self.label_transformer, 0, 1)
        self.label_transformer_max = QLabel("(max 0%)")
        usage_grid.addWidget(self.label_transformer_max, 0, 2)

        usage_grid.addWidget(QLabel("Voltage:"), 1, 0)
        self.label_voltage = QLabel("0%")
        usage_grid.addWidget(self.label_voltage, 1, 1)
        self.label_voltage_max = QLabel("(max 0%)")
        usage_grid.addWidget(self.label_voltage_max, 1, 2)
        root.addLayout(usage_grid)

        resistance_label = QLabel("Skin resistance [Ohm]")
        root.addWidget(resistance_label)

        resistance_grid = QGridLayout()
        self.label_a = QLabel("A")
        self.label_b = QLabel("B")
        self.label_c = QLabel("C")
        self.label_d = QLabel("D")
        for label, color in (
            (self.label_a, COLOR_A),
            (self.label_b, COLOR_B),
            (self.label_c, COLOR_C),
            (self.label_d, COLOR_D),
        ):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(self._stylesheet_with_color(color))

        self.resistance_a = self._new_value_label()
        self.resistance_b = self._new_value_label()
        self.resistance_c = self._new_value_label()
        self.resistance_d = self._new_value_label()

        for column, label in enumerate((self.label_a, self.label_b, self.label_c, self.label_d)):
            resistance_grid.addWidget(label, 0, column)
        for column, label in enumerate((self.resistance_a, self.resistance_b, self.resistance_c, self.resistance_d)):
            resistance_grid.addWidget(label, 1, column)

        root.addLayout(resistance_grid)
        self.reset_utilization()

    def _new_value_label(self):
        label = QLabel("0")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _stylesheet_with_color(self, background_color):
        return f"""
            QLabel {{
                background-color: {background_color.name()};
                color: white;
                border-radius: 12px;
                padding: 5px;
                font-weight: bold;
                font-size: 12px;
            }}
        """

    def update_utilization(self, transformer, voltage):
        self.transformer_max = max(transformer, self.transformer_max)
        self.voltage_max = max(voltage, self.voltage_max)
        self.label_transformer.setText(f"{transformer * 100:3.0f}%")
        self.label_transformer_max.setText(f"(max {self.transformer_max * 100:3.0f}%)")
        self.label_voltage.setText(f"{voltage * 100:3.0f}%")
        self.label_voltage_max.setText(f"(max {self.voltage_max * 100:3.0f}%)")

    def update_resistance(self, a, b, c, d):
        def format_impedance(value):
            resistance = np.clip(np.real(value), -9999, 9999)
            reactance = np.clip(np.imag(value), -9999, 9999)
            return f"{resistance:3.0f}\n{reactance:3.0f}i"

        self.resistance_a.setText(format_impedance(a))
        self.resistance_b.setText(format_impedance(b))
        self.resistance_c.setText(format_impedance(c))
        self.resistance_d.setText("N/A" if d is None else format_impedance(d))

    def reset_utilization(self):
        self.transformer_max = 0.0
        self.voltage_max = 0.0
        self.update_utilization(0.0, 0.0)
        self.update_resistance(0.0, 0.0, 0.0, 0.0)