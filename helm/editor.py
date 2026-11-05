"""Редактор удалённых файлов: вкладки, номера строк, подсветка, поиск/замена, sudo-сохранение."""
from __future__ import annotations

import posixpath
import re

from PySide6.QtCore import QRect, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QKeySequence, QPainter, QShortcut, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor, QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from . import theme
from .ssh import human_error
from .tasks import run_async
from .ui import error, run_priv, toolbar_frame, tool_button


def _fmt(color, bold=False, italic=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


KW = _fmt("#c58cff", bold=True)
STR = _fmt("#3ddc97")
NUM = _fmt("#ffb547")
COM = _fmt("#5c6680", italic=True)
KEY = _fmt("#6d9dff")
VAR = _fmt("#38d9c0")
SEC = _fmt("#ff7d94", bold=True)
FUNC = _fmt("#93b6ff")

LANG_KW = {
    "python": "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
    "sh": "if then else elif fi case esac for while until do done in function return local export readonly unset shift exit break continue source alias echo cd set trap eval exec",
    "js": "break case catch class const continue debugger default delete do else export extends false finally for function if import in instanceof let new null return super switch this throw true try typeof var void while with yield async await of undefined",
    "go": "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
    "c": "auto break case char const continue default do double else enum extern float for goto if int long register return short signed sizeof static struct switch typedef union unsigned void volatile while include define ifdef ifndef endif",
    "sql": "select from where insert into values update set delete create table drop alter index join left right inner outer on group by order having limit and or not null primary key foreign references as distinct union all",
    "docker": "FROM RUN CMD LABEL EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG ONBUILD STOPSIGNAL HEALTHCHECK SHELL AS",
    "nginx": "server location listen server_name root index return rewrite proxy_pass include upstream http events try_files add_header ssl_certificate ssl_certificate_key error_page access_log error_log",
}


def detect_lang(path: str, text: str) -> str:
    name = posixpath.basename(path).lower()
    ext = posixpath.splitext(name)[1]
    first = text[:200].split("\n", 1)[0]
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return "docker"
    if ext == ".py" or "python" in first:
        return "python"
    if ext in (".sh", ".bash", ".zsh") or name in (".bashrc", ".profile", ".zshrc", ".bash_profile") or first.startswith("#!") and "sh" in first:
        return "sh"
    if ext in (".js", ".ts", ".mjs", ".jsx", ".tsx"):
        return "js"
    if ext == ".go":
        return "go"
    if ext in (".c", ".h", ".cpp", ".hpp", ".cc", ".rs", ".java"):
        return "c"
    if ext == ".sql":
        return "sql"
    if ext in (".yml", ".yaml"):
        return "yaml"
    if ext == ".json":
        return "json"
    if ext in (".ini", ".conf", ".cfg", ".toml", ".service", ".env", ".timer", ".socket") or name.endswith("rc"):
        if "nginx" in path or "server {" in text[:4000]:
            return "nginx"
        return "ini"
    if ext in (".md",):
        return "md"
    if ext in (".html", ".xml", ".htm", ".vue", ".svg"):
        return "html"
    return "plain"


class Highlighter(QSyntaxHighlighter):
    def __init__(self, doc, lang):
        super().__init__(doc)
        self.lang = lang
        self.rules: list[tuple[QRegularExpression, QTextCharFormat, int]] = []
        R = lambda p, f, g=0, flags=QRegularExpression.NoPatternOption: self.rules.append(
            (QRegularExpression(p, flags), f, g))
        kws = LANG_KW.get(lang)
        if kws:
            flags = QRegularExpression.CaseInsensitiveOption if lang in ("sql", "docker") else QRegularExpression.NoPatternOption
            R(r"\b(" + "|".join(map(re.escape, kws.split())) + r")\b", KW, 0, flags)
        if lang in ("python", "js", "go", "c"):
            R(r"\b([A-Za-z_]\w*)\s*(?=\()", FUNC, 1)
        if lang in ("sh", "docker"):
            R(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", VAR)
        if lang == "yaml":
            R(r"^\s*-?\s*([\w.\-/\"']+)\s*:", KEY, 1)
            R(r"\b(true|false|yes|no|null|on|off)\b", KW)
            R(r"\$\{?[A-Za-z_]\w*\}?", VAR)
        if lang == "json":
            R(r"(\"(?:[^\"\\]|\\.)*\")\s*:", KEY, 1)
            R(r"\b(true|false|null)\b", KW)
        if lang in ("ini", "nginx"):
            R(r"^\s*\[[^\]]+\]", SEC)
            R(r"^\s*([\w.\-]+)\s*(?==|\s)", KEY, 1)
            R(r"\$\w+", VAR)
        if lang == "md":
            R(r"^#{1,6}\s.*$", SEC)
            R(r"`[^`]+`", STR)
            R(r"\*\*[^*]+\*\*", KW)
        if lang == "html":
            R(r"</?[\w\-:]+", KW)
            R(r"\s([\w\-:]+)=", KEY, 1)
        if lang != "plain":
            R(r"\b-?(0x[0-9a-fA-F]+|\d+(\.\d+)?)\b", NUM)
            R(r"\"(?:[^\"\\]|\\.)*\"", STR)
            R(r"'(?:[^'\\]|\\.)*'", STR)
        if lang in ("python", "sh", "yaml", "ini", "docker", "nginx"):
            R(r"(^|\s)#.*$", COM)
        if lang in ("js", "go", "c"):
            R(r"//.*$", COM)
        if lang == "sql":
            R(r"--.*$", COM)
        if lang == "html":
            R(r"<!--.*-->", COM)
        self.block_comment = lang in ("js", "go", "c")

    def highlightBlock(self, text):
        for rx, fmt, g in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(g), m.capturedLength(g), fmt)
        if self.block_comment:
            in_c = self.previousBlockState() == 1
            seg, pos = 0, 0
            self.setCurrentBlockState(0)
            while True:
                if in_c:
                    end = text.find("*/", pos)
                    if end < 0:
                        self.setFormat(seg, len(text) - seg, COM)
                        self.setCurrentBlockState(1)
                        return
                    self.setFormat(seg, end + 2 - seg, COM)
                    pos, in_c = end + 2, False
                else:
                    s = text.find("/*", pos)
                    if s < 0:
                        return
                    seg, pos, in_c = s, s + 2, True


class _Gutter(QWidget):
    def __init__(self, ed):
        super().__init__(ed)
        self.ed = ed

    def sizeHint(self):
        return QSize(self.ed.gutter_width(), 0)

    def paintEvent(self, e):
        self.ed.paint_gutter(e)


class CodeEditor(QPlainTextEdit):
    def __init__(self, font_size=11, parent=None):
        super().__init__(parent)
        self.setFont(theme.mono_font(font_size))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {theme.TERM_BG}; border: none; border-radius: 0;"
                           f" padding: 4px; selection-background-color: #3a3480; }}")
        self.gutter = _Gutter(self)
        self.comment = "#"
        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._hl_line)
        self._update_width()
        self._hl_line()

    def gutter_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * max(3, digits)

    def _update_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, e):
        p = QPainter(self.gutter)
        p.fillRect(e.rect(), QColor(theme.BG2))
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        p.setFont(self.font())
        while block.isValid() and top <= e.rect().bottom():
            if block.isVisible() and bottom >= e.rect().top():
                p.setPen(QColor(theme.TEXT if n == cur else "#4a5268"))
                p.drawText(0, top, self.gutter.width() - 8, self.fontMetrics().height(),
                           Qt.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            n += 1

    def _hl_line(self):
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor("#141a26"))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ControlModifier:
            line = self.textCursor().block().text()
            indent = re.match(r"\s*", line).group(0)
            if line.rstrip().endswith((":", "{", "(", "[")):
                indent += "    " if "\t" not in indent else "\t"
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        if e.key() == Qt.Key_Tab and not self.textCursor().hasSelection():
            self.insertPlainText("    ")
            return
        if e.key() == Qt.Key_Slash and e.modifiers() & Qt.ControlModifier:
            self.toggle_comment()
            return
        super().keyPressEvent(e)

    def toggle_comment(self):
        c = self.textCursor()
        start, end = sorted((c.selectionStart(), c.selectionEnd()))
        doc = self.document()
        b0 = doc.findBlock(start).blockNumber()
        b1 = doc.findBlock(end).blockNumber()
        blocks = [doc.findBlockByNumber(i) for i in range(b0, b1 + 1)]
        all_commented = all(b.text().lstrip().startswith(self.comment) for b in blocks if b.text().strip())
        c.beginEditBlock()
        for b in blocks:
            t = b.text()
            if not t.strip():
                continue
            cur = QTextCursor(b)
            ws = len(t) - len(t.lstrip())
            cur.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, ws)
            if all_commented:
                n = len(self.comment) + (1 if t.lstrip()[len(self.comment):].startswith(" ") else 0)
                cur.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                cur.removeSelectedText()
            else:
                cur.insertText(self.comment + " ")
        c.endEditBlock()


class FindBar(QWidget):
    def __init__(self, ed: CodeEditor, parent=None):
        super().__init__(parent)
        self.ed = ed
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Найти…")
        self.repl = QLineEdit()
        self.repl.setPlaceholderText("Заменить на…")
        self.info = QLabel("")
        self.info.setObjectName("muted")
        lay.addWidget(self.find, 2)
        lay.addWidget(QPushButton("↑", clicked=lambda: self.go(back=True)))
        lay.addWidget(QPushButton("↓", clicked=self.go))
        lay.addWidget(self.repl, 2)
        lay.addWidget(QPushButton("Заменить", clicked=self.replace_one))
        lay.addWidget(QPushButton("Все", clicked=self.replace_all))
        lay.addWidget(self.info)
        lay.addWidget(QPushButton("✕", clicked=self.hide, objectName="flat"))
        self.find.returnPressed.connect(self.go)
        self.find.textChanged.connect(lambda: self.go(incremental=True))

    def go(self, back=False, incremental=False):
        t = self.find.text()
        if not t:
            return
        if incremental:
            c = self.ed.textCursor()
            c.setPosition(c.selectionStart())
            self.ed.setTextCursor(c)
        flags = QTextDocument.FindBackward if back else QTextDocument.FindFlag(0)
        if not self.ed.find(t, flags):
            c = self.ed.textCursor()
            c.movePosition(QTextCursor.End if back else QTextCursor.Start)
            self.ed.setTextCursor(c)
            found = self.ed.find(t, flags)
            self.info.setText("" if found else "не найдено")
        else:
            self.info.setText("")

    def replace_one(self):
        c = self.ed.textCursor()
        if c.hasSelection() and c.selectedText() == self.find.text():
            c.insertText(self.repl.text())
        self.go()

    def replace_all(self):
        t, r = self.find.text(), self.repl.text()
        if not t:
            return
        text = self.ed.toPlainText()
        n = text.count(t)
        if n:
            c = self.ed.textCursor()
            c.beginEditBlock()
            c.select(QTextCursor.Document)
            c.insertText(text.replace(t, r))
            c.endEditBlock()
        self.info.setText(f"заменено: {n}")


class EditorDoc(QWidget):
    modifiedChanged = Signal(bool)

    def __init__(self, session, path, settings, readonly_text: str | None = None, title=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.path = path
        self.title = title or posixpath.basename(path)
        self.encoding = "utf-8"
        self.crlf = False
        self.virtual = readonly_text is not None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        tb, tl = toolbar_frame()
        self.lbl = QLabel(path if not self.virtual else self.title)
        self.lbl.setObjectName("muted")
        tl.addWidget(self.lbl, 1)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        tl.addWidget(self.meta)
        if not self.virtual:
            tl.addWidget(tool_button("refresh", "Перезагрузить", slot=self.load))
            self.save_btn = tool_button("save", "Сохранить  Ctrl+S", slot=self.save, obj="primary")
            tl.addWidget(self.save_btn)
        lay.addWidget(tb)
        self.ed = CodeEditor(settings.get("editor_font_size", 11))
        lay.addWidget(self.ed, 1)
        self.findbar = FindBar(self.ed)
        self.findbar.hide()
        lay.addWidget(self.findbar)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+F"), self, self.show_find, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Escape"), self, self.findbar.hide, context=Qt.WidgetWithChildrenShortcut)
        self.ed.document().modificationChanged.connect(self.modifiedChanged)
        self.hl = None
        if self.virtual:
            self._set_text(readonly_text)
            self.ed.setReadOnly(True)
        else:
            self.ed.setReadOnly(True)
            self.ed.setPlainText("Загрузка…")
            self.load()

    def show_find(self):
        self.findbar.show()
        c = self.ed.textCursor()
        if c.hasSelection():
            self.findbar.find.setText(c.selectedText())
        self.findbar.find.setFocus()
        self.findbar.find.selectAll()

    def _set_text(self, text):
        lang = detect_lang(self.path or self.title, text)
        self.ed.comment = {"js": "//", "go": "//", "c": "//", "sql": "--"}.get(lang, "#")
        self.hl = Highlighter(self.ed.document(), lang)
        self.ed.setPlainText(text)
        self.ed.document().setModified(False)
        lines = text.count("\n") + 1
        self.meta.setText(f"{lang} · {self.encoding} · {'CRLF' if self.crlf else 'LF'} · {lines} строк")

    def load(self):
        if self.ed.document().isModified() and not self.virtual and self.hl is not None:
            if QMessageBox.question(self, "Перезагрузить", "Отменить несохранённые изменения?") != QMessageBox.Yes:
                return
        run_async(self.session.read_file, self.path, on_done=self._loaded,
                  on_error=lambda e: self.ed.setPlainText(f"Ошибка: {human_error(e)}"))

    def _loaded(self, data: bytes):
        if b"\x00" in data[:8192]:
            self.ed.setPlainText("Похоже, это бинарный файл — редактирование отключено.\n"
                                 "Скачайте его через панель файлов.")
            return
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                text = data.decode(enc)
                self.encoding = enc
                break
            except UnicodeDecodeError:
                continue
        self.crlf = "\r\n" in text
        if self.crlf:
            text = text.replace("\r\n", "\n")
        self._set_text(text)
        self.ed.setReadOnly(False)
        self.ed.setFocus()

    def save(self, use_sudo=False):
        if self.virtual or self.ed.isReadOnly():
            return
        text = self.ed.toPlainText()
        if self.crlf:
            text = text.replace("\n", "\r\n")
        data = text.encode(self.encoding, "replace")
        self.save_btn.setEnabled(False)
        self.meta.setText("Сохранение…")

        def ok(_):
            self.save_btn.setEnabled(True)
            self.ed.document().setModified(False)
            self.meta.setText(f"Сохранено{' (sudo)' if use_sudo else ''} · {len(data)} байт")

        def fail(e):
            self.save_btn.setEnabled(True)
            self.meta.setText("Не сохранено")
            denied = isinstance(e, PermissionError) or getattr(e, "errno", None) == 13 or "denied" in str(e).lower()
            if denied and not use_sudo:
                if QMessageBox.question(self, "Нет прав", "Нет прав на запись. Сохранить через sudo?") == QMessageBox.Yes:
                    self.save(use_sudo=True)
                return
            error(self, e)
        run_priv(self, self.session, self.session.write_file, self.path, data, use_sudo,
                 on_done=ok, on_error=fail)

    @property
    def modified(self):
        return not self.virtual and self.ed.document().isModified()


class EditorPage(QWidget):
    def __init__(self, session, settings, parent=None):
        super().__init__(parent)
        self.session = session
        self.settings = settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        lay.addWidget(self.tabs)
        self.empty = QLabel("Откройте файл двойным кликом во вкладке «Файлы»\n"
                            "или нажмите «Открыть по пути».")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("muted")
        corner = tool_button("files", "Открыть по пути", slot=self.open_by_path)
        self.tabs.setCornerWidget(corner)
        self.tabs.addTab(self.empty, "Пусто")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)

    def _drop_empty(self):
        i = self.tabs.indexOf(self.empty)
        if i >= 0:
            self.tabs.removeTab(i)

    def open_by_path(self):
        from PySide6.QtWidgets import QInputDialog
        p, ok = QInputDialog.getText(self, "Открыть файл", "Путь на сервере:", text=self.session.home + "/")
        if ok and p:
            self.open(p.strip())

    def open(self, path):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, EditorDoc) and w.path == path and not w.virtual:
                self.tabs.setCurrentIndex(i)
                return
        self._drop_empty()
        doc = EditorDoc(self.session, path, self.settings)
        i = self.tabs.addTab(doc, doc.title)
        self.tabs.setTabToolTip(i, path)
        doc.modifiedChanged.connect(lambda m, d=doc: self._mark(d, m))
        self.tabs.setCurrentIndex(i)

    def open_text(self, title, text):
        self._drop_empty()
        doc = EditorDoc(self.session, title, self.settings, readonly_text=text, title=title)
        i = self.tabs.addTab(doc, "👁 " + title)
        self.tabs.setCurrentIndex(i)

    def _mark(self, doc, modified):
        i = self.tabs.indexOf(doc)
        if i >= 0:
            self.tabs.setTabText(i, ("● " if modified else "") + doc.title)

    def close_tab(self, i):
        w = self.tabs.widget(i)
        if isinstance(w, EditorDoc) and w.modified:
            r = QMessageBox.question(self, "Несохранённые изменения", f"Сохранить {w.title}?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Save:
                w.save()
                return
        self.tabs.removeTab(i)
        if w is not self.empty:
            w.deleteLater()
        if self.tabs.count() == 0:
            self.tabs.addTab(self.empty, "Пусто")

    def has_unsaved(self):
        return any(isinstance(self.tabs.widget(i), EditorDoc) and self.tabs.widget(i).modified
                   for i in range(self.tabs.count()))
