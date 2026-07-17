"""Line-aware transcript editor widget.

Words render as a wrapping flow grouped into lines. Each line starts with a
timeline-timecode gutter. Trimmed silence gaps appear as dim markers you can
delete to restore. Selection, Delete/Backspace, cut/paste, single-click to
jump the Resolve playhead. Search highlights matching words.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from ..transcript import (
    EditHistory,
    Transcript,
    TranscriptLine,
    apply_snapshot,
    frame_to_timecode,
    media_sec_to_timeline_frame,
    snapshot_transcript,
)

WORD_ROLE = Qt.ItemDataRole.UserRole + 1  # -> (word_index, removed: bool)
KIND_ROLE = Qt.ItemDataRole.UserRole + 2  # "line" | "word" | "silence"
LINE_ROLE = Qt.ItemDataRole.UserRole + 3  # TranscriptLine
MATCH_ROLE = Qt.ItemDataRole.UserRole + 4  # bool
SILENCE_ROLE = Qt.ItemDataRole.UserRole + 5  # (start, end) seconds


def _cuts_in_gap(
    cuts: list[tuple[float, float]], gap_start: float, gap_end: float
) -> list[tuple[float, float]]:
    """Return silence cuts that fall primarily in [gap_start, gap_end]."""
    out: list[tuple[float, float]] = []
    for cs, ce in cuts:
        if ce <= gap_start or cs >= gap_end:
            continue
        # Prefer cuts whose midpoint sits in the gap (avoids double-insert).
        mid = (cs + ce) / 2.0
        if gap_start - 1e-6 <= mid <= gap_end + 1e-6:
            out.append((cs, ce))
    return out


class TranscriptModel(QAbstractListModel):
    """Flat model: line headers, words, and silence-cut markers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.transcript: Transcript | None = None
        self._rows: list[tuple[str, object]] = []
        self._match_words: set[int] = set()
        self._timeline_start_frame: int = 0
        self._fps: float = 24.0
        self._viewport_width: int = 400

    def set_timeline_context(self, timeline_start_frame: int, fps: float) -> None:
        self._timeline_start_frame = timeline_start_frame
        self._fps = fps
        if self.transcript is not None:
            self.refresh()

    def set_viewport_width(self, width: int) -> None:
        width = max(200, width)
        if width != self._viewport_width:
            self._viewport_width = width
            if self.transcript is not None:
                top = self.index(0)
                bottom = self.index(self.rowCount() - 1)
                if top.isValid():
                    self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.SizeHintRole])

    def set_transcript(self, transcript: Transcript | None) -> None:
        self.beginResetModel()
        self.transcript = transcript
        self._match_words.clear()
        self._rebuild_rows()
        self.endResetModel()

    def set_matches(self, word_indices: list[int]) -> None:
        self._match_words = set(word_indices)
        if self.rowCount():
            top = self.index(0)
            bottom = self.index(self.rowCount() - 1)
            self.dataChanged.emit(top, bottom, [MATCH_ROLE])

    def clear_matches(self) -> None:
        self.set_matches([])

    def refresh(self) -> None:
        self.beginResetModel()
        self._rebuild_rows()
        self.endResetModel()

    def _append_silence_in_gap(self, gap_start: float, gap_end: float) -> None:
        assert self.transcript is not None
        for cut in _cuts_in_gap(self.transcript.silence_cuts, gap_start, gap_end):
            self._rows.append(("silence", cut))

    def _rebuild_rows(self) -> None:
        self._rows = []
        if self.transcript is None:
            return
        tr = self.transcript
        order = tr.order
        if not order:
            for cut in sorted(tr.silence_cuts):
                self._rows.append(("silence", cut))
            return

        line_starts = {line.word_indices[0] for line in tr.lines() if line.word_indices}
        self._append_silence_in_gap(0.0, tr.words[order[0]].start)

        for i, widx in enumerate(order):
            if widx in line_starts:
                for line in tr.lines():
                    if line.word_indices and line.word_indices[0] == widx:
                        self._rows.append(("line", line))
                        break
            if i > 0:
                prev = tr.words[order[i - 1]]
                cur = tr.words[widx]
                self._append_silence_in_gap(prev.end, cur.start)
            self._rows.append(("word", widx))

        last = tr.words[order[-1]]
        self._append_silence_in_gap(last.end, tr.duration)

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def word_index(self, row: int) -> int | None:
        if row < 0 or row >= len(self._rows):
            return None
        kind, payload = self._rows[row]
        if kind == "word":
            return int(payload)  # type: ignore[arg-type]
        if kind == "line":
            line: TranscriptLine = payload  # type: ignore[assignment]
            return line.start_word
        return None

    def silence_range(self, row: int) -> tuple[float, float] | None:
        if row < 0 or row >= len(self._rows):
            return None
        kind, payload = self._rows[row]
        if kind == "silence":
            cs, ce = payload  # type: ignore[misc]
            return float(cs), float(ce)
        return None

    def media_second_at_row(self, row: int) -> float | None:
        if self.transcript is None or row < 0 or row >= len(self._rows):
            return None
        kind, payload = self._rows[row]
        if kind == "word":
            return self.transcript.words[int(payload)].start  # type: ignore[arg-type]
        if kind == "line":
            line: TranscriptLine = payload  # type: ignore[assignment]
            return line.start
        if kind == "silence":
            cs, _ce = payload  # type: ignore[misc]
            return float(cs)
        return None

    def row_for_word(self, word_index: int) -> int:
        for i, (kind, payload) in enumerate(self._rows):
            if kind == "word" and payload == word_index:
                return i
        return -1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if self.transcript is None or not index.isValid():
            return None
        kind, payload = self._rows[index.row()]
        if role == KIND_ROLE:
            return kind
        if kind == "line":
            line: TranscriptLine = payload  # type: ignore[assignment]
            if role == LINE_ROLE:
                return line
            if role == Qt.ItemDataRole.DisplayRole:
                frame = media_sec_to_timeline_frame(
                    line.start, self._timeline_start_frame, self._fps
                )
                return frame_to_timecode(frame, self._fps)
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"Jump to {self.data(index, Qt.ItemDataRole.DisplayRole)}"
            if role == Qt.ItemDataRole.SizeHintRole:
                return QSize(self._viewport_width - 8, 22)
            return None

        if kind == "silence":
            cs, ce = payload  # type: ignore[misc]
            dur = max(0.0, float(ce) - float(cs))
            if role == SILENCE_ROLE:
                return (float(cs), float(ce))
            if role == Qt.ItemDataRole.DisplayRole:
                # Visual “space” for a trimmed gap; duration in tooltip.
                return "  ···  "
            if role == Qt.ItemDataRole.ToolTipRole:
                return (
                    f"Silence {dur:.1f}s (will be trimmed). "
                    "Delete to keep this silence."
                )
            return None

        widx = int(payload)  # type: ignore[arg-type]
        word = self.transcript.words[widx]
        if role == Qt.ItemDataRole.DisplayRole:
            return word.text
        if role == WORD_ROLE:
            return (widx, widx in self.transcript.removed)
        if role == MATCH_ROLE:
            return widx in self._match_words
        if role == Qt.ItemDataRole.ToolTipRole:
            frame = media_sec_to_timeline_frame(
                word.start, self._timeline_start_frame, self._fps
            )
            tc = frame_to_timecode(frame, self._fps)
            return f"{tc}  ({word.start:.2f}s – {word.end:.2f}s)"
        return None


class WordDelegate(QStyledItemDelegate):
    PAD_X = 6
    PAD_Y = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.font = QFont()
        self.font.setPointSize(14)
        self.tc_font = QFont()
        self.tc_font.setPointSize(11)
        self.tc_font.setStyleHint(QFont.StyleHint.Monospace)
        self.tc_font.setFamily("Menlo")

    def sizeHint(self, option, index) -> QSize:
        kind = index.data(KIND_ROLE)
        if kind == "line":
            hint = index.data(Qt.ItemDataRole.SizeHintRole)
            if isinstance(hint, QSize):
                return hint
            return QSize(option.rect.width() or 400, 22)
        fm = QFontMetrics(self.font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        return QSize(fm.horizontalAdvance(text) + self.PAD_X * 2,
                     fm.height() + self.PAD_Y * 2)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        kind = index.data(KIND_ROLE)
        rect = option.rect

        if kind == "line":
            painter.setFont(self.tc_font)
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(rect, option.palette.highlight())
                color = option.palette.highlightedText().color()
            else:
                color = QColor(135, 135, 143)
            painter.setPen(QPen(color))
            painter.drawText(
                rect.adjusted(4, 0, -4, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
            painter.restore()
            return

        if kind == "silence":
            painter.setFont(self.font)
            text = index.data(Qt.ItemDataRole.DisplayRole) or ""
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(rect, option.palette.highlight())
                color = option.palette.highlightedText().color()
            else:
                painter.fillRect(rect, QColor(60, 60, 70, 80))
                color = QColor(135, 135, 143)
            painter.setPen(QPen(color))
            text_rect = rect.adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
            painter.restore()
            return

        painter.setFont(self.font)
        _widx, removed = index.data(WORD_ROLE)
        is_match = bool(index.data(MATCH_ROLE))

        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, option.palette.highlight())
        elif is_match:
            painter.fillRect(rect, QColor(230, 75, 61, 60))

        if removed:
            color = QColor(128, 128, 128)
        elif option.state & QStyle.StateFlag.State_Selected:
            color = option.palette.highlightedText().color()
        else:
            color = option.palette.text().color()
        painter.setPen(QPen(color))

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        text_rect = rect.adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

        if removed:
            fm = QFontMetrics(self.font)
            width = fm.horizontalAdvance(text)
            y = rect.center().y()
            x0 = rect.center().x() - width // 2
            painter.drawLine(x0, y, x0 + width, y)
        painter.restore()


class TranscriptView(QListView):
    edited = Signal()
    word_activated = Signal(int)
    # Media-relative seconds (from clip analysis start) to seek in Resolve.
    time_activated = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = TranscriptModel(self)
        self.setModel(self._model)
        self.setItemDelegate(WordDelegate(self))
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setSpacing(2)
        self.setUniformItemSizes(False)
        self.clicked.connect(self._on_click)
        self._clipboard_words: list[int] = []
        self._history = EditHistory()

    @property
    def transcript(self) -> Transcript | None:
        return self._model.transcript

    def set_transcript(self, transcript: Transcript | None) -> None:
        self._history.clear()
        self._model.set_transcript(transcript)

    def set_timeline_context(self, timeline_start_frame: int, fps: float) -> None:
        self._model.set_timeline_context(timeline_start_frame, fps)

    def refresh(self) -> None:
        self._model.refresh()

    def set_search_matches(self, word_indices: list[int]) -> None:
        self._model.set_matches(word_indices)

    def clear_search_matches(self) -> None:
        self._model.clear_matches()

    def select_word(self, word_index: int, *, scroll: bool = True) -> None:
        row = self._model.row_for_word(word_index)
        if row < 0:
            return
        index = self._model.index(row)
        self.selectionModel().select(
            index,
            self.selectionModel().SelectionFlag.ClearAndSelect,
        )
        self.setCurrentIndex(index)
        if scroll:
            self.scrollTo(index, QListView.ScrollHint.PositionAtCenter)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._model.set_viewport_width(self.viewport().width())

    def _selected_word_indices(self) -> list[int]:
        rows = sorted(i.row() for i in self.selectionModel().selectedIndexes())
        out: list[int] = []
        for r in rows:
            kind = self._model.data(self._model.index(r), KIND_ROLE)
            if kind != "word":
                continue
            widx = self._model.word_index(r)
            if widx is not None:
                out.append(widx)
        return out

    def _selected_silence_cuts(self) -> list[tuple[float, float]]:
        rows = sorted(i.row() for i in self.selectionModel().selectedIndexes())
        out: list[tuple[float, float]] = []
        for r in rows:
            cut = self._model.silence_range(r)
            if cut is not None:
                out.append(cut)
        return out

    def push_history(self) -> None:
        if self.transcript is not None:
            self._history.push(snapshot_transcript(self.transcript))

    def undo(self) -> bool:
        if self.transcript is None or not self._history.can_undo():
            return False
        current = snapshot_transcript(self.transcript)
        snap = self._history.undo(current)
        if snap is None:
            return False
        apply_snapshot(self.transcript, snap)
        self._model.set_transcript(self.transcript)
        self.edited.emit()
        return True

    def redo(self) -> bool:
        if self.transcript is None or not self._history.can_redo():
            return False
        current = snapshot_transcript(self.transcript)
        snap = self._history.redo(current)
        if snap is None:
            return False
        apply_snapshot(self.transcript, snap)
        self._model.set_transcript(self.transcript)
        self.edited.emit()
        return True

    def delete_selection(self) -> None:
        if self.transcript is None:
            return
        words = self._selected_word_indices()
        silences = self._selected_silence_cuts()
        if not words and not silences:
            return
        self.push_history()
        if words:
            self.transcript.delete(words)
        if silences:
            # Deleting a silence marker restores that gap (removes the cut).
            remove_keys = {(round(s, 4), round(e, 4)) for s, e in silences}
            self.transcript.silence_cuts = [
                c for c in self.transcript.silence_cuts
                if (round(c[0], 4), round(c[1], 4)) not in remove_keys
            ]
        self._model.refresh()
        self.edited.emit()

    def cut_selection(self) -> None:
        indices = self._selected_word_indices()
        if indices and self.transcript:
            self.push_history()
            self._clipboard_words = indices
            self.transcript.delete(indices)
            self._model.refresh()
            self.edited.emit()

    def paste_at_current(self) -> None:
        if not self._clipboard_words or self.transcript is None:
            return
        self.push_history()
        current = self.currentIndex()
        if current.isValid():
            kind = self._model.data(current, KIND_ROLE)
            widx = self._model.word_index(current.row())
            if kind == "word" and widx is not None:
                try:
                    dest_row = self.transcript.order.index(widx) + 1
                except ValueError:
                    dest_row = len(self.transcript.order)
            else:
                dest_row = len(self.transcript.order)
        else:
            dest_row = len(self.transcript.order)
        moving = set(self._clipboard_words)
        dest_pos = sum(
            1 for i in self.transcript.order[:dest_row] if i not in moving
        )
        self.transcript.move(self._clipboard_words, dest_pos)
        self.transcript.restore(self._clipboard_words)
        self._clipboard_words = []
        self._model.set_transcript(self.transcript)
        self.edited.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selection()
        elif event.matches(QKeySequence.StandardKey.Cut):
            self.cut_selection()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.paste_at_current()
        else:
            super().keyPressEvent(event)

    def _on_click(self, index: QModelIndex) -> None:
        kind = self._model.data(index, KIND_ROLE)
        if kind == "word":
            widx = self._model.word_index(index.row())
            if widx is not None:
                self.word_activated.emit(widx)
            return
        sec = self._model.media_second_at_row(index.row())
        if sec is not None:
            self.time_activated.emit(sec)
            if kind == "line":
                widx = self._model.word_index(index.row())
                if widx is not None:
                    self.word_activated.emit(widx)
