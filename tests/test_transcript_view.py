"""GUI-level checks for silence markers, timestamp selection, and delete toggle."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from captain.gui.transcript_view import KIND_ROLE, TranscriptModel, TranscriptView
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


def _view_with_words(texts: list[str], *, qapp) -> TranscriptView:
    words = []
    t = 0.0
    for i, text in enumerate(texts):
        words.append(Word(index=i, text=text, start=t, end=t + 0.3))
        t += 0.4
    tr = Transcript(words=words, duration=t + 0.5)
    view = TranscriptView()
    view.set_transcript(tr)
    return view


def test_delete_toggles_word_remove_and_restore(qapp):
    view = _view_with_words(["a", "b", "c"], qapp=qapp)
    tr = view.transcript
    assert tr is not None

    view.select_word(1)
    view.delete_selection()
    assert tr.removed == {1}

    view.select_word(1)
    view.delete_selection()
    assert tr.removed == set()


def test_delete_mixed_selection_toggles_each_word(qapp):
    view = _view_with_words(["a", "b", "c", "d"], qapp=qapp)
    tr = view.transcript
    assert tr is not None
    tr.delete([1, 2])
    view.refresh()

    # Select active word 0 and removed word 1 together.
    model = view._model
    sm = view.selectionModel()
    sm.clearSelection()
    for widx in (0, 1):
        row = model.row_for_word(widx)
        sm.select(
            model.index(row),
            sm.SelectionFlag.Select,
        )

    view.delete_selection()
    # 0 was active → removed; 1 was removed → restored.
    assert tr.removed == {0, 2}


def test_delete_word_toggle_is_undoable(qapp):
    view = _view_with_words(["a", "b", "c"], qapp=qapp)
    tr = view.transcript
    assert tr is not None

    view.select_word(1)
    view.delete_selection()
    assert tr.removed == {1}

    assert view.undo()
    assert tr.removed == set()

    assert view.redo()
    assert tr.removed == {1}
