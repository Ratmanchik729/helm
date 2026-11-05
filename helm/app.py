from __future__ import annotations

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import APP_NAME, config, theme
from .window import MainWindow


def main():
    config.ensure_dir()
    QApplication.setApplicationName(APP_NAME)
    QApplication.setDesktopFileName("helm")
    app = QApplication(sys.argv)
    theme.apply(app)
    from pathlib import Path
    from PySide6.QtGui import QIcon
    icon = Path(__file__).resolve().parent.parent / "helm.svg"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    w = MainWindow()
    w.show()
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    t = QTimer()
    t.start(300)
    t.timeout.connect(lambda: None)  # даём Python обработать Ctrl+C
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
