"""Line-aware transcript editor widget.

Words render as a wrapping flow grouped into lines. Each line starts with a
timeline-timecode gutter (click-to-seek, not selectable). Natural silence
gaps (>= 0.1s) appear as period markers (one dot per 0.1s). After Trim
Silence / Delete, trimmed gaps are gray and struck through like removed
words. Selection, cut/paste, single-click to jump the Resolve playhead.
Search highlights matching words.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractListModel,
    QItemSelection,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from ..transcript import (
    SILENCE_DISPLAY_MIN,
    EditHistory,
    Transcript,
    TranscriptLine,
    apply_snapshot,
    cuts_in_gap,
    frame_to_timecode,
    gap_is_trimmed,
    media_sec_to_timeline_frame,
    shrink_silence_cut,
    silence_period_count,
    snapshot_transcript,
)
from .theme import (
    COMPARE_EXTRA,
    COMPARE_MATCH,
    COMPARE_MISMATCH,
    COMPARE_REMOVED,
)

WORD_ROLE = Qt.ItemDataRole.UserRole + 1  # -> (word_index, removed: bool)
KIND_ROLE = Qt.ItemDataRole.UserRole + 2  # "line" | "word" | "silence"
LINE_ROLE = Qt.ItemDataRole.UserRole + 3  # TranscriptLine
MATCH_ROLE = Qt.ItemDataRole.UserRole + 4  # bool
SILENCE_ROLE = Qt.ItemDataRole.UserRole + 5  # (start, end, trimmed)
STATUS_ROLE = Qt.ItemDataRole.UserRole + 6  # compare status: match|mismatch|extra|None


class TranscriptModel(QAbstractListModel):
    """Flat model: line headers, words, and silence gap markers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.transcript: Transcript | None = None
        self._rows: list[tuple[str, object]] = []
        self._match_words: set[int] = set()
        self._compare_statuses: dict[int, str] = {}
        self._timeline_start_frame: int = 0
        self._fps: float = 24.0
        self._viewport_width: int = 400
        # Used by delete_selection when adding Trim Silence–style cuts.
        self._silence_max_pause: float = 0.0

    def set_silence_thresholds(self, min_duration: float, max_pause: float) -> None:
        # min_duration is retained for API compatibility; markers use SILENCE_DISPLAY_MIN.
        self._silence_max_pause = float(max_pause)
        if self.transcript is not None:
            self.refresh()

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
        self._compare_statuses.clear()
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

    def set_compare_statuses(self, statuses: dict[int, str]) -> None:
        self._compare_statuses = dict(statuses)
        if self.rowCount():
            top = self.index(0)
            bottom = self.index(self.rowCount() - 1)
            self.dataChanged.emit(top, bottom, [STATUS_ROLE])

    def clear_compare_statuses(self) -> None:
        self.set_compare_statuses({})

    def refresh(self) -> None:
        self.beginResetModel()
        self._rebuild_rows()
        self.endResetModel()

    def _append_silence_in_gap(self, gap_start: float, gap_end: float) -> None:
        assert self.transcript is not None
        if gap_end - gap_start < SILENCE_DISPLAY_MIN:
            return
        trimmed = gap_is_trimmed(gap_start, gap_end, self.transcript.silence_cuts)
        self._rows.append(("silence", (gap_start, gap_end, trimmed)))

    def _rebuild_rows(self) -> None:
        self._rows = []
        if self.transcript is None:
            return
        tr = self.transcript
        order = tr.order
        if not order:
            if tr.duration >= SILENCE_DISPLAY_MIN:
                self._append_silence_in_gap(0.0, tr.duration)
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

    def flags(self, index: QModelIndex):
        """Timestamp/header rows are enabled (click-to-seek) but not selectable.

        Selection highlighting stays on words and silence markers only.
        """
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._rows):
            return Qt.ItemFlag.NoItemFlags
        kind, _payload = self._rows[index.row()]
        if kind == "line":
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

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
            cs, ce, _trimmed = payload  # type: ignore[misc]
            return float(cs), float(ce)
        return None

    def silence_trimmed(self, row: int) -> bool:
        if row < 0 or row >= len(self._rows):
            return False
        kind, payload = self._rows[row]
        if kind == "silence":
            return bool(payload[2])  # type: ignore[index]
        return False

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
            cs, _ce, _t = payload  # type: ignore[misc]
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
            cs, ce, trimmed = payload  # type: ignore[misc]
            dur = max(0.0, float(ce) - float(cs))
            if role == SILENCE_ROLE:
                return (float(cs), float(ce), bool(trimmed))
            if role == Qt.ItemDataRole.DisplayRole:
                return "." * silence_period_count(dur)
            if role == Qt.ItemDataRole.ToolTipRole:
                if trimmed:
                    return (
                        f"Silence {dur:.1f}s (will be trimmed). "
                        "Delete to keep this silence."
                    )
                return (
                    f"Silence {dur:.1f}s. Delete to trim, or use Trim Silence."
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
        if role == STATUS_ROLE:
            return self._compare_statuses.get(widx)
        if role == Qt.ItemDataRole.ToolTipRole:
            frame = media_sec_to_timeline_frame(
                word.start, self._timeline_start_frame, self._fps
            )
            tc = frame_to_timecode(frame, self._fps)
            status = self._compare_statuses.get(widx)
            tip = f"{tc}  ({word.start:.2f}s – {word.end:.2f}s)"
            if status == "extra":
                tip += " — in video, not in script"
            elif status == "mismatch":
                tip += " — differs from script"
            elif status == "match":
                tip += " — matches script"
            return tip
        return None


class WordDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pad_x = 1
        self.pad_y = 2
        self.font = QFont()
        self.font.setPointSize(14)
        self.tc_font = QFont()
        self.tc_font.setPointSize(11)
        self.tc_font.setStyleHint(QFont.StyleHint.Monospace)
        self.tc_font.setFamily("Menlo")

    def apply_typography(
        self,
        *,
        family: str = "",
        size: int = 14,
        pad_x: int = 1,
        pad_y: int = 2,
    ) -> None:
        self.pad_x = max(0, int(pad_x))
        self.pad_y = max(0, int(pad_y))
        font = QFont()
        if family:
            font.setFamily(family)
        font.setPointSize(max(8, int(size)))
        self.font = font
        self.tc_font.setPointSize(max(8, int(size) - 3))

    def sizeHint(self, option, index) -> QSize:
        kind = index.data(KIND_ROLE)
        if kind == "line":
            hint = index.data(Qt.ItemDataRole.SizeHintRole)
            if isinstance(hint, QSize):
                return hint
            return QSize(option.rect.width() or 400, 22)
        fm = QFontMetrics(self.font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        return QSize(
            fm.horizontalAdvance(text) + self.pad_x * 2,
            fm.height() + self.pad_y * 2,
        )

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
            silence = index.data(SILENCE_ROLE) or (0.0, 0.0, False)
            trimmed = bool(silence[2])
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(rect, option.palette.highlight())
                color = option.palette.highlightedText().color()
            elif trimmed:
                color = QColor(COMPARE_REMOVED)
            else:
                painter.fillRect(rect, QColor(60, 60, 70, 80))
                color = QColor(135, 135, 143)
            painter.setPen(QPen(color))
            text_rect = rect.adjusted(self.pad_x, self.pad_y, -self.pad_x, -self.pad_y)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
            if trimmed:
                fm = QFontMetrics(self.font)
                width = fm.horizontalAdvance(text)
                y = rect.center().y()
                x0 = rect.center().x() - width // 2
                painter.drawLine(x0, y, x0 + width, y)
            painter.restore()
            return

        painter.setFont(self.font)
        _widx, removed = index.data(WORD_ROLE)
        is_match = bool(index.data(MATCH_ROLE))
        compare_status = index.data(STATUS_ROLE)

        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, option.palette.highlight())
        elif is_match:
            painter.fillRect(rect, QColor(230, 75, 61, 60))

        if removed:
            color = QColor(COMPARE_REMOVED)
        elif option.state & QStyle.StateFlag.State_Selected:
            color = option.palette.highlightedText().color()
        elif compare_status == "extra":
            color = QColor(COMPARE_EXTRA)
        elif compare_status == "mismatch":
            color = QColor(COMPARE_MISMATCH)
        elif compare_status == "match":
            color = QColor(COMPARE_MATCH)
        else:
            color = option.palette.text().color()
        painter.setPen(QPen(color))

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        text_rect = rect.adjusted(self.pad_x, self.pad_y, -self.pad_x, -self.pad_y)
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
        self._delegate = WordDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setSelectionRectVisible(False)
        self.setSpacing(0)
        self.setUniformItemSizes(False)
        self._clipboard_words: list[int] = []
        self._history = EditHistory()
        # Reading-order drag selection (not rubber-band rectangle).
        self._select_anchor: int | None = None
        self._drag_selecting = False
        self._press_row: int | None = None
        self._suppress_clicked = False

    def apply_typography(
        self,
        *,
        family: str = "",
        size: int = 14,
        spacing: int = 0,
        pad_x: int = 1,
        pad_y: int = 2,
    ) -> None:
        self._delegate.apply_typography(
            family=family, size=size, pad_x=pad_x, pad_y=pad_y
        )
        self.setSpacing(max(0, int(spacing)))
        if self._model.rowCount():
            top = self._model.index(0)
            bottom = self._model.index(self._model.rowCount() - 1)
            self._model.dataChanged.emit(
                top, bottom, [Qt.ItemDataRole.SizeHintRole, Qt.ItemDataRole.DisplayRole]
            )
        self.viewport().update()
        self.doItemsLayout()

    @property
    def transcript(self) -> Transcript | None:
        return self._model.transcript

    def set_transcript(self, transcript: Transcript | None) -> None:
        self._history.clear()
        self._model.set_transcript(transcript)

    def set_silence_thresholds(self, min_duration: float, max_pause: float) -> None:
        self._model.set_silence_thresholds(min_duration, max_pause)

    def set_timeline_context(self, timeline_start_frame: int, fps: float) -> None:
        self._model.set_timeline_context(timeline_start_frame, fps)

    def refresh(self) -> None:
        self._model.refresh()

    def set_search_matches(self, word_indices: list[int]) -> None:
        self._model.set_matches(word_indices)

    def clear_search_matches(self) -> None:
        self._model.clear_matches()

    def set_compare_statuses(self, statuses: dict[int, str]) -> None:
        self._model.set_compare_statuses(statuses)

    def clear_compare_statuses(self) -> None:
        self._model.clear_compare_statuses()

    def select_word(self, word_index: int, *, scroll: bool = True) -> None:
        row = self._model.row_for_word(word_index)
        if row < 0:
            return
        index = self._model.index(row)
        sm = self.selectionModel()
        sm.select(
            index,
            sm.SelectionFlag.ClearAndSelect,
        )
        sm.setCurrentIndex(index, sm.SelectionFlag.NoUpdate)
        self._select_anchor = row
        if scroll:
            self.scrollTo(index, QListView.ScrollHint.PositionAtCenter)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._model.set_viewport_width(self.viewport().width())

    def select_model_range(self, start_row: int, end_row: int) -> None:
        """Select all selectable rows between start_row and end_row (inclusive)."""
        self._select_row_range(start_row, end_row, clear=True)
        self._select_anchor = start_row

    def _select_row_range(
        self, a: int, b: int, *, clear: bool = True
    ) -> None:
        sm = self.selectionModel()
        if sm is None or self._model.rowCount() == 0:
            return
        lo = max(0, min(a, b))
        hi = min(self._model.rowCount() - 1, max(a, b))
        selection = QItemSelection()
        selection.select(self._model.index(lo), self._model.index(hi))
        flags = (
            sm.SelectionFlag.ClearAndSelect
            if clear
            else sm.SelectionFlag.Select
        )
        sm.select(selection, flags)

    def _row_at_pos(self, pos) -> int:
        idx = self.indexAt(pos)
        if idx.isValid():
            return idx.row()
        n = self._model.rowCount()
        if n == 0:
            return -1

        r0_rect = self.visualRect(self._model.index(0))
        rn_rect = self.visualRect(self._model.index(n - 1))
        if pos.y() <= r0_rect.top():
            return 0
        if pos.y() >= rn_rect.bottom():
            return n - 1

        # Binary search for the last row with rect.top() <= pos.y()
        lo = 0
        hi = n - 1
        best_r = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            rect = self.visualRect(self._model.index(mid))
            if rect.top() <= pos.y():
                best_r = mid
                lo = mid + 1
            else:
                hi = mid - 1

        target_top = self.visualRect(self._model.index(best_r)).top()
        start_r = best_r
        while start_r > 0 and self.visualRect(self._model.index(start_r - 1)).top() == target_top:
            start_r -= 1
        end_r = best_r
        while end_r < n - 1 and self.visualRect(self._model.index(end_r + 1)).top() == target_top:
            end_r += 1

        line_rows = [(r, self.visualRect(self._model.index(r))) for r in range(start_r, end_r + 1)]
        if not line_rows:
            return best_r

        if pos.x() <= line_rows[0][1].left():
            return line_rows[0][0]
        if pos.x() >= line_rows[-1][1].right():
            return line_rows[-1][0]
        return min(line_rows, key=lambda item: abs(item[1].center().x() - pos.x()))[0]

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        pos = event.position().toPoint()
        row = self._row_at_pos(pos)
        self._press_row = row
        self._drag_selecting = False
        self._suppress_clicked = False
        mods = event.modifiers()
        ctrl = bool(
            mods
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.MetaModifier
            )
        )
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        sm = self.selectionModel()

        if row < 0:
            if not ctrl:
                sm.clearSelection()
            self._select_anchor = None
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return

        index = self._model.index(row)
        selectable = bool(
            self._model.flags(index) & Qt.ItemFlag.ItemIsSelectable
        )

        if ctrl:
            if selectable:
                sm.select(index, sm.SelectionFlag.Toggle)
                self._select_anchor = row
            sm.setCurrentIndex(index, sm.SelectionFlag.NoUpdate)
        elif shift:
            anchor = self._select_anchor if self._select_anchor is not None else row
            self._select_row_range(anchor, row, clear=True)
            sm.setCurrentIndex(index, sm.SelectionFlag.NoUpdate)
            self._drag_selecting = True
        else:
            self._select_anchor = row
            if selectable:
                sm.select(index, sm.SelectionFlag.ClearAndSelect)
            else:
                sm.clearSelection()
            sm.setCurrentIndex(index, sm.SelectionFlag.NoUpdate)
            self._drag_selecting = True

        self.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_selecting
            and event.buttons() & Qt.MouseButton.LeftButton
            and self._select_anchor is not None
        ):
            pos = event.position().toPoint()
            row = self._row_at_pos(pos)
            if row >= 0:
                if row != self._press_row:
                    self._suppress_clicked = True
                self._select_row_range(self._select_anchor, row, clear=True)
                sm = self.selectionModel()
                index = self._model.index(row)
                sm.setCurrentIndex(index, sm.SelectionFlag.NoUpdate)
                self.scrollTo(index)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            press_row = self._press_row
            suppress = self._suppress_clicked
            self._drag_selecting = False
            self._press_row = None
            self._suppress_clicked = False
            index = self.indexAt(event.position().toPoint())
            # Mirror QAbstractItemView.clicked: same item, no drag.
            if (
                not suppress
                and index.isValid()
                and press_row is not None
                and index.row() == press_row
            ):
                self._on_click(index)
            event.accept()
            return
        super().mouseReleaseEvent(event)

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

    def _selected_silence_gaps(self) -> list[tuple[float, float, bool]]:
        rows = sorted(i.row() for i in self.selectionModel().selectedIndexes())
        out: list[tuple[float, float, bool]] = []
        for r in rows:
            data = self._model.data(self._model.index(r), SILENCE_ROLE)
            if data is not None:
                cs, ce, trimmed = data
                out.append((float(cs), float(ce), bool(trimmed)))
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
        silences = self._selected_silence_gaps()
        if not words and not silences:
            return
        self.push_history()
        if words:
            # Toggle each word: removed → restore, active → remove.
            to_restore = [w for w in words if w in self.transcript.removed]
            to_remove = [w for w in words if w not in self.transcript.removed]
            if to_restore:
                self.transcript.restore(to_restore)
            if to_remove:
                self.transcript.delete(to_remove)
        if silences:
            max_pause = self._model._silence_max_pause
            cuts = list(self.transcript.silence_cuts)
            for gap_start, gap_end, trimmed in silences:
                if trimmed:
                    # Restore: drop cuts whose midpoint is in this gap.
                    drop = {
                        (round(c[0], 4), round(c[1], 4))
                        for c in cuts_in_gap(cuts, gap_start, gap_end)
                    }
                    cuts = [
                        c for c in cuts
                        if (round(c[0], 4), round(c[1], 4)) not in drop
                    ]
                else:
                    # Trim: add shrunk cut matching Trim Silence.
                    cut = shrink_silence_cut(gap_start, gap_end, max_pause)
                    if cut is not None:
                        key = (round(cut[0], 4), round(cut[1], 4))
                        if not any(
                            (round(c[0], 4), round(c[1], 4)) == key for c in cuts
                        ):
                            cuts.append(cut)
            self.transcript.silence_cuts = sorted(cuts)
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
            selected_words = self._selected_word_indices()
            if (
                event.key() == Qt.Key.Key_Backspace
                and (
                    not selected_words
                    or selected_words == [self._current_word_index()]
                )
                and self._merge_caption_break_at_current()
            ):
                event.accept()
                return
            self.delete_selection()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.insert_caption_break()
            event.accept()
            return
        elif event.matches(QKeySequence.StandardKey.Cut):
            self.cut_selection()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.paste_at_current()
        else:
            super().keyPressEvent(event)

    def _current_word_index(self) -> int | None:
        current = self.currentIndex()
        if not current.isValid():
            return None
        return self._model.word_index(current.row())

    def _focus_word(self, word_index: int) -> None:
        row = self._model.row_for_word(word_index)
        if row < 0:
            return
        index = self._model.index(row)
        selection = self.selectionModel()
        selection.clearSelection()
        selection.setCurrentIndex(index, selection.SelectionFlag.NoUpdate)
        self._select_anchor = row
        self.scrollTo(index, QListView.ScrollHint.PositionAtCenter)

    def insert_caption_break(self) -> bool:
        """Insert a persistent boundary before the selection/current word."""
        if self.transcript is None:
            return False
        selected = self._selected_word_indices()
        if selected:
            before_word = selected[0]
        else:
            before_word = self._current_word_index()
        if before_word is None:
            return False
        if (
            before_word not in self.transcript.order
            or before_word in self.transcript.caption_breaks
        ):
            return False
        if any(line.start_word == before_word for line in self.transcript.lines()):
            return False
        self.push_history()
        if not self.transcript.add_caption_break(before_word):
            return False
        self._model.refresh()
        self._focus_word(before_word)
        self.edited.emit()
        return True

    def _merge_caption_break_at_current(self) -> bool:
        """Remove a manual break at the current word for Backspace."""
        if self.transcript is None:
            return False
        word = self._current_word_index()
        if word is None or word not in self.transcript.caption_breaks:
            return False
        self.push_history()
        self.transcript.remove_caption_break(word)
        self._model.refresh()
        self._focus_word(word)
        self.edited.emit()
        return True

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
