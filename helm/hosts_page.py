"""Стартовый экран: список хостов, редактирование профилей, быстрое подключение."""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from . import config, theme
from .config import HostProfile
from .ui import card, confirm, ic, tool_button


class HostDialog(QDialog):
    def __init__(self, profile: HostProfile | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Хост" if profile else "Новый хост")
        self.setMinimumWidth(480)
        p = profile or HostProfile(name="", host="", user=os.environ.get("USER", "root"))
        self.profile = p
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        t = QLabel("Параметры подключения")
        t.setObjectName("h2")
        lay.addWidget(t)
        form = QFormLayout()
        form.setSpacing(10)
        self.name = QLineEdit(p.name)
        self.name.setPlaceholderText("prod-web-1")
        self.host = QLineEdit(p.host)
        self.host.setPlaceholderText("192.168.1.10 или example.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(p.port)
        self.user = QLineEdit(p.user)
        self.auth = QComboBox()
        self.auth.addItem("Авто (агент, ключи ~/.ssh, пароль)", "auto")
        self.auth.addItem("Пароль", "password")
        self.auth.addItem("Ключ", "key")
        self.auth.setCurrentIndex(max(0, self.auth.findData(p.auth)))
        keyrow = QHBoxLayout()
        self.key = QLineEdit(p.key_path)
        self.key.setPlaceholderText("~/.ssh/id_ed25519")
        b = QPushButton("…")
        b.clicked.connect(self._browse)
        keyrow.addWidget(self.key)
        keyrow.addWidget(b)
        self.password = QLineEdit(config.get_secret(p.id) or "")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("необязательно")
        self.save_pw = QCheckBox("Запомнить пароль (файл ~/.config/helm/secrets.json, 0600)")
        self.save_pw.setChecked(bool(self.password.text()))
        self.group = QLineEdit(p.group)
        self.group.setPlaceholderText("prod / staging / home")
        form.addRow("Название", self.name)
        form.addRow("Хост", self.host)
        form.addRow("Порт", self.port)
        form.addRow("Пользователь", self.user)
        form.addRow("Аутентификация", self.auth)
        form.addRow("Ключ", keyrow)
        form.addRow("Пароль", self.password)
        form.addRow("", self.save_pw)
        form.addRow("Группа", self.group)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setObjectName("primary")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Приватный ключ", os.path.expanduser("~/.ssh"))
        if f:
            self.key.setText(f)
            self.auth.setCurrentIndex(self.auth.findData("key"))

    def _accept(self):
        if not self.host.text().strip():
            self.host.setFocus()
            return
        p = self.profile
        p.host = self.host.text().strip()
        p.name = self.name.text().strip() or p.host
        p.port = self.port.value()
        p.user = self.user.text().strip() or "root"
        p.auth = self.auth.currentData()
        p.key_path = self.key.text().strip()
        p.group = self.group.text().strip()
        if p.source == "sshconfig":
            p.source = "saved"
            p.id = __import__("uuid").uuid4().hex
        config.set_secret(p.id, self.password.text() if self.save_pw.isChecked() else None)
        self.accept()


def parse_quick(text: str) -> HostProfile | None:
    """user@host:port → профиль."""
    t = text.strip()
    if t.startswith("ssh "):
        t = t[4:].strip()
    if not t:
        return None
    port = 22
    parts = t.split()
    if "-p" in parts:
        i = parts.index("-p")
        try:
            port = int(parts[i + 1])
        except (IndexError, ValueError):
            pass
        del parts[i:i + 2]
    t = parts[0] if parts else t
    user = os.environ.get("USER", "root")
    if "@" in t:
        user, t = t.rsplit("@", 1)
    if t.count(":") == 1:
        t, ps = t.split(":")
        try:
            port = int(ps)
        except ValueError:
            pass
    return HostProfile(name=t, host=t, port=port, user=user, source="quick")


class HostsPage(QWidget):
    connectRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hosts: list[HostProfile] = []
        root = QHBoxLayout(self)
        root.setContentsMargins(40, 30, 40, 30)
        root.setSpacing(30)

        # левая колонка
        left = QVBoxLayout()
        left.setSpacing(12)
        logo = QLabel("◆ HELM")
        logo.setObjectName("logo")
        left.addWidget(logo)
        h = QLabel("Серверы")
        h.setObjectName("h1")
        left.addWidget(h)
        sub = QLabel("SSH · SFTP · Docker · systemd — в одном окне")
        sub.setObjectName("muted")
        left.addWidget(sub)
        left.addSpacing(8)

        qc_f, qc = card("Быстрое подключение")
        row = QHBoxLayout()
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("user@host:port   или   ssh -p 2222 root@1.2.3.4")
        self.quick.returnPressed.connect(self._quick)
        row.addWidget(self.quick, 1)
        row.addWidget(tool_button("bolt", "Подключиться", slot=self._quick, obj="primary"))
        qc.addLayout(row)
        left.addWidget(qc_f)

        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск хостов…")
        self.search.textChanged.connect(self.reload_list)
        tools.addWidget(self.search, 1)
        tools.addWidget(tool_button("plus", "Новый хост", slot=self.add, obj="primary"))
        left.addLayout(tools)

        self.list = QListWidget()
        self.list.setObjectName("hosts")
        self.list.setSpacing(2)
        self.list.setIconSize(QSize(0, 0))
        self.list.itemActivated.connect(lambda it: self._connect_item(it))
        self.list.currentItemChanged.connect(self._show_details)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        left.addWidget(self.list, 1)
        root.addLayout(left, 3)

        # правая колонка — карточка выбранного хоста
        right = QVBoxLayout()
        self.det_f, dl = card("Выбранный хост")
        self.det_name = QLabel("—")
        self.det_name.setObjectName("h1")
        self.det_addr = QLabel("")
        self.det_addr.setObjectName("muted")
        self.det_auth = QLabel("")
        self.det_auth.setObjectName("muted")
        dl.addWidget(self.det_name)
        dl.addWidget(self.det_addr)
        dl.addWidget(self.det_auth)
        dl.addSpacing(10)
        self.btn_connect = tool_button("plug", "Подключиться", slot=self._connect_current, obj="primary")
        self.btn_connect.setMinimumHeight(42)
        dl.addWidget(self.btn_connect)
        br = QHBoxLayout()
        br.addWidget(tool_button("edit", "Изменить", slot=self.edit))
        br.addWidget(tool_button("copy", "Дублировать", slot=self.duplicate))
        br.addWidget(tool_button("trash", "Удалить", slot=self.delete, obj="danger"))
        dl.addLayout(br)
        right.addWidget(self.det_f)

        tips_f, tl = card("Горячие клавиши")
        tips = QLabel(
            "<table cellspacing=4>"
            "<tr><td><b>Ctrl+T</b></td><td>новый терминал</td></tr>"
            "<tr><td><b>Ctrl+1…7</b></td><td>разделы сессии</td></tr>"
            "<tr><td><b>Ctrl+Shift+C / V</b></td><td>копировать / вставить в терминале</td></tr>"
            "<tr><td><b>Ctrl+колесо</b></td><td>масштаб терминала</td></tr>"
            "<tr><td><b>Shift+PgUp/PgDn</b></td><td>прокрутка истории</td></tr>"
            "<tr><td><b>Ctrl+S / Ctrl+F</b></td><td>сохранить / найти в редакторе</td></tr>"
            "<tr><td><b>Ctrl+/</b></td><td>закомментировать строки</td></tr>"
            "<tr><td><b>F2 / Del / F5</b></td><td>переименовать / удалить / обновить файлы</td></tr>"
            "<tr><td><b>Drag &amp; drop</b></td><td>файлы между панелями и из системы</td></tr>"
            "</table>")
        tips.setTextFormat(Qt.RichText)
        tips.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        tl.addWidget(tips)
        right.addWidget(tips_f)
        right.addStretch()
        root.addLayout(right, 2)
        self.reload()

    # ------------------------------------------------------------------
    def reload(self):
        saved = config.load_hosts()
        names = {(h.host, h.user, h.port) for h in saved}
        cfg = [h for h in config.ssh_config_hosts() if (h.host, h.user, h.port) not in names]
        self.hosts = saved + cfg
        self.reload_list()

    def reload_list(self):
        cur = self.current()
        q = self.search.text().lower().strip()
        self.list.clear()
        groups: dict[str, list[HostProfile]] = {}
        for h in self.hosts:
            if q and q not in f"{h.name} {h.host} {h.user} {h.group}".lower():
                continue
            groups.setdefault(h.group or "", []).append(h)
        for g in sorted(groups, key=lambda x: (x == "~/.ssh/config", x)):
            if g:
                gi = QListWidgetItem(g.upper())
                gi.setFlags(Qt.NoItemFlags)
                gi.setForeground(Qt.GlobalColor.gray)
                gi.setSizeHint(QSize(0, 30))
                self.list.addItem(gi)
            for h in sorted(groups[g], key=lambda x: x.name.lower()):
                icon = ic("server")
                it = QListWidgetItem(f"{icon + '   ' if icon else ''}{h.name}\n{h.address}")
                it.setData(Qt.UserRole, h)
                it.setSizeHint(QSize(0, 62))
                self.list.addItem(it)
                if cur and cur.id == h.id:
                    self.list.setCurrentItem(it)
        if not self.list.currentItem():
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole):
                    self.list.setCurrentRow(i)
                    break
        if not self.hosts:
            self.det_name.setText("Нет хостов")
            self.det_addr.setText("Добавьте сервер кнопкой «Новый хост» или используйте быстрое подключение.")

    def current(self) -> HostProfile | None:
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _show_details(self, *_):
        h = self.current()
        if not h:
            return
        self.det_name.setText(h.name)
        self.det_addr.setText(h.address)
        auth = {"auto": "авто (агент / ключи / пароль)", "password": "пароль", "key": "ключ " + h.key_path}[h.auth] \
            if h.auth in ("auto", "password", "key") else h.auth
        saved = " · пароль сохранён" if config.get_secret(h.id) else ""
        src = " · из ~/.ssh/config" if h.source == "sshconfig" else ""
        self.det_auth.setText(f"Аутентификация: {auth}{saved}{src}")

    def _connect_item(self, it):
        h = it.data(Qt.UserRole)
        if h:
            self.connectRequested.emit(h)

    def _connect_current(self):
        h = self.current()
        if h:
            self.connectRequested.emit(h)

    def _quick(self):
        p = parse_quick(self.quick.text())
        if p:
            self.connectRequested.emit(p)

    def _save(self):
        config.save_hosts(self.hosts)
        self.reload()

    def add(self, preset: HostProfile | None = None):
        d = HostDialog(preset, self)
        if preset:
            d.setWindowTitle("Сохранить хост")
        if d.exec():
            d.profile.source = "saved"
            self.hosts.append(d.profile)
            self._save()

    def edit(self):
        h = self.current()
        if not h:
            return
        was_cfg = h.source == "sshconfig"
        d = HostDialog(h, self)
        if d.exec():
            if was_cfg:
                pass  # уже в self.hosts, источник сменился на saved
            self._save()

    def duplicate(self):
        h = self.current()
        if not h:
            return
        import copy
        import uuid
        n = copy.copy(h)
        n.id = uuid.uuid4().hex
        n.name = h.name + " (копия)"
        n.source = "saved"
        self.hosts.append(n)
        self._save()

    def delete(self):
        h = self.current()
        if not h:
            return
        if h.source == "sshconfig":
            from .ui import error
            error(self, "Этот хост из ~/.ssh/config — удалите его там.")
            return
        if confirm(self, f"Удалить хост «{h.name}»?"):
            self.hosts = [x for x in self.hosts if x.id != h.id]
            config.set_secret(h.id, None)
            self._save()

    def _menu(self, pos):
        from PySide6.QtWidgets import QMenu
        if not self.list.itemAt(pos) or not self.list.itemAt(pos).data(Qt.UserRole):
            return
        m = QMenu(self)
        m.addAction("Подключиться", self._connect_current)
        m.addAction("Изменить", self.edit)
        m.addAction("Дублировать", self.duplicate)
        m.addSeparator()
        m.addAction("Удалить", self.delete)
        m.exec(self.list.viewport().mapToGlobal(pos))
