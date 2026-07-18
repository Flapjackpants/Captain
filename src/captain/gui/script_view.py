"""Read-only script pane for Phase 2 compare view.

Shows imported script tokens as a wrapping word flow, colored by alignment
status (white match, blue missing, red mismatch). Click emits the script
token index for sync with the transcript pane.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from .theme import (
    COMPARE_MATCH,
    COMPARE_MISSING,
    COMPARE_MISMATCH,
)

STATUS_ROLE = Qt.ItemDataRole.UserRole + 1  # ScriptStatus str
INDEX_ROLE = Qt.ItemDataRole.UserRole + 2  # script token index
HIGHLIGHT_ROLE = Qt.ItemDataRole.UserRole + 3  # bool — linked highlight


class ScriptModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens: list[str] = []
        self._statuses: dict[int, str] = {}
        self._highlight: int | None = None

    def set_script(
        self,
        tokens: list[str],
        statuses: dict[int, str] | None = None,
    ) -> None:
        self.beginResetModel()
        self.tokens = list(tokens)
        self._statuses = dict(statuses or {})
        self._highlight = None
        self.endResetModel()

    def clear(self) -> None:
        self.set_script([])

    def set_statuses(self, statuses: dict[int, str]) -> None:
        self._statuses = dict(statuses)
        if self.rowCount():
            top = self.index(0)
            bottom = self.index(self.rowCount() - 1)
            self.dataChanged.emit(top, bottom, [STATUS_ROLE])

    def set_highlight(self, script_index: int | None) -> None:
        old = self._highlight
        self._highlight = script_index
        for idx in (old, script_index):
            if idx is not None and 0 <= idx < self.rowCount():
                i = self.index(idx)
                self.dataChanged.emit(i, i, [HIGHLIGHT_ROLE])

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.tokens)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.tokens):
            return None
        i = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.tokens[i]
        if role == STATUS_ROLE:
            return self._statuses.get(i, "match")
        if role == INDEX_ROLE:
            return i
        if role == HIGHLIGHT_ROLE:
            return i == self._highlight
        if role == Qt.ItemDataRole.ToolTipRole:
            st = self._statuses.get(i, "match")
            labels = {
                "match": "In script and video",
                "missing": "In script, missing from video",
                "mismatch": "Differs from video",
            }
            return labels.get(st, st)
        return None


class ScriptDelegate(QStyledItemDelegate):
    PAD_X = 6
    PAD_Y = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.font = QFont()
        self.font.setPointSize(14)

    def sizeHint(self, option, index) -> QSize:
        fm = QFontMetrics(self.font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        return QSize(
            fm.horizontalAdvance(text) + self.PAD_X * 2,
            fm.height() + self.PAD_Y * 2,
        )

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setFont(self.font)
        rect = option.rect
        status = index.data(STATUS_ROLE) or "match"
        highlighted = bool(index.data(HIGHLIGHT_ROLE))

        if option.state & QStyle.StateFlag.State_Selected or highlighted:
            painter.fillRect(rect, option.palette.highlight())

        if status == "missing":
            color = QColor(COMPARE_MISSING)
        elif status == "mismatch":
            color = QColor(COMPARE_MISMATCH)
        else:
            color = QColor(COMPARE_MATCH)

        if option.state & QStyle.StateFlag.State_Selected or highlighted:
            # Keep readable on accent selection.
            if status == "match":
                color = option.palette.highlightedText().color()

        painter.setPen(QPen(color))
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        text_rect = rect.adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class ScriptView(QListView):
    """Read-only wrapping word list for the imported script."""

    token_activated = Signal(int)  # script token index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = ScriptModel(self)
        self.setModel(self._model)
        self.setItemDelegate(ScriptDelegate(self))
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setSpacing(2)
        self.setUniformItemSizes(False)
        self.setObjectName("transcript")  # reuse editor chrome from theme
        self.clicked.connect(self._on_click)

    def set_script(
        self,
        tokens: list[str],
        statuses: dict[int, str] | None = None,
    ) -> None:
        self._model.set_script(tokens, statuses)

    def clear(self) -> None:
        self._model.clear()

    def set_statuses(self, statuses: dict[int, str]) -> None:
        self._model.set_statuses(statuses)

    def select_token(self, script_index: int, *, scroll: bool = True) -> None:
        if script_index < 0 or script_index >= self._model.rowCount():
            return
        index = self._model.index(script_index)
        self.selectionModel().select(
            index,
            self.selectionModel().SelectionFlag.ClearAndSelect,
        )
        self.setCurrentIndex(index)
        self._model.set_highlight(script_index)
        if scroll:
            self.scrollTo(index, QListView.ScrollHint.PositionAtCenter)

    def clear_highlight(self) -> None:
        self._model.set_highlight(None)

    def _on_click(self, index: QModelIndex) -> None:
        if index.isValid():
            self.token_activated.emit(index.row())
