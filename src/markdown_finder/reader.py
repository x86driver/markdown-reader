"""One local Markdown document, rendered in a self-contained WebEngine page."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
from typing import Callable

from PySide6.QtCore import QEvent, QFileSystemWatcher, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .rendering import RenderedDocument, render_document


_OUTLINE_ANCHOR_SCRIPT = """
(() => {
    // Cache elements, not positions: layout can change with zoom or images.
    const allHeadings = window.__markdownFinderOutlineHeadings ||= Array.from(
        document.querySelectorAll('h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]')
    );
    const hasDetails = window.__markdownFinderHasDetails ??= !!document.querySelector('details');
    // Hidden headings have no usable position; their ancestors can be opened
    // by chapter navigation, but scroll tracking must only use visible ones.
    const headings = hasDetails
        ? allHeadings.filter(heading => !heading.closest('details:not([open])'))
        : allHeadings;
    if (!headings.length) return '';
    const root = document.documentElement;
    // A short final section may never reach the top of the viewport.
    if (window.scrollY > 0 && Math.ceil(window.scrollY + window.innerHeight) >= root.scrollHeight)
        return headings[headings.length - 1].id;
    // Match the reading inset used by scrollIntoView for chapter links.
    const inset = (parseFloat(getComputedStyle(root).scrollPaddingTop) || 0)
        + (parseFloat(getComputedStyle(headings[0]).scrollMarginTop) || 0) + 1;
    let low = 0, high = headings.length;
    while (low < high) {
        const middle = Math.floor((low + high) / 2);
        if (headings[middle].getBoundingClientRect().top <= inset) low = middle + 1;
        else high = middle;
    }
    return headings[Math.max(0, low - 1)].id;
})()
"""


class RenderThread(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, output: Path, parent=None):
        super().__init__(parent)
        self.path = path
        self.output = output

    def run(self):
        try:
            document = render_document(self.path)
            if self.isInterruptionRequested():
                return
            self.output.write_text(document.html, encoding="utf-8")
            if not self.isInterruptionRequested():
                self.ready.emit(document)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(f"無法載入 {self.path.name}：{exc}")


class ReaderPage(QWebEnginePage):
    """Allow the generated page only; delegate explicit links to the reader."""

    def __init__(self, reader: DocumentTab, parent=None):
        super().__init__(parent)
        self.reader = reader

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        if self.reader._closing:
            return False
        if not is_main_frame:
            return False
        if navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            self.reader.follow_link(url)
            return False
        return url == self.reader._document_url


class DocumentTab(QWidget):
    document_ready = Signal(object)
    state_restored = Signal()
    open_markdown_requested = Signal(object, str)
    error_occurred = Signal(str)
    status_changed = Signal(str)

    def __init__(
        self, path: Path, parent=None,
        external_opener: Callable[[QUrl], bool] | None = None,
    ):
        super().__init__(parent)
        # Preserve the opened directory for relative links through symlinks.
        self.path = path.expanduser().absolute()
        self.external_opener = external_opener or QDesktopServices.openUrl
        self.loaded = False
        self.status_message = "準備載入…"
        self.document: RenderedDocument | None = None
        self._pending_document: RenderedDocument | None = None
        self._worker: RenderThread | None = None
        self._closing = False
        self._active_load = False
        self._dom_loading = False
        self._reload_pending = False
        self._pending_anchor: str | None = None
        self._scroll = (0.0, 0.0)
        self._generation = 0
        self._fit_reference_width: float | None = None
        self._pending_session_state: dict | None = None
        self._applied_session_scroll: tuple[float, float] | None = None
        self._session_restore_token = 0
        self._outline_width = 230
        self._outline_items: dict[str, QTreeWidgetItem] = {}
        self._outline_sync_pending = False
        self._outline_sync_requested = False
        self._scroll_input_widget = None
        self._document_url = QUrl()
        self._temporary = tempfile.TemporaryDirectory(prefix="markdown-finder-")
        self._fingerprint = self._file_fingerprint()
        self._build_ui()

        self.watcher = QFileSystemWatcher(self)
        if self.path.parent.is_dir():
            self.watcher.addPath(str(self.path.parent))
        self._watch_file()
        self.watch_timer = QTimer(self)
        self.watch_timer.setSingleShot(True)
        self.watch_timer.setInterval(250)
        self.watch_timer.timeout.connect(self._check_changed)
        self.watcher.fileChanged.connect(self._watch_changed)
        self.watcher.directoryChanged.connect(self._watch_changed)
        QTimer.singleShot(0, self.reload_document)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)
        toolbar.setSpacing(8)
        self.outline_button = QPushButton("目錄")
        self.outline_button.setCheckable(True)
        self.outline_button.setChecked(True)
        self.outline_button.toggled.connect(self._toggle_outline)
        toolbar.addWidget(self.outline_button)
        self.path_label = QLabel(str(self.path))
        self.path_label.setTextFormat(Qt.TextFormat.PlainText)
        self.path_label.setToolTip(str(self.path))
        self.path_label.setMinimumWidth(0)
        self.path_label.setStyleSheet("color: #65748b; font-size: 12px;")
        toolbar.addWidget(self.path_label, 1)
        for label, callback, tooltip in (
            ("搜尋", self.focus_find, "搜尋文件內容  Ctrl+F"),
            ("重新載入", self.reload_document, "重新載入文件  Ctrl+R"),
            ("−", self.zoom_out, "縮小  Ctrl+−"),
            ("+", self.zoom_in, "放大  Ctrl++"),
        ):
            button = QPushButton(label)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        self.zoom_button = QPushButton("100%")
        self.zoom_button.setToolTip("重設縮放  Ctrl+0")
        self.zoom_button.clicked.connect(self.zoom_reset)
        toolbar.addWidget(self.zoom_button)
        self.fit_width_button = QPushButton("符合寬度")
        self.fit_width_button.setCheckable(True)
        self.fit_width_button.setToolTip("Fit width：隨閱讀區寬度自動調整縮放")
        self.fit_width_button.toggled.connect(self._fit_width_toggled)
        toolbar.addWidget(self.fit_width_button)
        layout.addLayout(toolbar)

        self.find_bar = QFrame()
        find_layout = QHBoxLayout(self.find_bar)
        find_layout.setContentsMargins(12, 3, 12, 8)
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("搜尋文件內容…")
        self.find_input.setAccessibleName("搜尋文件內容")
        self.find_input.setClearButtonEnabled(True)
        self.find_input.textChanged.connect(lambda: self._find())
        self.find_input.returnPressed.connect(lambda: self._find())
        find_layout.addWidget(self.find_input, 1)
        self.find_count = QLabel("")
        self.find_count.setMinimumWidth(70)
        find_layout.addWidget(self.find_count)
        for label, callback in (
            ("上一個", lambda: self._find(backward=True)),
            ("下一個", lambda: self._find()),
            ("關閉", self.close_find),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            find_layout.addWidget(button)
        find_previous = QShortcut(QKeySequence("Shift+Return"), self.find_input)
        find_previous.setContext(Qt.ShortcutContext.WidgetShortcut)
        find_previous.activated.connect(lambda: self._find(backward=True))
        self.find_bar.hide()
        layout.addWidget(self.find_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.outline = QTreeWidget()
        self.outline.setHeaderLabel("章節目錄")
        self.outline.setMinimumWidth(130)
        self.outline.setMaximumWidth(480)
        self.outline.setIndentation(14)
        self.outline.setAccessibleName("文件章節目錄")
        self.outline.itemClicked.connect(self._outline_clicked)
        self.outline.itemActivated.connect(self._outline_clicked)
        self.splitter.addWidget(self.outline)
        self.view = QWebEngineView()
        self.view.setMinimumWidth(250)
        self.fit_width_timer = QTimer(self)
        self.fit_width_timer.setSingleShot(True)
        self.fit_width_timer.setInterval(60)
        self.fit_width_timer.timeout.connect(self._update_fit_width)
        self.restore_timer = QTimer(self)
        self.restore_timer.setSingleShot(True)
        self.restore_timer.setInterval(80)
        self.restore_timer.timeout.connect(self._restore_scroll)
        self.outline_sync_timer = QTimer(self)
        self.outline_sync_timer.setSingleShot(True)
        self.outline_sync_timer.setInterval(50)
        self.outline_sync_timer.timeout.connect(self._sync_outline)
        self.view.installEventFilter(self)
        self.page = ReaderPage(self, self.view)
        self.view.setPage(self.page)
        settings = self.page.settings()
        for attribute in (
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
            QWebEngineSettings.WebAttribute.PluginsEnabled,
            QWebEngineSettings.WebAttribute.LocalStorageEnabled,
            QWebEngineSettings.WebAttribute.NavigateOnDropEnabled,
            QWebEngineSettings.WebAttribute.AutoLoadIconsForPage,
            QWebEngineSettings.WebAttribute.HyperlinkAuditingEnabled,
            QWebEngineSettings.WebAttribute.DnsPrefetchEnabled,
        ):
            settings.setAttribute(attribute, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.view.loadFinished.connect(self._load_finished)
        self.page.findTextFinished.connect(self._find_finished)
        self.page.scrollPositionChanged.connect(self._schedule_outline_sync)
        self.page.contentsSizeChanged.connect(self._schedule_outline_sync)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([230, 850])
        self.splitter.splitterMoved.connect(self._remember_outline_width)
        layout.addWidget(self.splitter, 1)

    def _set_status(self, message: str):
        self.status_message = message
        self.status_changed.emit(message)

    def _context_menu(self, point):
        menu = QMenu(self.view)
        menu.addAction(self.page.action(QWebEnginePage.WebAction.Copy))
        menu.addAction(self.page.action(QWebEnginePage.WebAction.SelectAll))
        menu.exec(self.view.mapToGlobal(point))

    def _toggle_outline(self, visible):
        if not visible:
            self._remember_outline_width()
        self.outline.setVisible(visible)
        if visible:
            self._apply_outline_width()
            self._reveal_outline_item(self.outline.currentItem())
            self._schedule_outline_sync()

    def _remember_outline_width(self, *args):
        if not self.outline.isHidden() and self.splitter.sizes()[0] > 0:
            self._outline_width = self.splitter.sizes()[0]

    def _apply_outline_width(self):
        available = self.splitter.width() - self.splitter.handleWidth()
        self.splitter.setSizes([self._outline_width, max(250, available - self._outline_width)])

    @property
    def session_restore_pending(self) -> bool:
        return self._pending_session_state is not None

    def _css_scroll_position(self):
        # WebEngine reports scrollPosition in zoomed view pixels, whereas
        # window.scrollTo accepts CSS document pixels.
        position = self.page.scrollPosition()
        zoom = self.view.zoomFactor()
        return max(0.0, position.x() / zoom), max(0.0, position.y() / zoom)

    def session_state(self) -> dict:
        """Snapshot synchronously, including a not-yet-visible restored tab."""
        pending = self._pending_session_state
        if pending is not None:
            scroll_x, scroll_y = pending["scroll_x"], pending["scroll_y"]
        elif self._active_load or self._dom_loading:
            scroll_x, scroll_y = self._scroll
        else:
            scroll_x, scroll_y = self._css_scroll_position()
        if pending is None:
            self._remember_outline_width()
        return {
            "zoom": self.view.zoomFactor(),
            "fit_width": self.fit_width_button.isChecked(),
            "scroll_x": scroll_x,
            "scroll_y": scroll_y,
            "outline_visible": self.outline_button.isChecked(),
            "outline_width": self._outline_width,
        }

    def restore_session_state(self, state: dict):
        """Restore preferences now and the CSS scroll position after layout."""
        if self._closing:
            return

        def number(key, default, minimum, maximum):
            value = state.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            try:
                if not math.isfinite(value):
                    return default
            except OverflowError:
                return default
            return max(minimum, min(maximum, value))

        self._session_restore_token += 1
        self._applied_session_scroll = None
        self._pending_session_state = {
            "zoom": number("zoom", 1.0, 0.25, 5.0),
            "fit_width": state.get("fit_width") is True,
            "scroll_x": number("scroll_x", 0.0, 0.0, 1e12),
            "scroll_y": number("scroll_y", 0.0, 0.0, 1e12),
            "outline_visible": state.get("outline_visible", True) is True,
            "outline_width": int(number("outline_width", 230, 130, 480)),
        }
        saved = self._pending_session_state
        self._set_zoom(saved["zoom"], automatic=True)
        self.fit_width_button.setChecked(saved["fit_width"])
        self.outline_button.setChecked(saved["outline_visible"])
        self._outline_width = saved["outline_width"]
        self._apply_outline_width()
        self._schedule_restore()

    def _schedule_restore(self):
        if self.session_restore_pending and self.loaded and self.isVisible() and not self._closing:
            self.restore_timer.start()

    def _cancel_session_restore(self):
        if self.session_restore_pending:
            self._pending_session_state = None
            self._applied_session_scroll = None
            self._session_restore_token += 1
            self.restore_timer.stop()

    def _restore_scroll(self):
        if not self.session_restore_pending or not self.loaded or not self.isVisible() or self._closing:
            return
        if self._applied_session_scroll is not None:
            current = self._css_scroll_position()
            if all(abs(a - b) < 1 for a, b in zip(current, self._applied_session_scroll)):
                self._pending_session_state = None
                self._applied_session_scroll = None
                self.state_restored.emit()
            else:
                self._schedule_restore()
            return
        self._apply_outline_width()
        if self.fit_width_button.isChecked():
            if self._fit_reference_width is None or self.fit_width_timer.isActive():
                self._update_fit_width()
                self._schedule_restore()
                return
        saved = self._pending_session_state
        token = self._session_restore_token
        generation = self._generation
        expected_width = self.view.width() / self.view.zoomFactor()

        def restored(result):
            if self._closing or token != self._session_restore_token or generation != self._generation:
                return
            if isinstance(result, str):
                scroll_x, scroll_y = json.loads(result)
                self._applied_session_scroll = (scroll_x, scroll_y)
                # Keep a synchronous snapshot correct until Qt receives its
                # scrollPosition update from the renderer process.
                self._pending_session_state["scroll_x"] = scroll_x
                self._pending_session_state["scroll_y"] = scroll_y
            self._schedule_restore()

        # Checking the CSS viewport also waits for WebEngine's asynchronous
        # zoom layout. There is no delayed JavaScript scroll left behind that
        # could pull the reader back after the user starts navigating.
        self.page.runJavaScript(
            "{ if (Math.abs(window.innerWidth - " + repr(expected_width) + ") < 2) {"
            f" window.scrollTo({saved['scroll_x']}, {saved['scroll_y']});"
            " JSON.stringify([window.scrollX, window.scrollY]);"
            " } else { false; } }", restored,
        )

    def _outline_clicked(self, item, column=0):
        self.scroll_to_anchor(item.data(0, Qt.ItemDataRole.UserRole))

    def _schedule_outline_sync(self, *args):
        if (self._closing or not self.loaded or not self._outline_items
                or not self.isVisible() or self.outline.isHidden()):
            return
        self._outline_sync_requested = True
        # Throttle instead of restarting the timer on every scroll event, so
        # the outline also follows a continuous wheel/trackpad scroll.
        if not self._outline_sync_pending and not self.outline_sync_timer.isActive():
            self.outline_sync_timer.start()

    def _sync_outline(self):
        if (self._closing or not self.loaded or not self._outline_items
                or not self.isVisible() or self.outline.isHidden()):
            return
        self._outline_sync_requested = False
        self._outline_sync_pending = True
        generation = self._generation

        def located(anchor):
            if self._closing:
                return
            self._outline_sync_pending = False
            if (generation == self._generation and self.loaded
                    and self.isVisible() and not self.outline.isHidden()):
                item = self._outline_items.get(anchor) if isinstance(anchor, str) else None
                if item is not None and item is not self.outline.currentItem():
                    # Only click/activation navigate the reader. Changing the
                    # current item here must never move the document itself.
                    self.outline.setCurrentItem(item)
                    self._reveal_outline_item(item)
                elif anchor == "":
                    # A document can have all of its headings inside closed
                    # disclosures, leaving no visible chapter to highlight.
                    self.outline.setCurrentItem(None)
                    self.outline.clearSelection()
            if self._outline_sync_requested:
                self._schedule_outline_sync()

        self.page.runJavaScript(_OUTLINE_ANCHOR_SCRIPT, located)

    def _reveal_outline_item(self, item):
        if item is None:
            return
        ancestor = item.parent()
        while ancestor is not None:
            ancestor.setExpanded(True)
            ancestor = ancestor.parent()
        self.outline.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)

    def _populate_outline(self, document):
        self._outline_items.clear()
        self.outline.clear()
        ancestors: list[tuple[int, QTreeWidgetItem]] = []
        for heading in document.headings:
            while ancestors and ancestors[-1][0] >= heading.level:
                ancestors.pop()
            item = QTreeWidgetItem([heading.title])
            item.setToolTip(0, heading.title)
            item.setData(0, Qt.ItemDataRole.UserRole, heading.anchor)
            self._outline_items[heading.anchor] = item
            if ancestors:
                ancestors[-1][1].addChild(item)
            else:
                self.outline.addTopLevelItem(item)
            ancestors.append((heading.level, item))
        self.outline.expandAll()
        self.outline_button.setEnabled(bool(document.headings))
        self.outline.setVisible(bool(document.headings) and self.outline_button.isChecked())

    def reload_document(self):
        if self._closing:
            return
        if self._active_load or self._worker is not None:
            self._reload_pending = True
            return
        self._active_load = True
        self.loaded = False
        self.outline_sync_timer.stop()
        self._reload_pending = False
        self._applied_session_scroll = None
        self._scroll = self._css_scroll_position()
        self._fingerprint = self._file_fingerprint()
        self._watch_file()
        self._set_status("正在載入文件…")
        self._generation += 1
        output = Path(self._temporary.name) / f"document-{self._generation}.html"
        worker = RenderThread(self.path, output, self)
        self._worker = worker
        worker.ready.connect(self._render_ready)
        worker.failed.connect(self._render_failed)
        worker.finished.connect(self._render_finished)
        worker.start()

    def _render_ready(self, document):
        if self._closing:
            return
        self._pending_document = document
        output = Path(self._temporary.name) / f"document-{self._generation}.html"
        self._document_url = QUrl.fromLocalFile(str(output))
        self._dom_loading = True
        self.view.load(self._document_url)

    def _render_failed(self, message):
        if self._closing:
            return
        self._active_load = False
        self.loaded = self.document is not None
        self._set_status(message)
        self.error_occurred.emit(message)

    def _render_finished(self):
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if self._closing:
            self._temporary.cleanup()
        else:
            self._maybe_reload()

    def _load_finished(self, success):
        if self._closing or not self._dom_loading:
            return
        self._dom_loading = False
        self._active_load = False
        if success and self._pending_document is not None:
            self.document = self._pending_document
            self._pending_document = None
            self.loaded = True
            self._populate_outline(self.document)
            self._fit_reference_width = None
            if self.fit_width_button.isChecked():
                self.fit_width_timer.start()
            self._set_status("文件變更會自動更新  ·  Ctrl+F 搜尋內容  ·  Ctrl+P 開啟其他文件")
            self._install_scroll_input_filter()
            if self._pending_anchor is not None:
                anchor = self._pending_anchor
                self._pending_anchor = None
                self.scroll_to_anchor(anchor)
            elif self.session_restore_pending:
                self._schedule_restore()
            else:
                self.page.runJavaScript(f"window.scrollTo({self._scroll[0]}, {self._scroll[1]});")
            self._schedule_outline_sync()
            if not self.find_bar.isHidden() and self.find_input.text():
                self._find()
            # Old generated pages are no longer needed after the new DOM loads.
            current = Path(self._document_url.toLocalFile())
            for old in Path(self._temporary.name).glob("document-*.html"):
                if old != current:
                    old.unlink(missing_ok=True)
            self.document_ready.emit(self.document)
        else:
            self.loaded = False
            self._set_status(f"無法顯示 {self.path.name}，請按 Ctrl+R 重試。")
            self.error_occurred.emit(self.status_message)
        self._maybe_reload()

    def _maybe_reload(self):
        if self._reload_pending and not self._active_load and self._worker is None:
            QTimer.singleShot(0, self.reload_document)

    def _file_fingerprint(self):
        try:
            info = self.path.stat()
            return info.st_ino, info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def _watch_file(self):
        if hasattr(self, "watcher") and str(self.path) not in self.watcher.files() and self.path.is_file():
            self.watcher.addPath(str(self.path))

    def _watch_changed(self, path):
        if not self._closing:
            self.watch_timer.start()

    def _check_changed(self):
        self._watch_file()
        if self._file_fingerprint() != self._fingerprint:
            self.reload_document()

    def follow_link(self, url: QUrl):
        """Called only for a user-clicked link, never for resource requests."""
        if self._closing:
            return
        scheme = url.scheme().lower()
        if scheme in ("http", "https", "mailto"):
            if not self.external_opener(url):
                self._set_status("無法使用預設程式開啟連結。")
            return
        if not url.isLocalFile():
            return
        target = Path(url.toLocalFile()).absolute()
        fragment = url.fragment()
        # With <base href="file:///source/directory/">, a #heading link
        # resolves to the directory rather than the generated temporary page.
        if url.hasFragment() and target in (
            self.path, self.path.parent, Path(self._document_url.toLocalFile()),
        ):
            self.scroll_to_anchor(fragment)
        elif target.suffix.lower() in (".md", ".markdown"):
            self.open_markdown_requested.emit(target, fragment)

    def scroll_to_anchor(self, fragment: str):
        if self._closing:
            return
        self._cancel_session_restore()
        if not self.loaded:
            self._pending_anchor = fragment
            return
        if not fragment:
            self.page.runJavaScript("window.scrollTo(0, 0);")
            return
        encoded = json.dumps(fragment)
        self.page.runJavaScript(
            "{ const target = document.getElementById(" + encoded + ");"
            " if (target) {"
            " for (let parent = target.parentElement; parent; parent = parent.parentElement)"
            " if (parent.tagName === 'DETAILS') parent.open = true;"
            " target.scrollIntoView({block: 'start'}); } }"
        )

    def focus_find(self):
        self.find_bar.show()
        self.find_input.setFocus()
        self.find_input.selectAll()

    def close_find(self) -> bool:
        if self.find_bar.isHidden():
            return False
        self.find_bar.hide()
        self.page.findText("")
        self.view.setFocus()
        return True

    def _find(self, *, backward=False):
        if not self.loaded or self._closing:
            return
        self._cancel_session_restore()
        flags = QWebEnginePage.FindFlag.FindBackward if backward else QWebEnginePage.FindFlag(0)
        self.page.findText(self.find_input.text(), flags)

    def _find_finished(self, result):
        if self._closing:
            return
        if not self.find_input.text():
            self.find_count.setText("")
        elif result.numberOfMatches():
            self.find_count.setText(f"{result.activeMatch()} / {result.numberOfMatches()}")
        else:
            self.find_count.setText("找不到")

    def zoom_in(self):
        self._set_zoom(self.view.zoomFactor() + 0.1)

    def zoom_out(self):
        self._set_zoom(self.view.zoomFactor() - 0.1)

    def zoom_reset(self):
        self._set_zoom(1.0)

    def eventFilter(self, watched, event):
        if watched is self.view and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            if self.fit_width_button.isChecked() and not self._closing:
                self.fit_width_timer.start()
            self._install_scroll_input_filter()
            self._schedule_restore()
            self._schedule_outline_sync()
        if watched in (self.view, self._scroll_input_widget):
            # Toggling a short disclosure may leave the page's total size and
            # scroll position unchanged, so WebEngine emits neither signal.
            if event.type() == QEvent.Type.MouseButtonRelease or (
                event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
            ):
                self._schedule_outline_sync()
            if event.type() in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress, QEvent.Type.TouchBegin):
                self._cancel_session_restore()
            elif event.type() == QEvent.Type.KeyPress:
                # Modifier presses and window shortcuts (especially switching
                # tabs) must not discard a background tab's saved progress.
                modifiers = event.modifiers()
                navigation_key = event.key() in (
                    Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left,
                    Qt.Key.Key_Right, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                    Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_Space,
                )
                shortcut = modifiers & (Qt.KeyboardModifier.ControlModifier
                                        | Qt.KeyboardModifier.AltModifier
                                        | Qt.KeyboardModifier.MetaModifier)
                if navigation_key and not shortcut:
                    self._cancel_session_restore()
        return super().eventFilter(watched, event)

    def _install_scroll_input_filter(self):
        proxy = self.view.focusProxy()
        if proxy is not None and proxy is not self._scroll_input_widget:
            proxy.installEventFilter(self)
            self._scroll_input_widget = proxy

    def _fit_width_toggled(self, enabled):
        if enabled:
            self._update_fit_width()
        else:
            self.fit_width_timer.stop()

    def _update_fit_width(self):
        if self._closing or not self.loaded or not self.fit_width_button.isChecked():
            return
        if self._fit_reference_width is not None:
            self._set_zoom(self.view.width() / self._fit_reference_width, automatic=True)
            self._schedule_restore()
            return
        generation = self._generation

        def measured(width):
            # CSS owns the standard reading width; do not measure the already
            # zoomed content bounds, which would cause a resize feedback loop.
            if self._closing or generation != self._generation or not self.loaded:
                return
            if isinstance(width, (int, float)) and width > 0:
                self._fit_reference_width = float(width)
                self._update_fit_width()

        self.page.runJavaScript(
            "parseFloat(getComputedStyle(document.querySelector('.markdown-body')).maxWidth)",
            measured,
        )

    def _set_zoom(self, factor, *, automatic=False):
        if not automatic:
            self._cancel_session_restore()
            self.fit_width_button.setChecked(False)
            factor = round(factor, 2)
        factor = max(0.25, min(5.0, factor))
        self.view.setZoomFactor(factor)
        self.zoom_button.setText(f"{self.view.zoomFactor():.0%}")
        self._schedule_outline_sync()

    def begin_close(self):
        if self._closing:
            return
        self._closing = True
        self._reload_pending = False
        self.watch_timer.stop()
        self.fit_width_timer.stop()
        self.restore_timer.stop()
        self.outline_sync_timer.stop()
        paths = self.watcher.files() + self.watcher.directories()
        if paths:
            self.watcher.removePaths(paths)
        self.view.stop()
        if self._worker is not None:
            self._worker.requestInterruption()
        else:
            self._temporary.cleanup()

    def is_busy(self) -> bool:
        # Keep the widget alive until queued finished cleanup also ran.
        return self._worker is not None
