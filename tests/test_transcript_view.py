"""GUI-level checks for silence markers and timestamp selection."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from captain.gui.transcript_view import KIND_ROLE, TranscriptModel
from captain.transcript import Transcript, Word


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _model_with_gap(gap: float, *, qapp) -> TranscriptModel:
    # Start at t=0 so only the inter-word gap can produce a silence marker.
    words = [
        Word(index=0, text="hello", start=0.0, end=0.3),
        Word(index=1, text="world", start=0.3 + gap, end=0.3 + gap + 0.3),
    ]
    tr = Transcript(words=words, duration=words[-1].end)
    model = TranscriptModel()
    # Trim threshold stays high; display uses SILENCE_DISPLAY_MIN (0.1).
    model.set_silence_thresholds(min_duration=0.8, max_pause=0.25)
    model.set_transcript(tr)
    return model


def test_silence_marker_visible_from_point_one_second(qapp):
    model = _model_with_gap(0.1, qapp=qapp)
    kinds = [model.data(model.index(i), KIND_ROLE) for i in range(model.rowCount())]
    assert kinds.count("silence") == 1

    model_short = _model_with_gap(0.05, qapp=qapp)
    kinds_short = [
        model_short.data(model_short.index(i), KIND_ROLE)
        for i in range(model_short.rowCount())
    ]
    assert "silence" not in kinds_short


def test_timestamp_rows_not_selectable(qapp):
    model = _model_with_gap(1.0, qapp=qapp)
    found_line = False
    for i in range(model.rowCount()):
        index = model.index(i)
        kind = model.data(index, KIND_ROLE)
        flags = model.flags(index)
        if kind == "line":
            found_line = True
            assert flags & Qt.ItemFlag.ItemIsEnabled
            assert not (flags & Qt.ItemFlag.ItemIsSelectable)
        elif kind in ("word", "silence"):
            assert flags & Qt.ItemFlag.ItemIsSelectable
    assert found_line
