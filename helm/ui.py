"""Общие виджеты и хелперы интерфейса."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QToolButton, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import theme
from .ssh import NeedSudoPassword, human_error
from .tasks import run_async

_NERD = None


def nerd() -> bool:
    global _NERD
    if _NERD is None:
        _NERD = any("Nerd Font" in f for f in QFontDatabase.families())
    return _NERD


def icon_font_family() -> str:
    for f in QFontDatabase.families():
        if f.startswith("JetBrainsMono Nerd Font") or f.startswith("JetBrainsMono NF"):
            return f
    for f in QFontDatabase.families():
        if "Nerd Font" in f:
            return f
    return ""


# Иконки Nerd Font (Font Awesome коды)
ICONS = {
    "dash": "", "files": "", "term": "", "edit": "", "docker": "",
    "proc": "", "svc": "", "server": "", "plus": "", "refresh": "",
    "up": "", "home": "", "upload": "", "download": "", "trash": "",
    "folder": "", "file": "", "save": "", "search": "", "play": "",
    "stop": "", "restart": "", "logs": "", "info": "", "kill": "",
    "plug": "", "bolt": "", "link": "", "back": "", "copy": "",
    "pause": "", "box": "", "net": "", "db": "", "layers": "",
}


def ic(name: str) -> str:
    return ICONS.get(name, "") if nerd() else ""


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_time(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def human_uptime(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    if title:
        t = QLabel(title.upper())
        t.setObjectName("cardtitle")
        lay.addWidget(t)
    return f, lay


def tool_button(icon: str, text: str, tip: str = "", slot=None, obj: str = "") -> QPushButton:
    i = ic(icon)
    b = QPushButton(f"{i}  {text}" if i and text else (i or text or tip))
    if obj:
        b.setObjectName(obj)
    if tip:
        b.setToolTip(tip)
    if slot:
        b.clicked.connect(slot)
    b.setCursor(Qt.PointingHandCursor)
    fam = icon_font_family()
    if fam and i:
        f = b.font()
        f.setFamilies([f.family(), fam])
        b.setFont(f)
    return b


def icon_button(icon: str, tip: str, slot=None, fallback: str = "") -> QToolButton:
    b = QToolButton()
    i = ic(icon)
    b.setText(i or fallback or tip[:1])
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    fam = icon_font_family()
    if fam and i:
        f = b.font()
        f.setFamilies([f.family(), fam])
        f.setPointSize(11)
        b.setFont(f)
    if slot:
        b.clicked.connect(slot)
    return b


def toolbar_frame() -> tuple[QFrame, QHBoxLayout]:
    f = QFrame()
    f.setObjectName("toolbar")
    lay = QHBoxLayout(f)
    lay.setContentsMargins(10, 6, 10, 6)
    lay.setSpacing(6)
    return f, lay


def error(parent, text, title="Ошибка"):
    if isinstance(text, BaseException):
        text = human_error(text)
    QMessageBox.critical(parent, title, str(text))


def confirm(parent, text, title="Подтверждение") -> bool:
    return QMessageBox.question(parent, title, text,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


def ask_password(parent, title, label) -> str | None:
    pw, ok = QInputDialog.getText(parent, title, label, QLineEdit.Password)
    return pw if ok else None


def run_priv(parent, session, fn, *args, on_done=None, on_error=None, **kw):
    """Выполнить fn в фоне; при NeedSudoPassword спросить пароль и повторить."""
    def err(e):
        if isinstance(e, NeedSudoPassword):
            pw = ask_password(parent, "sudo", f"{e}\nПароль sudo для {session.profile.user}:")
            if pw:
                session.sudo_password = pw
                run_async(fn, *args, on_done=on_done, on_error=err, **kw)
            return
        if on_error:
            on_error(e)
        else:
            error(parent, e)
    run_async(fn, *args, on_done=on_done, on_error=err, **kw)


class OutputDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        ed = QPlainTextEdit()
        ed.setReadOnly(True)
        ed.setFont(theme.mono_font(10))
        ed.setPlainText(text)
        ed.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(ed)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def show_output(parent, title, text):
    OutputDialog(title, text, parent).exec()


class SortItem(QTreeWidgetItem):
    """Элемент дерева с сортировкой по UserRole (числа) и группировкой папок."""
    def __lt__(self, other):
        tw = self.treeWidget()
        col = tw.sortColumn() if tw else 0
        a = self.data(col, Qt.UserRole)
        b = other.data(col, Qt.UserRole)
        ga, gb = self.data(0, Qt.UserRole + 5), other.data(0, Qt.UserRole + 5)
        if ga is not None and gb is not None and ga != gb:
            asc = tw.header().sortIndicatorOrder() == Qt.AscendingOrder if tw else True
            return (ga < gb) if asc else (ga > gb)
        if a is not None and b is not None:
            try:
                return a < b
            except TypeError:
                pass
        return self.text(col).lower() < other.text(col).lower()


_dots: dict = {}


def dot_icon(color: str):
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    if color not in _dots:
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(3, 3, 8, 8)
        p.end()
        _dots[color] = QIcon(pm)
    return _dots[color]


def status_dot(color: str, size: int = 9) -> QLabel:
    l = QLabel()
    l.setFixedSize(size, size)
    l.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return l


class Banner(QFrame):
    def __init__(self, text: str, button: str, slot, parent=None):
        super().__init__(parent)
        self.setObjectName("banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel(text)
        self.label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        lay.addWidget(self.label, 1)
        b = QPushButton(button)
        b.setObjectName("primary")
        b.clicked.connect(slot)
        lay.addWidget(b)


def hline() -> QWidget:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {theme.BORDER};")
    return f
