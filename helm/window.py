"""Главное окно: вкладки сессий, навигация по разделам, логика подключения."""
from __future__ import annotations

import shlex

import paramiko
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSplitter,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, config, theme
from .config import HostProfile
from .docker_page import DockerPage
from .editor import EditorPage
from .files import FilesPage
from .hosts_page import HostsPage
from .ssh import Session, UnknownHostKey, human_error, trust_host_key
from .system_pages import OverviewPage, ProcessesPage, ServicesPage
from .tasks import run_async
from .terminal import Terminal
from .ui import Banner, ask_password, confirm, error, ic, icon_font_family, toolbar_frame, tool_button


class TerminalPage(QWidget):
    def __init__(self, session, settings, parent=None):
        super().__init__(parent)
        self.s = session
        self.settings = settings
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        corner = QWidget()
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 6, 0)
        cl.addWidget(tool_button("plus", "Терминал", "Новый терминал (Ctrl+T)", lambda: self.new()))
        self.snip_btn = tool_button("bolt", "Сниппеты", "Показать/скрыть сниппеты", self._toggle_snips)
        cl.addWidget(self.snip_btn)
        self.tabs.setCornerWidget(corner)
        split.addWidget(self.tabs)

        # панель сниппетов
        self.snips = QWidget()
        sl = QVBoxLayout(self.snips)
        sl.setContentsMargins(8, 8, 8, 8)
        t = QLabel("СНИППЕТЫ")
        t.setObjectName("cardtitle")
        sl.addWidget(t)
        hint = QLabel("Двойной клик — выполнить\nв активном терминале")
        hint.setObjectName("muted")
        sl.addWidget(hint)
        self.snip_list = QListWidget()
        self.snip_list.itemActivated.connect(self._run_snip)
        sl.addWidget(self.snip_list, 1)
        row = QHBoxLayout()
        row.addWidget(QPushButton("Добавить", clicked=self._add_snip))
        row.addWidget(QPushButton("Удалить", clicked=self._del_snip))
        sl.addLayout(row)
        split.addWidget(self.snips)
        split.setStretchFactor(0, 1)
        split.setSizes([1000, 240])
        lay.addWidget(split)
        self._fill_snips()
        self.snips.setVisible(settings.get("snippets_visible", False))

    def _toggle_snips(self):
        v = not self.snips.isVisible()
        self.snips.setVisible(v)
        self.settings["snippets_visible"] = v
        config.save_settings(self.settings)

    def _fill_snips(self):
        self.snip_list.clear()
        for s in self.settings.get("snippets", []):
            it = QListWidgetItem(f"{s['name']}\n{s['cmd']}")
            it.setToolTip(s["cmd"])
            it.setData(Qt.UserRole, s["cmd"])
            self.snip_list.addItem(it)

    def _run_snip(self, it):
        t = self.current()
        if not t:
            t = self.new()
        t.send_text(it.data(Qt.UserRole) + "\r")
        t.setFocus()

    def _add_snip(self):
        cmd, ok = QInputDialog.getText(self, "Сниппет", "Команда:")
        if not ok or not cmd:
            return
        name, ok = QInputDialog.getText(self, "Сниппет", "Название:", text=cmd[:30])
        if ok:
            self.settings.setdefault("snippets", []).append({"name": name or cmd, "cmd": cmd})
            config.save_settings(self.settings)
            self._fill_snips()

    def _del_snip(self):
        r = self.snip_list.currentRow()
        if r >= 0:
            del self.settings["snippets"][r]
            config.save_settings(self.settings)
            self._fill_snips()

    def current(self) -> Terminal | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, Terminal) else None

    def new(self, command: str | None = None, title: str | None = None, cwd: str | None = None) -> Terminal:
        t = Terminal(self.s, command, self.settings.get("term_font_size", 11))
        n = self.tabs.count() + 1
        i = self.tabs.addTab(t, title or f"Терминал {n}")
        self.tabs.setCurrentIndex(i)
        if not title:
            t.titleChanged.connect(lambda txt, w=t: self._retitle(w, txt))
        QApplication.processEvents()
        try:
            t.start()
        except Exception as e:  # noqa: BLE001
            error(self, e)
            return t
        if cwd:
            t.send_text(f"cd {shlex.quote(cwd)} && clear\r")
        t.setFocus()
        return t

    def _retitle(self, w, txt):
        i = self.tabs.indexOf(w)
        if i >= 0 and txt:
            self.tabs.setTabText(i, txt[:32])
            self.tabs.setTabToolTip(i, txt)

    def close_tab(self, i):
        w = self.tabs.widget(i)
        if isinstance(w, Terminal):
            w.close_channel()
        self.tabs.removeTab(i)
        w.deleteLater()

    def close_all(self):
        while self.tabs.count():
            self.close_tab(0)

    def showEvent(self, e):
        super().showEvent(e)
        if self.tabs.count() == 0:
            QTimer.singleShot(0, self.new)
        elif self.current():
            self.current().setFocus()


class SessionView(QWidget):
    PAGES = [("dash", "Обзор"), ("files", "Файлы"), ("term", "Терминал"), ("edit", "Редактор"),
             ("docker", "Docker"), ("proc", "Процессы"), ("svc", "Сервисы")]

    def __init__(self, session: Session, settings, main, parent=None):
        super().__init__(parent)
        self.s = session
        self.main = main
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.banner = Banner("Соединение потеряно", "Переподключить", self.reconnect)
        self.banner.hide()
        root.addWidget(self.banner)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(170)
        fam = icon_font_family()
        if fam:
            f = QFont(self.nav.font())
            f.setFamilies([QApplication.font().family(), fam])
            f.setPointSize(10)
            self.nav.setFont(f)
        for i, (icon, label) in enumerate(self.PAGES):
            g = ic(icon)
            it = QListWidgetItem(f"{g}   {label}" if g else label)
            it.setToolTip(f"{label}  (Ctrl+{i + 1})")
            self.nav.addItem(it)
        body.addWidget(self.nav)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.overview = OverviewPage(session)
        self.files = FilesPage(session, settings)
        self.terms = TerminalPage(session, settings)
        self.editor = EditorPage(session, settings)
        self.docker = DockerPage(session)
        self.procs = ProcessesPage(session)
        self.services = ServicesPage(session)
        for w in (self.overview, self.files, self.terms, self.editor, self.docker, self.procs, self.services):
            self.stack.addWidget(w)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        self.files.openFile.connect(self.open_file)
        self.files.terminalHere.connect(lambda p: self.open_terminal(None, None, cwd=p))
        self.docker.openTerminal.connect(self.open_terminal)
        self.docker.openText.connect(self.open_text)
        self.docker.openFile.connect(self.open_file)
        self.services.openTerminal.connect(self.open_terminal)
        self.services.openFile.connect(self.open_file)

        self.watch = QTimer(self, interval=4000, timeout=self._check)
        self.watch.start()

    def go(self, idx):
        self.nav.setCurrentRow(idx)

    def open_file(self, path):
        self.editor.open(path)
        self.go(3)

    def open_text(self, title, text):
        self.editor.open_text(title, text)
        self.go(3)

    def open_terminal(self, command=None, title=None, cwd=None):
        self.go(2)
        self.terms.new(command, title, cwd)

    def _check(self):
        dead = not self.s.alive
        self.banner.setVisible(dead)
        self.main.set_session_state(self, not dead)

    def reconnect(self):
        self.banner.label.setText("Переподключение…")

        def ok(_):
            self.banner.label.setText("Соединение потеряно")
            self.banner.hide()
            self.main.set_session_state(self, True)
            self.files.remote.refresh()

        def fail(e):
            self.banner.label.setText("Не удалось переподключиться: " + human_error(e))
        run_async(self.s.connect, on_done=ok, on_error=fail)

    def shutdown(self):
        self.watch.stop()
        self.terms.close_all()
        self.s.close()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — SSH Commander")
        self.resize(1480, 920)
        self.settings = config.load_settings()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("sessions")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_session)
        self.hosts = HostsPage()
        self.hosts.connectRequested.connect(self.connect_profile)
        g = ic("server")
        self.tabs.addTab(self.hosts, f"{g}  Хосты" if g else "Хосты")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)
        fam = icon_font_family()
        if fam:
            f = QFont(self.tabs.tabBar().font())
            f.setFamilies([QApplication.font().family(), fam])
            self.tabs.tabBar().setFont(f)
        self.setCentralWidget(self.tabs)
        self.status = self.statusBar()
        self.status_lbl = QLabel(f"{APP_NAME} {__version__}")
        self.status.addWidget(self.status_lbl, 1)
        self._connecting: set = set()

        QShortcut(QKeySequence("Ctrl+T"), self, self._new_term)
        QShortcut(QKeySequence("Ctrl+N"), self, lambda: (self.tabs.setCurrentIndex(0), self.hosts.add()))
        QShortcut(QKeySequence("Ctrl+Tab"), self, lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count()))
        for i in range(7):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, lambda i=i: self._go(i))

    # ---------------- сессии ----------------
    def _cur_session(self) -> SessionView | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, SessionView) else None

    def _new_term(self):
        sv = self._cur_session()
        if sv:
            sv.open_terminal()

    def _go(self, i):
        sv = self._cur_session()
        if sv:
            sv.go(i)

    def set_session_state(self, sv, alive):
        i = self.tabs.indexOf(sv)
        if i >= 0:
            self.tabs.tabBar().setTabTextColor(i, Qt.GlobalColor.white if alive else Qt.GlobalColor.red)

    def connect_profile(self, profile: HostProfile, password=None, passphrase=None):
        key = (profile.host, profile.port, profile.user)
        if key in self._connecting:
            return
        if password is None:
            password = config.get_secret(profile.id)
        if profile.auth == "password" and not password:
            password = ask_password(self, "Пароль", f"Пароль для {profile.address}:")
            if password is None:
                return
        self._connecting.add(key)
        self.status_lbl.setText(f"Подключение к {profile.address}…")
        QApplication.setOverrideCursor(Qt.BusyCursor)
        sess = Session(profile, password, passphrase)

        def done(_):
            self._connecting.discard(key)
            QApplication.restoreOverrideCursor()
            self.status_lbl.setText(f"Подключено: {profile.address}  ·  {sess.uname}")
            sv = SessionView(sess, self.settings, self)
            i = self.tabs.addTab(sv, profile.name)
            self.tabs.setTabToolTip(i, profile.address)
            self.tabs.setCurrentIndex(i)
            if profile.source == "quick" and confirm(self, f"Сохранить {profile.address} в список хостов?", "Сохранить"):
                profile.source = "saved"
                self.hosts.add(profile)
                if password:
                    pass

        def fail(e):
            self._connecting.discard(key)
            QApplication.restoreOverrideCursor()
            self.status_lbl.setText(f"Ошибка подключения к {profile.address}")
            if isinstance(e, UnknownHostKey):
                msg = (f"Сервер {e.hostname} ещё не известен.\n\n"
                       f"Тип ключа: {e.key.get_name()}\nОтпечаток: {e.fingerprint}\n\n"
                       "Доверять этому ключу и сохранить его?")
                if QMessageBox.question(self, "Новый ключ хоста", msg) == QMessageBox.Yes:
                    trust_host_key(e.hostname, e.key)
                    self.connect_profile(profile, password, passphrase)
                return
            if isinstance(e, paramiko.PasswordRequiredException):
                pp = ask_password(self, "Ключ зашифрован", f"Пароль от ключа {profile.key_path or ''}:")
                if pp is not None:
                    self.connect_profile(profile, password, pp)
                return
            if isinstance(e, paramiko.AuthenticationException):
                pw = ask_password(self, "Аутентификация",
                                  f"Не удалось войти как {profile.user}.\nПароль для {profile.address}:")
                if pw:
                    if profile.source == "saved" and confirm(self, "Запомнить этот пароль?"):
                        config.set_secret(profile.id, pw)
                    self.connect_profile(profile, pw, passphrase)
                return
            error(self, f"{profile.address}\n\n{human_error(e)}", "Не удалось подключиться")

        run_async(sess.connect, on_done=done, on_error=fail)

    def close_session(self, i):
        w = self.tabs.widget(i)
        if not isinstance(w, SessionView):
            return
        if w.editor.has_unsaved() and not confirm(self, "Есть несохранённые файлы. Закрыть сессию?"):
            return
        self.tabs.removeTab(i)
        w.shutdown()
        w.deleteLater()

    def closeEvent(self, e):
        sessions = [self.tabs.widget(i) for i in range(self.tabs.count())
                    if isinstance(self.tabs.widget(i), SessionView)]
        if any(s.editor.has_unsaved() for s in sessions):
            if not confirm(self, "Есть несохранённые файлы. Выйти?"):
                e.ignore()
                return
        for s in sessions:
            s.shutdown()
        e.accept()
