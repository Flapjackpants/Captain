"""DaVinci Resolve-style dark theme for the Captain UI."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Palette sampled from Resolve 19/20's Edit page.
BG_WINDOW = "#28282e"
BG_PANEL = "#212126"
BG_INPUT = "#1b1b20"
BG_EDITOR = "#141417"
BORDER = "#0e0e11"
BORDER_LIGHT = "#3a3a44"
TEXT = "#d6d6dc"
TEXT_DIM = "#87878f"
ACCENT = "#e64b3d"      # Resolve's warm selection red-orange
ACCENT_DARK = "#b23a2e"

QSS = f"""
QMainWindow, QDialog, QMessageBox {{
    background-color: {BG_WINDOW};
}}
QWidget {{
    color: {TEXT};
    font-size: 13px;
}}

QPushButton {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 16px;
    min-height: 16px;
}}
QPushButton:hover {{
    background-color: #33333b;
    border-color: {BORDER_LIGHT};
}}
QPushButton:pressed {{
    background-color: {BG_INPUT};
}}
QPushButton:disabled {{
    background-color: #232329;
    color: #5b5b63;
}}
QPushButton#accent {{
    background-color: {ACCENT_DARK};
    color: #f4f4f6;
    font-weight: 600;
    border: 1px solid #7d2a21;
}}
QPushButton#accent:hover {{
    background-color: {ACCENT};
}}
QPushButton#accent:disabled {{
    background-color: #4a2723;
    color: #8d7b78;
}}

QComboBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 10px;
}}
QComboBox:hover {{
    border-color: {BORDER_LIGHT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DARK};
    selection-color: #f4f4f6;
    outline: none;
}}

QLineEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 10px;
    selection-background-color: {ACCENT_DARK};
}}
QLineEdit:focus {{
    border-color: {BORDER_LIGHT};
}}
QLineEdit:disabled {{
    color: #5b5b63;
}}

QListView#transcript {{
    background-color: {BG_EDITOR};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 8px;
}}

QLabel#hint {{
    color: {TEXT_DIM};
    font-size: 11px;
    padding: 2px 1px;
}}
QLabel#stage {{
    color: {TEXT_DIM};
    font-size: 12px;
}}

QProgressBar {{
    background-color: {BG_EDITOR};
    border: 1px solid {BORDER};
    border-radius: 3px;
    height: 14px;
    text-align: center;
    color: {TEXT};
    font-size: 10px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT_DARK};
    border-radius: 2px;
}}

QStatusBar {{
    background-color: {BG_INPUT};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
}}
QStatusBar::item {{ border: none; }}

QScrollBar:vertical {{
    background: {BG_EDITOR};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #3d3d46;
    border-radius: 5px;
    min-height: 24px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: #4c4c56;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {BG_EDITOR};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #3d3d46;
    border-radius: 5px;
    min-width: 24px;
    margin: 2px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QToolTip {{
    background-color: {BG_PANEL};
    color: {TEXT};
    border: 1px solid {BORDER_LIGHT};
    padding: 4px 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_WINDOW))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_EDITOR))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_DARK))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#f4f4f6"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_DIM))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#5b5b63")
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#5b5b63")
    )
    app.setPalette(palette)
    app.setStyleSheet(QSS)
