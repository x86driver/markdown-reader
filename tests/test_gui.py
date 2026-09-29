"""Exercise real Qt widgets/workers without opening user files or applications."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder.app import FinderWindow, configure_application
from markdown_finder.engine import FileEntry, SearchHit, SearchResults
from markdown_finder.workers import SearchThread


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Qt condition did not complete before timeout")


class FinderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("cpu/docs/pipeline.md", "cpu/README.md", ".hidden/設計筆記.MD", "notes/readme.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Fixture\n", encoding="utf-8")
        self.opened = []
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.window = FinderWindow(
            self.root, settings=self.settings,
            opener=lambda url: self.opened.append(url.toLocalFile()) or True,
        )
        self.window.show()
        wait_until(lambda: self.window._scan is None and self.window._results_current)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window.isVisible() and not self.window.search_worker.isRunning())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    def search(self, query):
        self.window.search_input.setText(query)
        wait_until(lambda: self.window._results_current and not self.window.query_timer.isActive())

    def test_scan_and_multitoken_search_open_copy_keyboard(self):
        self.assertEqual(self.window.model.rowCount(), 3)
        self.search("cpu pipe")
        self.assertEqual(self.window.model.rowCount(), 1)
        expected = str(self.root / "cpu/docs/pipeline.md")
        self.assertEqual(self.window.selected_entry().path, expected)
        QTest.keyClick(self.window.search_input, Qt.Key.Key_Return)
        wait_until(lambda: self.window.current_reader() is not None and self.window.current_reader().loaded)
        self.assertEqual(str(self.window.current_reader().path), expected)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.settings.value("recent_files")[0], expected)
        QTest.keyClick(self.window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(QApplication.clipboard().text(), expected)
        QTest.keyClick(self.window, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
        self.assertIs(self.window.tabs.currentWidget(), self.window.finder_panel)
        self.assertTrue(self.window.search_input.hasFocus())
        self.assertEqual(self.window.search_input.selectedText(), "cpu pipe")

    def test_navigation_escape_and_unicode(self):
        self.search("設計")
        self.assertEqual(self.window.selected_entry().name, "設計筆記.MD")
        QTest.keyClick(self.window.search_input, Qt.Key.Key_Escape)
        wait_until(lambda: self.window._results_current and self.window.model.rowCount() == 3)
        QTest.keyClick(self.window.search_input, Qt.Key.Key_Down)
        self.assertEqual(self.window.results.currentIndex().row(), 1)
        QTest.keyClick(self.window.search_input, Qt.Key.Key_Up)
        self.assertEqual(self.window.results.currentIndex().row(), 0)

    def test_query_change_cannot_open_stale_selection(self):
        self.search("pipeline")
        self.window.search_input.setText("unfindable")
        self.window.open_selected()
        self.assertEqual(self.opened, [])
        wait_until(lambda: self.window._results_current)
        self.assertEqual(self.window.model.rowCount(), 0)
        self.assertFalse(self.window.open_button.isEnabled())

    def test_rapid_queries_and_stale_result_rejection(self):
        for query in ("pipe", "設", "notfound", "cpu", "設計"):
            self.window.search_input.setText(query)
        old_id = self.window._request_id - 1
        fake = FileEntry("/not/a/real/path.md", "old.md", "old.md")
        self.window._search_completed(old_id, SearchResults([SearchHit(fake, 100)], 1), 0.01)
        wait_until(lambda: self.window._results_current)
        self.assertEqual(self.window.model.rowCount(), 1)
        self.assertEqual(self.window.selected_entry().name, "設計筆記.MD")

    def test_refresh_adds_and_removes_files_and_missing_open_is_safe(self):
        self.search("pipeline")
        (self.root / "cpu/docs/pipeline.md").unlink()
        self.window.open_selected()
        self.assertEqual(self.opened, [])
        self.assertIn("已移動或刪除", self.window.selected_path.text())
        (self.root / "new.md").write_text("new", encoding="utf-8")
        self.window.start_scan()
        wait_until(lambda: self.window._scan is None and self.window._results_current)
        self.assertEqual(self.window.model.rowCount(), 0)
        self.search("new")
        self.assertEqual(self.window.selected_entry().name, "new.md")

    def test_close_cooperatively_stops_running_scan(self):
        entered = threading.Event()

        def slow_scan(root, *, cancelled, on_batch, on_progress):
            from markdown_finder.engine import ScanStats
            entered.set()
            while not cancelled():
                time.sleep(0.005)
            return ScanStats(cancelled=True)

        with patch("markdown_finder.workers.scan_markdown", side_effect=slow_scan):
            self.window.start_scan()
            wait_until(entered.is_set)
            self.window.close()
            wait_until(lambda: not self.window.isVisible() and not self.window.search_worker.isRunning())
            self.assertTrue(self.window._scan is None or not self.window._scan.isRunning())

    def test_search_mailbox_cancels_old_work(self):
        started = threading.Event()
        observed = []
        worker = SearchThread()

        def slow_search(entries, query, *, limit, cancelled, recent):
            if query == "old":
                started.set()
                while not cancelled():
                    time.sleep(0.005)
                return SearchResults([], 0, True)
            return SearchResults([], 42)

        worker.completed.connect(lambda request_id, result, elapsed: observed.append((request_id, result.total_matches)))
        with patch("markdown_finder.workers.search_files", side_effect=slow_search):
            worker.start()
            try:
                worker.submit(1, "old", [])
                wait_until(started.is_set)
                worker.submit(2, "new", [])
                wait_until(lambda: bool(observed))
                self.assertEqual(observed, [(2, 42)])
            finally:
                worker.stop()
                self.assertTrue(worker.wait(2000))

    def test_streaming_batches_do_not_starve_slow_searches(self):
        completed = []

        def slow_search(entries, query, *, limit, cancelled, recent):
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                if cancelled():
                    return SearchResults([], 0, True)
                time.sleep(0.005)
            completed.append(len(entries))
            return SearchResults([SearchHit(entries[0], 100)], len(entries))

        with patch("markdown_finder.workers.search_files", side_effect=slow_search):
            self.window.search_input.setText("slow")
            started = time.monotonic()
            while time.monotonic() - started < 1.1:
                batch = [FileEntry(f"/fixture/{len(completed)}.md", "extra.md", "extra.md")]
                self.window._batch_ready(self.window._scan_generation, batch)
                QTest.qWait(40)
            self.assertGreaterEqual(len(completed), 1, "stream updates cancelled every search")
            self.assertGreater(self.window.model.rowCount(), 0)
            self.window.stream_timer.stop()
            self.window.search_worker.invalidate()


if __name__ == "__main__":
    unittest.main()
