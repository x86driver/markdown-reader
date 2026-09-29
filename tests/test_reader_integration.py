"""Real WebEngine and main-window integration using generated local fixtures."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder.app import FinderWindow, configure_application


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Reader condition timed out")


def javascript(tab, source):
    result = []
    tab.view.page().runJavaScript(source, lambda value: result.append(value))
    wait_until(lambda: bool(result))
    return result[0]


class ReaderIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        configure_application(cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-integration-")
        self.root = Path(self.tmp.name)
        self.first = self.root / "first.md"
        self.second = self.root / "second 中文.md"
        (self.root / "assets").mkdir()
        (self.root / "assets/中文 圖.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="80"><rect width="160" height="80" fill="blue"/></svg>',
            encoding="utf-8",
        )
        self.first.write_text(
            '# 中文閱讀測試\n\n[跳至章節](#section) · [Second](second%20%E4%B8%AD%E6%96%87.md#details) · [Web](https://example.invalid/reader)\n\n'
            '| 名稱 | 數量 |\n|---|---|\n| 測試 | 42 |\n\n'
            '![圖片](assets/%E4%B8%AD%E6%96%87%20%E5%9C%96.svg)\n\n'
            '```python\nprint("hello")\n```\n\n'
            + ('一段可搜尋的中文內容 Reader needle。\n\n' * 80)
            + '\n## section\n\n最後一節。\n', encoding="utf-8",
        )
        self.second.write_text('# Second document\n\n## details\n\n第二份文件。\n', encoding="utf-8")
        self.external_urls = []
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.window = FinderWindow(self.root, settings=settings, auto_scan=False,
                                   opener=lambda url: self.external_urls.append(url.toString()) or True)
        self.window.show()

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window.isVisible() and not self.window._background_busy())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(40)
        self.tmp.cleanup()

    def open_first(self):
        tab = self.window.open_document(self.first)
        wait_until(lambda: tab.loaded)
        return tab

    def test_rendered_dom_relative_image_and_same_file_deduplication(self):
        tab = self.open_first()
        self.assertEqual(javascript(tab, "document.querySelector('h1').textContent"), "中文閱讀測試")
        self.assertEqual(javascript(tab, "document.querySelectorAll('table tbody tr').length"), 1)
        wait_until(lambda: javascript(tab, "document.querySelector('img').naturalWidth") == 160)
        self.assertIn("print", javascript(tab, "document.querySelector('pre').textContent"))
        again = self.window.open_document(self.root / "." / "first.md")
        self.assertIs(again, tab)
        self.assertEqual(self.window.tabs.count(), 2)

    def test_cross_document_and_external_links(self):
        first = self.open_first()
        javascript(first, "document.querySelector('a[href*=second]').click(); true")
        wait_until(lambda: self.window.current_reader() is not first and self.window.current_reader().loaded)
        self.assertEqual(self.window.current_reader().path, self.second)
        self.assertEqual(self.window.tabs.count(), 3)
        self.window.tabs.setCurrentWidget(first)
        javascript(first, "document.querySelector('a[href^=https]').click(); true")
        wait_until(lambda: bool(self.external_urls))
        self.assertEqual(self.external_urls, ["https://example.invalid/reader"])
        self.assertIs(self.window.current_reader(), first)
        javascript(first, "document.querySelector('a[href=\"#section\"]').click(); true")
        wait_until(lambda: javascript(first, "window.scrollY") > 100)
        self.assertEqual(self.window.tabs.count(), 3)

    def test_search_tab_shortcuts_zoom_and_close_tab(self):
        tab = self.open_first()
        QTest.keyClick(self.window, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
        self.assertIs(self.window.tabs.currentWidget(), self.window.finder_panel)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertIs(self.window.current_reader(), tab)
        original = tab.view.zoomFactor()
        QTest.keyClick(self.window, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier)
        self.assertGreater(tab.view.zoomFactor(), original)
        QTest.keyClick(self.window, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(tab.view.zoomFactor(), 1.0)
        QTest.keyClick(self.window, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: not self.window._closing_documents)
        self.assertEqual(self.window.tabs.count(), 1)
        self.assertEqual(self.window._documents, {})

    def test_reload_updates_content_and_preserves_scroll(self):
        tab = self.open_first()
        javascript(tab, "window.scrollTo(0, 600); true")
        wait_until(lambda: javascript(tab, "window.scrollY") >= 500)
        original = javascript(tab, "window.scrollY")
        text = self.first.read_text(encoding="utf-8").replace("中文閱讀測試", "更新後的標題")
        self.first.write_text(text, encoding="utf-8")
        tab.reload_document()
        wait_until(lambda: tab.loaded and javascript(tab, "document.querySelector('h1').textContent") == "更新後的標題")
        wait_until(lambda: abs(javascript(tab, "window.scrollY") - original) < 10)

    def test_page_shortcuts_cycle_tabs_with_webengine_and_search_focus(self):
        first = self.open_first()
        second = self.window.open_document(self.second)
        wait_until(lambda: second.loaded)

        def press(key, expected):
            reader = self.window.current_reader()
            target = ((reader.view.focusProxy() or reader.view) if reader
                      else self.window.search_input)
            target.setFocus()
            wait_until(target.hasFocus)
            QTest.keyClick(target, key, Qt.KeyboardModifier.ControlModifier)
            wait_until(lambda: self.window.tabs.currentWidget() is expected)

        press(Qt.Key.Key_PageUp, first)
        press(Qt.Key.Key_PageUp, self.window.finder_panel)
        press(Qt.Key.Key_PageUp, second)
        press(Qt.Key.Key_PageDown, self.window.finder_panel)
        press(Qt.Key.Key_PageDown, first)
        press(Qt.Key.Key_PageDown, second)

    def test_closing_a_loading_tab_leaves_search_window_alive(self):
        self.window.open_document(self.first)
        self.window.close_current_tab()
        wait_until(lambda: not self.window._closing_documents)
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.tabs.count(), 1)

    def test_reopen_updates_recency_but_background_reload_does_not(self):
        first = self.open_first()
        second = self.window.open_document(self.second)
        wait_until(lambda: second.loaded)
        self.assertEqual(self.window.recent[0], str(self.second))
        self.window.open_document(self.first)
        self.assertIs(self.window.current_reader(), first)
        self.assertEqual(self.window.recent[0], str(self.first))
        second.reload_document()
        wait_until(lambda: second.loaded)
        self.assertEqual(self.window.recent[0], str(self.first))


if __name__ == "__main__":
    unittest.main()
