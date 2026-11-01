"""Settings dialog for transcript typography, silence trim and repeat detection."""

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

from ..config import (
    REPEAT_MODES,
    REPEAT_SENSITIVITIES,
    WHISPER_MODELS,
    normalize_repeat_mode,
    normalize_repeat_sensitivity,
)


class SettingsDialog(QDialog):
    """Edit font, spacing, silence kept around words by Trim Silence, and how
    Remove Repeats detects retakes (mode and sensitivity)."""

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

        mode_labels = {
            "smart": "Smart (similar phrases)",
            "exact": "Exact (identical phrases)",
        }
        self.repeat_mode_combo = QComboBox()
        for mode in REPEAT_MODES:
            self.repeat_mode_combo.addItem(mode_labels[mode], mode)
        self.repeat_mode_combo.setCurrentIndex(
            self.repeat_mode_combo.findData(
                normalize_repeat_mode(cfg.get("repeat_mode"))
            )
        )
        self.repeat_mode_combo.setToolTip(
            "Smart also catches false starts that the next phrase re-says with "
            "slightly different words. Exact only removes identical phrases."
        )

        sensitivity_labels = {
            "conservative": "Conservative",
            "balanced": "Balanced",
            "aggressive": "Aggressive",
        }
        self.repeat_sensitivity_combo = QComboBox()
        for level in REPEAT_SENSITIVITIES:
            self.repeat_sensitivity_combo.addItem(sensitivity_labels[level], level)
        self.repeat_sensitivity_combo.setCurrentIndex(
            self.repeat_sensitivity_combo.findData(
                normalize_repeat_sensitivity(cfg.get("repeat_sensitivity"))
            )
        )
        self.repeat_sensitivity_combo.setToolTip(
            "Higher sensitivity catches more retakes but may strike phrases "
            "you meant to keep."
        )
        self.repeat_mode_combo.currentIndexChanged.connect(self._sync_repeat_controls)

        form.addRow("Whisper model", self.model_combo)
        form.addRow("Font", self.font_combo)
        form.addRow("Size", self.size_spin)
        form.addRow("Word spacing", self.spacing_spin)
        form.addRow("Horizontal pad", self.pad_x_spin)
        form.addRow("Vertical pad", self.pad_y_spin)
        form.addRow("Keep after word", self.keep_after_spin)
        form.addRow("Keep before word", self.keep_before_spin)
        form.addRow("Repeat detection", self.repeat_mode_combo)
        form.addRow("Retake sensitivity", self.repeat_sensitivity_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_repeat_controls()

    def _sync_repeat_controls(self) -> None:
        self.repeat_sensitivity_combo.setEnabled(
            self.repeat_mode_combo.currentData() != "exact"
        )

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "transcript_font_family": self.font_combo.currentFont().family(),
            "transcript_font_size": self.size_spin.value(),
            "transcript_word_spacing": self.spacing_spin.value(),
            "transcript_word_pad_x": self.pad_x_spin.value(),
            "transcript_word_pad_y": self.pad_y_spin.value(),
            "silence_keep_after": self.keep_after_spin.value(),
            "silence_keep_before": self.keep_before_spin.value(),
            "repeat_mode": self.repeat_mode_combo.currentData(),
            "repeat_sensitivity": self.repeat_sensitivity_combo.currentData(),
        }
        model = self.model_combo.currentText()
        if model != self._initial_model:
            out["whisper_model"] = model
            out["whisper_model_chosen"] = True
        return out
