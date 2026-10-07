"""Read GNOME's per-monitor work areas through XWayland without blocking Qt.

Wayland's QScreen.availableGeometry() can include docks. Mutter also publishes
the current workspace's _GTK_WORKAREAS_Dn on the X root window. Match those
rectangles to named RandR outputs, then convert monitor-local offsets to Qt
coordinates; scaling global origins would be wrong on mixed-DPI desktops.
"""

import math
import os
import re
import shutil
import sys
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QRect, QTimer, Signal
from PySide6.QtWidgets import QApplication


def _cardinals(output: str, name: str) -> list[int] | None:
    match = re.search(rf"^{re.escape(name)}\(CARDINAL\) = (.+)$", output, re.MULTILINE)
    if not match or not re.fullmatch(r"\d+(?:,\s*\d+)*", match[1]):
        return None
    values = [int(value) for value in match[1].split(",")]
    return values if all(value < 2**32 for value in values) else None


def _monitors(output: str) -> dict[str, QRect]:
    monitors = {}
    for line in output.splitlines():
        match = re.fullmatch(
            r"\s*\d+:\s+[+*]*([^\s]+)\s+(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)\s+(.+)",
            line,
        )
        if match:
            name, width, height, x, y, outputs = match.groups()
            x, y, width, height = int(x), int(y), int(width), int(height)
            if not (-2**31 <= x < 2**31 and -2**31 <= y < 2**31
                    and 0 < width < 2**31 and 0 < height < 2**31):
                continue
            rect = QRect(x, y, width, height)
            if rect.isValid():
                for output_name in (name, *outputs.split()):
                    monitors[output_name] = rect
    return monitors


def _desktop(output: str) -> int | None:
    current = _cardinals(output, "_NET_CURRENT_DESKTOP")
    if current is not None:
        return current[0] if len(current) == 1 else None
    if re.search(r"^_NET_CURRENT_DESKTOP:\s+not found\.$", output, re.MULTILINE):
        # Mutter on Wayland can omit this hint. GTK uses workspace 0 then.
        return 0
    return None


def _areas(output: str, desktop: int) -> list[QRect]:
    values = _cardinals(output, f"_GTK_WORKAREAS_D{desktop}")
    if not values or len(values) % 4 or _desktop(output) != desktop:
        return []
    if any(not 0 < values[index + offset] < 2**31
           for index in range(0, len(values), 4) for offset in (2, 3)):
        return []
    # Coordinates are signed 32-bit values stored in CARDINAL properties.
    def signed(value):
        return value - 2**32 if value >= 2**31 else value

    areas = [
        QRect(signed(values[index]), signed(values[index + 1]),
              values[index + 2], values[index + 3])
        for index in range(0, len(values), 4)
    ]
    return areas if all(area.isValid() for area in areas) else []


def _map_area(name: str, geometry: QRect, monitors: dict[str, QRect],
              areas: list[QRect]) -> QRect | None:
    native = monitors.get(name)
    if native is None or not geometry.isValid():
        return None
    # Reject stale/unrelated output dimensions rather than guessing a monitor.
    if abs(native.width() * geometry.height() - native.height() * geometry.width()) > max(
        native.width(), native.height()
    ):
        return None
    matches = {area.getRect() for area in areas if native.contains(area)}
    if len(matches) != 1:
        return None
    area = QRect(*matches.pop())
    sx, sy = geometry.width() / native.width(), geometry.height() / native.height()
    left = math.ceil((area.x() - native.x()) * sx)
    top = math.ceil((area.y() - native.y()) * sy)
    right = math.floor((area.x() + area.width() - native.x()) * sx)
    bottom = math.floor((area.y() + area.height() - native.y()) * sy)
    result = QRect(geometry.x() + left, geometry.y() + top, right - left, bottom - top)
    return result if result.isValid() else None


class DesktopWorkAreaProbe(QObject):
    """Optional, coalesced reads with a deadline and no shell or GUI wait."""

    ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._xprop = shutil.which("xprop")
        self._xrandr = shutil.which("xrandr")
        self.enabled = bool(sys.platform.startswith("linux") and os.environ.get("DISPLAY")
                            and QApplication.platformName().startswith("wayland")
                            and self._xprop and self._xrandr)
        self._stopped = False
        self._stage = None
        self._layout = None
        self._monitors = {}
        self._areas = []
        self._desktop = 0
        self._last_finished = float("-inf")
        self._sampled_at = float("-inf")
        self._error = None
        self._process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("LC_ALL", "C")
        self._process.setProcessEnvironment(environment)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._failed)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(1000)
        self._deadline.timeout.connect(self._timeout)

    @staticmethod
    def _layout_key():
        return tuple(sorted((screen.name(), screen.geometry().getRect())
                            for screen in QApplication.screens()))

    @property
    def pending(self):
        return self._stage is not None

    def request(self):
        if (not self.enabled or self._stopped or self._stage is not None
                or self._process.state() != QProcess.ProcessState.NotRunning
                or time.monotonic() - self._last_finished < 1.0):
            return
        self._layout = self._layout_key()
        self._monitors = {}
        self._areas = []
        self._deadline.start()
        self._stage = "monitors"
        self._process.start(self._xrandr, ["--listmonitors"])

    def area(self, screen) -> QRect | None:
        if (self._stopped or self._stage is not None or not self._areas
                or time.monotonic() - self._sampled_at > 2.0
                or self._layout != self._layout_key()):
            return None
        area = _map_area(screen.name(), screen.geometry(), self._monitors, self._areas)
        if area is not None:
            area = area.intersected(screen.availableGeometry())
            if area.isValid():
                return area
        return None

    def _finished(self, exit_code, exit_status):
        if self._stage is None or self._stopped:
            return
        output = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if exit_code or exit_status != QProcess.ExitStatus.NormalExit or len(output) > 65536:
            self._complete("query_failed")
        elif self._stage == "monitors":
            self._monitors = _monitors(output)
            if not self._monitors:
                self._complete("monitor_names_unavailable")
                return
            self._stage = "desktop"
            self._process.start(self._xprop, ["-root", "_NET_CURRENT_DESKTOP"])
        elif self._stage == "desktop":
            current = _desktop(output)
            if current is None:
                self._complete("workspace_unavailable")
                return
            self._desktop = current
            self._stage = "areas"
            self._process.start(self._xprop, ["-root", "_NET_CURRENT_DESKTOP",
                                             f"_GTK_WORKAREAS_D{self._desktop}"])
        else:
            self._areas = _areas(output, self._desktop)
            self._complete(None if self._areas else "work_area_unavailable")

    def _failed(self, error):
        if self._stage is not None and error == QProcess.ProcessError.FailedToStart:
            self._complete("query_failed")

    def _timeout(self):
        self._complete("query_timeout")
        self._process.kill()

    def _complete(self, error):
        self._deadline.stop()
        self._stage = None
        self._last_finished = time.monotonic()
        self._error = error
        if error:
            self._monitors = {}
            self._areas = []
        else:
            self._sampled_at = self._last_finished
        if not self._stopped:
            self.ready.emit()

    def snapshot(self, screen) -> dict:
        area = self.area(screen) if screen is not None else None
        last = (_map_area(screen.name(), screen.geometry(), self._monitors, self._areas)
                if screen is not None and self._layout == self._layout_key() else None)
        return {
            "enabled": self.enabled, "pending": self._stage is not None,
            "source": "_GTK_WORKAREAS" if area is not None else "QScreen",
            "area": dict(zip(("x", "y", "width", "height"), area.getRect())) if area else None,
            "last_sampled_area": dict(zip(("x", "y", "width", "height"), last.getRect())) if last else None,
            "sample_age_seconds": round(time.monotonic() - self._sampled_at, 3)
            if math.isfinite(self._sampled_at) else None,
            "error": self._error,
        }

    def stop(self):
        self._stopped = True
        self._stage = None
        self._deadline.stop()
        self._process.kill()
