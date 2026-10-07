"""Window state and work-area recovery with isolated widgets/settings."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, QSettings, QSize, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from markdown_finder.app import FinderWindow, configure_application
from markdown_finder.window_geometry import WindowGeometryGuard


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Window bounds did not recover")


class DesktopScreen(QObject):
    geometryChanged = Signal(QRect)
    availableGeometryChanged = Signal(QRect)
    logicalDotsPerInchChanged = Signal(float)

    def __init__(self, area=QRect(0, 30, 1920, 970)):
        super().__init__()
        self.area = QRect(area)

    def geometry(self):
        return QRect(self.area)

    def availableGeometry(self):
        return QRect(self.area)


class WindowGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def setUp(self):
        self.window = QMainWindow()
        self.window.setMinimumSize(760, 540)
        self.window.setGeometry(130, 120, 900, 620)
        self.screen = DesktopScreen()
        self.screen_patch = patch.object(self.window, "screen", return_value=self.screen)
        self.screen_mock = self.screen_patch.start()
        self.guard = WindowGeometryGuard(self.window)
        self.window.show()
        QTest.qWait(250)

    def tearDown(self):
        self.guard.stop()
        self.window.close()
        self.screen_patch.stop()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_unexpected_fullscreen_returns_to_normal_size(self):
        geometry = self.window.geometry()
        self.window.showFullScreen()
        self.assertTrue(self.window.isFullScreen())
        wait_until(lambda: not self.window.isFullScreen())
        QTest.qWait(250)
        self.assertFalse(self.window.isMaximized())
        self.assertEqual(self.window.geometry(), geometry)

    def test_fullscreen_from_maximized_restores_maximized_state(self):
        self.window.showMaximized()
        QTest.qWait(250)
        normal = self.window.normalGeometry()
        self.window.showFullScreen()
        wait_until(lambda: not self.window.isFullScreen() and self.window.isMaximized())
        self.window.showNormal()
        self.assertEqual(self.window.geometry(), normal)

    def test_work_area_change_fits_frame_and_keeps_negative_screen_origin(self):
        self.screen.area = QRect(-1920, 30, 1920, 970)
        self.window.setGeometry(-1400, 100, 900, 620)
        QApplication.processEvents()
        original = self.window.geometry()
        self.screen.availableGeometryChanged.emit(self.screen.area)
        QTest.qWait(350)
        self.assertEqual(self.window.geometry(), original)

        self.screen.area = QRect(-1280, 40, 1280, 760)
        self.window.setGeometry(-400, 600, 1200, 900)
        self.screen.availableGeometryChanged.emit(self.screen.area)
        wait_until(lambda: self.screen.area.contains(self.window.frameGeometry()))
        self.assertFalse(self.window.isMaximized())

    def test_small_work_area_temporarily_lowers_minimum_size(self):
        self.screen.area = QRect(0, 30, 640, 450)
        self.screen.geometryChanged.emit(self.screen.area)
        wait_until(lambda: self.screen.area.contains(self.window.frameGeometry()))
        self.assertLess(self.window.minimumWidth(), 760)
        self.assertLess(self.window.minimumHeight(), 540)
        self.screen.area = QRect(0, 30, 1920, 970)
        self.screen.geometryChanged.emit(self.screen.area)
        wait_until(lambda: self.window.minimumSize() == QSize(760, 540))

    def test_resume_reconfigures_maximized_window_without_hiding_it(self):
        self.window.showMaximized()
        QTest.qWait(250)
        native_id = int(self.window.winId())
        states = []
        self.window.windowHandle().windowStateChanged.connect(states.append)
        self.guard._prepare_for_sleep(True)
        QTest.qWait(250)
        self.assertEqual(states, [])
        self.guard._prepare_for_sleep(False)
        wait_until(lambda: Qt.WindowState.WindowNoState in states and self.window.isMaximized())
        self.assertEqual(int(self.window.winId()), native_id)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.isFullScreen())

    def test_activation_and_resume_preserve_healthy_normal_window(self):
        geometry = self.window.geometry()
        with patch.object(self.window, "setWindowState", wraps=self.window.setWindowState) as change:
            self.app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
            self.guard._prepare_for_sleep(False)
            QTest.qWait(1600)
            change.assert_not_called()
        self.assertEqual(self.window.geometry(), geometry)

    def test_reactivation_preserves_healthy_maximized_window_without_roundtrip(self):
        self.window.showMaximized()
        QTest.qWait(450)
        size = self.window.size()
        with patch.object(self.window, "setWindowState", wraps=self.window.setWindowState) as change:
            for _ in range(2):
                QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowDeactivate))
                QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
                self.app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
            QTest.qWait(1600)
            change.assert_not_called()
        self.assertTrue(self.window.isMaximized())
        self.assertEqual(self.window.size(), size)

    def test_recorded_dock_overflow_recovers_on_window_activation(self):
        # Qt's availableGeometry stays at 1080, exactly as in the live incident.
        self.screen.area = QRect(0, 0, 1920, 1080)
        self.window.showMaximized()
        QTest.qWait(450)
        native_id = int(self.window.winId())
        with patch.object(self.guard._work_area_probe, "area", return_value=QRect(0, 32, 1920, 982)), \
                patch.object(self.window, "frameGeometry", return_value=QRect(0, -38, 1920, 1048)), \
                patch.object(self.window, "setWindowState", wraps=self.window.setWindowState) as change:
            QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
            QTest.qWait(650)
            # Includes a second bad configure: the cooldown prevents a loop.
            self.assertEqual(change.call_count, 2)
            self.assertFalse(change.call_args_list[0].args[0] & Qt.WindowState.WindowMaximized)
            self.assertTrue(change.call_args_list[1].args[0] & Qt.WindowState.WindowMaximized)
        self.assertEqual(int(self.window.winId()), native_id)
        self.assertTrue(self.window.isVisible())

    def test_confirmed_healthy_work_area_avoids_activation_and_resume_roundtrip(self):
        self.screen.area = QRect(0, 0, 1920, 1080)
        self.window.showMaximized()
        QTest.qWait(450)
        with patch.object(self.guard._work_area_probe, "area", return_value=QRect(0, 32, 1920, 982)), \
                patch.object(self.window, "frameGeometry", return_value=QRect(0, -38, 1920, 982)), \
                patch.object(self.window, "setWindowState", wraps=self.window.setWindowState) as change:
            self.guard._prepare_for_sleep(False)
            QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
            self.app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
            QTest.qWait(1600)
            change.assert_not_called()

    def test_resume_waits_for_work_area_query_before_changing_window_state(self):
        self.window.showMaximized()
        QTest.qWait(450)
        with patch.object(type(self.guard._work_area_probe), "pending", new_callable=PropertyMock) as pending, \
                patch.object(self.window, "setWindowState", wraps=self.window.setWindowState) as change:
            pending.return_value = True
            self.guard._prepare_for_sleep(False)
            self.guard._recover()
            change.assert_not_called()
            self.assertTrue(self.guard._refresh_maximized)
            pending.return_value = False
            with patch.object(self.guard._work_area_probe, "area", return_value=self.screen.area):
                self.guard._recover()
                change.assert_not_called()
            self.assertFalse(self.guard._refresh_maximized)

    def test_resume_does_not_unminimize_or_reopen_closed_window(self):
        self.window.showMinimized()
        self.guard._prepare_for_sleep(False)
        QTest.qWait(1600)
        self.assertTrue(self.window.isMinimized())
        self.guard.stop()
        self.window.close()
        self.guard._prepare_for_sleep(False)
        self.app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
        QTest.qWait(300)
        self.assertFalse(self.window.isVisible())

    def test_destroyed_screen_can_be_replaced_before_deferred_recovery(self):
        replacement = DesktopScreen(QRect(0, 30, 1280, 750))
        self.screen_mock.return_value = replacement
        self.screen.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        wait_until(lambda: self.guard._screen is replacement)
        self.assertTrue(replacement.area.contains(self.window.frameGeometry()))
        self.guard.stop()

    def test_wayland_oversized_normal_window_uses_compositor_maximization(self):
        self.window.resize(2200, 1200)
        with patch.object(QApplication, "platformName", return_value="wayland"):
            self.screen.geometryChanged.emit(self.screen.area)
            wait_until(self.window.isMaximized)
        self.assertFalse(self.window.isFullScreen())


class SavedWindowGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def test_legacy_fullscreen_settings_and_immediate_close_are_sanitized(self):
        with tempfile.TemporaryDirectory(prefix="reader-window-state-") as temporary:
            root = Path(temporary)
            settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
            legacy = QMainWindow()
            legacy.resize(780, 600)
            legacy.showFullScreen()
            settings.setValue("window_geometry", legacy.saveGeometry())
            legacy.close()
            legacy.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

            for attempt in range(2):
                window = FinderWindow(root, settings=settings, auto_scan=False)
                try:
                    self.assertFalse(window.isFullScreen())
                    window.show()
                    if attempt == 0:
                        # Close before the deferred fullscreen repair runs.
                        window.showFullScreen()
                    window.close()
                    wait_until(lambda: not window.isVisible() and not window._background_busy())
                    self.assertFalse(window.isFullScreen())
                finally:
                    window.close()
                    wait_until(lambda: not window._background_busy())
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
