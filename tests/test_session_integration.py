"""Persist real reader tabs across complete window/settings lifetimes."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder.app import FinderWindow, configure_application
from markdown_finder.reader import DocumentTab


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Session condition timed out")


def javascript(tab, source):
    values = []
    tab.page.runJavaScript(source, values.append)
    wait_until(lambda: bool(values))
    return values[0]


class SessionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-session-")
        self.root = Path(self.tmp.name)
        self.settings_path = self.root / "settings.ini"
        self.first = self.root / "第一份 文件.md"
        self.second = self.root / "second.md"
        self.third = self.root / "closed.md"
        for path in (self.first, self.second, self.third):
            path.write_text(
                f"# {path.stem}\n\n"
                + "\n\n".join(
                    f"## 第 {i} 節\n\n閱讀進度 {i}：這是一段足以產生捲動範圍的 Markdown 文件內容。"
                    for i in range(90)
                ),
                encoding="utf-8",
            )
        self.window = None

    def tearDown(self):
        self.close_window()
        self.tmp.cleanup()

    def settings(self):
        return QSettings(str(self.settings_path), QSettings.Format.IniFormat)

    def new_window(self):
        self.window = FinderWindow(self.root, settings=self.settings(), auto_scan=False)
        self.window.show()
        return self.window

    def close_window(self):
        if self.window is None:
            return
        window, self.window = self.window, None
        window.close()
        wait_until(lambda: not window.isVisible() and not window._background_busy())
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(30)

    def open_document(self, path):
        tab = self.window.open_document(path)
        self.assertIsNotNone(tab)
        wait_until(lambda: tab.loaded)
        return tab

    def documents(self):
        return [
            widget
            for index in range(self.window.tabs.count())
            if isinstance(widget := self.window.tabs.widget(index), DocumentTab)
        ]

    def scroll_to(self, tab, y):
        javascript(tab, f"window.scrollTo(0, {y}); true")
        wait_until(lambda: abs(javascript(tab, "window.scrollY") - y) < 3)
        wait_until(lambda: abs(tab.page.scrollPosition().y() / tab.view.zoomFactor() - y) < 3)

    def write_session(self, value):
        settings = self.settings()
        settings.setValue("reader_session", json.dumps(value, ensure_ascii=False))
        settings.sync()

    def test_close_reopen_restores_order_active_reader_and_reading_state(self):
        self.new_window()
        first = self.open_document(self.first)
        first._set_zoom(1.4)
        first.splitter.setSizes([310, 700])
        self.scroll_to(first, 1300)
        outline_width = first.splitter.sizes()[0]

        second = self.open_document(self.second)
        second.outline_button.setChecked(False)
        second.fit_width_button.setChecked(True)
        wait_until(lambda: abs(second.view.zoomFactor() - second.view.width() / 948) < 0.01)
        self.scroll_to(second, 850)

        self.open_document(self.third)
        self.window.close_current_tab()
        wait_until(lambda: not self.window._closing_documents)
        self.window.tabs.tabBar().moveTab(self.window.tabs.indexOf(second), 0)
        self.window.tabs.setCurrentWidget(first)
        self.close_window()

        # Recreate QSettings too, so the assertion exercises the persisted file.
        saved = json.loads(self.settings().value("reader_session"))
        self.assertEqual(saved["version"], 1)
        self.assertEqual([tab["path"] for tab in saved["tabs"]], [str(self.second), str(self.first)])
        self.assertEqual(saved["active_path"], str(self.first))
        self.assertEqual(saved["search_tab_index"], 1)
        self.new_window()
        restored = self.documents()
        self.assertEqual([tab.path for tab in restored], [self.second, self.first])
        self.assertEqual(self.window.tabs.indexOf(self.window.finder_panel), 1)
        self.assertIs(self.window.current_reader(), restored[1])
        wait_until(lambda: all(tab.loaded for tab in restored))

        second, first = restored
        self.assertAlmostEqual(first.view.zoomFactor(), 1.4)
        self.assertFalse(first.fit_width_button.isChecked())
        self.assertTrue(first.outline_button.isChecked())
        self.assertAlmostEqual(first.splitter.sizes()[0], outline_width, delta=2)
        wait_until(lambda: not first.session_restore_pending)
        wait_until(lambda: abs(javascript(first, "window.scrollY") - 1300) < 5)
        self.window.tabs.setCurrentWidget(second)
        self.assertTrue(second.fit_width_button.isChecked())
        self.assertFalse(second.outline_button.isChecked())
        wait_until(lambda: abs(second.view.zoomFactor() - second.view.width() / 948) < 0.01)
        wait_until(lambda: not second.session_restore_pending)
        wait_until(lambda: abs(javascript(second, "window.scrollY") - 850) < 5)

    def test_search_tab_and_last_reader_restore_then_explicit_files_take_focus(self):
        self.new_window()
        self.open_document(self.first)
        second = self.open_document(self.second)
        self.scroll_to(second, 640)
        self.window.focus_search()
        self.window.save_session()
        saved = json.loads(self.settings().value("reader_session"))
        self.assertIsNone(saved["active_path"])
        self.assertEqual(saved["last_reader_path"], str(self.second))
        self.close_window()
        self.new_window()
        self.assertIs(self.window.tabs.currentWidget(), self.window.finder_panel)
        wait_until(lambda: all(tab.loaded for tab in self.documents()))
        self.window.escape_action()
        self.assertEqual(self.window.current_reader().path, self.second)
        wait_until(lambda: not self.window.current_reader().session_restore_pending)
        wait_until(lambda: abs(javascript(self.window.current_reader(), "window.scrollY") - 640) < 5)

        # main() opens CLI files after construction; restored tabs must deduplicate
        # while the last explicit file, whether new or restored, wins selection.
        existing = self.window.open_document(self.first)
        self.assertIs(self.window.current_reader(), existing)
        self.assertEqual(len(self.documents()), 2)
        self.window.open_document(self.third)
        self.assertEqual(self.window.current_reader().path, self.third)
        self.assertEqual(len(self.documents()), 3)

    def test_missing_duplicate_and_invalid_saved_entries_are_skipped(self):
        alias = self.root / "alias.md"
        alias.symlink_to(self.first)
        non_markdown = self.root / "plain.txt"
        non_markdown.write_text("plain", encoding="utf-8")
        self.write_session({
            "version": 1,
            "tabs": [
                {"path": str(self.root / "missing.md")},
                None,
                {"path": str(self.first), "zoom": "invalid", "scroll_y": -12},
                {"path": str(alias)},
                {"path": str(non_markdown)},
                {"path": str(self.second)},
            ],
            "active_path": str(self.second),
            "last_reader_path": str(self.first),
            "search_tab_index": 1,
        })
        self.new_window()
        self.assertEqual([tab.path for tab in self.documents()], [self.first, self.second])
        self.assertEqual(self.window.current_reader().path, self.second)
        self.assertEqual(self.window.tabs.indexOf(self.window.finder_panel), 0)
        wait_until(lambda: all(tab.loaded for tab in self.documents()))
        self.assertAlmostEqual(self.documents()[0].view.zoomFactor(), 1.0)
        self.assertGreaterEqual(self.documents()[0].page.scrollPosition().y(), 0)

    def test_missing_active_file_falls_back_to_reader_and_escape_returns_to_it(self):
        missing = str(self.root / "missing.md")
        self.write_session({
            "version": 1,
            "tabs": [{"path": missing}, {"path": str(self.first)}],
            "active_path": missing,
            "last_reader_path": missing,
            "search_tab_index": 0,
        })
        self.new_window()
        self.assertEqual(self.window.current_reader().path, self.first)
        self.window.focus_search()
        self.window.escape_action()
        self.assertEqual(self.window.current_reader().path, self.first)

    def test_corrupt_or_unsupported_session_opens_search_safely(self):
        for value in ("{broken", "[]", '{"version": 999, "tabs": []}', '{"version": 1, "tabs": false}'):
            with self.subTest(value=value):
                settings = self.settings()
                settings.setValue("reader_session", value)
                settings.sync()
                self.new_window()
                self.assertEqual(self.window.tabs.count(), 1)
                self.assertIs(self.window.tabs.currentWidget(), self.window.finder_panel)
                self.close_window()

    def test_closing_during_restore_keeps_unloaded_reading_position(self):
        self.write_session({
            "version": 1,
            "tabs": [{
                "path": str(self.first), "zoom": 1.3, "fit_width": False,
                "scroll_x": 0, "scroll_y": 1200,
                "outline_visible": True, "outline_width": 230,
            }],
            "active_path": str(self.first),
            "last_reader_path": str(self.first),
            "search_tab_index": 0,
        })
        self.new_window()
        self.assertFalse(self.window.current_reader().loaded)
        self.close_window()
        saved = json.loads(self.settings().value("reader_session"))
        self.assertAlmostEqual(saved["tabs"][0]["scroll_y"], 1200)
        self.new_window()
        first = self.window.current_reader()
        wait_until(lambda: first.loaded)
        self.assertAlmostEqual(first.view.zoomFactor(), 1.3)
        wait_until(lambda: not first.session_restore_pending)
        wait_until(lambda: abs(javascript(first, "window.scrollY") - 1200) < 5)


if __name__ == "__main__":
    unittest.main()
