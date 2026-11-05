"""Полноценный терминал поверх SSH PTY: VT100/xterm через pyte, 256/truecolor,
скроллбэк, альтернативный экран, выделение мышью, копирование/вставка."""
from __future__ import annotations

import copy
from collections import defaultdict, deque

import pyte
from pyte import modes as mo
from pyte.screens import Margins, StaticDefaultDict
from PySide6.QtCore import QPoint, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QScrollBar, QHBoxLayout, QWidget

from . import theme

NAMES = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3, "blue": 4, "magenta": 5,
    "cyan": 6, "white": 7, "brightblack": 8, "brightred": 9, "brightgreen": 10,
    "brightbrown": 11, "brightyellow": 11, "brightblue": 12, "brightmagenta": 13,
    "bfightmagenta": 13, "brightcyan": 14, "brightwhite": 15,
}
ALT_MODES = (47, 1047, 1049)
BRACKETED_PASTE = 2004 << 5
DECCKM = 1 << 5  # режим "application cursor keys" (в pyte нет константы)

# X11-коды клавиш QWERTY → латинская буква (Ctrl+C в русской раскладке)
SCANCODE_LATIN = {24: "q", 25: "w", 26: "e", 27: "r", 28: "t", 29: "y", 30: "u", 31: "i",
                  32: "o", 33: "p", 38: "a", 39: "s", 40: "d", 41: "f", 42: "g", 43: "h",
                  44: "j", 45: "k", 46: "l", 52: "z", 53: "x", 54: "c", 55: "v", 56: "b",
                  57: "n", 58: "m"}


class _Screen(pyte.Screen):
    def __init__(self, cols, rows, owner):
        self.scrollback: deque = deque(maxlen=10000)
        self.in_alt = False
        self._main = None
        self.added = 0
        self._owner = owner
        super().__init__(cols, rows)

    def _new_buffer(self):
        return defaultdict(lambda: StaticDefaultDict(self.default_char))

    def write_process_input(self, data):
        self._owner.send(data.encode())

    def set_title(self, param):
        super().set_title(param)
        self._owner.titleChanged.emit(self.title)

    def bell(self, *a):
        pass

    def index(self):
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and not self.in_alt:
            self.scrollback.append(self.buffer[0])
            self.added += 1
        super().index()

    def set_mode(self, *modes, **kw):
        if kw.get("private"):
            if any(m in ALT_MODES for m in modes):
                if not self.in_alt:
                    self._main = (self.buffer, copy.copy(self.cursor))
                    self.buffer = self._new_buffer()
                    self.in_alt = True
                    self.dirty.update(range(self.lines))
                modes = tuple(m for m in modes if m not in ALT_MODES)
                if not modes:
                    return
        super().set_mode(*modes, **kw)

    def reset_mode(self, *modes, **kw):
        if kw.get("private"):
            if any(m in ALT_MODES for m in modes):
                if self.in_alt and self._main:
                    self.buffer, cur = self._main
                    self.cursor.x = min(cur.x, self.columns - 1)
                    self.cursor.y = min(cur.y, self.lines - 1)
                    self.cursor.attrs = cur.attrs
                    self._main = None
                    self.in_alt = False
                    self.dirty.update(range(self.lines))
                modes = tuple(m for m in modes if m not in ALT_MODES)
                if not modes:
                    return
        super().reset_mode(*modes, **kw)

    def resize(self, lines=None, columns=None):
        lines = lines or self.lines
        columns = columns or self.columns
        if lines == self.lines and columns == self.columns:
            return
        if lines < self.lines:
            n = max(0, self.cursor.y - (lines - 1))
            if n:
                if not self.in_alt:
                    for y in range(n):
                        self.scrollback.append(self.buffer[y])
                for y in range(self.lines - n):
                    self.buffer[y] = self.buffer[y + n]
                for y in range(self.lines - n, self.lines):
                    self.buffer.pop(y, None)
                self.cursor.y -= n
            for y in range(lines, self.lines):
                self.buffer.pop(y, None)
        if columns < self.columns:
            for line in self.buffer.values():
                for x in range(columns, self.columns):
                    line.pop(x, None)
        self.lines, self.columns = lines, columns
        self.set_margins()
        self.cursor.x = min(self.cursor.x, columns - 1)
        self.cursor.y = min(self.cursor.y, lines - 1)
        self.dirty.update(range(lines))


class _Reader(QThread):
    data = Signal(bytes)
    closed = Signal()

    def __init__(self, chan):
        super().__init__()
        self.chan = chan

    def run(self):
        try:
            while True:
                d = self.chan.recv(65536)
                if not d:
                    break
                self.data.emit(bytes(d))
        except Exception:
            pass
        self.closed.emit()


class TerminalView(QWidget):
    titleChanged = Signal(str)
    closed = Signal()
    scrolled = Signal()

    def __init__(self, session, command: str | None = None, font_size: int = 11, parent=None):
        super().__init__(parent)
        self.session = session
        self.command = command
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.IBeamCursor)
        self.setMouseTracking(False)
        self._set_font(font_size)
        self.cols, self.rows = 100, 30
        self.screen = _Screen(self.cols, self.rows, self)
        self.stream = pyte.ByteStream(self.screen)
        self.offset = 0
        self.chan = None
        self.reader = None
        self.alive = False
        self.sel_start = self.sel_end = None
        self._selecting = False
        self._blink_on = True
        self._colors: dict = {}
        self._fg = QColor(theme.TERM_FG)
        self._bg = QColor(theme.TERM_BG)
        self._ansi = [QColor(c) for c in theme.TERM_ANSI]
        self._cursor_col = QColor(theme.ACCENT2)
        self._sel_col = QColor(theme.ACCENT)
        self._sel_col.setAlpha(110)
        self._repaint = QTimer(self, singleShot=True, interval=10, timeout=self.update)
        self._blink = QTimer(self, interval=600, timeout=self._toggle_blink)
        self._blink.start()

    # ---------- шрифт/размер ----------
    def _set_font(self, size):
        self.font_ = theme.mono_font(size)
        self.font_b = QFont(self.font_)
        self.font_b.setBold(True)
        self.font_i = QFont(self.font_)
        self.font_i.setItalic(True)
        fm = QFontMetricsF(self.font_)
        self.cw = fm.horizontalAdvance("M")
        self.ch = round(fm.height() + 1)
        self.ascent = fm.ascent()

    def zoom(self, delta):
        size = max(6, min(32, self.font_.pointSize() + delta))
        self._set_font(size)
        self._recalc_size()
        self.update()

    def start(self):
        self._recalc_size(force=True)
        self.chan = self.session.open_pty(self.cols, self.rows, self.command)
        self.alive = True
        self.reader = _Reader(self.chan)
        self.reader.data.connect(self._on_data)
        self.reader.closed.connect(self._on_closed)
        self.reader.start()

    def _recalc_size(self, force=False):
        cols = max(20, int((self.width() - 8) / self.cw))
        rows = max(5, int((self.height() - 4) / self.ch))
        if force or (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            self.screen.resize(rows, cols)
            if self.chan is not None and self.alive:
                try:
                    self.chan.resize_pty(width=cols, height=rows)
                except Exception:
                    pass

    def resizeEvent(self, e):
        self._recalc_size()
        super().resizeEvent(e)

    # ---------- ввод/вывод ----------
    def send(self, data: bytes):
        if self.chan is None or not self.alive:
            return
        try:
            self.chan.sendall(data)
        except Exception:
            self._on_closed()

    def send_text(self, text: str):
        self.send(text.encode("utf-8"))

    def _on_data(self, data: bytes):
        self.screen.added = 0
        try:
            self.stream.feed(data)
        except Exception:
            pass
        if self.offset and self.screen.added:
            self.offset = min(self.offset + self.screen.added, len(self.screen.scrollback))
        self._blink_on = True
        if not self._repaint.isActive():
            self._repaint.start()
        self.scrolled.emit()

    def _on_closed(self):
        if not self.alive:
            return
        self.alive = False
        self.stream.feed("\r\n\x1b[38;2;255;92;122m[сессия завершена — Enter, чтобы перезапустить]\x1b[0m\r\n".encode())
        self.update()
        self.closed.emit()

    def restart(self):
        try:
            if self.chan:
                self.chan.close()
        except Exception:
            pass
        self.alive = False
        self.screen.reset()
        self.offset = 0
        self.start()

    def close_channel(self):
        self.alive = False
        try:
            if self.chan:
                self.chan.close()
        except Exception:
            pass

    # ---------- клавиатура ----------
    def focusNextPrevChild(self, nxt):
        return False

    def inputMethodEvent(self, e):
        if e.commitString():
            self._to_bottom()
            self.send_text(e.commitString())
        e.accept()

    def keyPressEvent(self, e):
        key, mods, text = e.key(), e.modifiers(), e.text()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)

        if not self.alive and key in (Qt.Key_Return, Qt.Key_Enter):
            self.restart()
            return
        latin = SCANCODE_LATIN.get(e.nativeScanCode())
        if ctrl and shift and (key == Qt.Key_C or latin == "c"):
            self.copy_selection()
            return
        if ctrl and shift and (key == Qt.Key_V or latin == "v"):
            self.paste()
            return
        if shift and key == Qt.Key_Insert:
            self.paste()
            return
        if ctrl and key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom(1)
            return
        if ctrl and key == Qt.Key_Minus:
            self.zoom(-1)
            return
        if shift and key == Qt.Key_PageUp:
            self.scroll_view(self.rows // 2)
            return
        if shift and key == Qt.Key_PageDown:
            self.scroll_view(-(self.rows // 2))
            return

        app = DECCKM in self.screen.mode
        m = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
        seq = None
        arrows = {Qt.Key_Up: "A", Qt.Key_Down: "B", Qt.Key_Right: "C", Qt.Key_Left: "D",
                  Qt.Key_Home: "H", Qt.Key_End: "F"}
        tilde = {Qt.Key_Insert: 2, Qt.Key_Delete: 3, Qt.Key_PageUp: 5, Qt.Key_PageDown: 6,
                 Qt.Key_F5: 15, Qt.Key_F6: 17, Qt.Key_F7: 18, Qt.Key_F8: 19, Qt.Key_F9: 20,
                 Qt.Key_F10: 21, Qt.Key_F11: 23, Qt.Key_F12: 24}
        fkeys = {Qt.Key_F1: "P", Qt.Key_F2: "Q", Qt.Key_F3: "R", Qt.Key_F4: "S"}
        if key in arrows:
            c = arrows[key]
            seq = f"\x1b[1;{m}{c}" if m > 1 else (("\x1bO" if app else "\x1b[") + c)
        elif key in tilde:
            n = tilde[key]
            seq = f"\x1b[{n};{m}~" if m > 1 else f"\x1b[{n}~"
        elif key in fkeys:
            seq = f"\x1b[1;{m}{fkeys[key]}" if m > 1 else "\x1bO" + fkeys[key]
        elif key == Qt.Key_Backspace:
            seq = "\x08" if ctrl else "\x7f"
            if alt:
                seq = "\x1b" + seq
        elif key == Qt.Key_Backtab:
            seq = "\x1b[Z"
        elif key == Qt.Key_Tab:
            seq = "\t"
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            seq = "\r"
        elif key == Qt.Key_Escape:
            seq = "\x1b"
        elif ctrl:
            ch = None
            if Qt.Key_A <= key <= Qt.Key_Z:
                ch = chr(key)
            elif latin:
                ch = latin.upper()
            if ch:
                seq = chr(ord(ch) - 64)
            else:
                special = {Qt.Key_Space: "\x00", Qt.Key_At: "\x00", Qt.Key_BracketLeft: "\x1b",
                           Qt.Key_Backslash: "\x1c", Qt.Key_BracketRight: "\x1d",
                           Qt.Key_AsciiCircum: "\x1e", Qt.Key_Underscore: "\x1f",
                           Qt.Key_Slash: "\x1f", Qt.Key_2: "\x00", Qt.Key_6: "\x1e"}
                seq = special.get(key) or (text or None)
            if seq and alt:
                seq = "\x1b" + seq
        elif text:
            seq = ("\x1b" + text) if alt else text
        if seq:
            self._to_bottom()
            self.clear_selection()
            self.send_text(seq)
            e.accept()
        else:
            super().keyPressEvent(e)

    # ---------- скролл ----------
    def _to_bottom(self):
        if self.offset:
            self.offset = 0
            self.update()
            self.scrolled.emit()

    def scroll_view(self, lines):
        self.offset = max(0, min(len(self.screen.scrollback), self.offset + lines))
        self.update()
        self.scrolled.emit()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 40
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(1 if steps > 0 else -1)
            return
        if self.screen.in_alt:
            n = max(1, int(abs(steps)))
            self.send_text(("\x1bOA" if steps > 0 else "\x1bOB") * n
                           if DECCKM in self.screen.mode else
                           ("\x1b[A" if steps > 0 else "\x1b[B") * n)
            return
        self.scroll_view(int(steps) or (1 if steps > 0 else -1))

    # ---------- выделение ----------
    def _cell_at(self, pos: QPoint):
        row = max(0, min(self.rows - 1, int((pos.y() - 2) / self.ch)))
        col = max(0, min(self.cols, int((pos.x() - 4) / self.cw + 0.5)))
        abs_line = len(self.screen.scrollback) - self.offset + row
        return abs_line, col

    def _line(self, a):
        sb = self.screen.scrollback
        if a < len(sb):
            return sb[a] if a >= 0 else None
        return self.screen.buffer[a - len(sb)]

    def _line_text(self, line, x0=0, x1=None):
        if line is None:
            return ""
        x1 = self.cols if x1 is None else x1
        return "".join(line[x].data for x in range(x0, x1)).rstrip()

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() == Qt.LeftButton:
            self.sel_start = self.sel_end = self._cell_at(e.position().toPoint())
            self._selecting = True
            self.update()
        elif e.button() == Qt.MiddleButton:
            cb = QGuiApplication.clipboard()
            txt = cb.text(cb.Mode.Selection) or cb.text()
            if txt:
                self._paste_text(txt)

    def mouseMoveEvent(self, e):
        if self._selecting:
            self.sel_end = self._cell_at(e.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            if self.sel_start == self.sel_end:
                self.clear_selection()
            else:
                t = self.selected_text()
                if t:
                    QGuiApplication.clipboard().setText(t, QGuiApplication.clipboard().Mode.Selection)

    def mouseDoubleClickEvent(self, e):
        a, c = self._cell_at(e.position().toPoint())
        line = self._line(a)
        if line is None:
            return
        txt = "".join(line[x].data or " " for x in range(self.cols))
        c = min(c, self.cols - 1)
        delim = " \t\"'()[]{}<>|,;`"
        s = c
        while s > 0 and txt[s - 1] not in delim:
            s -= 1
        en = c
        while en < len(txt) and txt[en] not in delim:
            en += 1
        self.sel_start, self.sel_end = (a, s), (a, en)
        self.update()
        t = self.selected_text()
        if t:
            QGuiApplication.clipboard().setText(t, QGuiApplication.clipboard().Mode.Selection)

    def _sel_range(self):
        if not self.sel_start or not self.sel_end:
            return None
        s, e = sorted([self.sel_start, self.sel_end])
        return s, e

    def selected_text(self):
        r = self._sel_range()
        if not r:
            return ""
        (a0, c0), (a1, c1) = r
        out = []
        for a in range(a0, a1 + 1):
            line = self._line(a)
            x0 = c0 if a == a0 else 0
            x1 = c1 if a == a1 else self.cols
            out.append(self._line_text(line, x0, x1))
        return "\n".join(out)

    def clear_selection(self):
        if self.sel_start:
            self.sel_start = self.sel_end = None
            self.update()

    def copy_selection(self):
        t = self.selected_text()
        if t:
            QGuiApplication.clipboard().setText(t)

    def paste(self):
        t = QGuiApplication.clipboard().text()
        if t:
            self._paste_text(t)

    def _paste_text(self, t):
        self._to_bottom()
        t = t.replace("\r\n", "\r").replace("\n", "\r")
        if BRACKETED_PASTE in self.screen.mode:
            t = "\x1b[200~" + t + "\x1b[201~"
        self.send_text(t)

    # ---------- отрисовка ----------
    def _toggle_blink(self):
        if self.hasFocus():
            self._blink_on = not self._blink_on
            r = QRectF(4 + self.screen.cursor.x * self.cw, 2 + self.screen.cursor.y * self.ch,
                       self.cw + 1, self.ch)
            self.update(r.toAlignedRect())

    def _color(self, name, fg, bold=False):
        if name == "default":
            return self._fg if fg else self._bg
        idx = NAMES.get(name)
        if idx is not None:
            if fg and bold and idx < 8:
                idx += 8
            return self._ansi[idx]
        c = self._colors.get(name)
        if c is None:
            c = QColor("#" + name) if len(name) == 6 else (self._fg if fg else self._bg)
            self._colors[name] = c
        return c

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        sc = self.screen
        sb = sc.scrollback
        cw, chh = self.cw, self.ch
        base = len(sb) - self.offset
        sel = self._sel_range()
        x_off, y_off = 4.0, 2.0
        for row in range(self.rows):
            a = base + row
            line = self._line(a)
            if line is None:
                continue
            y = y_off + row * chh
            x = 0
            run_start = 0
            run_text = []
            run_key = None

            def flush(end_x):
                nonlocal run_text
                if not run_text:
                    return
                fgc, bgc, bold, ital, und, strike = run_key
                rx = x_off + run_start * cw
                rw = (end_x - run_start) * cw
                if bgc is not None:
                    p.fillRect(QRectF(rx, y, rw + 0.6, chh), bgc)
                txt = "".join(run_text)
                if txt.strip():
                    p.setFont(self.font_b if bold else (self.font_i if ital else self.font_))
                    p.setPen(fgc)
                    p.drawText(QRectF(rx, y, rw + cw, chh), Qt.AlignLeft | Qt.AlignVCenter, txt)
                if und:
                    p.setPen(fgc)
                    p.drawLine(int(rx), int(y + chh - 2), int(rx + rw), int(y + chh - 2))
                if strike:
                    p.setPen(fgc)
                    p.drawLine(int(rx), int(y + chh / 2), int(rx + rw), int(y + chh / 2))
                run_text = []

            while x < self.cols:
                c = line[x]
                data = c.data
                fgc = self._color(c.fg, True, c.bold)
                bgn = c.bg
                bgc = None if bgn == "default" else self._color(bgn, False)
                if c.reverse:
                    fgc, bgc = (bgc or self._bg), fgc
                in_sel = False
                if sel:
                    (a0, c0), (a1, c1) = sel
                    in_sel = (a0 < a < a1) or (a0 == a1 == a and c0 <= x < c1) or \
                             (a0 == a != a1 and x >= c0) or (a1 == a != a0 and x < c1)
                if in_sel:
                    bgc = self._sel_col
                key = (fgc, bgc, c.bold, c.italics, c.underscore, c.strikethrough)
                special = not data or ord(data[0]) > 0x7E
                if key != run_key or special:
                    flush(x)
                    run_start = x
                    run_key = key
                if special:
                    if data:  # пустая data = правая половина широкого символа, уже нарисована
                        span = 2 if (x + 1 < self.cols and line[x + 1].data == "") else 1
                        rx = x_off + x * cw
                        if bgc is not None:
                            p.fillRect(QRectF(rx, y, span * cw + 0.6, chh), bgc)
                        p.setFont(self.font_b if c.bold else self.font_)
                        p.setPen(fgc)
                        p.drawText(QRectF(rx - cw, y, cw * (span + 2), chh), Qt.AlignHCenter | Qt.AlignVCenter, data)
                    run_text = []
                    run_start = x + 1
                    run_key = None
                else:
                    run_text.append(data)
                x += 1
            flush(self.cols)

        # курсор
        if self.offset == 0 and not sc.cursor.hidden and self.alive:
            cx = x_off + sc.cursor.x * cw
            cy = y_off + sc.cursor.y * chh
            r = QRectF(cx, cy, cw, chh)
            if self.hasFocus():
                if self._blink_on:
                    p.fillRect(r, self._cursor_col)
                    ch = sc.buffer[sc.cursor.y][sc.cursor.x].data
                    if ch.strip():
                        p.setPen(self._bg)
                        p.setFont(self.font_)
                        p.drawText(QRectF(cx - cw, cy, cw * 3, chh), Qt.AlignHCenter | Qt.AlignVCenter, ch)
            else:
                p.setPen(self._cursor_col)
                p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.offset:
            p.setPen(QColor(theme.MUTED))
            p.setFont(self.font_)
            p.drawText(self.rect().adjusted(0, 4, -14, 0), Qt.AlignRight | Qt.AlignTop,
                       f"↑ {self.offset} / {len(sb)}")
        p.end()

    def focusInEvent(self, e):
        self._blink_on = True
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self.update()
        super().focusOutEvent(e)


class Terminal(QWidget):
    """Терминал + вертикальный скроллбар."""
    titleChanged = Signal(str)

    def __init__(self, session, command=None, font_size=11, parent=None):
        super().__init__(parent)
        self.view = TerminalView(session, command, font_size, self)
        self.bar = QScrollBar(Qt.Vertical, self)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.bar)
        self.setStyleSheet(f"background: {theme.TERM_BG};")
        self.view.titleChanged.connect(self.titleChanged)
        self.view.scrolled.connect(self._sync_bar)
        self.bar.valueChanged.connect(self._bar_moved)
        self._syncing = False

    def _sync_bar(self):
        self._syncing = True
        n = len(self.view.screen.scrollback)
        self.bar.setRange(0, n)
        self.bar.setPageStep(self.view.rows)
        self.bar.setValue(n - self.view.offset)
        self._syncing = False

    def _bar_moved(self, v):
        if self._syncing:
            return
        n = len(self.view.screen.scrollback)
        self.view.offset = max(0, n - v)
        self.view.update()

    def start(self):
        self.view.start()

    def setFocus(self):
        self.view.setFocus()

    def send_text(self, t):
        self.view.send_text(t)

    def close_channel(self):
        self.view.close_channel()
