"""Settings dialog for transcript typography and silence trim."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFontComboBox,
    QSpinBox,
    QVBoxLayout,
)

from ..config import WHISPER_MODELS


class SettingsDialog(QDialog):
    """Edit font, spacing, and silence kept around words by Trim Silence."""

    def __init__(self, cfg: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.model_combo = QComboBox()
        self.model_combo.addItems(WHISPER_MODELS)
        current_model = str(cfg.get("whisper_model", "large-v3-turbo"))
        if self.model_combo.findText(current_model) < 0:
            self.model_combo.addItem(current_model)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.setToolTip(
            "Used for new transcriptions. Larger models are more accurate but "
            "slower and download once (large-v3-turbo \u2248 1.6 GB)."
        )
        self._initial_model = current_model

        self.font_combo = QFontComboBox()
        family = str(cfg.get("transcript_font_family") or "").strip()
        if family:
            self.font_combo.setCurrentFont(QFont(family))

        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 36)
        self.size_spin.setValue(int(cfg.get("transcript_font_size", 14)))

        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(0, 24)
        self.spacing_spin.setSuffix(" px")
        self.spacing_spin.setValue(int(cfg.get("transcript_word_spacing", 0)))

        self.pad_x_spin = QSpinBox()
        self.pad_x_spin.setRange(0, 24)
        self.pad_x_spin.setSuffix(" px")
        self.pad_x_spin.setValue(int(cfg.get("transcript_word_pad_x", 1)))

        self.pad_y_spin = QSpinBox()
        self.pad_y_spin.setRange(0, 24)
        self.pad_y_spin.setSuffix(" px")
        self.pad_y_spin.setValue(int(cfg.get("transcript_word_pad_y", 2)))

        self.keep_after_spin = QDoubleSpinBox()
        self.keep_after_spin.setRange(0.0, 1.0)
        self.keep_after_spin.setSingleStep(0.01)
        self.keep_after_spin.setDecimals(2)
        self.keep_after_spin.setSuffix(" s")
        self.keep_after_spin.setValue(float(cfg.get("silence_keep_after", 0.20)))
        self.keep_after_spin.setToolTip(
            "Silence kept after each word when trimming, so word endings aren't clipped."
        )

        self.keep_before_spin = QDoubleSpinBox()
        self.keep_before_spin.setRange(0.0, 1.0)
        self.keep_before_spin.setSingleStep(0.01)
        self.keep_before_spin.setDecimals(2)
        self.keep_before_spin.setSuffix(" s")
        self.keep_before_spin.setValue(float(cfg.get("silence_keep_before", 0.03)))
        self.keep_before_spin.setToolTip("Silence kept before each word when trimming.")

        form.addRow("Whisper model", self.model_combo)
        form.addRow("Font", self.font_combo)
        form.addRow("Size", self.size_spin)
        form.addRow("Word spacing", self.spacing_spin)
        form.addRow("Horizontal pad", self.pad_x_spin)
        form.addRow("Vertical pad", self.pad_y_spin)
        form.addRow("Keep after word", self.keep_after_spin)
        form.addRow("Keep before word", self.keep_before_spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "transcript_font_family": self.font_combo.currentFont().family(),
            "transcript_font_size": self.size_spin.value(),
            "transcript_word_spacing": self.spacing_spin.value(),
            "transcript_word_pad_x": self.pad_x_spin.value(),
            "transcript_word_pad_y": self.pad_y_spin.value(),
            "silence_keep_after": self.keep_after_spin.value(),
            "silence_keep_before": self.keep_before_spin.value(),
        }
        model = self.model_combo.currentText()
        if model != self._initial_model:
            out["whisper_model"] = model
            out["whisper_model_chosen"] = True
        return out
