"""Version information stays available both without a display and in the reader."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder import __version__
from markdown_finder.app import FinderWindow, configure_application, main


class VersionCliTests(unittest.TestCase):
    def test_version_exits_before_root_validation_or_gui_creation(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            missing_root = str(Path(temporary) / "missing")
            with contextlib.redirect_stdout(output), patch("markdown_finder.app.QApplication") as application:
                with self.assertRaises(SystemExit) as exited:
                    main(["--root", missing_root, "--version"])
        self.assertEqual(exited.exception.code, 0)
        application.assert_not_called()
        self.assertEqual(output.getvalue(), f"markdown-finder {__version__}\n")

    def test_module_version_works_without_a_display(self):
        environment = os.environ.copy()
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
            environment.pop(name, None)
        result = subprocess.run(
            [sys.executable, "-m", "markdown_finder", "--version"],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"markdown-finder {__version__}\n")


class VersionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def test_permanent_button_and_about_use_application_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
            window = FinderWindow(root, settings=settings, auto_scan=False)
            try:
                window.show()
                QApplication.processEvents()
                self.assertEqual(self.app.applicationVersion(), __version__)
                self.assertEqual(window.about_button.text(), f"關於 v{__version__}")
                window.statusBar().showMessage("測試狀態訊息")
                QApplication.processEvents()
                self.assertTrue(window.about_button.isVisible())
                with patch("markdown_finder.app.QMessageBox.about") as about:
                    QTest.mouseClick(window.about_button, Qt.MouseButton.LeftButton)
                about.assert_called_once_with(
                    window, "關於 Markdown Reader",
                    f"Markdown Reader\n\n版本：{__version__}\n套件／啟動指令：markdown-finder",
                )
                self.assertEqual(window.windowTitle(), "Markdown Reader")
                self.assertEqual(self.app.applicationName(), "MarkdownFinder")
            finally:
                window.close()
                deadline = time.monotonic() + 5
                while window.isVisible() and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertFalse(window.search_worker.isRunning())
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
