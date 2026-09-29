"""Background work; widgets are only ever touched by the GUI thread."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .engine import FileEntry, scan_markdown, search_files


class ScanThread(QThread):
    batch_ready = Signal(int, object)
    progress = Signal(int, object)
    completed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, root: Path, generation: int, parent=None):
        super().__init__(parent)
        self.root = root
        self.generation = generation

    def run(self):
        try:
            stats = scan_markdown(
                self.root,
                cancelled=self.isInterruptionRequested,
                on_batch=lambda batch: self.batch_ready.emit(self.generation, batch),
                on_progress=lambda stats: self.progress.emit(self.generation, stats),
            )
            self.completed.emit(self.generation, stats)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class SearchThread(QThread):
    """One worker, one replaceable pending query; old work is cancellable.

    Directory batches are appended under the same lock that protects snapshots.
    Each query gets its own immutable snapshot and cancellation event. An older
    query can never overwrite a newer result in the GUI (which checks request ID).
    """

    completed = Signal(int, object, float)
    failed = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._condition = threading.Condition()
        self._entries: list[FileEntry] = []
        self._pending = None
        self._running_cancel: threading.Event | None = None
        self._stopping = False

    def reset_entries(self):
        with self._condition:
            self._cancel_locked()
            self._entries = []

    def append_entries(self, entries: list[FileEntry]):
        with self._condition:
            self._entries.extend(entries)

    def _cancel_locked(self):
        if self._running_cancel is not None:
            self._running_cancel.set()
        if self._pending is not None:
            self._pending[3].set()
        self._pending = None

    def invalidate(self):
        with self._condition:
            self._cancel_locked()

    def submit(self, request_id: int, query: str, recent: list[str]):
        with self._condition:
            if self._stopping:
                return
            self._cancel_locked()
            self._pending = (request_id, query, tuple(recent), threading.Event())
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._stopping = True
            self._cancel_locked()
            self._condition.notify()

    def run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._pending is not None)
                if self._stopping:
                    return
                request_id, query, recent, cancel = self._pending
                self._pending = None
                self._running_cancel = cancel
                entries = tuple(self._entries)
            started = time.perf_counter()
            try:
                result = search_files(
                    entries, query, limit=100, cancelled=cancel.is_set, recent=recent
                )
                if not cancel.is_set() and not result.cancelled:
                    self.completed.emit(request_id, result, time.perf_counter() - started)
            except Exception as exc:
                if not cancel.is_set():
                    self.failed.emit(request_id, str(exc))
            finally:
                with self._condition:
                    if self._running_cancel is cancel:
                        self._running_cancel = None
