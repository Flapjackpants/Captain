"""GUI-level checks for silence markers, timestamp selection, and delete toggle."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from captain.gui.transcript_view import KIND_ROLE, TranscriptModel, TranscriptView
from captain.transcript import SILENCE_DISPLAY_MIN, Transcript, Word


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
    model.set_silence_thresholds(min_duration=SILENCE_DISPLAY_MIN, max_pause=0.0)
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


def _view_with_words(texts: list[str], *, qapp, gap: float = 0.1) -> TranscriptView:
    words = []
    t = 0.0
    for i, text in enumerate(texts):
        words.append(Word(index=i, text=text, start=t, end=t + 0.3))
        t += 0.3 + gap
    tr = Transcript(words=words, duration=t + 0.5)
    view = TranscriptView()
    view.set_silence_thresholds(SILENCE_DISPLAY_MIN, 0.0)
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


def test_delete_trims_short_visible_silence_with_zero_pause(qapp):
    view = _view_with_words(["a", "b"], qapp=qapp, gap=0.15)
    view.set_silence_thresholds(SILENCE_DISPLAY_MIN, 0.0)
    view.refresh()
    tr = view.transcript
    assert tr is not None
    model = view._model
    silence_rows = [
        i
        for i in range(model.rowCount())
        if model.data(model.index(i), KIND_ROLE) == "silence"
    ]
    assert silence_rows
    sm = view.selectionModel()
    sm.clearSelection()
    sm.select(model.index(silence_rows[0]), sm.SelectionFlag.ClearAndSelect)
    view.delete_selection()
    assert tr.silence_cuts
    gap_start, gap_end = tr.words[0].end, tr.words[1].start
    assert any(
        c[0] == pytest.approx(gap_start) and c[1] == pytest.approx(gap_end)
        for c in tr.silence_cuts
    )


def test_select_model_range_selects_intervening_words_and_silence(qapp):
    """Range selection is model-order contiguous, not rubber-band rectangle."""
    view = _view_with_words(["one", "two", "three", "four"], qapp=qapp, gap=0.2)
    model = view._model
    # Force a narrow viewport so words wrap across visual lines.
    view.resize(80, 200)
    view._model.set_viewport_width(80)
    view.doItemsLayout()

    row_a = model.row_for_word(0)
    row_d = model.row_for_word(3)
    assert row_a >= 0 and row_d > row_a
    view.select_model_range(row_a, row_d)

    selected_rows = sorted(i.row() for i in view.selectionModel().selectedIndexes())
    # Every selectable row between endpoints must be selected.
    for r in range(row_a, row_d + 1):
        flags = model.flags(model.index(r))
        if flags & Qt.ItemFlag.ItemIsSelectable:
            assert r in selected_rows
        else:
            assert r not in selected_rows

    selected_words = set(view._selected_word_indices())
    assert selected_words == {0, 1, 2, 3}
    # Silence markers between words are included when present.
    silence_in_range = [
        r
        for r in range(row_a, row_d + 1)
        if model.data(model.index(r), KIND_ROLE) == "silence"
    ]
    for r in silence_in_range:
        assert r in selected_rows


def test_mouse_drag_selection_selects_all_words_between_press_and_release(qapp):
    from PySide6.QtCore import QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    view = _view_with_words(["first", "second", "third", "fourth", "fifth"], qapp=qapp, gap=0.2)
    view.resize(100, 200)
    view.show()
    qapp.processEvents()

    model = view._model
    row1 = model.row_for_word(1)  # "second"
    row3 = model.row_for_word(3)  # "fourth"
    p1 = view.visualRect(model.index(row1)).center()
    p3 = view.visualRect(model.index(row3)).center()

    # Press on row1
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(p1),
        QPointF(p1),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(view.viewport(), press)
    assert set(view._selected_word_indices()) == {1}

    # Drag to row3
    move = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(p3),
        QPointF(p3),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(view.viewport(), move)
    # Must select all words between 1 and 3 in reading order (1, 2, 3), not just 3!
    assert set(view._selected_word_indices()) == {1, 2, 3}

    # Release at row3
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(p3),
        QPointF(p3),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(view.viewport(), release)
    assert set(view._selected_word_indices()) == {1, 2, 3}


def test_mouse_drag_backward_and_margins(qapp):
    from PySide6.QtCore import QPoint, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    view = _view_with_words(["a", "b", "c", "d"], qapp=qapp, gap=0.2)
    view.resize(300, 200)
    view.show()
    qapp.processEvents()

    model = view._model
    row2 = model.row_for_word(2)  # "c"
    row0 = model.row_for_word(0)  # "a"
    p2 = view.visualRect(model.index(row2)).center()
    p0 = view.visualRect(model.index(row0)).center()

    # Press on word 2
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(p2),
        QPointF(p2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(view.viewport(), press)

    # Drag backward to word 0
    move = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(p0),
        QPointF(p0),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(view.viewport(), move)
    assert set(view._selected_word_indices()) == {0, 1, 2}

    # Drag into right margin beyond last word
    p_margin = QPoint(290, p2.y())
    move_margin = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(p_margin),
        QPointF(p_margin),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(view.viewport(), move_margin)
    # Dragged from 2 forward into margin of that line: words 2 and 3 should be selected
    assert 2 in view._selected_word_indices()
    assert 3 in view._selected_word_indices()

