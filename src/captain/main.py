"""Captain application entry point."""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from . import config
    from .gui.main_window import MainWindow

    config.setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("Captain")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
