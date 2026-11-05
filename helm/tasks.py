"""Запуск блокирующих операций (SSH) в пуле потоков с колбэками в GUI-потоке."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

_live: set = set()
QThreadPool.globalInstance().setMaxThreadCount(24)


class _Bridge(QObject):
    done = Signal(object)
    fail = Signal(object)

    def __init__(self, ok, err):
        super().__init__()
        self._ok, self._err = ok, err
        self.done.connect(self._on_done)
        self.fail.connect(self._on_fail)

    @Slot(object)
    def _on_done(self, result):
        _live.discard(self)
        if self._ok:
            try:
                self._ok(result)
            except RuntimeError as e:  # виджет уже удалён
                if "deleted" not in str(e):
                    raise

    @Slot(object)
    def _on_fail(self, exc):
        _live.discard(self)
        try:
            if self._err:
                self._err(exc)
            else:
                traceback.print_exception(exc)
        except RuntimeError as e:
            if "deleted" not in str(e):
                raise


class _Job(QRunnable):
    def __init__(self, fn, args, kwargs, bridge):
        super().__init__()
        self.fn, self.args, self.kwargs, self.bridge = fn, args, kwargs, bridge

    def run(self):
        try:
            r = self.fn(*self.args, **self.kwargs)
        except BaseException as e:  # noqa: BLE001
            self.bridge.fail.emit(e)
        else:
            self.bridge.done.emit(r)


def run_async(fn, *args, on_done=None, on_error=None, **kwargs) -> None:
    """Выполнить fn(*args) в фоне. on_done/on_error вызываются в GUI-потоке."""
    bridge = _Bridge(on_done, on_error)
    _live.add(bridge)
    QThreadPool.globalInstance().start(_Job(fn, args, kwargs, bridge))
