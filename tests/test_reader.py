"""Exercise real WebEngine rendering, links and asynchronous reader lifecycle."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from markdown_finder.reader import DocumentTab
from markdown_finder.rendering import render_document


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Reader condition did not complete before timeout")


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "閱讀 測試.md"
        self.path.write_text("# 閱讀測試\n\nHello needle.\n\n## 細節\n\nneedle again.\n", encoding="utf-8")
        self.external = []
        self.tab = DocumentTab(self.path, external_opener=lambda url: self.external.append(url) or True)
        self.tab.resize(1000, 700)
        self.tab.show()
        self.ready = []
        self.errors = []
        self.tab.document_ready.connect(self.ready.append)
        self.tab.error_occurred.connect(self.errors.append)
        wait_until(lambda: self.tab.loaded)

    def tearDown(self):
        self.tab.begin_close()
        wait_until(lambda: not self.tab.is_busy())
        self.tab.close()
        self.tab.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.tmp.cleanup()

    def javascript(self, script):
        result = []
        self.tab.page.runJavaScript(script, lambda value: result.append(value))
        wait_until(lambda: bool(result))
        return result[0]

    def replace(self, content):
        count = len(self.ready)
        self.path.write_text(content, encoding="utf-8")
        self.tab.reload_document()
        wait_until(lambda: len(self.ready) > count)

    def reopen(self, state, *, show=True, width=1000):
        self.tab.begin_close()
        wait_until(lambda: not self.tab.is_busy())
        self.tab.close()
        self.tab.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tab = DocumentTab(self.path)
        self.tab.resize(width, 700)
        self.tab.restore_session_state(state)
        if show:
            self.tab.show()
        wait_until(lambda: self.tab.loaded)
        if show:
            wait_until(lambda: not self.tab.session_restore_pending)

    def test_rendered_dom_outline_and_local_images(self):
        # A real PNG verifies <base> handling of relative paths with spaces.
        from PySide6.QtGui import QImage, QColor
        image = QImage(8, 6, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        image.save(str(self.root / "local image.png"))
        self.replace(
            "# 文件\n\n## 細節\n\n| 名稱 | 值 |\n| --- | --- |\n| A | 1 |\n\n"
            "```python\nprint('hello')\n```\n\n![圖](local%20image.png)\n"
        )
        self.assertEqual(self.tab.outline.topLevelItemCount(), 1)
        self.assertEqual(self.tab.outline.topLevelItem(0).child(0).text(0), "細節")
        self.assertEqual(self.javascript("document.querySelectorAll('table').length"), 1)
        self.assertEqual(self.javascript("document.querySelector('img').naturalWidth"), 8)
        self.assertIn("hello", self.javascript("document.querySelector('pre').innerText"))
        self.assertEqual(self.errors, [])

    def test_clicked_links_route_without_navigating_reader(self):
        self.replace(
            "# Links\n\n[Markdown](other%20file.md#細節)\n\n"
            "[Website](https://example.invalid/read)\n\n[Executable](run.sh)\n"
        )
        requested = []
        self.tab.open_markdown_requested.connect(lambda path, fragment: requested.append((path, fragment)))
        before = self.tab.view.url()
        self.javascript("document.querySelectorAll('a')[0].click(); true")
        wait_until(lambda: bool(requested))
        self.assertEqual(requested, [(self.root / "other file.md", "細節")])
        self.javascript("document.querySelectorAll('a')[1].click(); true")
        wait_until(lambda: bool(self.external))
        self.assertEqual(self.external[0].toString(), "https://example.invalid/read")
        self.javascript("document.querySelectorAll('a')[2].click(); true")
        QTest.qWait(80)
        self.assertEqual(self.tab.view.url(), before)
        self.assertEqual(len(self.external), 1)
        self.assertTrue(self.tab.loaded)

    def test_details_native_mouse_keyboard_and_markdown_dom(self):
        self.replace(
            '# 文件\n\n<details>\n<summary>展開 **說明**</summary>\n'
            '## 內文\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n'
            '```python\nprint("hello")\n```\n</details>\n'
            '<details open><summary>預設展開</summary>可見內容</details>\n'
        )
        self.assertEqual(self.javascript("document.querySelectorAll('details').length"), 2)
        self.assertFalse(self.javascript("document.querySelector('details').open"))
        self.assertTrue(self.javascript("document.querySelectorAll('details')[1].open"))
        self.assertEqual(self.javascript("document.querySelector('summary').parentElement.tagName"), "DETAILS")
        self.assertEqual(self.javascript("document.querySelector('summary strong').textContent"), "說明")
        self.assertEqual(self.javascript(
            "Array.from(document.querySelectorAll('details td')).map(td => td.textContent).join(',')"
        ), "1,2")
        position = self.javascript(
            "JSON.stringify((() => { const r = document.querySelector('summary').getBoundingClientRect();"
            " return [r.left + 35, r.top + r.height / 2]; })())"
        )
        x, y = json.loads(position)
        target = self.tab.view.focusProxy() or self.tab.view
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=QPoint(round(x), round(y)))
        wait_until(lambda: self.javascript("document.querySelector('details').open"))
        self.assertIn("hello", self.javascript("document.querySelector('details pre').innerText"))
        self.javascript("document.querySelector('summary').focus(); true")
        QTest.keyClick(target, Qt.Key.Key_Space)
        wait_until(lambda: not self.javascript("document.querySelector('details').open"))
        self.assertEqual(self.errors, [])

    def test_anchor_and_outline_navigation_open_nested_details(self):
        self.replace(
            '# 文件\n\n[跳至內部](#內部章節)\n\n' + '前言段落\n\n' * 30
            + '<details>\n<summary>外層</summary>\n<details>\n<summary>內層</summary>\n'
            '## 內部章節\n\n' + '內文段落\n\n' * 35
            + '</details>\n</details>\n## 外部章節\n\n' + '後記段落\n\n' * 30
        )
        item = self.tab.outline.topLevelItem(0).child(0)
        self.assertEqual(item.text(0), "內部章節")
        self.javascript("document.querySelector('a').click(); true")
        wait_until(lambda: self.javascript("Array.from(document.querySelectorAll('details')).every(d => d.open)"))
        wait_until(lambda: self.tab.outline.currentItem() is item)
        wait_until(lambda: 0 <= self.javascript("document.getElementById('內部章節').getBoundingClientRect().top") <= 49)
        self.javascript("document.querySelectorAll('details').forEach(d => d.open = false); window.scrollTo(0, 0); true")
        wait_until(lambda: self.tab.outline.currentItem() is self.tab.outline.topLevelItem(0))
        self.tab._outline_clicked(item)
        wait_until(lambda: self.javascript("Array.from(document.querySelectorAll('details')).every(d => d.open)"))
        wait_until(lambda: self.tab.outline.currentItem() is item)
        self.assertEqual(self.errors, [])

    def test_outline_ignores_hidden_details_headings_including_last_section(self):
        self.replace(
            '# 文件\n\n' + '前言段落\n\n' * 35
            + '<details>\n<summary>隱藏章節</summary>\n## 隱藏章節\n\n'
            + '內文段落\n\n' * 35 + '</details>\n## 可見章節\n\n'
            + '後記段落\n\n' * 35
            + '<details>\n<summary>隱藏尾章</summary>\n## 隱藏尾章\n\n結尾\n</details>\n'
        )
        root = self.tab.outline.topLevelItem(0)
        self.assertEqual(root.childCount(), 3)
        self.javascript("window.scrollTo(0, 0); true")
        wait_until(lambda: self.tab.outline.currentItem() is root)
        self.javascript("window.scrollTo(0, document.documentElement.scrollHeight); true")
        wait_until(lambda: self.tab.outline.currentItem() is root.child(1))
        self.assertEqual(self.tab.outline.currentItem().text(0), "可見章節")
        self.assertEqual(self.errors, [])

    def test_short_details_collapse_clears_hidden_chapter_highlight(self):
        self.replace('<details open>\n<summary>說明</summary>\n## 唯一章節\n\n短內文\n</details>\n')
        item = self.tab.outline.topLevelItem(0)
        wait_until(lambda: self.tab.outline.currentItem() is item)
        initial_height = self.javascript("document.documentElement.scrollHeight")
        position = self.javascript(
            "JSON.stringify((() => { const r = document.querySelector('summary').getBoundingClientRect();"
            " return [r.left + 35, r.top + r.height / 2]; })())"
        )
        x, y = json.loads(position)
        target = self.tab.view.focusProxy() or self.tab.view
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=QPoint(round(x), round(y)))
        wait_until(lambda: not self.javascript("document.querySelector('details').open"))
        wait_until(lambda: self.tab.outline.currentItem() is None)
        self.assertEqual(self.tab.outline.selectedItems(), [])
        self.assertEqual(self.javascript("document.documentElement.scrollHeight"), initial_height)
        self.javascript("document.querySelector('summary').focus(); true")
        QTest.keyClick(target, Qt.Key.Key_Return)
        wait_until(lambda: self.javascript("document.querySelector('details').open"))
        wait_until(lambda: self.tab.outline.currentItem() is item)

    def test_unicode_anchor_and_find_zoom(self):
        self.replace("# 開始\n\n[跳到細節](#細節)\n\n" + "段落 needle\n\n" * 90 + "## 細節\n\n結束 needle\n")
        self.javascript("document.querySelector('a').click(); true")
        wait_until(lambda: self.tab.page.scrollPosition().y() > 100)
        self.tab.focus_find()
        self.tab.find_input.setText("needle")
        wait_until(lambda: "/" in self.tab.find_count.text())
        self.assertTrue(self.tab.find_count.text().endswith("/ 91"))
        self.tab.zoom_in()
        self.assertAlmostEqual(self.tab.view.zoomFactor(), 1.1)
        self.tab.zoom_reset()
        self.assertAlmostEqual(self.tab.view.zoomFactor(), 1.0)
        self.assertTrue(self.tab.close_find())
        self.assertFalse(self.tab.close_find())

    def test_outline_follows_scrolling_down_and_up_without_moving_reader(self):
        self.replace("# Chapters\n\n" + "".join(
            f"## Section {index}\n\n" + "Paragraph content.\n\n" * 12
            for index in range(60)
        ))
        root = self.tab.outline.topLevelItem(0)
        for index in (48, 3):
            item = root.child(index)
            target = self.javascript(
                f"window.scrollTo(0, document.querySelectorAll('h2')[{index}]"
                ".getBoundingClientRect().top + window.scrollY); window.scrollY"
            )
            wait_until(lambda: self.tab.outline.currentItem() is item)
            wait_until(lambda: self.tab.outline.viewport().rect().contains(
                self.tab.outline.visualItemRect(item).center()
            ))
            QTest.qWait(150)
            self.assertAlmostEqual(self.javascript("window.scrollY"), target, delta=1)
        clicked = root.child(6)
        wait_until(lambda: self.tab.outline.viewport().rect().contains(
            self.tab.outline.visualItemRect(clicked).center()
        ))
        before_click = self.javascript("window.scrollY")
        QTest.mouseClick(
            self.tab.outline.viewport(), Qt.MouseButton.LeftButton,
            pos=self.tab.outline.visualItemRect(clicked).center(),
        )
        wait_until(lambda: self.tab.page.scrollPosition().y() > before_click + 100)
        wait_until(lambda: 0 <= self.javascript(
            "document.querySelectorAll('h2')[6].getBoundingClientRect().top"
        ) <= 49)
        QTest.qWait(150)
        self.assertIs(self.tab.outline.currentItem(), clicked)
        self.assertEqual(self.errors, [])

    def test_outline_reveals_collapsed_ancestors_of_current_section(self):
        self.replace(
            "# Root\n\n## First\n\n" + "Paragraph.\n\n" * 35
            + "## Parent\n\n" + "Paragraph.\n\n" * 35
            + "### Child\n\n" + "Paragraph.\n\n" * 35
            + "## End\n\n" + "Paragraph.\n\n" * 35
        )
        root = self.tab.outline.topLevelItem(0)
        parent = root.child(1)
        child = parent.child(0)
        root.setExpanded(False)
        parent.setExpanded(False)
        self.javascript(
            "window.scrollTo(0, document.querySelector('h3').getBoundingClientRect().top"
            " + window.scrollY); true"
        )
        wait_until(lambda: self.tab.outline.currentItem() is child)
        self.assertTrue(root.isExpanded())
        self.assertTrue(parent.isExpanded())
        wait_until(lambda: self.tab.outline.viewport().rect().contains(
            self.tab.outline.visualItemRect(child).center()
        ))

    def test_outline_matches_section_after_zoom_reload_and_session_restore(self):
        self.replace("# Chapters\n\n" + "".join(
            f"## Section {index}\n\n" + "Paragraph content.\n\n" * 25
            for index in range(14)
        ))
        for _ in range(5):
            self.tab.zoom_in()
        QTest.qWait(100)
        target = self.javascript(
            "window.scrollTo(0, document.querySelectorAll('h2')[8].getBoundingClientRect().top"
            " + window.scrollY + 80); window.scrollY"
        )
        wait_until(lambda: self.tab.outline.currentItem() is self.tab.outline.topLevelItem(0).child(8))
        state = self.tab.session_state()
        self.assertAlmostEqual(state["scroll_y"], target, delta=1)
        count = len(self.ready)
        self.tab.reload_document()
        wait_until(lambda: len(self.ready) > count)
        wait_until(lambda: self.tab.outline.currentItem() is self.tab.outline.topLevelItem(0).child(8))
        self.assertAlmostEqual(self.javascript("window.scrollY"), target, delta=1)
        self.reopen(state)
        wait_until(lambda: self.tab.outline.currentItem() is self.tab.outline.topLevelItem(0).child(8))
        self.assertAlmostEqual(self.tab.view.zoomFactor(), 1.5)
        self.assertAlmostEqual(self.javascript("window.scrollY"), target, delta=1)

    def test_reopened_outline_shows_section_reached_while_hidden(self):
        self.replace("# Chapters\n\n" + "".join(
            f"## Section {index}\n\n" + "Paragraph.\n\n" * 15
            for index in range(50)
        ))
        self.tab.outline_button.click()
        self.assertTrue(self.tab.outline.isHidden())
        self.javascript(
            "window.scrollTo(0, document.querySelectorAll('h2')[40].getBoundingClientRect().top"
            " + window.scrollY + 80); true"
        )
        wait_until(lambda: self.tab.page.scrollPosition().y() > 1000)
        self.tab.outline_button.click()
        item = self.tab.outline.topLevelItem(0).child(40)
        wait_until(lambda: self.tab.outline.currentItem() is item)
        wait_until(lambda: self.tab.outline.viewport().rect().contains(
            self.tab.outline.visualItemRect(item).center()
        ))

    def test_outline_distinguishes_repeated_headings_and_short_final_section(self):
        self.replace("# Chapters\n\n" + (
            "## Repeated\n\n" + "Paragraph.\n\n" * 35
        ) * 6 + "## Final\n\nEnd.\n")
        root = self.tab.outline.topLevelItem(0)
        repeated = root.child(4)
        self.javascript(
            "window.scrollTo(0, document.querySelectorAll('h2')[4].getBoundingClientRect().top"
            " + window.scrollY + 80); true"
        )
        wait_until(lambda: self.tab.outline.currentItem() is repeated)
        self.assertNotEqual(repeated.data(0, Qt.ItemDataRole.UserRole), root.child(3).data(0, Qt.ItemDataRole.UserRole))
        self.javascript("window.scrollTo(0, document.documentElement.scrollHeight); true")
        wait_until(lambda: self.tab.outline.currentItem() is root.child(6))
        self.assertEqual(self.tab.outline.currentItem().text(0), "Final")

    def test_outline_clears_when_reloaded_document_has_no_headings(self):
        wait_until(lambda: self.tab.outline.currentItem() is self.tab.outline.topLevelItem(0))
        self.replace("Plain paragraph.\n\n" * 80)
        self.javascript("window.scrollTo(0, 500); true")
        QTest.qWait(150)
        self.assertIsNone(self.tab.outline.currentItem())
        self.assertEqual(self.tab.outline.topLevelItemCount(), 0)
        self.assertFalse(self.tab.outline_button.isEnabled())
        self.assertTrue(self.tab.outline.isHidden())
        self.replace("# Headings return\n\n" + "Paragraph.\n\n" * 80)
        wait_until(lambda: self.tab.outline.currentItem() is self.tab.outline.topLevelItem(0))
        self.assertTrue(self.tab.outline_button.isEnabled())
        self.assertFalse(self.tab.outline.isHidden())

    def assert_fits_width(self):
        wait_until(lambda: abs(self.javascript(
            "document.querySelector('.markdown-body').getBoundingClientRect().width"
            " - document.documentElement.clientWidth"
        )) < 2)

    def test_fit_width_tracks_window_outline_and_splitter(self):
        self.tab.resize(1600, 700)
        QTest.mouseClick(self.tab.fit_width_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: self.tab.view.zoomFactor() > 1.0)
        self.assert_fits_width()
        wide_zoom = self.tab.view.zoomFactor()
        self.tab.resize(1100, 700)
        wait_until(lambda: self.tab.view.zoomFactor() < wide_zoom)
        self.assert_fits_width()
        narrow_zoom = self.tab.view.zoomFactor()
        self.tab.outline_button.click()
        wait_until(lambda: self.tab.view.zoomFactor() > narrow_zoom)
        self.assert_fits_width()
        self.tab.outline_button.click()
        self.tab.splitter.setSizes([480, 620])
        wait_until(lambda: self.tab.view.zoomFactor() < narrow_zoom)
        self.assert_fits_width()
        self.assertEqual(self.tab.zoom_button.text(), f"{self.tab.view.zoomFactor():.0%}")

    def test_manual_zoom_leaves_fit_width_and_stays_fixed_on_resize(self):
        for manual_zoom in (self.tab.zoom_in, self.tab.zoom_out, self.tab.zoom_reset):
            self.tab.fit_width_button.setChecked(True)
            wait_until(lambda: abs(self.tab.view.zoomFactor() - 1.0) > 0.01)
            manual_zoom()
            self.assertFalse(self.tab.fit_width_button.isChecked())
            fixed_zoom = self.tab.view.zoomFactor()
            self.tab.resize(self.tab.width() + 100, 700)
            QTest.qWait(120)
            self.assertAlmostEqual(self.tab.view.zoomFactor(), fixed_zoom)
        self.assertAlmostEqual(self.tab.view.zoomFactor(), 1.0)
        self.tab.fit_width_button.setChecked(True)
        wait_until(lambda: abs(self.tab.view.zoomFactor() - 1.0) > 0.01)
        fixed_zoom = self.tab.view.zoomFactor()
        self.tab.fit_width_button.click()
        self.tab.resize(1500, 700)
        QTest.qWait(120)
        self.assertAlmostEqual(self.tab.view.zoomFactor(), fixed_zoom)

    def test_fit_width_survives_reload_and_hidden_resize(self):
        self.tab.fit_width_button.click()
        wait_until(lambda: self.tab.view.zoomFactor() < 1.0)
        self.replace("# 更新\n\n" + "測試段落\n\n" * 50)
        self.assertTrue(self.tab.fit_width_button.isChecked())
        self.assert_fits_width()
        old_zoom = self.tab.view.zoomFactor()
        self.tab.hide()
        self.tab.resize(1600, 700)
        self.tab.show()
        wait_until(lambda: self.tab.view.zoomFactor() > old_zoom)
        self.assert_fits_width()

    def test_session_uses_css_scroll_and_restores_zoom_and_outline_width(self):
        self.replace("# Session\n\n" + "段落內容\n\n" * 150)
        self.tab._set_zoom(1.5)
        self.tab.splitter.setSizes([330, 670])
        QTest.qWait(80)
        self.javascript("window.scrollTo(0, 1000); true")
        wait_until(lambda: self.tab.page.scrollPosition().y() > 1400)
        state = self.tab.session_state()
        self.assertAlmostEqual(state["scroll_y"], 1000, delta=1)
        self.assertAlmostEqual(state["zoom"], 1.5)
        self.assertAlmostEqual(state["outline_width"], 330, delta=2)
        self.reopen(state)
        self.assertAlmostEqual(self.tab.view.zoomFactor(), 1.5)
        self.assertAlmostEqual(self.javascript("window.scrollY"), 1000, delta=1)
        self.assertAlmostEqual(self.tab.splitter.sizes()[0], 330, delta=2)

    def test_session_fit_width_waits_for_visible_layout_and_preserves_hidden_outline_width(self):
        self.replace("# Session\n\n" + "段落內容\n\n" * 150)
        self.tab.splitter.setSizes([340, 660])
        self.tab.outline_button.click()
        self.tab.fit_width_button.click()
        self.assert_fits_width()
        self.javascript("window.scrollTo(0, 800); true")
        wait_until(lambda: abs(self.tab.page.scrollPosition().y() / self.tab.view.zoomFactor() - 800) < 1)
        state = self.tab.session_state()
        self.assertAlmostEqual(state["outline_width"], 340, delta=2)
        self.reopen(state, show=False, width=1500)
        QTest.qWait(120)
        self.assertTrue(self.tab.session_restore_pending)
        self.assertAlmostEqual(self.tab.session_state()["scroll_y"], 800, delta=1)
        self.tab.show()
        wait_until(lambda: not self.tab.session_restore_pending)
        self.assertTrue(self.tab.fit_width_button.isChecked())
        self.assertTrue(self.tab.outline.isHidden())
        self.assert_fits_width()
        self.assertAlmostEqual(self.javascript("window.scrollY"), 800, delta=1)
        self.tab.outline_button.click()
        self.assertAlmostEqual(self.tab.splitter.sizes()[0], 340, delta=2)

    def test_session_restore_stops_when_user_navigates(self):
        self.replace("# Session\n\n" + "段落內容\n\n" * 150)
        state = self.tab.session_state()
        state["scroll_y"] = 1200
        self.tab.restore_session_state(state)
        self.assertTrue(self.tab.session_restore_pending)
        self.tab.scroll_to_anchor("")
        self.assertFalse(self.tab.session_restore_pending)
        QTest.qWait(200)
        self.assertEqual(self.javascript("window.scrollY"), 0)
        self.tab.restore_session_state(state)
        target = self.tab.view.focusProxy() or self.tab.view
        QTest.keyClick(target, Qt.Key.Key_Control)
        self.assertTrue(self.tab.session_restore_pending)
        QTest.keyClick(target, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.tab.session_restore_pending)
        QTest.keyClick(target, Qt.Key.Key_Home)
        self.assertFalse(self.tab.session_restore_pending)
        QTest.qWait(200)
        self.assertEqual(self.javascript("window.scrollY"), 0)

    def test_reloading_at_non_default_zoom_preserves_css_scroll(self):
        self.replace("# Session\n\n" + "段落內容\n\n" * 150)
        self.tab._set_zoom(1.5)
        QTest.qWait(80)
        self.javascript("window.scrollTo(0, 900); true")
        wait_until(lambda: self.tab.page.scrollPosition().y() > 1300)
        count = len(self.ready)
        self.tab.reload_document()
        wait_until(lambda: len(self.ready) > count)
        wait_until(lambda: self.tab.page.scrollPosition().y() > 1300)
        self.assertAlmostEqual(self.javascript("window.scrollY"), 900, delta=1)

    def test_atomic_save_reloads_and_unrelated_directory_changes_do_not(self):
        self.replace("# Long document\n\n" + "line\n\n" * 100)
        self.javascript("window.scrollTo(0, 500); true")
        wait_until(lambda: self.tab.page.scrollPosition().y() > 450)
        count = len(self.ready)
        replacement = self.root / "replacement.tmp"
        replacement.write_text("# Changed\n\n" + "line\n\n" * 100, encoding="utf-8")
        replacement.replace(self.path)
        wait_until(lambda: len(self.ready) > count)
        wait_until(lambda: self.tab.page.scrollPosition().y() > 450)
        self.assertEqual(self.tab.document.title, "Changed")
        count = len(self.ready)
        (self.root / "unrelated.txt").write_text("unrelated", encoding="utf-8")
        QTest.qWait(450)
        self.assertEqual(len(self.ready), count)

    def test_load_larger_than_sethtml_limit(self):
        self.replace("# Large\n\n" + ("x" * 2_200_000) + "\n\nend-marker")
        self.assertGreater(self.javascript("document.body.innerText.length"), 2_200_000)
        self.assertTrue(self.javascript("document.body.innerText.includes('end-marker')"))

    def test_missing_file_can_recover(self):
        self.path.unlink()
        self.tab.reload_document()
        wait_until(lambda: bool(self.errors))
        self.assertIn("無法載入", self.tab.status_message)
        self.path.write_text("# Recovered", encoding="utf-8")
        wait_until(lambda: self.tab.document.title == "Recovered")

    def test_closing_waits_for_render_and_discards_result(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_render(path):
            entered.set()
            release.wait(3)
            return render_document(path)

        with patch("markdown_finder.reader.render_document", side_effect=slow_render):
            self.tab.reload_document()
            wait_until(entered.is_set)
            self.tab.begin_close()
            self.assertTrue(self.tab.is_busy())
            release.set()
            wait_until(lambda: not self.tab.is_busy())
            self.assertFalse(Path(self.tab._temporary.name).exists())


if __name__ == "__main__":
    unittest.main()
