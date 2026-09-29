"""Native window lifecycle regression for the first WebEngine reader tab.

Run explicitly with QT_QPA_PLATFORM=wayland (or xcb) on a desktop. The
offscreen/minimal platforms cannot reproduce a native window being replaced.
"""

from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder.app import FinderWindow, configure_application
from markdown_finder.reader import DocumentTab


def wait_until(predicate, timeout=10, message="Native reader condition timed out"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError(message)


def javascript(tab, source):
    values = []
    tab.page.runJavaScript(source, values.append)
    wait_until(lambda: bool(values))
    return values[0]


class ObservedFinderWindow(FinderWindow):
    """Observe only the main widget, without reentering native window APIs."""

    def __init__(self, *args, **kwargs):
        self.window_transitions = []
        super().__init__(*args, **kwargs)

    def event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.Hide, QEvent.Type.Show, QEvent.Type.WinIdChange):
            self.window_transitions.append(kind.name)
        return super().event(event)


class NativeWindowSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if cls.app.platformName() not in ("wayland", "wayland-egl", "xcb"):
            raise unittest.SkipTest("Requires a native Wayland or X11 desktop")
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-native-surface-")
        self.root = Path(self.tmp.name)
        self.settings_path = self.root / "settings.ini"
        self.first = self.root / "first.md"
        self.second = self.root / "第二份.md"
        for path in (self.first, self.second):
            path.write_text(
                f"# {path.stem}\n\n"
                + "\n\n".join(
                    f"## Section {index}\n\n閱讀進度測試 {index}：本機 Markdown 文件。"
                    for index in range(70)
                ),
                encoding="utf-8",
            )
        self.window = None

    def tearDown(self):
        self.close_window()
        self.tmp.cleanup()

    def show_window(self):
        settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)
        self.window = ObservedFinderWindow(self.root, settings=settings, auto_scan=False)
        self.window.show()
        # Snapshot before pumping events: restored readers begin loading
        # asynchronously and must not replace even the first visible surface.
        self.native_id = int(self.window.winId())
        self.surface_type = self.window.windowHandle().surfaceType()
        self.window.window_transitions.clear()
        # isExposed() may stay false while the desktop is locked or the window
        # is occluded. Native ID/lifecycle changes still occur in those cases,
        # so they remain useful regression evidence without unlocking a screen.
        self.assert_stable_window()
        return self.window

    def close_window(self):
        if self.window is None:
            return
        window, self.window = self.window, None
        window.close()
        wait_until(lambda: not window.isVisible() and not window._background_busy())
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(40)

    def assert_stable_window(self):
        # loadFinished precedes some compositor work. Also catch a replacement
        # queued after the document has reported that it finished loading.
        QTest.qWait(250)
        self.assertTrue(self.window.isVisible())
        self.assertEqual(
            int(self.window.winId()), self.native_id,
            f"Main-window lifecycle events: {self.window.window_transitions}",
        )
        self.assertEqual(self.window.windowHandle().surfaceType(), self.surface_type)
        self.assertEqual(self.window.window_transitions, [])

    def open_document(self, path):
        tab = self.window.open_document(path)
        self.assertIsNotNone(tab)
        wait_until(lambda: tab.loaded)
        self.assert_stable_window()
        return tab

    def documents(self):
        return [
            widget
            for index in range(self.window.tabs.count())
            if isinstance(widget := self.window.tabs.widget(index), DocumentTab)
        ]

    def scroll_to(self, tab, position):
        javascript(tab, f"window.scrollTo(0, {position}); true")
        wait_until(lambda: abs(javascript(tab, "window.scrollY") - position) < 3)
        wait_until(
            lambda: abs(tab.page.scrollPosition().y() / tab.view.zoomFactor() - position) < 3
        )

    def test_first_second_and_reopened_reader_preserve_native_window(self):
        self.show_window()
        self.assertEqual(self.window.tabs.count(), 1)
        self.assertIs(self.window.tabs.currentWidget(), self.window.finder_panel)
        self.open_document(self.first)
        self.open_document(self.second)

        for tab in self.documents():
            self.window.close_document_tab(self.window.tabs.indexOf(tab))
        wait_until(lambda: not self.window._closing_documents)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.window.tabs.count(), 1)
        self.assert_stable_window()
        self.open_document(self.first)

    def test_restored_session_preserves_layout_and_initial_native_window(self):
        self.show_window()
        first = self.open_document(self.first)
        first._set_zoom(1.3)
        first.splitter.setSizes([280, 760])
        self.scroll_to(first, 900)
        outline_width = first.splitter.sizes()[0]

        second = self.open_document(self.second)
        second.outline_button.setChecked(False)
        second.fit_width_button.setChecked(True)
        wait_until(lambda: abs(second.view.zoomFactor() - second.view.width() / 948) < 0.01)
        self.scroll_to(second, 650)
        self.window.tabs.tabBar().moveTab(self.window.tabs.indexOf(second), 0)
        self.window.tabs.setCurrentWidget(first)
        self.close_window()

        self.show_window()
        restored = self.documents()
        self.assertEqual([tab.path for tab in restored], [self.second, self.first])
        self.assertEqual(self.window.tabs.indexOf(self.window.finder_panel), 1)
        second, first = restored
        self.assertIs(self.window.current_reader(), first)
        wait_until(lambda: all(tab.loaded for tab in restored))
        wait_until(lambda: not first.session_restore_pending)
        self.assertAlmostEqual(first.view.zoomFactor(), 1.3)
        self.assertTrue(first.outline_button.isChecked())
        self.assertAlmostEqual(first.splitter.sizes()[0], outline_width, delta=2)
        wait_until(lambda: abs(javascript(first, "window.scrollY") - 900) < 5)
        self.assert_stable_window()

        self.window.tabs.setCurrentWidget(second)
        wait_until(lambda: not second.session_restore_pending)
        self.assertTrue(second.fit_width_button.isChecked())
        self.assertFalse(second.outline_button.isChecked())
        wait_until(lambda: abs(second.view.zoomFactor() - second.view.width() / 948) < 0.01)
        wait_until(lambda: abs(javascript(second, "window.scrollY") - 650) < 5)
        self.assert_stable_window()


if __name__ == "__main__":
    unittest.main()
