"""Word-flow transcript editor widget.

Words render as a wrapping flow (like a document). Selection, Delete/
Backspace to remove, Ctrl+X / Ctrl+V to cut words and paste them at the
current position, double-click to jump the Resolve playhead.
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

from ..transcript import Transcript

WORD_ROLE = Qt.ItemDataRole.UserRole + 1  # -> (word_index, removed: bool)


class TranscriptModel(QAbstractListModel):
    """Rows follow transcript.order; each row is one word."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.transcript: Transcript | None = None

    def set_transcript(self, transcript: Transcript | None) -> None:
        self.beginResetModel()
        self.transcript = transcript
        self.endResetModel()

    def refresh(self) -> None:
        if self.transcript is not None:
            top = self.index(0)
            bottom = self.index(self.rowCount() - 1)
            self.dataChanged.emit(top, bottom)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if self.transcript is None else len(self.transcript.order)

    def word_index(self, row: int) -> int:
        return self.transcript.order[row]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if self.transcript is None or not index.isValid():
            return None
        widx = self.transcript.order[index.row()]
        word = self.transcript.words[widx]
        if role == Qt.ItemDataRole.DisplayRole:
            return word.text
        if role == WORD_ROLE:
            return (widx, widx in self.transcript.removed)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{word.start:.2f}s – {word.end:.2f}s"
        return None


class WordDelegate(QStyledItemDelegate):
    PAD_X = 6
    PAD_Y = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.font = QFont()
        self.font.setPointSize(14)

    def sizeHint(self, option, index) -> QSize:
        fm = QFontMetrics(self.font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        return QSize(fm.horizontalAdvance(text) + self.PAD_X * 2,
                     fm.height() + self.PAD_Y * 2)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setFont(self.font)
        _widx, removed = index.data(WORD_ROLE)
        rect = option.rect

        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, option.palette.highlight())

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
    edited = Signal()                # any op that changes keep-ranges
    word_activated = Signal(int)     # word index (double click)

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
        self.doubleClicked.connect(self._on_double_click)
        self._clipboard_words: list[int] = []

    # ---- helpers ----------------------------------------------------------

    @property
    def transcript(self) -> Transcript | None:
        return self._model.transcript

    def set_transcript(self, transcript: Transcript | None) -> None:
        self._model.set_transcript(transcript)

    def refresh(self) -> None:
        self._model.refresh()

    def _selected_word_indices(self) -> list[int]:
        rows = sorted(i.row() for i in self.selectionModel().selectedIndexes())
        return [self._model.word_index(r) for r in rows]

    # ---- ops --------------------------------------------------------------

    def delete_selection(self) -> None:
        indices = self._selected_word_indices()
        if indices and self.transcript:
            self.transcript.delete(indices)
            self._model.refresh()
            self.edited.emit()

    def restore_selection(self) -> None:
        indices = self._selected_word_indices()
        if indices and self.transcript:
            self.transcript.restore(indices)
            self._model.refresh()
            self.edited.emit()

    def cut_selection(self) -> None:
        indices = self._selected_word_indices()
        if indices and self.transcript:
            self._clipboard_words = indices
            self.transcript.delete(indices)
            self._model.refresh()
            self.edited.emit()

    def paste_at_current(self) -> None:
        if not self._clipboard_words or self.transcript is None:
            return
        current = self.currentIndex()
        dest_row = current.row() + 1 if current.isValid() else len(self.transcript.order)
        moving = set(self._clipboard_words)
        # dest position counts order entries before dest_row not being moved
        dest_pos = sum(
            1 for i in self.transcript.order[:dest_row] if i not in moving
        )
        self.transcript.move(self._clipboard_words, dest_pos)
        self.transcript.restore(self._clipboard_words)
        self._clipboard_words = []
        self._model.set_transcript(self.transcript)  # order changed: full reset
        self.edited.emit()

    # ---- events -------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selection()
        elif event.matches(QKeySequence.StandardKey.Cut):
            self.cut_selection()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.paste_at_current()
        elif event.matches(QKeySequence.StandardKey.Undo):
            self.restore_selection()
        else:
            super().keyPressEvent(event)

    def _on_double_click(self, index: QModelIndex) -> None:
        self.word_activated.emit(self._model.word_index(index.row()))
