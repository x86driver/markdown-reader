"""Inspect a live fixture through local IPC without ptrace or user settings."""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder import __version__
from markdown_finder.app import FinderWindow, configure_application
from markdown_finder.diagnostics import (
    DiagnosticInterface, EVENT_LIMIT, MAX_REQUEST_BYTES, query_endpoint, query_running, running_instances,
)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Diagnostic condition timed out")


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.window = FinderWindow(self.root, settings=self.settings, auto_scan=False)
        self.interface = DiagnosticInterface(self.window, directory=self.root / "diagnostics")
        self.window._diagnostics = self.interface
        self.window.show()
        QTest.qWait(250)

    def tearDown(self):
        self.interface.stop()
        self.window.close()
        wait_until(lambda: not self.window.isVisible() and not self.window._background_busy())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    def request(self, command):
        responses = []
        errors = []
        def query():
            try:
                responses.append(query_endpoint(self.interface.path, command))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=query)
        worker.start()
        wait_until(lambda: not worker.is_alive())
        worker.join()
        if errors:
            raise errors[0]
        return responses[0]

    def raw_request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.interface.path))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            reply = bytearray()
            while b"\n" not in reply:
                reply.extend(connection.recv(65536))
            return json.loads(reply.partition(b"\n")[0])

    def test_snapshot_reads_live_gui_guard_and_reader_without_mutation(self):
        path = self.root / "fixture.md"
        path.write_text("# Fixture\n\n" + "Text\n\n" * 350, encoding="utf-8")
        reader = self.window.open_document(path)
        wait_until(lambda: reader.loaded)
        reader._set_zoom(1.3)
        scrolled = []
        reader.page.runJavaScript("window.scrollTo(0, 600); true", scrolled.append)
        wait_until(lambda: scrolled and reader._css_scroll_position()[1] > 590)
        geometry = self.window.geometry()
        native_id = self.window.internalWinId()
        settings = {key: self.settings.value(key) for key in self.settings.allKeys()}
        main_thread = threading.get_ident()
        original = self.interface._snapshot
        def checked_snapshot():
            self.assertEqual(threading.get_ident(), main_thread)
            return original()
        with patch.object(self.interface, "_snapshot", side_effect=checked_snapshot):
            response = self.request("snapshot")
        self.assertTrue(response["ok"])
        data = response["data"]
        self.assertEqual(data["process"]["version"], __version__)
        self.assertEqual(data["process"]["pid"], os.getpid())
        self.assertEqual(data["window"]["native_id"], int(native_id))
        self.assertEqual(data["window"]["guard"]["maximized_intent"], self.window.isMaximized())
        self.assertTrue(data["screens"])
        self.assertAlmostEqual(data["readers"][0]["zoom"], 1.3)
        self.assertAlmostEqual(data["readers"][0]["css_scroll"]["y"], 600, delta=5)
        self.assertEqual(self.window.geometry(), geometry)
        self.assertEqual(self.window.internalWinId(), native_id)
        self.assertEqual({key: self.settings.value(key) for key in self.settings.allKeys()}, settings)

    def test_activation_and_diagnostic_queries_preserve_maximized_size(self):
        self.window.showMaximized()
        QTest.qWait(450)
        size = self.window.size()
        states = []
        self.window.windowHandle().windowStateChanged.connect(states.append)
        for _ in range(2):
            QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowDeactivate))
            QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
            self.app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
            self.assertTrue(self.request("snapshot")["data"]["window"]["maximized"])
        QTest.qWait(1600)
        self.assertEqual(states, [])
        self.assertEqual(self.window.size(), size)

    def test_owner_permissions_discovery_and_stale_endpoint(self):
        self.assertEqual(stat.S_IMODE(self.interface.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.interface.path.stat().st_mode), 0o600)
        stale = self.interface.directory / "debug-99999999.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.bind(str(stale))
        instances = running_instances(self.interface.directory)
        self.assertEqual([item["pid"] for item in instances], [os.getpid()])
        response = query_running("ping", directory=self.interface.directory)
        self.assertTrue(response["ok"])
        self.assertTrue(stale.exists())

    def test_unknown_malformed_and_oversized_commands_are_rejected(self):
        geometry = self.window.geometry()
        for payload, error in [
            (b'{"command":"recover"}\n', "unknown_command"),
            (b'{"command":"snapshot","code":"x"}\n', "invalid_request"),
            (b'[]\n', "invalid_request"),
            (b'not-json\n', "invalid_json"),
            (b'{"command":"ping"}', "incomplete_request"),
            (b'x' * (MAX_REQUEST_BYTES + 1) + b'\n', "request_too_large"),
        ]:
            with self.subTest(error=error):
                response = self.raw_request(payload)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], error)
        self.assertEqual(self.window.geometry(), geometry)
        self.assertTrue(query_endpoint(self.interface.path, "ping")["ok"])

    def test_threads_events_and_gui_timeout_work_without_gui_event_pumping(self):
        self.interface._gui_timeout = 0.15
        threads = query_endpoint(self.interface.path, "threads")
        self.assertTrue(threads["ok"])
        self.assertTrue(any(item["name"] == "MainThread" and item["stack"] for item in threads["data"]))
        events = query_endpoint(self.interface.path, "events")
        self.assertTrue(events["ok"])
        self.assertTrue(events["data"])
        response = query_endpoint(self.interface.path, "snapshot")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "gui_timeout")
        self.assertTrue(response["threads"])
        self.assertIn("window", response["last_event"])
        # An expired queued request is discarded when the event loop resumes.
        with patch.object(self.interface, "_snapshot", wraps=self.interface._snapshot) as snapshot:
            QApplication.processEvents()
            snapshot.assert_not_called()

    def test_recent_event_history_is_bounded_and_records_state(self):
        for _ in range(EVENT_LIMIT + 10):
            QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
        events = query_endpoint(self.interface.path, "events")["data"]
        self.assertEqual(len(events), EVENT_LIMIT)
        self.assertEqual(events[-1]["event"], "WindowActivate")
        self.assertGreater(events[0]["sequence"], 1)
        self.assertEqual(events[-1]["window"]["state"], self.window.windowState().value)

    def test_close_removes_endpoint_and_stops_diagnostic_thread(self):
        self.window.close()
        wait_until(lambda: not self.window.isVisible())
        self.assertFalse(self.interface.path.exists())
        self.assertFalse(self.interface._thread.is_alive())
        self.assertEqual(running_instances(self.interface.directory), [])

    def test_existing_service_and_unsafe_directory_are_not_overwritten(self):
        with self.assertRaises(OSError):
            DiagnosticInterface(self.window, directory=self.interface.directory)
        self.assertTrue(query_endpoint(self.interface.path, "ping")["ok"])
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaises(PermissionError):
            DiagnosticInterface(self.window, directory=unsafe)
        link = self.root / "link"
        link.symlink_to(self.interface.directory)
        with self.assertRaises(PermissionError):
            DiagnosticInterface(self.window, directory=link)

    def test_cli_queries_running_instance_without_display_or_new_gui(self):
        env = dict(os.environ, XDG_RUNTIME_DIR=str(self.root))
        self.assertEqual(self.interface.directory, self.root / "diagnostics")
        # Use the standard discoverable name for the standalone CLI client.
        self.interface.stop()
        self.interface = DiagnosticInterface(self.window, directory=self.root / "markdown-finder")
        self.window._diagnostics = self.interface
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        result = subprocess.run([sys.executable, "-m", "markdown_finder", "--debug", "list",
                                 "--root", str(self.root / "missing")],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)["data"]
        self.assertEqual(data[0]["version"], __version__)
        self.assertEqual(data[0]["pid"], os.getpid())

    def test_main_starts_interface_for_external_cli_with_isolated_settings(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=str(self.root / "config"),
                   XDG_CACHE_HOME=str(self.root / "cache"), XDG_DATA_HOME=str(self.root / "data"),
                   QT_QPA_PLATFORM="offscreen")
        source = (
            "import sys\n"
            "from PySide6.QtCore import QTimer\n"
            "from markdown_finder import app\n"
            "OriginalWindow = app.FinderWindow\n"
            "class FixtureWindow(OriginalWindow):\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        super().__init__(*args, **kwargs)\n"
            "        QTimer.singleShot(6000, self.close)\n"
            "app.FinderWindow = FixtureWindow\n"
            "raise SystemExit(app.main(['--root', sys.argv[1]]))\n"
        )
        process = subprocess.Popen([sys.executable, "-c", source, str(self.root)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            endpoint = runtime / "markdown-finder" / f"debug-{process.pid}.sock"
            wait_until(lambda: endpoint.exists() or process.poll() is not None)
            result = subprocess.run([sys.executable, "-m", "markdown_finder", "--debug", "snapshot",
                                     "--debug-pid", str(process.pid)],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)["data"]
            self.assertEqual(data["process"]["pid"], process.pid)
            self.assertEqual(data["process"]["version"], __version__)
            self.assertTrue(data["window"]["visible"])
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse(endpoint.exists())
            self.assertTrue(list((self.root / "config").rglob("*.conf")))
        finally:
            if process.poll() is None:
                process.terminate()  # Only the isolated test fixture, never the user's reader.
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
