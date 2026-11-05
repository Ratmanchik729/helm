"""Тёмная тема, палитра и вспомогательные виджеты оформления."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0d1017"
BG2 = "#131722"
PANEL = "#171c28"
PANEL2 = "#1d2333"
BORDER = "#262e40"
TEXT = "#d7dce6"
MUTED = "#7f8aa3"
ACCENT = "#7c6cff"
ACCENT2 = "#38d9c0"
OK = "#3ddc97"
WARN = "#ffb547"
ERR = "#ff5c7a"

# палитра терминала (16 цветов)
TERM_FG = "#d7dce6"
TERM_BG = "#0b0e14"
TERM_ANSI = [
    "#1b1f2b", "#ff5c7a", "#3ddc97", "#ffb547", "#6d9dff", "#c58cff", "#38d9c0", "#c6cbd6",
    "#4a5268", "#ff7d94", "#6ff0b4", "#ffd07a", "#93b6ff", "#d9acff", "#7ff0de", "#ffffff",
]


def mono_font(size: int = 11) -> QFont:
    fams = set(QFontDatabase.families())
    for name in ("JetBrainsMono Nerd Font", "JetBrains Mono", "Fira Code", "Cascadia Code",
                 "Hack", "DejaVu Sans Mono", "Monospace"):
        if name in fams:
            f = QFont(name, size)
            break
    else:
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        f.setPointSize(size)
    f.setStyleHint(QFont.Monospace)
    f.setFixedPitch(True)
    f.setHintingPreference(QFont.PreferFullHinting)
    return f


QSS = f"""
* {{ outline: 0; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; border-radius: 6px; }}

QLabel {{ background: transparent; }}
QWidget#clear {{ background: transparent; }}
QLabel#h1 {{ font-size: 20pt; font-weight: 700; color: #ffffff; }}
QLabel#h2 {{ font-size: 13pt; font-weight: 600; color: #ffffff; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#logo {{ font-size: 15pt; font-weight: 800; color: {ACCENT}; letter-spacing: 3px; }}
QLabel#bigval {{ font-size: 22pt; font-weight: 700; color: #ffffff; }}
QLabel#cardtitle {{ color: {MUTED}; font-size: 9pt; font-weight: 600; letter-spacing: 1px; }}

QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#sidebar {{ background: {BG2}; border-right: 1px solid {BORDER}; }}
QFrame#toolbar {{ background: {BG2}; border-bottom: 1px solid {BORDER}; }}
QFrame#banner {{ background: #3a1622; border: 1px solid {ERR}; border-radius: 8px; }}

QPushButton {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: #252c40; border-color: #36405a; }}
QPushButton:pressed {{ background: #1a1f2e; }}
QPushButton:disabled {{ color: #4d566b; background: {PANEL}; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #8f82ff; }}
QPushButton#danger {{ background: transparent; border: 1px solid #5a2433; color: {ERR}; }}
QPushButton#danger:hover {{ background: #2e121b; }}
QPushButton#flat, QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 5px 8px; }}
QPushButton#flat:hover, QToolButton:hover {{ background: {PANEL2}; }}
QToolButton:checked {{ background: {PANEL2}; color: {ACCENT2}; }}
QToolButton::menu-indicator {{ image: none; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {ACCENT}; selection-color: white;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QTreeView, QTreeWidget, QListWidget, QTableView {{
    background: {BG}; border: none; alternate-background-color: #10141d;
    selection-background-color: #2a2560; selection-color: white;
}}
QTreeView::item, QTreeWidget::item {{ padding: 4px 2px; border: none; }}
QTreeView::item:hover, QListWidget::item:hover {{ background: #161b28; }}
QTreeView::item:selected, QListWidget::item:selected {{ background: #2a2560; color: white; }}
QHeaderView::section {{
    background: {BG2}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER}; padding: 6px 8px; font-weight: 600;
}}
QHeaderView {{ background: {BG2}; }}

QListWidget#nav {{ background: {BG2}; border: none; padding: 8px 6px; }}
QListWidget#nav::item {{ padding: 10px 12px; border-radius: 8px; margin: 2px 0; color: {MUTED}; }}
QListWidget#nav::item:selected {{ background: #221f45; color: white; border-left: 3px solid {ACCENT}; }}
QListWidget#nav::item:hover:!selected {{ background: {PANEL}; color: {TEXT}; }}

QListWidget#hosts {{ background: transparent; border: none; }}
QListWidget#hosts::item {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; margin: 4px 0; padding: 10px; }}
QListWidget#hosts::item:selected {{ border-color: {ACCENT}; background: #1d1a3a; }}
QListWidget#hosts::item:hover:!selected {{ border-color: #3a4560; }}

QTabWidget::pane {{ border: none; background: {BG}; }}
QTabBar {{ background: {BG2}; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 8px 16px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px;
}}
QTabBar::tab:selected {{ color: white; border-bottom: 2px solid {ACCENT}; background: {BG}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {PANEL}; }}
QTabBar::close-button {{ subcontrol-position: right; image: url(__CLOSE__); border-radius: 4px; padding: 2px; }}
QTabBar::close-button:hover {{ background: #3a1622; image: url(__CLOSE_HOVER__); }}
QTabWidget#sessions > QTabBar::tab {{ padding: 10px 18px; font-weight: 600; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2b3348; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4560; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2b3348; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{ background: {BG2}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 4px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QProgressBar#textbar {{ color: {TEXT}; height: 18px; font-size: 8pt; }}

QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QStatusBar {{ background: {BG2}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ background: transparent; color: {MUTED}; padding: 0 6px; }}
QDialog {{ background: {BG2}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {BG2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in (
        (QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, BG2),
        (QPalette.AlternateBase, PANEL), (QPalette.Text, TEXT), (QPalette.Button, PANEL2),
        (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, PANEL2), (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, MUTED),
        (QPalette.Link, ACCENT2),
    ):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    from . import config
    config.ensure_dir()
    icons = {}
    for name, col in (("close", MUTED), ("close_hover", ERR)):
        path = config.CONFIG_DIR / f"{name}.svg"
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'<path d="M4 4L12 12M12 4L4 12" stroke="{col}" stroke-width="1.8" stroke-linecap="round"/></svg>')
        icons[name] = path.as_posix()
    app.setStyleSheet(QSS.replace("__CLOSE__", icons["close"]).replace("__CLOSE_HOVER__", icons["close_hover"]))
