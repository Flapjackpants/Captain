from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from captain.gui.caption_dialog import CaptionSettingsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_caption_dialog_previews_project_frame_and_retains_style_settings(qapp):
    still = QPixmap(640, 360)
    still.fill(QColor("#335577"))
    dialog = CaptionSettingsDialog(
        1920,
        1080,
        still,
        ["This is the longest caption", "Short line"],
        ["This", "is", "the", "longest", "caption"],
        {"font_size": 80, "auto_fit": True},
    )

    assert (dialog.preview.project_width, dialog.preview.project_height) == (1920, 1080)
    assert dialog.preview.pixmap.size() == still.size()
    assert dialog.preview.texts == ["This is the longest caption", "Short line"]
    assert dialog.values()["font_family"] == dialog.font.currentFont().family()
    fitted_size = dialog.preview.settings["font_size"]
    assert fitted_size >= 1

    dialog.position_x.setValue(0.6)
    dialog.outline_opacity.setValue(0.75)
    dialog.font_size.setValue(100)
    assert dialog.values()["position_x"] == pytest.approx(0.6)
    assert dialog.values()["outline_opacity"] == pytest.approx(0.75)
    assert dialog.values()["font_size"] == 100
    assert dialog.values()["auto_fit"] is False

    assert dialog.link_scale.isChecked()
    dialog.scale_y.setValue(1.35)
    assert dialog.scale_x.value() == pytest.approx(1.35)
    assert dialog.values()["scale_x"] == pytest.approx(1.35)
    assert dialog.values()["scale_y"] == pytest.approx(1.35)
    dialog.link_scale.setChecked(False)
    dialog.scale_y.setValue(0.75)
    assert dialog.scale_x.value() == pytest.approx(1.35)
    assert dialog.scale_y.value() == pytest.approx(0.75)

    dialog.word_by_word.setChecked(True)
    assert dialog.preview.texts == ["This", "is", "the", "longest", "caption"]
    assert dialog.preview_combo.count() == 6
