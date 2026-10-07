"""Recover desktop window bounds without replacing the native surface."""

import sys
import time

from PySide6.QtCore import QObject, QEvent, QSize, QTimer, Qt, SLOT, Slot
from PySide6.QtWidgets import QApplication, QWidget

from .desktop_workarea import DesktopWorkAreaProbe


_SLEEP_SIGNAL = (
    "org.freedesktop.login1", "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager", "PrepareForSleep",
)


class WindowGeometryGuard(QObject):
    """Keep the reader windowed and reapply desktop bounds after resume.

    Wayland does not expose reliable global positions or dock work areas;
    GNOME's optional XWayland work-area hints supplement QScreen's bounds.
    Maximized windows must be sized by the compositor, rather than by setting
    their geometry to QScreen.availableGeometry().
    """

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.window = window
        self._stopped = False
        self._remaximizing = False
        self._refresh_maximized = False
        self._retry_refresh = False
        self._maximized = window.isMaximized()
        self._normal_geometry = window.normalGeometry()
        self._restore_normal = False
        self._minimum_size = window.minimumSize()
        self._screen = None
        self._handle = None
        self._sleep_connection = None
        self._last_repair_key = None
        self._last_repair_at = float("-inf")
        self._work_area_probe = DesktopWorkAreaProbe(self)
        self._work_area_probe.ready.connect(self._schedule_check)
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(200)
        self._check_timer.timeout.connect(self._recover)
        # Outputs and their scale/work area can settle after the first check.
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(1200)
        self._retry_timer.timeout.connect(self._retry_check)
        self._maximize_timer = QTimer(self)
        self._maximize_timer.setSingleShot(True)
        self._maximize_timer.setInterval(100)
        self._maximize_timer.timeout.connect(self._finish_maximize)

        # Old settings may already contain the unsupported fullscreen state.
        self._leave_fullscreen()
        window.installEventFilter(self)
        app = QApplication.instance()
        app.applicationStateChanged.connect(self._application_state_changed)
        app.screenAdded.connect(self._desktop_changed)
        app.screenRemoved.connect(self._desktop_changed)
        app.primaryScreenChanged.connect(self._desktop_changed)
        self._watch_screen(window.screen())
        if sys.platform.startswith("linux") and app.platformName() in {
            "wayland", "wayland-egl", "xcb",
        }:
            from PySide6.QtDBus import QDBusConnection

            bus = QDBusConnection.systemBus()
            if bus.connect(*_SLEEP_SIGNAL, self, SLOT("_prepare_for_sleep(bool)")):
                self._sleep_connection = bus

    def eventFilter(self, watched, event):
        if self._stopped or watched is not self.window:
            return False
        kind = event.type()
        if kind == QEvent.Type.Show:
            handle = self.window.windowHandle()
            if handle is not None and handle is not self._handle:
                self._handle = handle
                handle.screenChanged.connect(self._desktop_changed)
            self._schedule_check()
        elif kind == QEvent.Type.WindowActivate:
            self._schedule_check()
            self._retry_timer.start()
        elif kind == QEvent.Type.WindowStateChange:
            if not self._remaximizing:
                state = self.window.windowState()
                if not state & (Qt.WindowState.WindowFullScreen | Qt.WindowState.WindowMinimized):
                    self._maximized = bool(state & Qt.WindowState.WindowMaximized)
                    if not self._maximized and event.oldState() & Qt.WindowState.WindowMaximized:
                        self._restore_normal = True
                    elif self._maximized and not event.oldState() & (
                        Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
                    ):
                        self._normal_geometry = self.window.normalGeometry()
                self._schedule_check()
        elif kind in (QEvent.Type.Resize, QEvent.Type.ScreenChangeInternal):
            self._remember_normal_geometry()
            if self._remaximizing and self._maximize_timer.isActive():
                self._maximize_timer.start()
            else:
                self._schedule_check()
        elif kind == QEvent.Type.Move:
            self._remember_normal_geometry()
        return False

    def _remember_normal_geometry(self):
        if not self._remaximizing and not self._restore_normal and not self.window.windowState() & (
            Qt.WindowState.WindowFullScreen | Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized
        ):
            self._normal_geometry = self.window.geometry()

    def _restore_normal_geometry(self):
        if self._normal_geometry.isValid():
            if QApplication.platformName().startswith("wayland"):
                self.window.resize(self._normal_geometry.size())
            else:
                self.window.setGeometry(self._normal_geometry)
        self._restore_normal = False

    def _watch_screen(self, screen):
        if screen is self._screen:
            return
        if self._screen is not None:
            self._screen.geometryChanged.disconnect(self._desktop_changed)
            self._screen.availableGeometryChanged.disconnect(self._desktop_changed)
            self._screen.logicalDotsPerInchChanged.disconnect(self._desktop_changed)
            self._screen.destroyed.disconnect(self._screen_destroyed)
        self._screen = screen
        if screen is not None:
            screen.geometryChanged.connect(self._desktop_changed)
            screen.availableGeometryChanged.connect(self._desktop_changed)
            screen.logicalDotsPerInchChanged.connect(self._desktop_changed)
            screen.destroyed.connect(self._screen_destroyed)

    def _screen_destroyed(self):
        self._screen = None
        self._desktop_changed()

    def _desktop_changed(self, *args):
        if self._stopped:
            return
        # screenRemoved is emitted before Qt moves windows to another output.
        # Rebind on the deferred check, after that transition has completed.
        self._refresh_maximized = True
        self._retry_refresh = True
        self._schedule_check()
        self._retry_timer.start()

    def _application_state_changed(self, state):
        if state == Qt.ApplicationState.ApplicationActive:
            self._schedule_check()
            if not self._stopped:
                self._retry_timer.start()

    @Slot(bool)
    def _prepare_for_sleep(self, sleeping):
        if not sleeping:
            self._desktop_changed()

    def _schedule_check(self):
        if not self._stopped and not self._remaximizing:
            self._check_timer.start()

    def _retry_check(self):
        self._refresh_maximized |= self._retry_refresh
        self._retry_refresh = False
        self._schedule_check()

    def _leave_fullscreen(self):
        state = self.window.windowState()
        if state & Qt.WindowState.WindowFullScreen:
            # Passing directly from fullscreen to maximized makes Qt replace
            # normalGeometry with the fullscreen size. Restore normal first.
            self._remaximizing = self._maximized
            self._restore_normal = True
            state &= ~(Qt.WindowState.WindowFullScreen | Qt.WindowState.WindowMaximized)
            self.window.setWindowState(state)
            if self._maximized:
                if self._stopped or not self.window.isVisible():
                    self._restore_normal_geometry()
                    self.window.setWindowState(state | Qt.WindowState.WindowMaximized)
                    self._remaximizing = False
                else:
                    self._maximize_timer.start()
            return True
        return False

    def _recover(self):
        window = self.window
        if self._stopped or not window.isVisible() or window.isMinimized():
            return
        self._watch_screen(window.screen())
        if self._leave_fullscreen():
            # Let the native configure restore the windowed size before
            # checking it against the current screen.
            self._schedule_check()
            return
        screen = window.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        if not available.isValid():
            return
        self._work_area_probe.request()
        if self._work_area_probe.pending:
            # Do not force a resume round trip before the desktop query can
            # confirm whether this maximized window actually needs one.
            return
        desktop_area = self._work_area_probe.area(screen)
        if desktop_area is not None:
            available = desktop_area
        if not window.isMaximized() and self._restore_normal:
            self._restore_normal_geometry()
        frame = window.frameGeometry()
        extra_width = max(0, frame.width() - window.width())
        extra_height = max(0, frame.height() - window.height())
        client_space = QSize(
            max(1, available.width() - extra_width),
            max(1, available.height() - extra_height),
        )
        window.setMinimumSize(self._minimum_size.boundedTo(client_space))
        refresh, self._refresh_maximized = self._refresh_maximized, False
        if window.isMaximized():
            # Compare the whole frame, including Qt's client-side decoration.
            # A couple of logical pixels tolerate fractional-scale rounding.
            oversized = (frame.width() > available.width() + 2
                         or frame.height() > available.height() + 2)
            if desktop_area is None and QApplication.platformName().startswith("wayland"):
                oversized |= window.height() >= screen.geometry().height()
            if oversized or (refresh and desktop_area is None):
                key = (id(screen), available.getRect())
                now = time.monotonic()
                # A compositor still settling after unlock may resend the bad
                # size. Do not enter an endless maximize/resize flash loop.
                if key == self._last_repair_key and now - self._last_repair_at < 2.0:
                    return
                self._last_repair_key, self._last_repair_at = key, now
                self._remaximizing = True
                window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMaximized)
                self._maximize_timer.start()
            return

        size = window.size().boundedTo(client_space)
        oversized = size != window.size()
        if oversized:
            window.resize(size)
        if QApplication.platformName().startswith("wayland"):
            if oversized:
                # Let the compositor reserve dock/panel space and place the
                # window on this output. Global move() is unavailable here.
                window.setWindowState(window.windowState() | Qt.WindowState.WindowMaximized)
            else:
                self._remember_normal_geometry()
            return
        frame = window.frameGeometry()
        left = min(max(frame.left(), available.left()), available.right() - frame.width() + 1)
        top = min(max(frame.top(), available.top()), available.bottom() - frame.height() + 1)
        if (left, top) != (frame.left(), frame.top()):
            window.move(left, top)
        self._remember_normal_geometry()

    def _finish_maximize(self):
        if not self._stopped and self.window.isVisible() and not self.window.isMinimized():
            # A Wayland configure may suggest a different normal size during
            # the round trip. Keep the user's size for later unmaximizing.
            self._restore_normal_geometry()
            self.window.setWindowState(self.window.windowState() | Qt.WindowState.WindowMaximized)
        self._remaximizing = False
        self._schedule_check()

    def stop(self):
        """Cancel pending repairs before close and sanitize saved geometry."""
        self._stopped = True
        self._check_timer.stop()
        self._retry_timer.stop()
        self._maximize_timer.stop()
        self._work_area_probe.stop()
        self._leave_fullscreen()
        if self._remaximizing:
            self._restore_normal_geometry()
            self.window.setWindowState(self.window.windowState() | Qt.WindowState.WindowMaximized)
        elif self._restore_normal and not self.window.isMaximized():
            self._restore_normal_geometry()
        if self._sleep_connection is not None:
            self._sleep_connection.disconnect(*_SLEEP_SIGNAL, self, SLOT("_prepare_for_sleep(bool)"))
            self._sleep_connection = None
