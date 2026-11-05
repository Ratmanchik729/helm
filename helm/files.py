"""Двухпанельный файловый менеджер: локальная ФС ⇄ сервер (SFTP), drag&drop, очередь передач."""
from __future__ import annotations

import itertools
import json
import os
import posixpath
import queue
import shlex
import stat
import threading
import time

from PySide6.QtCore import QDir, QMimeData, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileIconProvider, QFileSystemModel,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
                               QMessageBox, QProgressBar, QSplitter, QTreeView, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import QMimeDatabase

from . import theme
from .ssh import human_error
from .tasks import run_async
from .ui import (SortItem, confirm, error, human_size, human_time, icon_button, run_priv,
                 toolbar_frame, tool_button)

REMOTE_MIME = "application/x-helm-remote"
_icons = QFileIconProvider()
_mimedb = QMimeDatabase()
_icon_cache: dict = {}


def remote_icon(name: str, is_dir: bool, is_link: bool) -> QIcon:
    if is_dir:
        key = "<dir-link>" if is_link else "<dir>"
        if key not in _icon_cache:
            ic = QIcon.fromTheme("folder-link" if is_link else "folder")
            _icon_cache[key] = ic if not ic.isNull() else _icons.icon(QFileIconProvider.Folder)
        return _icon_cache[key]
    ext = os.path.splitext(name)[1].lower() or name
    if ext not in _icon_cache:
        mt = _mimedb.mimeTypeForFile(name, QMimeDatabase.MatchExtension)
        ic = QIcon.fromTheme(mt.iconName())
        if ic.isNull():
            ic = QIcon.fromTheme(mt.genericIconName())
        _icon_cache[ext] = ic if not ic.isNull() else _icons.icon(QFileIconProvider.File)
    return _icon_cache[ext]


# ============================ передачи ============================

class Cancelled(Exception):
    pass


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind: str, src: str, dst_dir: str):
        self.id = next(self._ids)
        self.kind = kind  # up | down
        self.src = src
        self.dst_dir = dst_dir
        self.name = os.path.basename(src.rstrip("/")) or src
        self.total = 0
        self.done = 0
        self.cancel = False
        self.state = "queued"


class TransferSignals(QObject):
    added = Signal(object)
    progress = Signal(int, int, int, str)
    finished = Signal(object, bool, str)


class TransferManager:
    def __init__(self, session, workers: int = 2):
        self.session = session
        self.sig = TransferSignals()
        self.q: queue.Queue = queue.Queue()
        self.jobs: dict[int, Job] = {}
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()

    def upload(self, local_path: str, remote_dir: str):
        self._add(Job("up", local_path, remote_dir))

    def download(self, remote_path: str, local_dir: str):
        self._add(Job("down", remote_path, local_dir))

    def _add(self, job: Job):
        self.jobs[job.id] = job
        self.sig.added.emit(job)
        self.q.put(job)

    def cancel(self, job_id: int):
        j = self.jobs.get(job_id)
        if j:
            j.cancel = True

    def _worker(self):
        sftp = None
        while True:
            job = self.q.get()
            if job is None:
                return
            if job.cancel:
                self.sig.finished.emit(job, False, "Отменено")
                continue
            try:
                if sftp is None or sftp.sock.closed:
                    sftp = self.session.open_sftp()
                job.state = "running"
                if job.kind == "up":
                    self._upload(sftp, job)
                else:
                    self._download(sftp, job)
                job.state = "done"
                self.sig.finished.emit(job, True, "Готово")
            except Cancelled:
                job.state = "cancelled"
                self.sig.finished.emit(job, False, "Отменено")
            except Exception as e:  # noqa: BLE001
                job.state = "error"
                try:
                    sftp.close()
                except Exception:
                    pass
                sftp = None
                self.sig.finished.emit(job, False, human_error(e))

    def _cb_factory(self, job: Job, base_ref: list, cur_name: list):
        last = [0.0]

        def cb(done, _total):
            if job.cancel:
                raise Cancelled()
            job.done = base_ref[0] + done
            now = time.monotonic()
            if now - last[0] > 0.12:
                last[0] = now
                self.sig.progress.emit(job.id, job.done, job.total, cur_name[0])
        return cb

    def _upload(self, sftp, job: Job):
        src = job.src.rstrip("/") or job.src
        dst = posixpath.join(job.dst_dir, os.path.basename(src))
        files, dirs = [], []
        if os.path.isdir(src):
            for root, dnames, fnames in os.walk(src):
                rel = os.path.relpath(root, src)
                rdir = dst if rel == "." else posixpath.join(dst, *rel.split(os.sep))
                dirs.append(rdir)
                for f in fnames:
                    lp = os.path.join(root, f)
                    try:
                        files.append((lp, posixpath.join(rdir, f), os.path.getsize(lp)))
                    except OSError:
                        pass
        else:
            files.append((src, dst, os.path.getsize(src)))
        job.total = sum(f[2] for f in files) or 1
        for d in dirs:
            try:
                sftp.mkdir(d)
            except OSError:
                pass
        base, cur = [0], [""]
        cb = self._cb_factory(job, base, cur)
        for lp, rp, sz in files:
            cur[0] = os.path.basename(lp)
            sftp.put(lp, rp, callback=cb)
            try:
                sftp.chmod(rp, stat.S_IMODE(os.stat(lp).st_mode))
            except Exception:
                pass
            base[0] += sz
        job.done = job.total
        self.sig.progress.emit(job.id, job.done, job.total, "")

    def _download(self, sftp, job: Job):
        src = job.src.rstrip("/") or "/"
        dst = os.path.join(job.dst_dir, posixpath.basename(src) or "root")
        files, dirs = [], []
        st0 = sftp.stat(src)
        if stat.S_ISDIR(st0.st_mode):
            stack = [(src, dst)]
            while stack:
                if job.cancel:
                    raise Cancelled()
                rdir, ldir = stack.pop()
                dirs.append(ldir)
                for a in sftp.listdir_attr(rdir):
                    rp = posixpath.join(rdir, a.filename)
                    lp = os.path.join(ldir, a.filename)
                    if stat.S_ISDIR(a.st_mode):
                        stack.append((rp, lp))
                    elif stat.S_ISREG(a.st_mode):
                        files.append((rp, lp, a.st_size or 0, a.st_mode))
                    elif stat.S_ISLNK(a.st_mode):
                        try:
                            t = sftp.stat(rp)
                            if stat.S_ISREG(t.st_mode):
                                files.append((rp, lp, t.st_size or 0, t.st_mode))
                        except OSError:
                            pass
        else:
            files.append((src, dst, st0.st_size or 0, st0.st_mode))
        job.total = sum(f[2] for f in files) or 1
        for d in dirs:
            os.makedirs(d, exist_ok=True)
        base, cur = [0], [""]
        cb = self._cb_factory(job, base, cur)
        for rp, lp, sz, mode in files:
            cur[0] = posixpath.basename(rp)
            sftp.get(rp, lp, callback=cb, prefetch=True)
            try:
                os.chmod(lp, stat.S_IMODE(mode))
            except Exception:
                pass
            base[0] += sz
        job.done = job.total
        self.sig.progress.emit(job.id, job.done, job.total, "")


class TransfersPanel(QWidget):
    def __init__(self, manager: TransferManager, parent=None):
        super().__init__(parent)
        self.m = manager
        self.items: dict[int, tuple[QTreeWidgetItem, QProgressBar]] = {}
        self.speed: dict[int, tuple[float, int]] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        tb, tl = toolbar_frame()
        self.title = QLabel("Передачи")
        self.title.setObjectName("cardtitle")
        tl.addWidget(self.title)
        tl.addStretch()
        tl.addWidget(tool_button("kill", "Отменить", slot=self._cancel_sel))
        tl.addWidget(tool_button("trash", "Очистить завершённые", slot=self._clear))
        lay.addWidget(tb)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "Файл", "Куда", "Прогресс", "Скорость", "Статус"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        h = self.tree.header()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.resizeSection(0, 34)
        h.resizeSection(2, 260)
        h.resizeSection(3, 200)
        h.resizeSection(4, 100)
        lay.addWidget(self.tree)
        manager.sig.added.connect(self._added)
        manager.sig.progress.connect(self._progress)
        manager.sig.finished.connect(self._finished)

    def _added(self, job: Job):
        it = QTreeWidgetItem(["⬆" if job.kind == "up" else "⬇", job.name, job.dst_dir, "", "", "В очереди"])
        it.setData(0, Qt.UserRole, job.id)
        it.setForeground(0, Qt.GlobalColor.cyan if job.kind == "up" else Qt.GlobalColor.green)
        self.tree.insertTopLevelItem(0, it)
        bar = QProgressBar()
        bar.setObjectName("textbar")
        bar.setRange(0, 1000)
        bar.setFixedHeight(16)
        self.tree.setItemWidget(it, 3, bar)
        self.items[job.id] = (it, bar)
        self.speed[job.id] = (time.monotonic(), 0)
        self._update_title()

    def _progress(self, jid, done, total, cur):
        if jid not in self.items:
            return
        it, bar = self.items[jid]
        bar.setValue(int(done * 1000 / max(total, 1)))
        bar.setFormat(f"{human_size(done)} / {human_size(total)}")
        t0, d0 = self.speed[jid]
        now = time.monotonic()
        if now - t0 > 0.5:
            sp = (done - d0) / (now - t0)
            it.setText(4, human_size(sp) + "/с")
            self.speed[jid] = (now, done)
        it.setText(5, cur or "…")

    def _finished(self, job, ok, msg):
        if job.id not in self.items:
            return
        it, bar = self.items[job.id]
        if ok:
            bar.setValue(1000)
            bar.setFormat(human_size(job.total))
        it.setText(4, "")
        it.setText(5, msg)
        it.setForeground(5, Qt.GlobalColor.green if ok else Qt.GlobalColor.red)
        if not ok:
            it.setToolTip(5, msg)
        self._update_title()

    def _update_title(self):
        active = sum(1 for j in self.m.jobs.values() if j.state in ("queued", "running"))
        self.title.setText(f"ПЕРЕДАЧИ · активно: {active}" if active else "ПЕРЕДАЧИ")

    def _cancel_sel(self):
        for it in self.tree.selectedItems():
            self.m.cancel(it.data(0, Qt.UserRole))

    def _clear(self):
        for jid, (it, _) in list(self.items.items()):
            j = self.m.jobs.get(jid)
            if j and j.state not in ("queued", "running"):
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))
                del self.items[jid]
                self.m.jobs.pop(jid, None)


# ============================ удалённая панель ============================

class RemoteTree(QTreeWidget):
    filesDropped = Signal(list, str)       # локальные пути, удалённая папка
    remoteMoved = Signal(list, str)        # удалённые пути, папка назначения

    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

    def mimeData(self, items):
        md = QMimeData()
        paths = [it.data(0, Qt.UserRole + 1) for it in items if it.data(0, Qt.UserRole + 1)]
        md.setData(REMOTE_MIME, json.dumps(paths).encode())
        return md

    def mimeTypes(self):
        return [REMOTE_MIME, "text/uri-list"]

    def _target_dir(self, pos):
        it = self.itemAt(pos)
        if it and it.data(0, Qt.UserRole + 2):
            return it.data(0, Qt.UserRole + 1)
        return self.pane.cwd

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasFormat(REMOTE_MIME):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            e.acceptProposedAction()
        elif md.hasFormat(REMOTE_MIME):
            it = self.itemAt(e.position().toPoint())
            if it and it.data(0, Qt.UserRole + 2) and not it.isSelected():
                e.setDropAction(Qt.MoveAction)
                e.accept()
            else:
                e.ignore()
        else:
            e.ignore()

    def dropEvent(self, e):
        md = e.mimeData()
        target = self._target_dir(e.position().toPoint())
        if md.hasFormat(REMOTE_MIME):
            paths = json.loads(bytes(md.data(REMOTE_MIME)).decode())
            if target != self.pane.cwd:
                self.remoteMoved.emit(paths, target)
            e.accept()
            return
        if md.hasUrls():
            local = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if local:
                self.filesDropped.emit(local, target)
            e.acceptProposedAction()


class RemotePane(QWidget):
    openFile = Signal(str)
    downloadRequested = Signal(list)
    terminalHere = Signal(str)

    def __init__(self, session, settings, parent=None):
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.cwd = session.home or "/"
        self.history: list[str] = []
        self.entries = []
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tb, tl = toolbar_frame()
        hdr = QLabel(f"СЕРВЕР · {session.profile.host}")
        hdr.setObjectName("cardtitle")
        tl.addWidget(icon_button("back", "Назад", self.back, "←"))
        tl.addWidget(icon_button("up", "Вверх", self.up, "↑"))
        tl.addWidget(icon_button("home", "Домой", lambda: self.cd(self.session.home), "⌂"))
        tl.addWidget(icon_button("refresh", "Обновить (F5)", self.refresh, "⟳"))
        self.path = QLineEdit(self.cwd)
        self.path.returnPressed.connect(lambda: self.cd(self.path.text().strip()))
        tl.addWidget(self.path, 1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фильтр…")
        self.filter.setFixedWidth(150)
        self.filter.textChanged.connect(self._apply_filter)
        tl.addWidget(self.filter)
        tl.addWidget(icon_button("plus", "Новая папка", self.mkdir, "+"))
        lay.addWidget(tb)

        self.tree = RemoteTree(self)
        self.tree.setHeaderLabels(["Имя", "Размер", "Изменён", "Права", "Владелец"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemActivated.connect(self._activated)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in ((1, 90), (2, 140), (3, 100), (4, 120)):
            h.resizeSection(i, w)
        lay.addWidget(self.tree)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)

    # -------- навигация --------
    def cd(self, path: str, push: bool = True):
        if not path:
            return
        if not path.startswith("/"):
            path = posixpath.join(self.cwd, path)
        path = posixpath.normpath(path)
        if push and path != self.cwd:
            self.history.append(self.cwd)
        self.cwd = path
        self.path.setText(path)
        self.refresh()

    def up(self):
        if self.cwd != "/":
            self.cd(posixpath.dirname(self.cwd))

    def back(self):
        if self.history:
            self.cd(self.history.pop(), push=False)

    def refresh(self):
        if self.session.sftp is None:
            self.status.setText("SFTP недоступен на этом сервере")
            return
        self.status.setText("Загрузка…")
        path = self.cwd
        run_async(self.session.listdir, path,
                  on_done=lambda r: self._loaded(path, r),
                  on_error=lambda e: self._failed(path, e))

    def _failed(self, path, e):
        self.status.setText(f"{path}: {human_error(e)}")
        if self.history and path == self.cwd:
            QMessageBox.warning(self, "Файлы", f"{path}\n{human_error(e)}")
            self.cwd = self.history.pop()
            self.path.setText(self.cwd)

    def _loaded(self, path, entries):
        if path != self.cwd:
            return
        sel = {it.text(0) for it in self.tree.selectedItems()}
        self.entries = entries
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        show_hidden = self.settings.get("show_hidden", False)
        nd = nf = 0
        total = 0
        for e in entries:
            if not show_hidden and e["name"].startswith("."):
                continue
            name = e["name"] + (f"  →  {e['target']}" if e["is_link"] else "")
            it = SortItem([name, "" if e["is_dir"] else human_size(e["size"]),
                           human_time(e["mtime"]), stat.filemode(e["mode"]), e["owner"]])
            it.setIcon(0, remote_icon(e["name"], e["is_dir"], e["is_link"]))
            it.setData(0, Qt.UserRole, e["name"].lower())
            it.setData(0, Qt.UserRole + 1, posixpath.join(path, e["name"]))
            it.setData(0, Qt.UserRole + 2, e["is_dir"])
            it.setData(0, Qt.UserRole + 3, e["size"])
            it.setData(0, Qt.UserRole + 5, 0 if e["is_dir"] else 1)
            it.setData(1, Qt.UserRole, -1 if e["is_dir"] else e["size"])
            it.setData(2, Qt.UserRole, e["mtime"])
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            it.setForeground(3, Qt.GlobalColor.gray)
            it.setForeground(4, Qt.GlobalColor.gray)
            if e["is_link"]:
                it.setForeground(0, Qt.GlobalColor.cyan)
            self.tree.addTopLevelItem(it)
            if e["name"] in sel:
                it.setSelected(True)
            if e["is_dir"]:
                nd += 1
            else:
                nf += 1
                total += e["size"]
        self.tree.setSortingEnabled(True)
        self._apply_filter(self.filter.text())
        self.status.setText(f"Папок: {nd}   Файлов: {nf}   Размер: {human_size(total)}")

    def _apply_filter(self, text):
        t = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setHidden(bool(t) and t not in it.text(0).lower())

    def _activated(self, it, _col):
        p = it.data(0, Qt.UserRole + 1)
        if it.data(0, Qt.UserRole + 2):
            self.cd(p)
        else:
            size = it.data(0, Qt.UserRole + 3) or 0
            if size > 8 * 1024 * 1024:
                if confirm(self, f"Файл большой ({human_size(size)}). Скачать вместо открытия?"):
                    self.downloadRequested.emit([p])
                return
            self.openFile.emit(p)

    def selected_paths(self) -> list[str]:
        return [it.data(0, Qt.UserRole + 1) for it in self.tree.selectedItems()]

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F5:
            self.refresh()
        elif e.key() == Qt.Key_Delete:
            self.delete()
        elif e.key() == Qt.Key_Backspace:
            self.up()
        elif e.key() == Qt.Key_F2:
            self.rename()
        else:
            super().keyPressEvent(e)

    # -------- операции --------
    def _run(self, cmd, msg=None, sudo=False):
        def done(r):
            rc, out, err = r
            if rc != 0:
                error(self, (err or out).strip() or f"Код {rc}")
            self.refresh()
        run_priv(self, self.session, self.session.run, cmd, 120, sudo, on_done=done)

    def mkdir(self):
        name, ok = QInputDialog.getText(self, "Новая папка", "Имя:")
        if ok and name:
            self._run(f"mkdir -p -- {shlex.quote(posixpath.join(self.cwd, name))}")

    def new_file(self):
        name, ok = QInputDialog.getText(self, "Новый файл", "Имя:")
        if ok and name:
            p = posixpath.join(self.cwd, name)
            self._run(f"touch -- {shlex.quote(p)}")

    def rename(self):
        paths = self.selected_paths()
        if len(paths) != 1:
            return
        old = paths[0]
        name, ok = QInputDialog.getText(self, "Переименовать", "Новое имя:", text=posixpath.basename(old))
        if ok and name and name != posixpath.basename(old):
            new = posixpath.join(posixpath.dirname(old), name)
            self._run(f"mv -- {shlex.quote(old)} {shlex.quote(new)}")

    def move(self, paths, target):
        q = " ".join(shlex.quote(p) for p in paths)
        if confirm(self, f"Переместить {len(paths)} объект(ов) в {target}?"):
            self._run(f"mv -- {q} {shlex.quote(target)}/")

    def delete(self, sudo=False):
        paths = self.selected_paths()
        if not paths:
            return
        names = "\n".join(posixpath.basename(p) for p in paths[:12])
        more = f"\n… и ещё {len(paths) - 12}" if len(paths) > 12 else ""
        if not confirm(self, f"Удалить безвозвратно{' (sudo)' if sudo else ''}?\n\n{names}{more}"):
            return
        q = " ".join(shlex.quote(p) for p in paths)
        self._run(f"rm -rf -- {q}", sudo=sudo)

    def chmod(self):
        paths = self.selected_paths()
        if not paths:
            return
        mode, ok = QInputDialog.getText(self, "Права доступа", "chmod (например 755, u+x, -R 644):", text="755")
        if ok and mode:
            q = " ".join(shlex.quote(p) for p in paths)
            self._run(f"chmod {mode} -- {q}")

    def chown(self):
        paths = self.selected_paths()
        if not paths:
            return
        own, ok = QInputDialog.getText(self, "Владелец", "chown (user:group, можно с -R):",
                                       text=f"{self.session.profile.user}:{self.session.profile.user}")
        if ok and own:
            q = " ".join(shlex.quote(p) for p in paths)
            self._run(f"chown {own} -- {q}", sudo=True)

    def archive(self):
        paths = self.selected_paths()
        if not paths:
            return
        default = (posixpath.basename(paths[0]) if len(paths) == 1 else "archive") + ".tar.gz"
        name, ok = QInputDialog.getText(self, "Архивировать", "Имя архива:", text=default)
        if ok and name:
            q = " ".join(shlex.quote(posixpath.relpath(p, self.cwd)) for p in paths)
            self._run(f"cd {shlex.quote(self.cwd)} && tar -czf {shlex.quote(name)} -- {q}")

    def extract(self):
        paths = self.selected_paths()
        for p in paths:
            q = shlex.quote(p)
            low = p.lower()
            if low.endswith(".zip"):
                cmd = f"unzip -o {q} -d {shlex.quote(self.cwd)}"
            elif any(low.endswith(x) for x in (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst")):
                cmd = f"tar -xf {q} -C {shlex.quote(self.cwd)}"
            elif low.endswith(".gz"):
                cmd = f"gunzip -k {q}"
            else:
                error(self, f"Не знаю, как распаковать {posixpath.basename(p)}")
                continue
            self._run(cmd)

    def disk_usage(self):
        paths = self.selected_paths() or [self.cwd]
        q = " ".join(shlex.quote(p) for p in paths)

        def done(r):
            from .ui import show_output
            show_output(self, "Размер", r[1] or r[2])
        run_async(self.session.run, f"du -sh -- {q} 2>&1", 300, on_done=done,
                  on_error=lambda e: error(self, e))

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        m = QMenu(self)
        paths = self.selected_paths()
        if it and paths:
            is_dir = it.data(0, Qt.UserRole + 2)
            if is_dir:
                m.addAction("Открыть", lambda: self.cd(it.data(0, Qt.UserRole + 1)))
                m.addAction("Терминал здесь", lambda: self.terminalHere.emit(it.data(0, Qt.UserRole + 1)))
            else:
                m.addAction("Редактировать", lambda: self.openFile.emit(it.data(0, Qt.UserRole + 1)))
            m.addAction("⬇  Скачать", lambda: self.downloadRequested.emit(self.selected_paths()))
            m.addSeparator()
            m.addAction("Переименовать  F2", self.rename)
            m.addAction("Права (chmod)…", self.chmod)
            m.addAction("Владелец (chown, sudo)…", self.chown)
            m.addAction("Размер (du)", self.disk_usage)
            m.addAction("Скопировать путь", lambda: self._copy_paths(paths))
            m.addSeparator()
            m.addAction("Архивировать в tar.gz…", self.archive)
            if any(p.lower().endswith((".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".zst")) for p in paths):
                m.addAction("Распаковать сюда", self.extract)
            m.addSeparator()
            m.addAction("Удалить  Del", self.delete)
            m.addAction("Удалить через sudo", lambda: self.delete(sudo=True))
        else:
            m.addAction("Новая папка", self.mkdir)
            m.addAction("Новый файл", self.new_file)
            m.addAction("Терминал здесь", lambda: self.terminalHere.emit(self.cwd))
            m.addAction("Размер папки (du)", self.disk_usage)
            m.addSeparator()
            act = m.addAction("Показывать скрытые")
            act.setCheckable(True)
            act.setChecked(self.settings.get("show_hidden", False))
            act.toggled.connect(self._toggle_hidden)
            m.addAction("Обновить  F5", self.refresh)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _toggle_hidden(self, v):
        self.settings["show_hidden"] = v
        from .config import save_settings
        save_settings(self.settings)
        self.refresh()

    def _copy_paths(self, paths):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText("\n".join(paths))


# ============================ локальная панель ============================

class LocalTree(QTreeView):
    remoteDropped = Signal(list, str)

    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(REMOTE_MIME):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(REMOTE_MIME):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(REMOTE_MIME):
            paths = json.loads(bytes(md.data(REMOTE_MIME)).decode())
            idx = self.indexAt(e.position().toPoint())
            target = self.pane.cwd
            if idx.isValid() and self.pane.model.isDir(idx):
                target = self.pane.model.filePath(idx)
            self.remoteDropped.emit(paths, target)
            e.acceptProposedAction()
        else:
            e.ignore()


class LocalPane(QWidget):
    uploadRequested = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cwd = os.path.expanduser("~")
        self.history: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        tb, tl = toolbar_frame()
        tl.addWidget(icon_button("back", "Назад", self.back, "←"))
        tl.addWidget(icon_button("up", "Вверх", self.up, "↑"))
        tl.addWidget(icon_button("home", "Домой", lambda: self.cd(os.path.expanduser("~")), "⌂"))
        self.path = QLineEdit(self.cwd)
        self.path.returnPressed.connect(lambda: self.cd(os.path.expanduser(self.path.text().strip())))
        tl.addWidget(self.path, 1)
        self.hidden = QCheckBox("скрытые")
        self.hidden.toggled.connect(self._hidden)
        tl.addWidget(self.hidden)
        lay.addWidget(tb)

        self.model = QFileSystemModel(self)
        self.model.setRootPath("/")
        self.model.setReadOnly(True)
        self.model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.AllDirs)
        self.view = LocalTree(self)
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setItemsExpandable(False)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAlternatingRowColors(True)
        self.view.setUniformRowHeights(True)
        self.view.hideColumn(2)
        self.view.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.view.header().resizeSection(1, 90)
        self.view.header().resizeSection(3, 140)
        self.view.doubleClicked.connect(self._activated)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.view)
        self.cd(self.cwd, push=False)

    def cd(self, path, push=True):
        if not os.path.isdir(path):
            return
        path = os.path.abspath(path)
        if push and path != self.cwd:
            self.history.append(self.cwd)
        self.cwd = path
        self.path.setText(path)
        self.view.setRootIndex(self.model.index(path))

    def up(self):
        self.cd(os.path.dirname(self.cwd))

    def back(self):
        if self.history:
            self.cd(self.history.pop(), push=False)

    def _hidden(self, v):
        f = QDir.AllEntries | QDir.NoDotAndDotDot | QDir.AllDirs
        if v:
            f |= QDir.Hidden
        self.model.setFilter(f)

    def _activated(self, idx):
        p = self.model.filePath(idx)
        if self.model.isDir(idx):
            self.cd(p)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def selected_paths(self):
        return [self.model.filePath(i) for i in self.view.selectionModel().selectedRows(0)]

    def _menu(self, pos):
        m = QMenu(self)
        if self.selected_paths():
            m.addAction("⬆  Загрузить на сервер", lambda: self.uploadRequested.emit(self.selected_paths()))
            m.addAction("Открыть", lambda: [QDesktopServices.openUrl(QUrl.fromLocalFile(p))
                                            for p in self.selected_paths()])
        m.addAction("Открыть папку в системе", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.cwd)))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Backspace:
            self.up()
        else:
            super().keyPressEvent(e)


# ============================ страница ============================

class FilesPage(QWidget):
    openFile = Signal(str)
    terminalHere = Signal(str)

    def __init__(self, session, settings, parent=None):
        super().__init__(parent)
        self.session = session
        self.tm = TransferManager(session)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        vsplit = QSplitter(Qt.Vertical)
        hsplit = QSplitter(Qt.Horizontal)
        self.local = LocalPane()
        self.remote = RemotePane(session, settings)

        mid = QWidget()
        mid.setFixedWidth(52)
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(6, 0, 6, 0)
        ml.addStretch()
        up = icon_button("upload", "Загрузить выбранное на сервер →", self._upload_sel, "→")
        down = icon_button("download", "← Скачать выбранное с сервера", self._download_sel, "←")
        for b in (up, down):
            b.setFixedSize(40, 40)
            b.setStyleSheet(f"QToolButton {{ background: {theme.PANEL2}; border: 1px solid {theme.BORDER};"
                            f" border-radius: 10px; font-size: 14pt; }} QToolButton:hover {{ border-color: {theme.ACCENT}; }}")
        ml.addWidget(up)
        ml.addSpacing(8)
        ml.addWidget(down)
        ml.addStretch()

        hsplit.addWidget(self.local)
        hsplit.addWidget(mid)
        hsplit.addWidget(self.remote)
        hsplit.setStretchFactor(0, 1)
        hsplit.setStretchFactor(2, 1)
        hsplit.setCollapsible(1, False)
        vsplit.addWidget(hsplit)
        self.transfers = TransfersPanel(self.tm)
        vsplit.addWidget(self.transfers)
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([600, 170])
        lay.addWidget(vsplit)

        self.remote.openFile.connect(self.openFile)
        self.remote.terminalHere.connect(self.terminalHere)
        self.remote.downloadRequested.connect(lambda ps: self._download(ps, self.local.cwd))
        self.remote.tree.filesDropped.connect(self._upload)
        self.remote.tree.remoteMoved.connect(self.remote.move)
        self.local.view.remoteDropped.connect(self._download)
        self.local.uploadRequested.connect(lambda ps: self._upload(ps, self.remote.cwd))
        self.tm.sig.finished.connect(self._job_done)
        self._loaded = False

    def showEvent(self, e):
        if not self._loaded:
            self._loaded = True
            self.remote.refresh()
        super().showEvent(e)

    def _upload_sel(self):
        self._upload(self.local.selected_paths(), self.remote.cwd)

    def _download_sel(self):
        self._download(self.remote.selected_paths(), self.local.cwd)

    def _upload(self, paths, remote_dir):
        for p in paths:
            self.tm.upload(p, remote_dir)

    def _download(self, paths, local_dir):
        for p in paths:
            self.tm.download(p, local_dir)

    def _job_done(self, job, ok, msg):
        if job.kind == "up" and job.dst_dir == self.remote.cwd:
            self.remote.refresh()
