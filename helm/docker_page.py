"""Docker: контейнеры (+ live-статистика), образы, тома, сети, compose-проекты."""
from __future__ import annotations

import json
import shlex

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMenu, QTabWidget, QTreeWidget, QVBoxLayout, QWidget)

from . import theme
from .tasks import run_async
from .ui import SortItem, confirm, dot_icon, error, run_priv, show_output, toolbar_frame, tool_button

STATE_COL = {"running": theme.OK, "exited": theme.ERR, "paused": theme.WARN, "created": theme.MUTED,
             "restarting": theme.WARN, "dead": theme.ERR, "removing": theme.WARN}


def _json_lines(out: str):
    res = []
    out = out.strip()
    if out.startswith("["):
        try:
            return json.loads(out)
        except ValueError:
            pass
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                res.append(json.loads(line))
            except ValueError:
                pass
    return res


def _size_to_bytes(s: str) -> float:
    s = (s or "").strip().split(" ")[0]
    units = {"kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "B": 1,
             "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
    for u in sorted(units, key=len, reverse=True):
        if s.endswith(u):
            try:
                return float(s[: -len(u)]) * units[u]
            except ValueError:
                return 0
    try:
        return float(s)
    except ValueError:
        return 0


class ResourceTable(QWidget):
    """Таблица ресурсов docker с тулбаром и фильтром."""
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.tb, self.tl = toolbar_frame()
        lay.addWidget(self.tb)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(columns)
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.header().setStretchLastSection(True)
        lay.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фильтр…")
        self.filter.setFixedWidth(180)
        self.filter.textChanged.connect(self.apply_filter)

    def finish_toolbar(self):
        self.tl.addStretch()
        self.tl.addWidget(self.filter)

    def apply_filter(self, *_):
        t = self.filter.text().lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            txt = " ".join(it.text(c) for c in range(self.tree.columnCount())).lower()
            it.setHidden(bool(t) and t not in txt)

    def selected(self, col=0, role=Qt.UserRole + 1):
        return [it.data(col, role) for it in self.tree.selectedItems()]

    def keep_selection(self):
        return {it.data(0, Qt.UserRole + 1) for it in self.tree.selectedItems()}


class DockerPage(QWidget):
    openTerminal = Signal(str, str)   # команда, заголовок
    openText = Signal(str, str)       # заголовок, текст
    openFile = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.stats: dict = {}
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.msg = QLabel("")
        self.msg.setAlignment(Qt.AlignCenter)
        self.msg.setObjectName("muted")
        self.msg.hide()
        lay.addWidget(self.msg)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs)
        self._build_containers()
        self._build_images()
        self._build_volumes()
        self._build_networks()
        self._build_compose()
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.timer = QTimer(self, interval=5000, timeout=self._tick)
        self._first = True

    # ---------- общие ----------
    def showEvent(self, e):
        super().showEvent(e)
        if self._first:
            self._first = False
            run_async(self.s.detect_docker, on_done=self._detected, on_error=lambda e: self._detected("missing"))
        else:
            self.refresh()
        self.timer.start()

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def _detected(self, mode):
        if mode == "missing":
            self.tabs.hide()
            self.msg.setText("Docker не найден на сервере.\n\nУстановка: curl -fsSL https://get.docker.com | sh")
            self.msg.show()
            self.timer.stop()
            return
        self.refresh()

    def _tick(self):
        if self.tabs.currentIndex() == 0 and self.auto.isChecked() and self.s.docker_mode not in (None, "missing"):
            self.refresh()

    def refresh(self):
        if self.s.docker_mode in (None, "missing"):
            return
        [self.load_containers, self.load_images, self.load_volumes,
         self.load_networks, self.load_compose][self.tabs.currentIndex()]()

    def _docker(self, args, on_done, timeout=60):
        run_priv(self, self.s, self.s.docker, args, timeout, on_done=on_done)

    def _action(self, args, refresh, timeout=120, show=False):
        def done(r):
            rc, out, err = r
            if rc != 0:
                error(self, (err or out).strip() or f"Код {rc}")
            elif show and (out or err).strip():
                show_output(self, "docker " + args.split()[0], (out + err).strip())
            refresh()
        self._docker(args, done, timeout)

    # ---------- контейнеры ----------
    def _build_containers(self):
        t = self.ct = ResourceTable(["Имя", "Образ", "Состояние", "CPU", "Память", "Порты", "Создан", "ID"])
        tb = t.tl
        tb.addWidget(tool_button("refresh", "", "Обновить", self.load_containers))
        tb.addWidget(tool_button("play", "Старт", slot=lambda: self._c_act("start")))
        tb.addWidget(tool_button("stop", "Стоп", slot=lambda: self._c_act("stop")))
        tb.addWidget(tool_button("restart", "Рестарт", slot=lambda: self._c_act("restart")))
        tb.addWidget(tool_button("term", "Shell", slot=self._c_shell))
        tb.addWidget(tool_button("logs", "Логи", slot=self._c_logs))
        tb.addWidget(tool_button("info", "Inspect", slot=self._c_inspect))
        tb.addWidget(tool_button("trash", "Удалить", slot=self._c_rm, obj="danger"))
        self.show_all = QCheckBox("остановленные")
        self.show_all.setChecked(True)
        self.show_all.toggled.connect(lambda: self.load_containers())
        self.auto = QCheckBox("авто")
        self.auto.setChecked(True)
        t.finish_toolbar()
        tb.insertWidget(tb.count() - 1, self.show_all)
        tb.insertWidget(tb.count() - 1, self.auto)
        h = t.tree.header()
        for i, w in enumerate((220, 240, 170, 70, 150, 220, 130, 110)):
            h.resizeSection(i, w)
        t.tree.itemActivated.connect(lambda *_: self._c_shell())
        t.tree.customContextMenuRequested.connect(self._c_menu)
        self.tabs.addTab(t, "Контейнеры")

    def load_containers(self):
        if self._busy:
            return
        self._busy = True
        flag = "-a " if self.show_all.isChecked() else ""

        def done(r):
            self._busy = False
            rc, out, err = r
            if rc != 0:
                self.ct.status.setText(err.strip())
                return
            self._fill_containers(_json_lines(out))
            self._load_stats()

        def fail(e):
            self._busy = False
            self.ct.status.setText(str(e))
        run_priv(self, self.s, self.s.docker, f"ps {flag}--no-trunc --format '{{{{json .}}}}'", 30,
                 on_done=done, on_error=fail)

    def _load_stats(self):
        def done(r):
            rc, out, _ = r
            if rc == 0:
                self.stats = {d.get("Name"): d for d in _json_lines(out)}
                self._apply_stats()
        run_async(self.s.docker, "stats --no-stream --format '{{json .}}'", 30, on_done=done,
                  on_error=lambda e: None)

    def _fill_containers(self, rows):
        t = self.ct
        keep = t.keep_selection()
        t.tree.setSortingEnabled(False)
        t.tree.clear()
        running = 0
        for c in rows:
            name = c.get("Names", "").split(",")[0]
            state = (c.get("State") or ("running" if c.get("Status", "").startswith("Up") else "exited")).lower()
            running += state == "running"
            ports = c.get("Ports", "").replace("0.0.0.0:", "").replace(":::", "").replace(", ", " ")
            it = SortItem([name, c.get("Image", ""), c.get("Status", ""), "", "", ports,
                           c.get("RunningFor", ""), c.get("ID", "")[:12]])
            it.setData(0, Qt.UserRole + 1, name)
            it.setData(0, Qt.UserRole + 2, state)
            it.setData(0, Qt.UserRole + 5, 0 if state == "running" else 1)
            col = STATE_COL.get(state, theme.MUTED)
            it.setForeground(2, QColor(col))
            it.setIcon(0, dot_icon(col))
            it.setData(0, Qt.UserRole, name.lower())
            it.setToolTip(5, c.get("Ports", ""))
            t.tree.addTopLevelItem(it)
            if name in keep:
                it.setSelected(True)
        t.tree.setSortingEnabled(True)
        t.apply_filter()
        t.status.setText(f"Контейнеров: {len(rows)} · запущено: {running}")
        self._apply_stats()

    def _apply_stats(self):
        t = self.ct
        for i in range(t.tree.topLevelItemCount()):
            it = t.tree.topLevelItem(i)
            st = self.stats.get(it.data(0, Qt.UserRole + 1))
            if st and it.data(0, Qt.UserRole + 2) == "running":
                cpu = st.get("CPUPerc", "")
                it.setText(3, cpu)
                try:
                    it.setData(3, Qt.UserRole, float(cpu.rstrip("%")))
                except ValueError:
                    pass
                mem = st.get("MemUsage", "").split("/")[0].strip()
                it.setText(4, f"{mem} ({st.get('MemPerc', '')})")
                it.setData(4, Qt.UserRole, _size_to_bytes(mem))
            else:
                it.setText(3, "")
                it.setText(4, "")

    def _sel_c(self):
        names = self.ct.selected()
        if not names:
            error(self, "Выберите контейнер")
        return names

    def _c_act(self, act):
        names = self._sel_c()
        if names:
            self._action(f"{act} " + " ".join(map(shlex.quote, names)), self.load_containers)

    def _c_rm(self):
        names = self._sel_c()
        if names and confirm(self, "Удалить контейнеры (принудительно)?\n\n" + "\n".join(names)):
            self._action("rm -f " + " ".join(map(shlex.quote, names)), self.load_containers)

    def _c_shell(self):
        names = self._sel_c()
        if names:
            n = shlex.quote(names[0])
            self.openTerminal.emit(
                f"{self.s.docker_prefix} exec -it {n} sh -c 'command -v bash >/dev/null && exec bash || exec sh'",
                f"🐳 {names[0]}")

    def _c_logs(self):
        names = self._sel_c()
        if names:
            self.openTerminal.emit(f"{self.s.docker_prefix} logs -f --tail 500 {shlex.quote(names[0])}",
                                   f"📜 {names[0]}")

    def _c_inspect(self):
        names = self._sel_c()
        if names:
            self._docker("inspect " + shlex.quote(names[0]),
                         lambda r: self.openText.emit(f"inspect {names[0]}.json", r[1] or r[2]))

    def _c_menu(self, pos):
        if not self.ct.tree.itemAt(pos):
            return
        m = QMenu(self)
        m.addAction("▶  Запустить", lambda: self._c_act("start"))
        m.addAction("■  Остановить", lambda: self._c_act("stop"))
        m.addAction("⟳  Перезапустить", lambda: self._c_act("restart"))
        m.addAction("⏸  Пауза", lambda: self._c_act("pause"))
        m.addAction("⏵  Снять паузу", lambda: self._c_act("unpause"))
        m.addSeparator()
        m.addAction("Shell", self._c_shell)
        m.addAction("Логи (follow)", self._c_logs)
        m.addAction("Inspect", self._c_inspect)
        m.addAction("Процессы (top)", lambda: self._docker(
            "top " + shlex.quote(self.ct.selected()[0]),
            lambda r: show_output(self, "docker top", r[1] or r[2])))
        m.addAction("Переименовать…", self._c_rename)
        m.addSeparator()
        m.addAction("Удалить", self._c_rm)
        m.exec(self.ct.tree.viewport().mapToGlobal(pos))

    def _c_rename(self):
        names = self._sel_c()
        if not names:
            return
        new, ok = QInputDialog.getText(self, "Переименовать", "Новое имя:", text=names[0])
        if ok and new and new != names[0]:
            self._action(f"rename {shlex.quote(names[0])} {shlex.quote(new)}", self.load_containers)

    # ---------- образы ----------
    def _build_images(self):
        t = self.it = ResourceTable(["Репозиторий", "Тег", "ID", "Размер", "Создан"])
        tb = t.tl
        tb.addWidget(tool_button("refresh", "", "Обновить", self.load_images))
        tb.addWidget(tool_button("download", "Pull…", slot=self._i_pull))
        tb.addWidget(tool_button("play", "Запустить…", slot=self._i_run))
        tb.addWidget(tool_button("trash", "Удалить", slot=self._i_rm, obj="danger"))
        tb.addWidget(tool_button("bolt", "Prune", "Удалить неиспользуемые образы", self._i_prune))
        t.finish_toolbar()
        h = t.tree.header()
        for i, w in enumerate((320, 160, 140, 110)):
            h.resizeSection(i, w)
        self.tabs.addTab(t, "Образы")

    def load_images(self):
        def done(r):
            rc, out, err = r
            t = self.it
            t.tree.setSortingEnabled(False)
            t.tree.clear()
            rows = _json_lines(out)
            for d in rows:
                it = SortItem([d.get("Repository", ""), d.get("Tag", ""), d.get("ID", "")[:19].replace("sha256:", ""),
                               d.get("Size", ""), d.get("CreatedSince", "")])
                ref = d.get("ID") if d.get("Repository") == "<none>" else f"{d.get('Repository')}:{d.get('Tag')}"
                it.setData(0, Qt.UserRole + 1, ref)
                it.setData(3, Qt.UserRole, _size_to_bytes(d.get("Size", "")))
                t.tree.addTopLevelItem(it)
            t.tree.setSortingEnabled(True)
            t.apply_filter()
            t.status.setText(f"Образов: {len(rows)}" if rc == 0 else err)
        self._docker("images --format '{{json .}}'", done)

    def _i_pull(self):
        ref, ok = QInputDialog.getText(self, "docker pull", "Образ (например nginx:latest):")
        if ok and ref.strip():
            self.openTerminal.emit(f"{self.s.docker_prefix} pull {shlex.quote(ref.strip())}", f"pull {ref.strip()}")

    def _i_run(self):
        refs = self.it.selected()
        img = refs[0] if refs else ""
        args, ok = QInputDialog.getText(
            self, "docker run", "Аргументы (образ подставлен в конец):",
            text=f"-d --name app -p 8080:80 --restart unless-stopped")
        if ok:
            self._action(f"run {args} {shlex.quote(img)}", self.load_images, show=True)

    def _i_rm(self):
        refs = self.it.selected()
        if refs and confirm(self, "Удалить образы?\n\n" + "\n".join(refs)):
            self._action("rmi " + " ".join(map(shlex.quote, refs)), self.load_images)

    def _i_prune(self):
        if confirm(self, "Удалить все неиспользуемые образы (docker image prune -a)?"):
            self._action("image prune -af", self.load_images, timeout=600, show=True)

    # ---------- тома ----------
    def _build_volumes(self):
        t = self.vt = ResourceTable(["Имя", "Драйвер", "Mountpoint"])
        t.tl.addWidget(tool_button("refresh", "", "Обновить", self.load_volumes))
        t.tl.addWidget(tool_button("plus", "Создать…", slot=self._v_create))
        t.tl.addWidget(tool_button("info", "Inspect", slot=lambda: self._inspect("volume", self.vt)))
        t.tl.addWidget(tool_button("trash", "Удалить", slot=lambda: self._rm("volume", self.vt, self.load_volumes), obj="danger"))
        t.tl.addWidget(tool_button("bolt", "Prune", slot=lambda: self._prune("volume", self.load_volumes)))
        t.finish_toolbar()
        t.tree.header().resizeSection(0, 360)
        self.tabs.addTab(t, "Тома")

    def load_volumes(self):
        def done(r):
            self._fill_simple(self.vt, _json_lines(r[1]), ("Name", "Driver", "Mountpoint"), "Name", "Томов")
        self._docker("volume ls --format '{{json .}}'", done)

    def _v_create(self):
        name, ok = QInputDialog.getText(self, "Новый том", "Имя тома:")
        if ok and name:
            self._action("volume create " + shlex.quote(name), self.load_volumes)

    # ---------- сети ----------
    def _build_networks(self):
        t = self.nt = ResourceTable(["Имя", "Драйвер", "Scope", "ID"])
        t.tl.addWidget(tool_button("refresh", "", "Обновить", self.load_networks))
        t.tl.addWidget(tool_button("plus", "Создать…", slot=self._n_create))
        t.tl.addWidget(tool_button("info", "Inspect", slot=lambda: self._inspect("network", self.nt)))
        t.tl.addWidget(tool_button("trash", "Удалить", slot=lambda: self._rm("network", self.nt, self.load_networks), obj="danger"))
        t.finish_toolbar()
        t.tree.header().resizeSection(0, 300)
        self.tabs.addTab(t, "Сети")

    def load_networks(self):
        def done(r):
            self._fill_simple(self.nt, _json_lines(r[1]), ("Name", "Driver", "Scope", "ID"), "Name", "Сетей")
        self._docker("network ls --format '{{json .}}'", done)

    def _n_create(self):
        name, ok = QInputDialog.getText(self, "Новая сеть", "Имя сети:")
        if ok and name:
            self._action("network create " + shlex.quote(name), self.load_networks)

    # ---------- compose ----------
    def _build_compose(self):
        t = self.cp = ResourceTable(["Проект", "Статус", "Файлы"])
        tb = t.tl
        tb.addWidget(tool_button("refresh", "", "Обновить", self.load_compose))
        tb.addWidget(tool_button("play", "Up -d", slot=lambda: self._cp_act("up -d")))
        tb.addWidget(tool_button("stop", "Down", slot=lambda: self._cp_act("down", True)))
        tb.addWidget(tool_button("restart", "Restart", slot=lambda: self._cp_act("restart")))
        tb.addWidget(tool_button("download", "Pull", slot=lambda: self._cp_act("pull")))
        tb.addWidget(tool_button("logs", "Логи", slot=self._cp_logs))
        tb.addWidget(tool_button("edit", "Редактировать", slot=self._cp_edit))
        t.finish_toolbar()
        t.tree.header().resizeSection(0, 220)
        t.tree.header().resizeSection(1, 200)
        self.tabs.addTab(t, "Compose")

    def load_compose(self):
        def done(r):
            rc, out, err = r
            rows = _json_lines(out)
            if rc != 0:
                self.cp.tree.clear()
                self.cp.status.setText("docker compose недоступен: " + (err.strip() or out.strip()))
                return
            self._fill_simple(self.cp, rows, ("Name", "Status", "ConfigFiles"), "Name", "Проектов")
            for i in range(self.cp.tree.topLevelItemCount()):
                it = self.cp.tree.topLevelItem(i)
                it.setData(0, Qt.UserRole + 2, rows[i].get("ConfigFiles", "") if i < len(rows) else "")
                st = it.text(1).lower()
                it.setForeground(1, QColor(theme.OK if "running" in st else theme.ERR))
        self._docker("compose ls -a --format json", done)

    def _cp_sel(self):
        its = self.cp.tree.selectedItems()
        if not its:
            error(self, "Выберите проект")
            return None
        it = its[0]
        files = (it.data(0, Qt.UserRole + 2) or "").split(",")
        fargs = " ".join(f"-f {shlex.quote(f.strip())}" for f in files if f.strip())
        return it.data(0, Qt.UserRole + 1), fargs, files

    def _cp_act(self, act, ask=False):
        sel = self._cp_sel()
        if not sel:
            return
        name, fargs, _ = sel
        if ask and not confirm(self, f"docker compose {act} для «{name}»?"):
            return
        self.openTerminal.emit(f"{self.s.docker_prefix} compose -p {shlex.quote(name)} {fargs} {act}; "
                               f"echo; echo '[готово — Ctrl+D для выхода]'; exec $SHELL -l",
                               f"compose {act.split()[0]} {name}")

    def _cp_logs(self):
        sel = self._cp_sel()
        if sel:
            name, fargs, _ = sel
            self.openTerminal.emit(f"{self.s.docker_prefix} compose -p {shlex.quote(name)} {fargs} logs -f --tail 300",
                                   f"📜 {name}")

    def _cp_edit(self):
        sel = self._cp_sel()
        if sel:
            for f in sel[2]:
                if f.strip():
                    self.openFile.emit(f.strip())

    # ---------- хелперы ----------
    def _fill_simple(self, t: ResourceTable, rows, keys, id_key, label):
        t.tree.setSortingEnabled(False)
        t.tree.clear()
        for d in rows:
            it = SortItem([str(d.get(k, "")) for k in keys])
            it.setData(0, Qt.UserRole + 1, d.get(id_key))
            t.tree.addTopLevelItem(it)
        t.tree.setSortingEnabled(True)
        t.apply_filter()
        t.status.setText(f"{label}: {len(rows)}")

    def _inspect(self, kind, t):
        ids = t.selected()
        if ids:
            self._docker(f"{kind} inspect " + shlex.quote(ids[0]),
                         lambda r: self.openText.emit(f"{kind} {ids[0]}.json", r[1] or r[2]))

    def _rm(self, kind, t, refresh):
        ids = t.selected()
        if ids and confirm(self, f"Удалить ({kind})?\n\n" + "\n".join(ids)):
            self._action(f"{kind} rm " + " ".join(map(shlex.quote, ids)), refresh)

    def _prune(self, kind, refresh):
        if confirm(self, f"docker {kind} prune — удалить все неиспользуемые?"):
            self._action(f"{kind} prune -f", refresh, show=True)
