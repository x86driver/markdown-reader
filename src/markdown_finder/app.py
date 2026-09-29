from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QAbstractListModel, QCoreApplication, QEvent, QModelIndex, QRect, QSettings, QSize,
    Qt, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPalette, QShortcut,
)
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMessageBox,
    QPushButton, QSizePolicy, QStackedWidget, QStyledItemDelegate, QStyle, QTabBar, QTabWidget,
    QVBoxLayout, QWidget,
)

from . import __version__
from .engine import ScanStats, SearchHit, SearchResults
from .workers import ScanThread, SearchThread
from .reader import DocumentTab
from .session import SESSION_KEY, read_session


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        relative = path.relative_to(Path.home())
        return "~" if str(relative) == "." else "~/" + relative.as_posix()
    except ValueError:
        return str(path)


def one_line(text: str) -> str:
    return text.replace("\n", " ↵ ").replace("\r", "").replace("\t", "  ")


class ResultModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hits: list[SearchHit] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.hits)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.hits):
            return None
        entry = self.hits[index.row()].entry
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return f"{one_line(entry.name)}\n{one_line(display_path(entry.path))}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.path
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def replace(self, hits: list[SearchHit]):
        self.beginResetModel()
        self.hits = hits
        self.endResetModel()


class FileDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(300, 68)

    def paint(self, painter: QPainter, option, index):
        entry = index.data(Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(7, 3, -7, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e9f1ff" if selected else "#f5f7fa"))
            painter.drawRoundedRect(rect, 7, 7)
        badge = QRect(rect.left() + 12, rect.top() + 14, 35, 35)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d6e5ff" if selected else "#edf0f5"))
        painter.drawRoundedRect(badge, 6, 6)
        font = QFont(option.font)
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#2c5fb6" if selected else "#64748b"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "MD")
        left = badge.right() + 14
        width = max(0, rect.right() - left - 14)
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#172b4d"))
        name = painter.fontMetrics().elidedText(one_line(entry.name), Qt.TextElideMode.ElideMiddle, width)
        painter.drawText(QRect(left, rect.top() + 6, width, 26), Qt.AlignmentFlag.AlignVCenter, name)
        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#65748b"))
        location = painter.fontMetrics().elidedText(
            one_line(display_path(entry.path)), Qt.TextElideMode.ElideMiddle, width
        )
        painter.drawText(QRect(left, rect.top() + 33, width, 22), Qt.AlignmentFlag.AlignVCenter, location)
        painter.restore()


class SearchInput(QLineEdit):
    move_selection = Signal(int)
    open_selection = Signal()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self.move_selection.emit(1 if key == Qt.Key.Key_Down else -1)
            event.accept()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_selection.emit()
            event.accept()
        elif key == Qt.Key.Key_Escape:
            self.clear()
            event.accept()
        else:
            super().keyPressEvent(event)


class FinderWindow(QMainWindow):
    def __init__(
        self, root: Path, *, settings: QSettings | None = None,
        opener: Callable[[QUrl], bool] | None = None, auto_scan: bool = True,
    ):
        super().__init__()
        self.root = root.expanduser().absolute()
        self.settings = settings if settings is not None else QSettings()
        self.opener = opener if opener is not None else QDesktopServices.openUrl
        stored = self.settings.value("recent_files", [])
        self.recent = [str(item) for item in stored] if isinstance(stored, list) else []
        self.recent = self.recent[:40]
        self._scan: ScanThread | None = None
        self._scan_generation = 0
        self._request_id = 0
        self._search_inflight: int | None = None
        self._stream_dirty = False
        self._displayed_query = ""
        self._results_current = False
        self._scanning = False
        self._closing = False
        self._scan_started = 0.0
        self._entry_count = 0
        self._stats = ScanStats()
        self._documents: dict[str, DocumentTab] = {}
        self._pending_recent: set[DocumentTab] = set()
        self._closing_documents: set[DocumentTab] = set()
        self._last_reader: DocumentTab | None = None
        self._restoring_session = False
        self._saved_session: str | None = None
        self.last_search_seconds = 0.0
        self.last_scan_seconds = 0.0

        self.setWindowTitle("Markdown Reader")
        self.resize(1140, 820)
        self.setMinimumSize(760, 540)
        self._build_ui()
        # WebEngine embeds a QQuickWidget. On Qt 6.4+, introducing the first
        # one after show() can recreate the native window (Raster -> OpenGL).
        # Declare that compositor before the first show, including an empty
        # search-only session. No QML or extra WebEngine page is loaded here.
        # Keep it for the window's lifetime, even when all reader tabs close.
        self._reader_surface = None
        if QApplication.platformName() in {"wayland", "wayland-egl", "xcb"}:
            self._reader_surface = QQuickWidget(self)
            self._reader_surface.hide()
        geometry = self.settings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        self.search_worker = SearchThread(self)
        self.search_worker.completed.connect(self._search_completed)
        self.search_worker.failed.connect(self._search_failed)
        self.search_worker.start()
        self.query_timer = QTimer(self)
        self.query_timer.setSingleShot(True)
        self.query_timer.setInterval(60)
        self.query_timer.timeout.connect(self._submit_search)
        self.stream_timer = QTimer(self)
        self.stream_timer.setSingleShot(True)
        self.stream_timer.setInterval(250)
        self.stream_timer.timeout.connect(self._submit_stream_search)
        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.setInterval(20)
        self.shutdown_timer.timeout.connect(self._finish_close)
        self.reader_cleanup_timer = QTimer(self)
        self.reader_cleanup_timer.setInterval(20)
        self.reader_cleanup_timer.timeout.connect(self._collect_closed_tabs)
        self.search_input.textChanged.connect(self._query_changed)

        for sequence, callback in (
            ("Ctrl+P", self.focus_search), ("Ctrl+L", self.focus_search),
            ("F5", self.start_scan), ("Ctrl+Shift+C", self.copy_path),
            ("Ctrl+Q", self.close), ("Escape", self.escape_action),
            ("Ctrl+W", self.close_current_tab),
            ("Ctrl+Tab", lambda: self.cycle_tabs(1)),
            ("Ctrl+Shift+Tab", lambda: self.cycle_tabs(-1)),
            ("Ctrl+PgDown", lambda: self.cycle_tabs(1)),
            ("Ctrl+PgUp", lambda: self.cycle_tabs(-1)),
            ("Ctrl+F", lambda: self.reader_action("focus_find")),
            ("Ctrl+R", lambda: self.reader_action("reload_document")),
            ("Ctrl++", lambda: self.reader_action("zoom_in")),
            ("Ctrl+=", lambda: self.reader_action("zoom_in")),
            ("Ctrl+-", lambda: self.reader_action("zoom_out")),
            ("Ctrl+0", lambda: self.reader_action("zoom_reset")),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
        self._restore_session()
        self.session_timer = QTimer(self)
        self.session_timer.setInterval(5000)
        self.session_timer.timeout.connect(self.save_session)
        self.session_timer.start()
        if auto_scan:
            QTimer.singleShot(0, self.start_scan)
        QTimer.singleShot(0, self._focus_current_tab)

    def _focus_current_tab(self):
        if not self._closing:
            reader = self.current_reader()
            (reader.view if reader else self.search_input).setFocus()

    def _restore_session(self):
        session = read_session(self.settings.value(SESSION_KEY))
        if session is None:
            return
        self._restoring_session = True
        opened = {}
        restored = set()
        missing = 0
        search_index = 0
        try:
            for index, state in enumerate(session["tabs"]):
                path = Path(state["path"])
                try:
                    tab = self.open_document(path)
                except (OSError, RuntimeError, ValueError):
                    tab = None
                if tab is None:
                    missing += 1
                    continue
                opened[str(path)] = tab
                if tab not in restored:
                    tab.restore_session_state(state)
                    restored.add(tab)
                    if index < session["search_tab_index"]:
                        search_index += 1
            self.tabs.tabBar().moveTab(self.tabs.indexOf(self.finder_panel), search_index)
            self._last_reader = opened.get(session["last_reader_path"])
            active = opened.get(session["active_path"])
            if session["active_path"] is None:
                active = self.finder_panel
            elif active is None:
                active = next(iter(opened.values()), self.finder_panel)
            self.tabs.setCurrentWidget(active)
            if isinstance(active, DocumentTab):
                self._last_reader = active
        finally:
            self._restoring_session = False
        if missing:
            self.statusBar().showMessage(f"已還原閱讀分頁；略過 {missing} 份無法開啟的文件。", 10000)

    def save_session(self):
        if self._closing or self._restoring_session:
            return
        tabs = []
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, DocumentTab):
                tabs.append({"path": str(tab.path), **tab.session_state()})
        active = self.current_reader()
        session = json.dumps({
            "version": 1,
            "tabs": tabs,
            "active_path": str(active.path) if active else None,
            "last_reader_path": str(self._last_reader.path) if self._last_reader else None,
            "search_tab_index": self.tabs.indexOf(self.finder_panel),
        }, ensure_ascii=False, allow_nan=False)
        if session != self._saved_session:
            self.settings.setValue(SESSION_KEY, session)
            self.settings.sync()
            if self.settings.status() == QSettings.Status.NoError:
                self._saved_session = session
            else:
                self.statusBar().showMessage("無法保存閱讀進度，請檢查設定檔的寫入權限。", 10000)

    def _build_ui(self):
        self.setStyleSheet("""
            QMainWindow { background: #f6f8fb; }
            QWidget { color: #243550; font-size: 14px; }
            QLabel#title { color: #172b4d; font-size: 24px; font-weight: 700; }
            QLabel#subtitle, QLabel#help, QLabel#scanStatus { color: #6b7890; font-size: 12px; }
            QLabel#logo { background: #285fd0; color: white; border-radius: 11px; font-size: 16px; font-weight: 700; }
            QLabel#scope { color: #53647e; font-size: 12px; }
            QLabel#resultCount { color: #53647e; font-size: 12px; }
            QLineEdit { background: white; border: 1px solid #cbd5e3; border-radius: 9px; padding: 12px 15px; font-size: 16px; selection-background-color: #c7dcff; selection-color: #172b4d; }
            QLineEdit:focus { border: 2px solid #4b7fe5; padding: 11px 14px; }
            QListView { background: white; border: 1px solid #dfe5ee; border-radius: 10px; padding: 4px; outline: none; }
            QPushButton { background: white; border: 1px solid #ced7e4; border-radius: 7px; padding: 8px 13px; }
            QPushButton:hover { background: #edf3ff; border-color: #a8c0ec; }
            QPushButton:checked { background: #dce8ff; border-color: #7297da; color: #184da7; }
            QPushButton:pressed { background: #dce8ff; }
            QPushButton:disabled { color: #a3aebd; background: #f1f4f8; border-color: #e0e5ed; }
            QPushButton#openButton { background: #285fd0; border-color: #285fd0; color: white; }
            QPushButton#openButton:hover { background: #214fb0; }
            QPushButton#openButton:disabled { background: #b4c7ea; border-color: #b4c7ea; }
            QLabel#empty { background: white; border: 1px solid #dfe5ee; border-radius: 10px; color: #718096; }
            QLabel#selectedPath { color: #53647e; font-size: 12px; }
            QFrame#divider { background: #dfe5ee; max-height: 1px; border: none; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 5px 1px; }
            QScrollBar::handle:vertical { background: #cbd5e1; border-radius: 4px; min-height: 32px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #e9edf4; border: none; padding: 10px 16px; min-width: 80px; }
            QTabBar::tab:selected { background: #f6f8fb; color: #285fd0; border-top: 2px solid #4b7fe5; padding-top: 8px; }
            QTabBar::tab:hover { background: #edf3ff; }
            QStatusBar { color: #65748b; background: #f6f8fb; font-size: 12px; }
            QStatusBar::item { border: none; }
            QLabel#readerStatus { color: #65748b; font-size: 12px; }
            QPushButton#aboutButton { padding: 3px 8px; font-size: 12px; }
        """)
        self.reader_status = QLabel()
        self.reader_status.setObjectName("readerStatus")
        self.reader_status.setTextFormat(Qt.TextFormat.PlainText)
        self.reader_status.setWordWrap(False)
        self.reader_status.setContentsMargins(10, 0, 0, 0)
        self.reader_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.statusBar().addWidget(self.reader_status, 1)
        self.about_button = QPushButton(f"關於 v{__version__}")
        self.about_button.setObjectName("aboutButton")
        self.about_button.setToolTip("查看 Markdown Reader 版本")
        self.about_button.clicked.connect(self.show_about)
        self.statusBar().addPermanentWidget(self.about_button)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("documentTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.close_document_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)
        central = QWidget(self.tabs)
        self.finder_panel = central
        self.tabs.addTab(central, "搜尋文件")
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.LeftSide, None)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 26, 28, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(14)
        logo = QLabel("MD")
        logo.setObjectName("logo")
        logo.setFixedSize(48, 48)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Markdown Reader")
        title.setObjectName("title")
        titles.addWidget(title)
        subtitle = QLabel("找到文件，在分頁裡接著閱讀。")
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.scan_button = QPushButton("重新掃描  F5")
        self.scan_button.clicked.connect(self._scan_button_clicked)
        header.addWidget(self.scan_button)
        layout.addLayout(header)
        self.scope = QLabel(f"搜尋範圍  {one_line(display_path(self.root))}   ·   .md / .MD")
        self.scope.setObjectName("scope")
        self.scope.setTextFormat(Qt.TextFormat.PlainText)
        self.scope.setWordWrap(True)
        self.scope.setToolTip(
            f"{self.root}\n包含隱藏目錄與 .gitignore 忽略的檔案。\n"
            "不遞迴跟隨目錄符號連結；無權限的項目會跳過。\n"
            "新增、刪除或移動檔案後，可按 F5 更新清單。"
        )
        layout.addWidget(self.scope)
        self.search_input = SearchInput()
        self.search_input.setObjectName("searchInput")
        self.search_input.setAccessibleName("搜尋 Markdown 檔名與路徑")
        self.search_input.setPlaceholderText("搜尋文件，例如：readme、cpu pipe、設計筆記")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.move_selection.connect(self.move_selection)
        self.search_input.open_selection.connect(self.open_selected)
        layout.addWidget(self.search_input)
        info = QHBoxLayout()
        self.result_count = QLabel("準備搜尋")
        self.result_count.setObjectName("resultCount")
        info.addWidget(self.result_count, 1)
        self.search_timing = QLabel("")
        self.search_timing.setObjectName("help")
        info.addWidget(self.search_timing)
        layout.addLayout(info)

        self.model = ResultModel(self)
        self.results = QListView()
        self.results.setObjectName("results")
        self.results.setAccessibleName("Markdown 搜尋結果")
        self.results.setModel(self.model)
        self.results.setItemDelegate(FileDelegate(self.results))
        self.results.setUniformItemSizes(True)
        self.results.setMouseTracking(True)
        self.results.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.results.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.results.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.results.selectionModel().currentChanged.connect(self._selection_changed)
        self.results.doubleClicked.connect(self.open_selected)
        self.results.installEventFilter(self)
        self.empty_label = QLabel("準備掃描文件…")
        self.empty_label.setObjectName("empty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.result_stack = QStackedWidget()
        self.result_stack.addWidget(self.results)
        self.result_stack.addWidget(self.empty_label)
        self.result_stack.setCurrentWidget(self.empty_label)
        layout.addWidget(self.result_stack, 1)

        helper = QLabel("Ctrl+P 搜尋　　↑ ↓ 選取　　Enter 開啟　　Esc 返回／清除　　Ctrl+Shift+C 複製路徑")
        helper.setObjectName("help")
        helper.setWordWrap(True)
        layout.addWidget(helper)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.selected_path = QLabel("選取一份文件，在新的閱讀分頁開啟。")
        self.selected_path.setObjectName("selectedPath")
        self.selected_path.setTextFormat(Qt.TextFormat.PlainText)
        self.selected_path.setWordWrap(True)
        self.selected_path.setMinimumWidth(0)
        footer.addWidget(self.selected_path, 1)
        self.copy_button = QPushButton("複製路徑")
        self.copy_button.clicked.connect(self.copy_path)
        self.copy_button.setEnabled(False)
        footer.addWidget(self.copy_button)
        self.open_button = QPushButton("開啟文件  ↵")
        self.open_button.setObjectName("openButton")
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.setEnabled(False)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)
        self.scan_status = QLabel("檔案清單保留於本次執行；按 F5 可重新掃描。")
        self.scan_status.setObjectName("scanStatus")
        self.scan_status.setWordWrap(True)
        layout.addWidget(self.scan_status)

    def show_about(self):
        QMessageBox.about(
            self, "關於 Markdown Reader",
            f"Markdown Reader\n\n版本：{__version__}\n套件／啟動指令：markdown-finder",
        )

    def focus_search(self):
        self.tabs.setCurrentWidget(self.finder_panel)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def clear_search(self):
        self.search_input.clear()
        self.search_input.setFocus()

    def escape_action(self):
        tab = self.current_reader()
        if tab is not None:
            tab.close_find()
        elif self._last_reader is not None and self.tabs.indexOf(self._last_reader) >= 0:
            self.tabs.setCurrentWidget(self._last_reader)
            self._last_reader.view.setFocus()
        else:
            self.clear_search()

    def current_reader(self) -> DocumentTab | None:
        current = self.tabs.currentWidget()
        return current if isinstance(current, DocumentTab) else None

    def reader_action(self, method: str):
        reader = self.current_reader()
        if reader is not None:
            getattr(reader, method)()

    def cycle_tabs(self, direction: int):
        self.tabs.setCurrentIndex((self.tabs.currentIndex() + direction) % self.tabs.count())

    def _tab_changed(self, index):
        self._update_reader_status()
        reader = self.current_reader()
        if reader is not None:
            self._last_reader = reader
            self.setWindowTitle(f"{reader.path.name} — Markdown Reader")
        else:
            self.setWindowTitle("Markdown Reader")

    def _update_reader_status(self):
        reader = self.current_reader()
        message = reader.status_message if reader is not None else ""
        self.reader_status.setText(" ".join(message.splitlines()))
        self.reader_status.setToolTip(message)

    def open_document(self, path: Path, fragment: str = "") -> DocumentTab | None:
        if self._closing:
            return None
        path = path.expanduser().absolute()
        if not path.is_file():
            self._show_document_error("檔案已移動或刪除，請按 F5 重新掃描。")
            return None
        if path.suffix.casefold() not in (".md", ".markdown"):
            self._show_document_error("目前支援開啟 .md 與 .markdown 文件。")
            return None
        key = str(path.resolve())
        tab = self._documents.get(key)
        if tab is None:
            tab = DocumentTab(path, self.tabs, external_opener=self.opener)
            self._documents[key] = tab
            tab.document_ready.connect(lambda document, tab=tab: self._document_ready(tab, document))
            tab.open_markdown_requested.connect(self.open_document)
            tab.error_occurred.connect(self._show_document_error)
            tab.status_changed.connect(self._update_reader_status)
            index = self.tabs.addTab(tab, path.name)
            self.tabs.setTabToolTip(index, str(path))
        if not self._restoring_session:
            if tab.loaded:
                self._remember_document(tab.path)
            else:
                self._pending_recent.add(tab)
        self.tabs.setCurrentWidget(tab)
        if fragment:
            tab.scroll_to_anchor(fragment)
        tab.view.setFocus()
        return tab

    def _document_ready(self, tab: DocumentTab, document):
        if self._closing or tab in self._closing_documents:
            return
        if tab in self._pending_recent:
            self._pending_recent.remove(tab)
            self._remember_document(tab.path)
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabToolTip(index, f"{document.title}\n{tab.path}")

    def _remember_document(self, path: Path):
        path = str(path)
        self.recent = [path] + [item for item in self.recent if item != path]
        self.recent = self.recent[:40]
        self.settings.setValue("recent_files", self.recent)

    def _show_document_error(self, message: str):
        if not self._closing:
            self.selected_path.setText(message)
            self.statusBar().showMessage(message, 8000)

    def close_current_tab(self):
        self.close_document_tab(self.tabs.currentIndex())

    def close_document_tab(self, index: int):
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocumentTab):
            return
        for key, value in tuple(self._documents.items()):
            if value is tab:
                del self._documents[key]
        if self._last_reader is tab:
            self._last_reader = None
        self._pending_recent.discard(tab)
        self.tabs.removeTab(index)
        tab.begin_close()
        self._closing_documents.add(tab)
        self.reader_cleanup_timer.start()
        if self.current_reader() is None:
            self.search_input.setFocus()

    def _collect_closed_tabs(self):
        for tab in tuple(self._closing_documents):
            if not tab.is_busy():
                self._closing_documents.remove(tab)
                tab.deleteLater()
        if not self._closing_documents:
            self.reader_cleanup_timer.stop()

    def eventFilter(self, watched, event):
        if watched is self.results and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.open_selected()
                return True
        return super().eventFilter(watched, event)

    @Slot()
    def start_scan(self):
        if self._closing or self._scan is not None:
            return
        self._scan_generation += 1
        self._entry_count = 0
        self._stats = ScanStats()
        self._scanning = True
        self._scan_started = time.perf_counter()
        self._request_id += 1
        self._search_inflight = None
        self._stream_dirty = False
        self._results_current = False
        self.query_timer.stop()
        self.stream_timer.stop()
        self.search_worker.reset_entries()
        self.model.replace([])
        self._selection_changed()
        self.result_stack.setCurrentWidget(self.empty_label)
        self.empty_label.setText("正在尋找 Markdown 文件…\n掃描期間也可以輸入關鍵字。")
        self.result_count.setText("正在建立文件清單…")
        self.scan_status.setText("正在掃描目錄…")
        self.search_timing.clear()
        self.scan_button.setText("停止掃描")
        self.scan_button.setEnabled(True)
        self._scan = ScanThread(self.root, self._scan_generation, self)
        self._scan.batch_ready.connect(self._batch_ready)
        self._scan.progress.connect(self._scan_progress)
        self._scan.completed.connect(self._scan_completed)
        self._scan.failed.connect(self._scan_failed)
        self._scan.finished.connect(self._scan_thread_finished)
        self._scan.start()

    def _scan_button_clicked(self):
        if self._scan is not None:
            self._scan.requestInterruption()
            self.scan_button.setEnabled(False)
            self.scan_button.setText("正在停止…")
        else:
            self.start_scan()

    @Slot(int, object)
    def _batch_ready(self, generation, batch):
        if generation != self._scan_generation or self._closing:
            return
        self._entry_count += len(batch)
        self.search_worker.append_entries(batch)
        self._stream_dirty = True
        if not self.stream_timer.isActive():
            self.stream_timer.start()

    @Slot(int, object)
    def _scan_progress(self, generation, stats):
        if generation != self._scan_generation or self._closing:
            return
        self._stats = stats
        self.scan_status.setText(
            f"掃描中 · {stats.directories:,} 個目錄 · {stats.files_seen:,} 個檔案 · "
            f"找到 {stats.markdown_files:,} 份 Markdown · 無法存取 {stats.inaccessible:,} 項"
        )

    @Slot(int, object)
    def _scan_completed(self, generation, stats):
        if generation != self._scan_generation or self._closing:
            return
        self._scanning = False
        self._stats = stats
        self.last_scan_seconds = time.perf_counter() - self._scan_started
        state = "掃描已停止，清單尚未完整" if stats.cancelled else "掃描完成"
        self.scan_status.setText(
            f"{state} · {stats.markdown_files:,} 份文件 · {self.last_scan_seconds:.2f} 秒 · "
            f"無法存取 {stats.inaccessible:,} 項 · 略過 {stats.skipped_symlinks:,} 個符號連結"
        )
        self.scan_status.setToolTip("\n".join(stats.errors))
        self.stream_timer.stop()
        self._stream_dirty = True
        self._submit_stream_search()

    @Slot(int, str)
    def _scan_failed(self, generation, message):
        if generation != self._scan_generation or self._closing:
            return
        self._scanning = False
        self.scan_status.setText("掃描未完成：" + message)
        self.stream_timer.stop()
        self._stream_dirty = True
        self._submit_stream_search()

    @Slot()
    def _scan_thread_finished(self):
        thread = self._scan
        self._scan = None
        if thread is not None:
            thread.deleteLater()
        self.scan_button.setText("重新掃描  F5")
        self.scan_button.setEnabled(not self._closing)

    @Slot(str)
    def _query_changed(self, query):
        self._request_id += 1
        self._search_inflight = None
        self._results_current = False
        self.search_worker.invalidate()
        self._selection_changed()
        self.result_count.setText("搜尋中…")
        self.query_timer.start()

    def _submit_stream_search(self):
        # A new batch must not repeatedly cancel a slower search of the same
        # query. Finish that snapshot, then schedule the newest snapshot.
        if self._stream_dirty and not self.query_timer.isActive() and self._search_inflight is None:
            self._submit_search()

    def _submit_search(self):
        if self._closing:
            return
        self._request_id += 1
        self._search_inflight = self._request_id
        self._stream_dirty = False
        self.search_worker.submit(self._request_id, self.search_input.text(), self.recent)

    @Slot(int, object, float)
    def _search_completed(self, request_id: int, result: SearchResults, elapsed: float):
        if request_id != self._request_id or self._closing:
            return
        self._search_inflight = None
        query = self.search_input.text()
        previous = self.selected_entry() if query == self._displayed_query else None
        self._displayed_query = query
        self._results_current = True
        self.model.replace(result.matches)
        self.last_search_seconds = elapsed
        if result.matches:
            row = next((i for i, hit in enumerate(result.matches) if previous and hit.entry.path == previous.path), 0)
            self.results.setCurrentIndex(self.model.index(row, 0))
            self.result_stack.setCurrentWidget(self.results)
        else:
            if self._scanning:
                text = "目前還沒有符合的文件\n掃描仍在進行，結果會陸續加入。"
            elif self.search_input.text().strip():
                text = "找不到符合的文件\n試試較短的檔名，或用空格分開路徑關鍵字。"
            else:
                text = "這個範圍尚未找到 Markdown 文件\n新增文件後，按 F5 重新掃描。"
            self.empty_label.setText(text)
            self.result_stack.setCurrentWidget(self.empty_label)
        suffix = " · 掃描中" if self._scanning else ""
        prefix = "符合" if self.search_input.text().strip() else "全部"
        cap = f" · 顯示前 {len(result.matches)} 筆" if result.total_matches > len(result.matches) else ""
        self.result_count.setText(f"{prefix} {result.total_matches:,} 份文件{cap}{suffix}")
        self.search_timing.setText(f"比對 {elapsed * 1000:.1f} ms")
        self._selection_changed()
        if self._stream_dirty and not self.stream_timer.isActive():
            self.stream_timer.start()

    @Slot(int, str)
    def _search_failed(self, request_id, message):
        if request_id != self._request_id or self._closing:
            return
        self._search_inflight = None
        self._results_current = False
        self.result_count.setText("搜尋失敗：" + message)
        self._selection_changed()

    def selected_entry(self):
        return self.results.currentIndex().data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self, *args):
        entry = self.selected_entry() if self._results_current else None
        self.copy_button.setEnabled(entry is not None)
        self.open_button.setEnabled(entry is not None)
        if entry is None:
            self.selected_path.setText("選取一份文件，在新的閱讀分頁開啟。")
            self.selected_path.setToolTip("")
        else:
            path = one_line(display_path(entry.path))
            self.selected_path.setText(self.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, 420))
            self.selected_path.setToolTip(entry.path)

    def move_selection(self, offset):
        count = self.model.rowCount()
        if not count:
            return
        current = self.results.currentIndex().row()
        row = max(0, min(count - 1, current + offset))
        self.results.setCurrentIndex(self.model.index(row, 0))
        self.results.scrollTo(self.model.index(row, 0))

    def copy_path(self):
        reader = self.current_reader()
        if reader is not None:
            QApplication.clipboard().setText(str(reader.path))
            self.statusBar().showMessage("已複製完整路徑", 2000)
            return
        entry = self.selected_entry()
        if entry is not None and self._results_current:
            QApplication.clipboard().setText(entry.path)
            self.selected_path.setText("已複製完整路徑")

    def open_selected(self, *args):
        entry = self.selected_entry()
        if entry is None or not self._results_current:
            return
        self.open_document(Path(entry.path))

    def _background_busy(self):
        return (
            self.search_worker.isRunning()
            or (self._scan is not None and self._scan.isRunning())
            or any(tab.is_busy() for tab in (*self._documents.values(), *self._closing_documents))
        )

    def closeEvent(self, event):
        if not self._closing:
            self.save_session()
            self._closing = True
            self.session_timer.stop()
            self.query_timer.stop()
            self.stream_timer.stop()
            self.search_worker.stop()
            if self._scan is not None:
                self._scan.requestInterruption()
            for tab in (*self._documents.values(), *self._closing_documents):
                tab.begin_close()
            self.settings.setValue("window_geometry", self.saveGeometry())
            self.settings.sync()
        if self._background_busy():
            event.ignore()
            self.centralWidget().setEnabled(False)
            self.scan_status.setText("正在停止背景工作…")
            self.shutdown_timer.start()
        else:
            self.shutdown_timer.stop()
            event.accept()

    def _finish_close(self):
        if not self._background_busy():
            self.close()


def configure_application(app: QApplication):
    app.setApplicationName("MarkdownFinder")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("MarkdownFinder")
    app.setApplicationDisplayName("Markdown Reader")
    app.setDesktopFileName("markdown-finder")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets/markdown-finder.svg")))
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f6f8fb"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#243550"))
    palette.setColor(QPalette.ColorRole.Base, QColor("white"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#243550"))
    palette.setColor(QPalette.ColorRole.Button, QColor("white"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#243550"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#dce8ff"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#172b4d"))
    app.setPalette(palette)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 Qt 快速搜尋本機 Markdown 檔案")
    parser.add_argument("--version", action="version", version=f"markdown-finder {__version__}")
    parser.add_argument("--root", type=Path, default=Path.home(), help="搜尋根目錄（預設為家目錄）")
    parser.add_argument("files", nargs="*", type=Path, help="啟動時在分頁開啟的 Markdown 文件")
    args = parser.parse_args(argv)
    root = args.root.expanduser().absolute()
    if not root.is_dir():
        parser.error(f"搜尋根目錄不存在或無法存取：{root}")
    app = QApplication([sys.argv[0]])
    configure_application(app)
    window = FinderWindow(root)
    window.show()
    for path in args.files:
        window.open_document(path)
    result = app.exec()
    # WebEngine pages must be destroyed while QApplication and its default
    # profile are still alive, including when the last window stops the loop.
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
