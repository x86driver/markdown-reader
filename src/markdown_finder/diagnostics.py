"""Read-only local diagnostics for an already running reader.

The socket thread can answer ping/events/threads even when the GUI is blocked.
Qt objects are inspected only through a queued request on the GUI thread.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import traceback

from PySide6.QtCore import QObject, QEvent, Qt, Signal, SLOT, Slot, qVersion
from PySide6.QtWidgets import QApplication

from .window_geometry import _SLEEP_SIGNAL


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EVENT_LIMIT = 256


def runtime_directory() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "markdown-finder"
    return Path(tempfile.gettempdir()) / f"markdown-finder-{os.getuid()}"


def _private_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise PermissionError(f"診斷目錄必須由目前使用者持有，且權限為 0700：{path}")


def _endpoint(directory: Path, pid: int) -> Path:
    return directory / f"debug-{pid}.sock"


def _check_endpoint(path: Path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"診斷入口不是目前使用者的本機 socket：{path}")


def query_endpoint(path: Path, command: str, timeout: float = 3.0) -> dict:
    """Query without creating a GUI, changing focus, or touching QSettings."""
    _check_endpoint(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps({"command": command}).encode() + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks:
            block = connection.recv(65536)
            if not block:
                raise ConnectionError("診斷連線在回覆完成前中斷")
            chunks.extend(block)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise ValueError("診斷回覆超過大小上限")
        response = json.loads(chunks.partition(b"\n")[0])
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("診斷回覆格式不正確")
        return response


def running_instances(directory: Path | None = None) -> list[dict]:
    directory = directory or runtime_directory()
    if not directory.exists():
        return []
    _private_directory(directory)
    instances = []
    for path in sorted(directory.glob("debug-*.sock")):
        try:
            response = query_endpoint(path, "ping", timeout=0.5)
            identity = response.get("data", {})
            if (response["ok"] and identity.get("application") == "MarkdownFinder"
                    and identity.get("protocol_version") == PROTOCOL_VERSION
                    and path.name == f"debug-{identity.get('pid')}.sock"):
                instances.append(identity)
        except (OSError, ValueError, ConnectionError):
            continue
    return instances


def query_running(command: str, pid: int | None = None, *, directory: Path | None = None) -> dict:
    directory = directory or runtime_directory()
    if directory.exists():
        _private_directory(directory)
    if pid is None:
        instances = running_instances(directory)
        if not instances:
            raise ConnectionError("找不到已啟用診斷介面的程式；更新後請正常關閉並重新開啟")
        if len(instances) != 1:
            raise ValueError("有多個執行中的程式，請加上 --debug-pid 指定 PID")
        pid = instances[0]["pid"]
    return query_endpoint(_endpoint(directory, pid), command)


def _rect(rect) -> dict:
    return dict(zip(("x", "y", "width", "height"), rect.getRect()))


def _size(size) -> dict:
    return {"width": size.width(), "height": size.height()}


def _thread_stacks() -> list[dict]:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    return [
        {"thread_id": ident, "name": names.get(ident, "Qt/Python thread"),
         "stack": traceback.format_stack(frame, limit=40)}
        for ident, frame in sys._current_frames().items()
    ]


class DiagnosticInterface(QObject):
    _snapshot_requested = Signal(object)

    def __init__(self, window, *, directory: Path | None = None, gui_timeout: float = 2.0):
        super().__init__(window)
        self.window = window
        self.directory = directory or runtime_directory()
        _private_directory(self.directory)
        self.path = _endpoint(self.directory, os.getpid())
        self._started = time.monotonic()
        self._closed = False
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._pending = None
        self._connection = None
        self._events = deque(maxlen=EVENT_LIMIT)
        self._sequence = 0
        self._watched_screens = set()
        self._gui_timeout = gui_timeout
        self._sleep_connection = None
        app = QApplication.instance()
        self.identity = {
            "application": app.applicationName(), "version": app.applicationVersion(),
            "pid": os.getpid(), "protocol_version": PROTOCOL_VERSION,
            "qt_version": qVersion(), "platform": app.platformName(), "socket": str(self.path),
        }
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.path.exists():
                _check_endpoint(self.path)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(str(self.path))
                    except ConnectionRefusedError:
                        self.path.unlink()
                    else:
                        raise OSError(f"這個程序已有診斷介面：{self.path}")
            self._listener.bind(str(self.path))
            self.path.chmod(0o600)
            self._listener.listen(8)
            self._listener.settimeout(0.2)
        except OSError:
            self._listener.close()
            raise
        self._snapshot_requested.connect(self._answer_snapshot, Qt.ConnectionType.QueuedConnection)
        window.installEventFilter(self)
        app.applicationStateChanged.connect(self._application_changed)
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(self._desktop_changed)
        for screen in app.screens():
            self._watch_screen(screen)
        if sys.platform.startswith("linux") and app.platformName() in {"wayland", "wayland-egl", "xcb"}:
            from PySide6.QtDBus import QDBusConnection

            bus = QDBusConnection.systemBus()
            if bus.connect(*_SLEEP_SIGNAL, self, SLOT("_prepare_for_sleep(bool)")):
                self._sleep_connection = bus
        self.identity["resume_events_connected"] = self._sleep_connection is not None
        self._record("interface_started")
        self._thread = threading.Thread(target=self._serve, name="reader-diagnostics", daemon=True)
        self._thread.start()

    def _watch_screen(self, screen):
        if screen in self._watched_screens:
            return
        self._watched_screens.add(screen)
        screen.geometryChanged.connect(self._desktop_changed)
        screen.availableGeometryChanged.connect(self._desktop_changed)
        screen.logicalDotsPerInchChanged.connect(self._desktop_changed)

    def _screen_added(self, screen):
        self._watch_screen(screen)
        self._desktop_changed()

    def _desktop_changed(self, *args):
        self._record("desktop_changed")

    def _application_changed(self, state):
        self._record("application_state", {"application_state": state.name})

    @Slot(bool)
    def _prepare_for_sleep(self, sleeping):
        self._record("prepare_for_sleep", {"sleeping": sleeping})

    def eventFilter(self, watched, event):
        if not self._closed and watched is self.window and event.type() in {
            QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Move, QEvent.Type.Resize,
            QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate,
            QEvent.Type.WindowStateChange, QEvent.Type.ScreenChangeInternal, QEvent.Type.Close,
        }:
            extra = {"spontaneous": event.spontaneous()}
            if event.type() == QEvent.Type.WindowStateChange:
                extra["old_state"] = event.oldState().value
            self._record(event.type().name, extra)
        return False

    def _window_snapshot(self) -> dict:
        window = self.window
        guard = window._window_geometry_guard
        return {
            "geometry": _rect(window.geometry()), "frame_geometry": _rect(window.frameGeometry()),
            "normal_geometry": _rect(window.normalGeometry()), "minimum_size": _size(window.minimumSize()),
            "screen": window.screen().name() if window.screen() else None,
            "state": window.windowState().value, "flags": window.windowFlags().value,
            "active": window.isActiveWindow(), "visible": window.isVisible(),
            "maximized": window.isMaximized(), "fullscreen": window.isFullScreen(),
            "minimized": window.isMinimized(), "native_id": int(window.internalWinId()),
            "guard": {
                "stopped": guard._stopped, "remaximizing": guard._remaximizing,
                "maximized_intent": guard._maximized, "refresh_maximized": guard._refresh_maximized,
                "retry_refresh": guard._retry_refresh, "restore_normal": guard._restore_normal,
                "normal_geometry": _rect(guard._normal_geometry),
                "resume_signal_connected": guard._sleep_connection is not None,
                "desktop_work_area": guard._work_area_probe.snapshot(window.screen()),
                "timers": {
                    name: {"active": timer.isActive(), "remaining_ms": timer.remainingTime()}
                    for name, timer in (("check", guard._check_timer), ("retry", guard._retry_timer),
                                        ("maximize", guard._maximize_timer))
                },
            },
        }

    def _record(self, event, extra=None):
        if self._closed:
            return
        self._sequence += 1
        record = {
            "sequence": self._sequence, "time": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.monotonic() - self._started) * 1000),
            "event": event, "window": self._window_snapshot(), **(extra or {}),
        }
        with self._lock:
            self._events.append(record)

    def _snapshot(self) -> dict:
        window = self.window
        app = QApplication.instance()
        readers = []
        for reader in window._documents.values():
            scroll_x, scroll_y = reader._css_scroll_position()
            readers.append({
                "path": str(reader.path), "current": reader is window.current_reader(),
                "loaded": reader.loaded, "busy": reader.is_busy(), "closing": reader._closing,
                "generation": reader._generation, "zoom": reader.view.zoomFactor(),
                "css_scroll": {"x": scroll_x, "y": scroll_y},
                "fit_width": reader.fit_width_button.isChecked(),
                "session_restore_pending": reader.session_restore_pending,
                "view_geometry": _rect(reader.view.geometry()),
                "outline_visible": reader.outline_button.isChecked(),
            })
        with self._lock:
            events = list(self._events)
        return {
            "process": self.identity, "time": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.monotonic() - self._started, 3),
            "application_state": app.applicationState().name, "window": self._window_snapshot(),
            "screens": [{"name": screen.name(), "geometry": _rect(screen.geometry()),
                         "available_geometry": _rect(screen.availableGeometry()),
                         "device_pixel_ratio": screen.devicePixelRatio(),
                         "logical_dpi": screen.logicalDotsPerInch()}
                        for screen in app.screens()],
            "readers": readers,
            "background": {"closing": window._closing, "scanning": window._scanning,
                           "search_running": window.search_worker.isRunning(),
                           "search_inflight": window._search_inflight,
                           "results_current": window._results_current, "entry_count": window._entry_count},
            "events": events,
        }

    @Slot(object)
    def _answer_snapshot(self, request):
        if request["done"].is_set():
            return
        try:
            request["response"] = {"ok": True, "data": self._snapshot()}
        except Exception as error:
            request["response"] = {"ok": False, "error": f"snapshot_failed: {error}"}
        finally:
            request["done"].set()

    def _dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {"command"}:
            return {"ok": False, "error": "invalid_request"}
        command = request["command"]
        if command == "ping":
            return {"ok": True, "data": self.identity}
        if command == "threads":
            return {"ok": True, "data": _thread_stacks()}
        if command == "events":
            with self._lock:
                return {"ok": True, "data": list(self._events)}
        if command != "snapshot":
            return {"ok": False, "error": "unknown_command"}
        pending = {"done": threading.Event()}
        with self._lock:
            if self._closed:
                return {"ok": False, "error": "closing"}
            self._pending = pending
        self._snapshot_requested.emit(pending)
        answered = pending["done"].wait(self._gui_timeout)
        with self._lock:
            self._pending = None
            last_event = self._events[-1] if self._events else None
        if not answered:
            pending["done"].set()
            return {"ok": False, "error": "gui_timeout", "last_event": last_event,
                    "threads": _thread_stacks()}
        return pending["response"]

    def _serve(self):
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._connection = connection
            try:
                with connection:
                    connection.settimeout(1.0)
                    line = bytearray()
                    while b"\n" not in line and len(line) <= MAX_REQUEST_BYTES:
                        part = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(line)))
                        if not part:
                            break
                        line.extend(part)
                    if len(line) > MAX_REQUEST_BYTES:
                        response = {"ok": False, "error": "request_too_large"}
                    elif b"\n" not in line:
                        response = {"ok": False, "error": "incomplete_request"}
                    else:
                        try:
                            response = self._dispatch(json.loads(line.partition(b"\n")[0]))
                        except (ValueError, UnicodeError):
                            response = {"ok": False, "error": "invalid_json"}
                    payload = json.dumps(response, ensure_ascii=False).encode() + b"\n"
                    if len(payload) > MAX_RESPONSE_BYTES:
                        payload = b'{"ok":false,"error":"response_too_large"}\n'
                    connection.sendall(payload)
            except OSError:
                pass
            finally:
                with self._lock:
                    self._connection = None

    def stop(self):
        if self._closed:
            return
        self._closed = True
        self._stopping.set()
        self._listener.close()
        with self._lock:
            if self._pending is not None:
                self._pending["response"] = {"ok": False, "error": "closing"}
                self._pending["done"].set()
            if self._connection is not None:
                try:
                    self._connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._connection.close()
        self._thread.join(timeout=1.2)
        self.path.unlink(missing_ok=True)
        if self._sleep_connection is not None:
            self._sleep_connection.disconnect(*_SLEEP_SIGNAL, self, SLOT("_prepare_for_sleep(bool)"))
            self._sleep_connection = None
