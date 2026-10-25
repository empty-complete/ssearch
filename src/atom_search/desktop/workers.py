"""Workers cancel promptly and retain process lifetime through owned cleanup."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from atom_search.content import message

from PySide6.QtCore import QObject, Signal


class WorkerSignals(QObject):
    finished = Signal(int, str, object)
    failed = Signal(int, str, str)
    progress = Signal(int, object)


class Workers:
    def __init__(self, service_factory: Callable, parent: QObject):
        self.signals = WorkerSignals(parent)
        self._factory = service_factory
        self._service = None
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._init_lock = threading.Lock()
        self._jobs: dict[int, threading.Event] = {}
        self._serial = 0
        self._closing = False
        self._factory_cleanup_unknown = False
        self._cleanup_confirmed = True  # No service has owned a Runtime yet.
        self._idle = threading.Event()
        self._idle.set()
        self.on_close = None
        self._owner_closed = False

    def start(self, kind: str, operation: Callable) -> int:
        with self._lock:
            if self._closing:
                return -1
            self._serial += 1
            job = self._serial
            cancel = threading.Event()
            self._jobs[job] = cancel
            self._idle.clear()

        def emit(signal, *values):
            with self._lock:
                active = not self._closing
            if active:
                try:
                    signal.emit(*values)
                except RuntimeError:
                    # The window may already have been destroyed at shutdown.
                    pass

        def run():
            try:
                with self._init_lock:
                    if self._service is None:
                        self._cleanup_confirmed = False
                        try:
                            self._service = self._factory()
                        except BaseException:
                            # A failed factory may have failed its own cleanup.
                            # A later successful job cannot erase that uncertainty.
                            self._factory_cleanup_unknown = True
                            raise
                result = operation(self._service,
                                   lambda value: emit(self.signals.progress, job, value), cancel)
                emit(self.signals.finished, job, kind, result)
            except Exception:
                emit(self.signals.failed, job, kind,
                     message("worker.failure"))
            finally:
                with self._lock:
                    self._jobs.pop(job, None)
                    self._changed.notify_all()
                    should_close = self._closing and not self._jobs
                if should_close:
                    try:
                        self._close_service()
                    finally:
                        self._idle.set()

        threading.Thread(target=run, name=f"atom-{kind}-{job}", daemon=False).start()
        return job

    def cancel(self, job: int):
        with self._lock:
            event = self._jobs.get(job)
            if event:
                event.set()

    def quiesce(self, jobs: tuple[int, ...], timeout_seconds: float = 5) -> bool:
        """Cancel only incompatible jobs, without waiting for readers or PG exit."""
        deadline = time.monotonic() + min(5, max(0, timeout_seconds))
        with self._changed:
            for job in jobs:
                if job in self._jobs:
                    self._jobs[job].set()
            while any(job in self._jobs for job in jobs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    def shutdown(self):
        with self._lock:
            if self._closing:
                return
            self._closing = True
            for event in self._jobs.values():
                event.set()
            should_close = not self._jobs
        if should_close:
            self._idle.clear()
            def cleanup():
                try:
                    self._close_service()
                finally:
                    self._idle.set()
            threading.Thread(target=cleanup, name="atom-close", daemon=False).start()

    def wait(self):
        """Called after the GUI loop exits, never blocks the active window."""
        self._idle.wait()
        return self._cleanup_confirmed and not self._factory_cleanup_unknown

    def _close_service(self):
        with self._init_lock:
            service, self._service = self._service, None
            if service is not None:
                try:
                    service.close()
                except Exception:
                    self._cleanup_confirmed = False
                else:
                    self._cleanup_confirmed = True
            if self.on_close is not None and not self._owner_closed:
                self._owner_closed = True
                try:
                    self.on_close()
                except Exception:
                    self._cleanup_confirmed = False
