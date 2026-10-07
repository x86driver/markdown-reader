"""Desktop work-area conversion and bounded asynchronous queries."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QProcess, QRect, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder.desktop_workarea import DesktopWorkAreaProbe, _areas, _map_area, _monitors


MONITORS = """Monitors: 2
 0: +*DP-6 3840/630x2160/360+0+0  DP-6
 1: +DP-5 3840/630x2160/360+3840+0  DP-5
"""
AREAS = """_NET_CURRENT_DESKTOP(CARDINAL) = 0
_GTK_WORKAREAS_D0(CARDINAL) = 3840, 0, 3840, 2160, 0, 64, 3840, 1964
"""


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Work-area query did not finish")


class WorkAreaMappingTests(unittest.TestCase):
    def test_captured_primary_dock_and_unrestricted_secondary_at_double_scale(self):
        monitors, areas = _monitors(MONITORS), _areas(AREAS, 0)
        self.assertEqual(_map_area("DP-6", QRect(0, 0, 1920, 1080), monitors, areas),
                         QRect(0, 32, 1920, 982))
        self.assertEqual(_map_area("DP-5", QRect(3840, 0, 1920, 1080), monitors, areas),
                         QRect(3840, 0, 1920, 1080))

    def test_mixed_scale_preserves_qt_origins_and_rounds_inward(self):
        monitors = _monitors(" 0: +DP-1 2560/600x1440/340-2560+0  DP-1\n")
        area = _map_area("DP-1", QRect(-1707, 0, 1707, 960), monitors,
                         [QRect(-2560, 47, 2560, 1301)])
        self.assertEqual(area, QRect(-1707, 32, 1707, 866))

    def test_negative_cardinal_coordinates_and_output_alias(self):
        areas = _areas("_NET_CURRENT_DESKTOP(CARDINAL) = 2\n"
                       "_GTK_WORKAREAS_D2(CARDINAL) = 4294964736, 30, 2560, 1410", 2)
        monitors = _monitors(" 0: +monitor-group 2560/600x1440/340-2560+0  DP-1\n")
        self.assertEqual(_map_area("DP-1", QRect(-2560, 0, 2560, 1440), monitors, areas),
                         QRect(-2560, 30, 2560, 1410))

    def test_missing_current_desktop_uses_workspace_zero_like_gtk(self):
        output = AREAS.replace("_NET_CURRENT_DESKTOP(CARDINAL) = 0",
                               "_NET_CURRENT_DESKTOP:  not found.")
        self.assertEqual(_map_area("DP-6", QRect(0, 0, 1920, 1080),
                                   _monitors(MONITORS), _areas(output, 0)),
                         QRect(0, 32, 1920, 982))

    def test_workspace_race_missing_properties_and_invalid_numbers_are_rejected(self):
        self.assertEqual(_areas(AREAS, 1), [])
        self.assertEqual(_areas(AREAS.replace(" = 0\n", " = 1\n"), 0), [])
        self.assertEqual(_areas(AREAS.replace("1964", "0"), 0), [])
        self.assertEqual(_areas(AREAS.replace("1964", str(2**32)), 0), [])
        self.assertEqual(_areas(AREAS.replace("1964", str(2**31)), 0), [])
        self.assertEqual(_areas("_GTK_WORKAREAS_D0: not found.", 0), [])
        self.assertEqual(_monitors(" 0: +DP-6 9999999999/1x2160/1+0+0  DP-6"), {})

    def test_unknown_ambiguous_or_mismatched_monitor_is_not_guessed(self):
        monitors, areas = _monitors(MONITORS), _areas(AREAS, 0)
        self.assertIsNone(_map_area("unknown", QRect(0, 0, 1920, 1080), monitors, areas))
        self.assertIsNone(_map_area("DP-6", QRect(0, 0, 1280, 1024), monitors, areas))
        self.assertIsNone(_map_area("DP-6", QRect(0, 0, 1920, 1080), monitors,
                                   areas + [QRect(0, 0, 3840, 2160)]))


class WorkAreaProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-workarea-")
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "data.json"
        self.fixture.write_text(json.dumps({"desktop": 0, "areas": AREAS, "monitors": MONITORS}))
        script = (
            f"#!{sys.executable}\n"
            "import json, pathlib, sys, time\n"
            f"data = json.loads(pathlib.Path({str(self.fixture)!r}).read_text())\n"
            "time.sleep(data.get('delay', 0))\n"
            "if data.get('fail'): sys.exit(1)\n"
            "if '--listmonitors' in sys.argv: print(data['monitors'])\n"
            "elif any(arg.startswith('_GTK_WORKAREAS_D') for arg in sys.argv): print(data['areas'])\n"
            "else: print('_NET_CURRENT_DESKTOP(CARDINAL) = ' + str(data['desktop']))\n"
        )
        for name in ("xprop", "xrandr"):
            path = self.root / name
            path.write_text(script)
            path.chmod(0o755)
        with patch("markdown_finder.desktop_workarea.shutil.which",
                   side_effect=lambda name: str(self.root / name)), \
                patch.object(QApplication, "platformName", return_value="wayland"), \
                patch.dict(os.environ, {"DISPLAY": ":fixture"}):
            self.probe = DesktopWorkAreaProbe()
        self.screen = SimpleNamespace(
            name=lambda: "DP-6", geometry=lambda: QRect(0, 0, 1920, 1080),
            availableGeometry=lambda: QRect(0, 0, 1920, 1080),
        )

    def tearDown(self):
        self.probe.stop()
        wait_until(lambda: self.probe._process.state() == QProcess.ProcessState.NotRunning)
        self.tmp.cleanup()

    def test_query_coalesces_requests_and_expires_stale_layouts(self):
        responses = []
        self.probe.ready.connect(lambda: responses.append(self.probe.area(self.screen)))
        self.probe.request()
        self.assertIsNone(self.probe.area(self.screen))
        for _ in range(10):
            self.probe.request()
        wait_until(lambda: responses)
        self.assertEqual(responses, [QRect(0, 32, 1920, 982)])
        self.assertEqual(self.probe.snapshot(self.screen)["source"], "_GTK_WORKAREAS")
        self.probe.request()
        QTest.qWait(40)
        self.assertEqual(len(responses), 1)
        with patch.object(self.probe, "_layout_key", return_value=("different",)):
            self.assertIsNone(self.probe.area(self.screen))
        self.probe._sampled_at -= 3
        self.assertIsNone(self.probe.area(self.screen))

    def test_current_workspace_selects_the_matching_property(self):
        self.fixture.write_text(json.dumps({"desktop": 2, "areas": AREAS.replace(" = 0\n", " = 2\n")
                                            .replace("_D0", "_D2"), "monitors": MONITORS}))
        self.probe.request()
        wait_until(lambda: not self.probe.pending)
        self.assertEqual(self.probe.area(self.screen), QRect(0, 32, 1920, 982))

    def test_failed_query_returns_to_qt_without_work_area(self):
        self.fixture.write_text('{"fail": true}')
        self.probe.request()
        wait_until(lambda: not self.probe.pending)
        self.assertIsNone(self.probe.area(self.screen))
        self.assertEqual(self.probe.snapshot(self.screen)["error"], "query_failed")

    def test_timeout_keeps_gui_responsive_and_close_cancels_child(self):
        self.fixture.write_text('{"delay": 10}')
        self.probe._deadline.setInterval(100)
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        self.probe.request()
        wait_until(lambda: not self.probe.pending)
        timer.stop()
        self.assertGreater(len(ticks), 2)
        self.assertEqual(self.probe.snapshot(self.screen)["error"], "query_timeout")
        wait_until(lambda: self.probe._process.state() == QProcess.ProcessState.NotRunning)
        self.probe._last_finished = float("-inf")
        self.probe.request()
        responses = []
        self.probe.ready.connect(lambda: responses.append(True))
        self.probe.stop()
        wait_until(lambda: self.probe._process.state() == QProcess.ProcessState.NotRunning)
        self.assertEqual(responses, [])

    def test_missing_tools_disable_probe(self):
        with patch("markdown_finder.desktop_workarea.shutil.which", return_value=None):
            probe = DesktopWorkAreaProbe()
        try:
            probe.request()
            self.assertFalse(probe.enabled)
            self.assertFalse(probe.pending)
            self.assertIsNone(probe.area(self.screen))
        finally:
            probe.stop()


if __name__ == "__main__":
    unittest.main()
