"""Обзор сервера (live-метрики с графиками), процессы, systemd-сервисы."""
from __future__ import annotations

import shlex
import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMenu, QProgressBar, QScrollArea,
                               QTreeWidget, QVBoxLayout, QWidget)

from . import theme
from .tasks import run_async
from .ui import (SortItem, card, confirm, dot_icon, error, human_size, human_uptime, run_priv,
                 show_output, toolbar_frame, tool_button)

STATS_SCRIPT = r"""
echo "HOST=$(hostname 2>/dev/null)"
. /etc/os-release 2>/dev/null; echo "OS=${PRETTY_NAME:-$(uname -s)}"
echo "KERNEL=$(uname -r)"; echo "ARCH=$(uname -m)"
echo "NPROC=$(nproc 2>/dev/null || echo 1)"
echo "UPTIME=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
echo "LOAD=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
echo "CPUMODEL=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2)"
echo "USERS=$(who 2>/dev/null | wc -l)"
echo "PROCS=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)"
echo "IP=$(hostname -I 2>/dev/null | cut -d' ' -f1-3)"
head -1 /proc/stat
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree|Cached|Buffers):' /proc/meminfo
echo '--DF--'
df -PTk -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | tail -n +2
echo '--NET--'
tail -n +3 /proc/net/dev
"""


class Sparkline(QWidget):
    def __init__(self, color: str, maxlen=90, fixed_max: float | None = 100, parent=None):
        super().__init__(parent)
        self.data = deque(maxlen=maxlen)
        self.data2 = deque(maxlen=maxlen)
        self.color = QColor(color)
        self.color2 = QColor(theme.ACCENT)
        self.fixed_max = fixed_max
        self.setMinimumHeight(70)

    def push(self, v, v2=None):
        self.data.append(v)
        if v2 is not None:
            self.data2.append(v2)
        self.update()

    def _path(self, data, mx, w, h):
        n = self.data.maxlen
        path = QPainterPath()
        pts = [QPointF(w - (len(data) - 1 - i) * w / (n - 1), h - 4 - (v / mx) * (h - 10))
               for i, v in enumerate(data)]
        if not pts:
            return None, None
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        fill = QPainterPath(path)
        fill.lineTo(pts[-1].x(), h)
        fill.lineTo(pts[0].x(), h)
        fill.closeSubpath()
        return path, fill

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(theme.BORDER), 1, Qt.DashLine))
        for i in range(1, 4):
            y = h * i / 4
            p.drawLine(0, int(y), w, int(y))
        mx = self.fixed_max or max([1, *self.data, *self.data2]) * 1.15
        for data, col in ((self.data2, self.color2), (self.data, self.color)):
            if len(data) < 2:
                continue
            path, fill = self._path(data, mx, w, h)
            g = QLinearGradient(0, 0, 0, h)
            c1 = QColor(col)
            c1.setAlpha(90)
            c2 = QColor(col)
            c2.setAlpha(0)
            g.setColorAt(0, c1)
            g.setColorAt(1, c2)
            p.fillPath(fill, g)
            p.setPen(QPen(col, 2))
            p.drawPath(path)
        p.end()


class MetricCard(QWidget):
    def __init__(self, title, color, fixed_max=100, parent=None):
        super().__init__(parent)
        self.frame, lay = card(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)
        row = QHBoxLayout()
        self.value = QLabel("—")
        self.value.setObjectName("bigval")
        self.value.setStyleSheet(f"color: {color}; background: transparent;")
        self.sub = QLabel("")
        self.sub.setObjectName("muted")
        self.sub.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        row.addWidget(self.value)
        row.addStretch()
        row.addWidget(self.sub)
        lay.addLayout(row)
        self.spark = Sparkline(color, fixed_max=fixed_max)
        lay.addWidget(self.spark)


class OverviewPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.prev_cpu = None
        self.prev_net = None
        self.prev_t = None
        self._busy = False
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)

        head = QHBoxLayout()
        tl = QVBoxLayout()
        self.title = QLabel(session.profile.name)
        self.title.setObjectName("h1")
        self.subtitle = QLabel(session.profile.address)
        self.subtitle.setObjectName("muted")
        tl.addWidget(self.title)
        tl.addWidget(self.subtitle)
        head.addLayout(tl)
        head.addStretch()
        self.live = QLabel("● LIVE")
        self.live.setStyleSheet(f"color: {theme.OK}; font-weight: 700; letter-spacing: 2px; background: transparent;")
        head.addWidget(self.live)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(16)
        self.cpu = MetricCard("CPU", theme.ACCENT2)
        self.mem = MetricCard("Память", theme.ACCENT)
        self.net = MetricCard("Сеть  ↓ rx / ↑ tx", theme.OK, fixed_max=None)
        grid.addWidget(self.cpu, 0, 0)
        grid.addWidget(self.mem, 0, 1)
        grid.addWidget(self.net, 0, 2)

        info_f, il = card("Система")
        self.info = QGridLayout()
        self.info.setHorizontalSpacing(18)
        self.info.setVerticalSpacing(6)
        il.addLayout(self.info)
        il.addStretch()
        self.info_labels = {}
        for r, (k, label) in enumerate((("HOST", "Хост"), ("OS", "ОС"), ("KERNEL", "Ядро"),
                                        ("CPUMODEL", "CPU"), ("IP", "IP"), ("UPTIME", "Аптайм"),
                                        ("LOAD", "Load avg"), ("PROCS", "Процессы"), ("USERS", "Сессии"),
                                        ("SWAP", "Swap"))):
            kl = QLabel(label)
            kl.setObjectName("muted")
            vl = QLabel("—")
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            vl.setWordWrap(True)
            self.info.addWidget(kl, r, 0, Qt.AlignTop)
            self.info.addWidget(vl, r, 1)
            self.info_labels[k] = vl
        grid.addWidget(info_f, 1, 0)

        disk_f, self.disk_lay = card("Диски")
        self.disk_box = QVBoxLayout()
        self.disk_box.setSpacing(10)
        self.disk_lay.addLayout(self.disk_box)
        self.disk_lay.addStretch()
        grid.addWidget(disk_f, 1, 1, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        lay.addLayout(grid)
        lay.addStretch()
        self.timer = QTimer(self, interval=2000, timeout=self.refresh)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.timer.start()

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def refresh(self):
        if self._busy:
            return
        if not self.s.alive:
            self.live.setStyleSheet(f"color: {theme.ERR}; font-weight: 700; background: transparent;")
            self.live.setText("● OFFLINE")
            return
        self._busy = True
        run_async(self.s.run, STATS_SCRIPT, 15, on_done=self._got, on_error=self._fail)

    def _fail(self, e):
        self._busy = False
        self.live.setText("● ОШИБКА")
        self.live.setStyleSheet(f"color: {theme.ERR}; font-weight: 700; background: transparent;")

    def _got(self, r):
        self._busy = False
        self.live.setText("● LIVE")
        self.live.setStyleSheet(f"color: {theme.OK}; font-weight: 700; letter-spacing: 2px; background: transparent;")
        out = r[1]
        kv, mem, disks, net = {}, {}, [], {}
        section = None
        cpu_line = None
        for line in out.splitlines():
            if line == "--DF--":
                section = "df"
                continue
            if line == "--NET--":
                section = "net"
                continue
            if section == "df":
                p = line.split()
                if len(p) >= 7:
                    disks.append((p[6], p[1], int(p[2]) * 1024, int(p[3]) * 1024, p[0]))
            elif section == "net":
                if ":" in line:
                    name, rest = line.split(":", 1)
                    name = name.strip()
                    f = rest.split()
                    if name != "lo" and len(f) >= 9 and not name.startswith(("veth", "br-", "docker")):
                        net[name] = (int(f[0]), int(f[8]))
            elif line.startswith("cpu "):
                cpu_line = [int(x) for x in line.split()[1:]]
            elif ":" in line and line.split(":")[0] in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Cached", "Buffers"):
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
            elif "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v.strip()

        now = time.monotonic()
        # CPU
        if cpu_line:
            idle = cpu_line[3] + (cpu_line[4] if len(cpu_line) > 4 else 0)
            total = sum(cpu_line[:8])
            if self.prev_cpu:
                dt = total - self.prev_cpu[0]
                di = idle - self.prev_cpu[1]
                pct = max(0.0, min(100.0, 100.0 * (1 - di / dt))) if dt > 0 else 0
                self.cpu.value.setText(f"{pct:.0f}%")
                self.cpu.spark.push(pct)
            self.prev_cpu = (total, idle)
        self.cpu.sub.setText(f"{kv.get('NPROC', '?')} ядер\nload {kv.get('LOAD', '')}")
        # память
        if mem.get("MemTotal"):
            used = mem["MemTotal"] - mem.get("MemAvailable", 0)
            pct = 100 * used / mem["MemTotal"]
            self.mem.value.setText(f"{pct:.0f}%")
            self.mem.sub.setText(f"{human_size(used)}\nиз {human_size(mem['MemTotal'])}")
            self.mem.spark.push(pct)
            if mem.get("SwapTotal"):
                su = mem["SwapTotal"] - mem.get("SwapFree", 0)
                self.info_labels["SWAP"].setText(f"{human_size(su)} / {human_size(mem['SwapTotal'])}")
            else:
                self.info_labels["SWAP"].setText("нет")
        # сеть
        rx = sum(v[0] for v in net.values())
        tx = sum(v[1] for v in net.values())
        if self.prev_net and self.prev_t:
            dt = now - self.prev_t
            rxs = max(0, (rx - self.prev_net[0]) / dt)
            txs = max(0, (tx - self.prev_net[1]) / dt)
            self.net.value.setText(f"↓ {human_size(rxs)}/с")
            self.net.sub.setText(f"↑ {human_size(txs)}/с\nвсего ↓{human_size(rx)} ↑{human_size(tx)}")
            self.net.spark.push(rxs, txs)
        self.prev_net, self.prev_t = (rx, tx), now
        # инфо
        for k, lbl in self.info_labels.items():
            if k == "UPTIME" and kv.get(k):
                try:
                    lbl.setText(human_uptime(float(kv[k])))
                except ValueError:
                    pass
            elif k in kv:
                lbl.setText(kv[k].strip() or "—")
        if kv.get("HOST"):
            self.title.setText(f"{self.s.profile.name}")
            self.subtitle.setText(f"{self.s.profile.address}  ·  {kv.get('HOST')}  ·  {kv.get('ARCH', '')}")
        self._disks(disks)

    def _disks(self, disks):
        while self.disk_box.count():
            it = self.disk_box.takeAt(0)
            w = it.widget() or (it.layout() and None)
            if it.widget():
                it.widget().deleteLater()
        seen = set()
        for mount, fstype, used, avail, dev in disks:
            if dev in seen:
                continue
            seen.add(dev)
            total = used + avail
            pct = 100 * used / total if total else 0
            row = QWidget()
            row.setObjectName("clear")
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(3)
            top = QHBoxLayout()
            name = QLabel(f"<b>{mount}</b>  <span style='color:{theme.MUTED}'>{dev} · {fstype}</span>")
            val = QLabel(f"{human_size(used)} / {human_size(total)}  ·  {pct:.0f}%")
            val.setObjectName("muted")
            top.addWidget(name)
            top.addStretch()
            top.addWidget(val)
            rl.addLayout(top)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(pct))
            bar.setFixedHeight(8)
            if pct > 90:
                bar.setStyleSheet(f"QProgressBar::chunk {{ background: {theme.ERR}; border-radius: 4px; }}")
            elif pct > 75:
                bar.setStyleSheet(f"QProgressBar::chunk {{ background: {theme.WARN}; border-radius: 4px; }}")
            rl.addWidget(bar)
            self.disk_box.addWidget(row)


# ============================ процессы ============================

class ProcessesPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        tb, tl = toolbar_frame()
        tl.addWidget(tool_button("refresh", "", "Обновить", self.refresh))
        tl.addWidget(tool_button("stop", "SIGTERM", slot=lambda: self.kill("TERM")))
        tl.addWidget(tool_button("kill", "SIGKILL", slot=lambda: self.kill("KILL"), obj="danger"))
        tl.addStretch()
        self.auto = QCheckBox("автообновление")
        self.auto.setChecked(True)
        tl.addWidget(self.auto)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фильтр: имя, пользователь, PID…")
        self.filter.setFixedWidth(260)
        self.filter.textChanged.connect(self._apply_filter)
        tl.addWidget(self.filter)
        lay.addWidget(tb)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["PID", "Пользователь", "CPU %", "MEM %", "RSS", "Время", "Команда"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(2, Qt.DescendingOrder)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        h = self.tree.header()
        for i, w in enumerate((80, 110, 70, 70, 90, 100)):
            h.resizeSection(i, w)
        h.setStretchLastSection(True)
        lay.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)
        self.timer = QTimer(self, interval=3000, timeout=lambda: self.auto.isChecked() and self.refresh())

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.timer.start()

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        run_async(self.s.run, "ps -eo pid=,user:20=,pcpu=,pmem=,rss=,etime=,args= --sort=-pcpu 2>/dev/null "
                              "|| ps -eo pid=,user=,pcpu=,pmem=,rss=,etime=,args=", 20,
                  on_done=self._got, on_error=lambda e: setattr(self, "_busy", False))

    def _got(self, r):
        self._busy = False
        sel = {it.text(0) for it in self.tree.selectedItems()}
        sb = self.tree.verticalScrollBar().value()
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        n = 0
        cpu_total = 0.0
        for line in r[1].splitlines():
            p = line.split(None, 6)
            if len(p) < 7:
                continue
            pid, user, cpu, mem, rss, et, cmd = p
            try:
                fc, fm, ri = float(cpu), float(mem), int(rss) * 1024
            except ValueError:
                continue
            it = SortItem([pid, user, f"{fc:.1f}", f"{fm:.1f}", human_size(ri), et, cmd])
            it.setData(0, Qt.UserRole, int(pid))
            it.setData(2, Qt.UserRole, fc)
            it.setData(3, Qt.UserRole, fm)
            it.setData(4, Qt.UserRole, ri)
            for c in (0, 2, 3, 4):
                it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            if fc > 50:
                it.setForeground(2, QColor(theme.ERR))
            elif fc > 10:
                it.setForeground(2, QColor(theme.WARN))
            if user == "root":
                it.setForeground(1, QColor(theme.WARN))
            self.tree.addTopLevelItem(it)
            if pid in sel:
                it.setSelected(True)
            n += 1
            cpu_total += fc
        self.tree.setSortingEnabled(True)
        self._apply_filter()
        self.tree.verticalScrollBar().setValue(sb)
        self.status.setText(f"Процессов: {n}")

    def _apply_filter(self, *_):
        t = self.filter.text().lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setHidden(bool(t) and t not in (it.text(0) + " " + it.text(1) + " " + it.text(6)).lower())

    def kill(self, sig):
        pids = [it.text(0) for it in self.tree.selectedItems()]
        if not pids:
            return
        names = "\n".join(f"{it.text(0)}  {it.text(6)[:70]}" for it in self.tree.selectedItems()[:10])
        if not confirm(self, f"Отправить SIG{sig}?\n\n{names}"):
            return
        cmd = f"kill -{sig} " + " ".join(pids)

        def done(r):
            if r[0] != 0 and not self.s.is_root and "not permitted" in (r[2] or "").lower():
                if confirm(self, "Недостаточно прав. Повторить через sudo?"):
                    run_priv(self, self.s, self.s.run, cmd, 20, True, on_done=lambda _: self.refresh())
                return
            if r[0] != 0:
                error(self, r[2] or r[1])
            self.refresh()
        run_async(self.s.run, cmd, 20, on_done=done, on_error=lambda e: error(self, e))

    def _menu(self, pos):
        if not self.tree.itemAt(pos):
            return
        m = QMenu(self)
        m.addAction("SIGTERM (завершить)", lambda: self.kill("TERM"))
        m.addAction("SIGKILL (убить)", lambda: self.kill("KILL"))
        m.addAction("SIGHUP (перечитать конфиг)", lambda: self.kill("HUP"))
        m.addSeparator()
        it = self.tree.itemAt(pos)
        m.addAction("Открытые файлы (lsof)", lambda: run_priv(
            self, self.s, self.s.run, f"lsof -p {it.text(0)} 2>&1 | head -500 || ls -l /proc/{it.text(0)}/fd",
            30, True, on_done=lambda r: show_output(self, f"lsof {it.text(0)}", r[1] or r[2])))
        m.exec(self.tree.viewport().mapToGlobal(pos))


# ============================ сервисы ============================

class ServicesPage(QWidget):
    openTerminal = Signal(str, str)
    openFile = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        tb, tl = toolbar_frame()
        tl.addWidget(tool_button("refresh", "", "Обновить", self.refresh))
        for icon, text, act in (("play", "Старт", "start"), ("stop", "Стоп", "stop"),
                                ("restart", "Рестарт", "restart"), ("refresh", "Reload", "reload")):
            tl.addWidget(tool_button(icon, text, slot=lambda _=False, a=act: self.act(a)))
        tl.addWidget(tool_button("bolt", "Enable", slot=lambda: self.act("enable")))
        tl.addWidget(tool_button("kill", "Disable", slot=lambda: self.act("disable")))
        tl.addWidget(tool_button("info", "Статус", slot=self.status_of))
        tl.addWidget(tool_button("logs", "Логи", slot=self.logs))
        tl.addStretch()
        self.state = QComboBox()
        self.state.addItems(["Все", "Активные", "Упавшие", "Неактивные"])
        self.state.currentIndexChanged.connect(self._apply_filter)
        tl.addWidget(self.state)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фильтр…")
        self.filter.setFixedWidth(200)
        self.filter.textChanged.connect(self._apply_filter)
        tl.addWidget(self.filter)
        lay.addWidget(tb)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Сервис", "Active", "Sub", "Автозапуск", "Описание"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemActivated.connect(lambda *_: self.status_of())
        h = self.tree.header()
        for i, w in enumerate((300, 90, 90, 110)):
            h.resizeSection(i, w)
        h.setStretchLastSection(True)
        lay.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)
        self._loaded = False

    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def refresh(self):
        self.status.setText("Загрузка…")
        cmd = ("command -v systemctl >/dev/null || { echo NOSYSTEMD; exit 0; }; "
               "systemctl list-units --type=service --all --no-pager --no-legend --plain; "
               "echo '--FILES--'; systemctl list-unit-files --type=service --no-pager --no-legend")
        run_async(self.s.run, cmd, 30, on_done=self._got, on_error=lambda e: self.status.setText(str(e)))

    def _got(self, r):
        out = r[1]
        if "NOSYSTEMD" in out:
            self.status.setText("systemd не найден на сервере")
            return
        units, files = out.split("--FILES--", 1) if "--FILES--" in out else (out, "")
        enabled = {}
        for line in files.splitlines():
            p = line.split()
            if len(p) >= 2:
                enabled[p[0]] = p[1]
        sel = {it.text(0) for it in self.tree.selectedItems()}
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        seen = set()
        n_active = n_failed = 0
        rows = []
        for line in units.splitlines():
            p = line.split(None, 4)
            if len(p) < 4 or not p[0].endswith(".service"):
                continue
            rows.append((p[0], p[2], p[3], enabled.get(p[0], ""), p[4] if len(p) > 4 else ""))
            seen.add(p[0])
        for unit, st in enabled.items():
            if unit not in seen and "@" not in unit:
                rows.append((unit, "inactive", "dead", st, ""))
        for unit, active, sub, en, desc in rows:
            it = SortItem([unit, active, sub, en, desc])
            col = {"active": theme.OK, "failed": theme.ERR, "activating": theme.WARN,
                   "deactivating": theme.WARN}.get(active, theme.MUTED)
            it.setIcon(0, dot_icon(col))
            it.setForeground(1, QColor(col))
            it.setForeground(3, QColor(theme.ACCENT2 if en == "enabled" else theme.MUTED))
            it.setData(0, Qt.UserRole + 1, unit)
            self.tree.addTopLevelItem(it)
            if unit in sel:
                it.setSelected(True)
            n_active += active == "active"
            n_failed += active == "failed"
        self.tree.setSortingEnabled(True)
        self._apply_filter()
        self.status.setText(f"Сервисов: {len(rows)} · активно: {n_active} · упало: {n_failed}")

    def _apply_filter(self, *_):
        t = self.filter.text().lower().strip()
        mode = self.state.currentIndex()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            a = it.text(1)
            ok = mode == 0 or (mode == 1 and a == "active") or (mode == 2 and a == "failed") or \
                 (mode == 3 and a == "inactive")
            if t and t not in (it.text(0) + " " + it.text(4)).lower():
                ok = False
            it.setHidden(not ok)

    def _sel(self):
        u = [it.text(0) for it in self.tree.selectedItems()]
        if not u:
            error(self, "Выберите сервис")
        return u

    def act(self, action):
        units = self._sel()
        if not units:
            return
        if action in ("stop", "disable") and not confirm(self, f"systemctl {action}:\n\n" + "\n".join(units)):
            return
        cmd = f"systemctl {action} " + " ".join(map(shlex.quote, units))

        def done(r):
            if r[0] != 0:
                error(self, (r[2] or r[1]).strip())
            self.refresh()
        run_priv(self, self.s, self.s.run, cmd, 90, True, on_done=done)

    def status_of(self):
        units = self._sel()
        if units:
            run_async(self.s.run, f"systemctl status --no-pager -l -n 40 {shlex.quote(units[0])}", 20,
                      on_done=lambda r: show_output(self, units[0], r[1] + r[2]),
                      on_error=lambda e: error(self, e))

    def logs(self):
        units = self._sel()
        if units:
            sudo = "" if self.s.is_root else "sudo "
            self.openTerminal.emit(f"{sudo}journalctl -u {shlex.quote(units[0])} -f -n 300 --no-pager",
                                   f"📜 {units[0]}")

    def edit_unit(self):
        units = self._sel()
        if not units:
            return

        def done(r):
            path = r[1].strip().split("=", 1)[-1]
            if path:
                self.openFile.emit(path)
            else:
                error(self, "Файл юнита не найден")
        run_async(self.s.run, f"systemctl show -p FragmentPath {shlex.quote(units[0])}", 15,
                  on_done=done, on_error=lambda e: error(self, e))

    def _menu(self, pos):
        if not self.tree.itemAt(pos):
            return
        m = QMenu(self)
        for a, t in (("start", "▶  Запустить"), ("stop", "■  Остановить"), ("restart", "⟳  Перезапустить"),
                     ("reload", "Reload"), ("enable", "Включить автозапуск"), ("disable", "Выключить автозапуск")):
            m.addAction(t, lambda a=a: self.act(a))
        m.addSeparator()
        m.addAction("Статус", self.status_of)
        m.addAction("Логи (journalctl -f)", self.logs)
        m.addAction("Редактировать unit-файл", self.edit_unit)
        m.addAction("daemon-reload", lambda: run_priv(self, self.s, self.s.run, "systemctl daemon-reload", 60, True,
                                                      on_done=lambda _: self.refresh()))
        m.exec(self.tree.viewport().mapToGlobal(pos))
