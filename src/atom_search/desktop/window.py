from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from atom_search.content import message, message_html
from atom_search.service.contracts import ServiceError
from atom_search.service.mutations import MutationCoordinator
from atom_search.service.sources import SourceRegistry
from atom_search_core.source import canonical_root
from atom_search.settings.repository import SettingsRepository
from atom_search.runtime.storage import StorageBudgetError
from atom_search.runtime._windows import WindowsError

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPushButton, QSpinBox, QStackedWidget, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from .workers import Workers


STYLE = """
QWidget { background: #0F0F15; color: #F5F5F7; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow { background: #0F0F15; }
QLabel { background: transparent; }
QLabel#subtitle, QLabel#source, QLabel#keys { color: #9393A5; }
QLabel#source { font-size: 12px; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#message { color: #C8D5FF; padding: 11px 14px; background: #1D2438; border-radius: 8px; }
QLabel#emptyTitle { color: #D8D8E3; font-size: 19px; font-weight: 600; }
QLabel#emptyBody { color: #9393A5; font-size: 13px; }
QLineEdit { border: 1px solid #323241; border-radius: 10px; background: #17171F; padding: 10px 13px; selection-background-color: #536BAE; }
QLineEdit:focus { border: 1px solid #7C9CFF; }
QLineEdit#search { font-size: 18px; padding: 14px 18px; border-radius: 12px; }
QPushButton { padding: 8px 13px; border: 1px solid #343443; border-radius: 7px; background: #1C1C27; }
QPushButton:hover { background: #292938; border-color: #55556A; }
QPushButton:focus { border: 1px solid #7C9CFF; }
QPushButton:disabled { color: #606071; border-color: #282833; background: #17171F; }
QPushButton#primary { background: #7C9CFF; color: #101525; border-color: #7C9CFF; font-weight: 600; }
QPushButton#primary:hover { background: #9AB3FF; }
QListWidget { background: #17171F; border: 1px solid #292936; border-radius: 11px; outline: none; padding: 6px; }
QListWidget:focus { border-color: #7C9CFF; }
QScrollBar:vertical { background: #17171F; width: 9px; margin: 5px 1px; }
QScrollBar::handle:vertical { background: #444454; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSpinBox { padding: 8px; border: 1px solid #343443; border-radius: 6px; background: #17171F; }
QFrame#statusPanel { border-top: 1px solid #292936; }
"""


def resource(*parts):
    from atom_search.paths import resource_path
    return resource_path(*parts)


def size_text(value: int) -> str:
    number = float(max(0, value))
    units = ("size.bytes", "size.kilobytes", "size.megabytes", "size.gigabytes", "size.terabytes")
    for index, key in enumerate(units):
        if number < 1024 or index == len(units) - 1:
            return message(key, number=str(int(number)) if index == 0 else f"{number:.1f}")
        number /= 1024
    return ""


class ResultDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(600, 76)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        result = index.data(Qt.ItemDataRole.UserRole)
        if result is None:
            return
        painter.save()
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected:
            painter.setBrush(QColor("#283553"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 7, 7)
        suffix = Path(result.name).suffix.lstrip(".").upper()[:5] or message("result.file_badge")
        badge = rect.adjusted(12, 16, 0, 0)
        badge.setSize(QSize(47, 37))
        painter.setBrush(QColor("#242432"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge, 5, 5)
        font = QFont("Segoe UI", 8)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#B4C6FF"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, suffix)
        text_x = rect.left() + 73
        text_w = max(30, rect.width() - 176)
        name = message("result.stale_name", name=result.name) if result.stale else result.name
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        painter.setPen(QColor("#F5F5F7"))
        painter.drawText(text_x, rect.top() + 29,
                         painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, text_w))
        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(QColor("#B0B0C1" if selected else "#9393A5"))
        parent = result.relative_path.replace("/", "\\").rsplit("\\", 1)
        path = result.root + ("\\" + parent[0] if len(parent) > 1 else "")
        painter.drawText(text_x, rect.top() + 51,
                         painter.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, text_w))
        painter.drawText(rect.adjusted(0, 0, -14, 0),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         size_text(result.size))
        painter.restore()


class SearchWindow(QMainWindow):
    def __init__(self, *, data_dir: Path, service_factory, root: str | None = None,
                 auto_scan: bool = True, reveal_func=None, model_dir: Path | None = None,
                 service_owner=None):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.service_owner = service_owner
        self.settings_repository = (service_owner.repository if service_owner is not None
                                    else SettingsRepository(self.data_dir, model_dir=model_dir))
        self._settings_snapshot = (self.settings_repository.snapshot() if service_owner is not None
                                   else self.settings_repository.load())
        settings = self._settings_snapshot.settings
        self._source_index = next((i for i, source in enumerate(settings['sources']) if source['enabled']), 0)
        selected = settings['sources'][self._source_index] if settings['sources'] else None
        self.root = root or (selected['path'] if selected else '')
        self.refresh_days = settings['indexing']['interval_seconds'] // 86400
        state = self.settings_repository.runtime_state()
        compatible_state = state.get('source_path', self.root) == self.root
        self._last_attempt = float(state['last_attempt']) if compatible_state else 0
        self._last_refresh = float(state['last_refresh']) if compatible_state else 0
        self._auto_scan_requested = auto_scan
        self._auto_scan = auto_scan and (bool(root) or bool(selected and selected['enabled']))
        self._closing = False
        self._search_generation = 0
        self._search_jobs = {}
        self._status_jobs = {}
        self._source_generation = 0
        self._scan_context = None
        self._embedding_jobs = {}
        self._scan_job = -1
        self._reveal_jobs = set()
        self._reveal = reveal_func or self._default_reveal
        self.source_registry = None
        self._starting_registry = None
        def factory():
            service = service_owner.desktop_view() if service_owner is not None else service_factory()
            if service_owner is not None:
                # start() may attach this shell to an already-running owner.
                self.settings_repository = service_owner.repository
                self.settings_coordinator = service_owner.coordinator
            runtime = getattr(service, 'runtime', None)
            if runtime is not None:
                try:
                    self.settings_repository.bind_runtime_budget(runtime.storage_budget)
                except Exception:
                    service.close()
                    raise
            try:
                identities = getattr(service, 'public_ids', None)
                if service_owner is not None or identities is not None:
                    registry = service_owner.registry if service_owner is not None else SourceRegistry(self.settings_repository, identities)
                    # --root is an explicit owner selection, with exactly the
                    # same journal/UUID rules as choosing a folder after start.
                    if root is not None:
                        current = self.settings_repository.snapshot()
                        candidate = current.settings
                        selected = candidate['sources'][self._source_index] if candidate['sources'] else None
                        if selected is None or canonical_root(selected['path']) != canonical_root(root):
                            if selected is None:
                                selected = dict(id=registry.select_source_id(root), path=root, label='', enabled=True)
                                candidate['sources'].append(selected)
                            else:
                                selected.update(id=registry.select_source_id(root), path=root)
                            self._starting_registry = registry
                            try:
                                self._settings_snapshot = self.settings_coordinator.owner_apply(candidate, current.revision)
                            finally:
                                self._starting_registry = None
                    registry.activate()
                    self.source_registry = registry
            except Exception:
                service.close()
                raise
            return service
        self.workers = Workers(factory, self)
        self.settings_coordinator = (service_owner.coordinator if service_owner is not None else
            MutationCoordinator(self.settings_repository, quiesce=self._quiesce_settings, apply=self._apply_settings))
        if service_owner is not None:
            self.workers.on_close = service_owner.close
        self.workers.signals.finished.connect(self._finished)
        self.workers.signals.failed.connect(self._failed)
        self.workers.signals.progress.connect(self._progress)
        self.setWindowTitle(message("app.title"))
        self.setWindowIcon(QIcon(str(resource("branding", "atom-search.ico"))))
        self.setMinimumSize(760, 540)
        self.resize(980, 690)
        self.setStyleSheet(STYLE)
        self._build()
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(150)
        self.debounce.timeout.connect(self._search)
        self.search_box.textChanged.connect(self._query_changed)
        self.daily_timer = QTimer(self)
        self.daily_timer.setInterval(60_000)
        self.daily_timer.timeout.connect(self._auto_refresh)
        if self._auto_scan and self.service_owner is None:
            self.daily_timer.start()
        QTimer.singleShot(0, self._status)

    def _build(self):
        outer = QWidget()
        self.setCentralWidget(outer)
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(26, 23, 26, 15)
        layout.setSpacing(15)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel(message("app.name"))
        title.setObjectName("title")
        subtitle = QLabel(message("app.subtitle"))
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        layout.addLayout(header)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("search")
        self.search_box.setPlaceholderText(message("search.placeholder"))
        self.search_box.setAccessibleName(message("search.accessible_name"))
        self.search_box.setClearButtonEnabled(True)
        self.search_box.installEventFilter(self)
        layout.addWidget(self.search_box)
        toolbar = QHBoxLayout()
        self.source_label = QLabel(self.root or message("settings.no_source"))
        self.source_label.setObjectName("source")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_label.setToolTip(self.root)
        toolbar.addWidget(self.source_label, 1)
        self.refresh_button = QPushButton(message("action.refresh"))
        self.refresh_button.setObjectName("primary")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        self.cancel_button = QPushButton(message("action.cancel"))
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.cancel_button.setEnabled(False)
        toolbar.addWidget(self.cancel_button)
        settings = QPushButton(message("action.settings"))
        settings.clicked.connect(self.open_settings)
        toolbar.addWidget(settings)
        help_button = QPushButton(message("action.help"))
        help_button.clicked.connect(self.open_help)
        toolbar.addWidget(help_button)
        layout.addLayout(toolbar)
        self.message = QLabel()
        self.message.setObjectName("message")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        layout.addWidget(self.message)
        self.pages = QStackedWidget()
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        self.empty_title = QLabel(message("search.initial_title"))
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_body = QLabel(message("search.initial_body"))
        self.empty_body.setObjectName("emptyBody")
        self.empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_body.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addSpacing(5)
        empty_layout.addWidget(self.empty_body)
        empty_layout.addStretch()
        self.pages.addWidget(empty)
        self.results = QListWidget()
        self.results.setAccessibleName(message("result.accessible_name"))
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setItemDelegate(ResultDelegate(self.results))
        self.results.itemDoubleClicked.connect(self.reveal_selected)
        self.results.installEventFilter(self)
        self.pages.addWidget(self.results)
        layout.addWidget(self.pages, 1)
        bottom = QHBoxLayout()
        self.result_count = QLabel(message("result.initial_count"))
        self.result_count.setObjectName("keys")
        bottom.addWidget(self.result_count, 1)
        keys = QLabel(message("search.keyboard_hint"))
        keys.setObjectName("keys")
        bottom.addWidget(keys)
        layout.addLayout(bottom)
        panel = QFrame()
        panel.setObjectName("statusPanel")
        statuses = QHBoxLayout(panel)
        statuses.setContentsMargins(0, 12, 0, 0)
        self.index_status = QLabel(message("index.loading"))
        self.model_status = QLabel(message("model.checking"))
        self.network_status = QLabel(message("network.unchecked"))
        for label in (self.index_status, self.model_status, self.network_status):
            label.setObjectName("keys")
            statuses.addWidget(label)
        statuses.addStretch()
        self.last_updated = QLabel(message("updated.never"))
        self.last_updated.setObjectName("keys")
        statuses.addWidget(self.last_updated)
        layout.addWidget(panel)
        self.search_box.setFocus()
        if not self.root:
            self.refresh_button.setEnabled(False)
            self._show_message(message("settings.choose_source"))

    def _quiesce_settings(self, changes, timeout):
        if any(change.path == 'sources' for change in changes) and self._scan_job != -1:
            return self.workers.quiesce((self._scan_job,), timeout)
        # Embeddings are bound to immutable source/input identities and may
        # finish safely; after publication the old UI generation is cancelled.
        # Search/status readers do not participate in the mutation lock.
        return True

    def _apply_settings(self, snapshot):
        registry = self.source_registry or self._starting_registry
        if registry is not None:
            registry.apply(snapshot)

    def _save_runtime_state(self):
        try:
            self.settings_repository.save_runtime_state(last_attempt=self._last_attempt,
                last_refresh=self._last_refresh, source_path=self.root)
        except (OSError, ServiceError, StorageBudgetError, WindowsError):
            self._show_message(message('settings.save_failed'))

    def save_settings(self, root: str, refresh_days: int):
        root = root.strip()
        if not root:
            self._show_message(message("settings.root_required"))
            return False
        changed = root != self.root
        if type(refresh_days) is not int or not 1 <= refresh_days <= 365:
            self._show_message(message('settings.settings_invalid'))
            return False
        days = refresh_days
        last_attempt = 0 if changed else self._last_attempt
        last_refresh = 0 if changed else self._last_refresh
        try:
            current = self.settings_repository.snapshot()
            candidate = current.settings
            selected = candidate['sources'][self._source_index] if candidate['sources'] else None
            previous_root = selected['path'] if selected else ''
            if self.source_registry is None and (not previous_root or canonical_root(previous_root) != canonical_root(root)
                    or canonical_root(self.root) != canonical_root(root)):
                raise ServiceError('settings_busy', message('settings.registry_warming'), status_code=409)
            if selected is None:
                selected = dict(id=self.source_registry.select_source_id(root), path=root, label='', enabled=True)
                candidate['sources'].append(selected)
            elif canonical_root(previous_root) != canonical_root(root):
                selected['id'] = self.source_registry.select_source_id(root)
            selected['path'] = root
            candidate['indexing']['interval_seconds'] = days * 86400
            snapshot = self.settings_coordinator.owner_apply(candidate, current.revision)
        except ServiceError as error:
            self._show_message(error.message)
            return False
        except (OSError, StorageBudgetError, WindowsError):
            self._show_message(message("settings.save_failed"))
            return False
        self._settings_snapshot = snapshot
        self.root = snapshot.settings['sources'][self._source_index]['path']
        self.refresh_days = snapshot.settings['indexing']['interval_seconds'] // 86400
        self._auto_scan = self._auto_scan_requested and snapshot.settings['sources'][self._source_index]['enabled']
        self.refresh_button.setEnabled(snapshot.settings['sources'][self._source_index]['enabled'])
        if self._auto_scan and self.service_owner is None:
            self.daily_timer.start()
        self._last_attempt = last_attempt
        self._last_refresh = last_refresh
        if changed:
            self._save_runtime_state()
        if changed:
            self._source_generation += 1
            for job in self._embedding_jobs:
                self.workers.cancel(job)
            self._search_generation += 1
            self.results.clear()
            self.pages.setCurrentIndex(0)
            self.network_status.setText(message("network.unchecked"))
        self.source_label.setText(self.root)
        self.source_label.setToolTip(self.root)
        if changed:
            self._query_changed()
            self._status()
        return True

    def _query_changed(self, *_):
        self._search_generation += 1
        self.debounce.stop()
        if not self.search_box.text().strip():
            self.results.clear()
            self.pages.setCurrentIndex(0)
            self.empty_title.setText(message("search.initial_title"))
            self.empty_body.setText(message("search.initial_body"))
            self.result_count.setText(message("result.initial_count"))
            return
        self.debounce.start()

    def _search(self):
        query = self.search_box.text().strip()
        if not self._settings_snapshot.settings['sources'] or not self._settings_snapshot.settings['sources'][self._source_index]['enabled']:
            self.results.clear()
            self.pages.setCurrentIndex(0)
            return
        if not query:
            return
        generation = self._search_generation
        root = self.root
        job = self.workers.start("search", lambda service, progress, cancel: service.search(query, limit=100, root=root))
        self._search_jobs[job] = generation

    def _status(self):
        if not self._closing:
            root = self.root or None
            job = self.workers.start("status", lambda service, progress, cancel: service.status(root=root))
            self._status_jobs[job] = (self._source_generation, self.root)

    def _auto_refresh(self):
        if self.service_owner is not None:
            return  # The shared owner schedules every source, including attached views.
        if self._auto_scan and time.time() - self._last_attempt >= self.refresh_days * 86400:
            self.refresh()

    def refresh(self):
        if self._scan_job != -1 or self._closing:
            return
        if not self._settings_snapshot.settings['sources'] or not self._settings_snapshot.settings['sources'][self._source_index]['enabled']:
            self._show_message(message('settings.source_disabled'))
            return
        self._source_generation += 1
        for job in self._embedding_jobs:
            self.workers.cancel(job)
        self._scan_context = (self._source_generation, self.root)
        self._last_attempt = time.time()
        self._save_runtime_state()
        self.refresh_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.network_status.setText(message("network.checking"))
        self._show_message(message("scan.started"))
        root = self.root
        self._scan_job = self.workers.start("scan", lambda service, progress, cancel: service.scan(root, progress=progress, cancel=cancel))

    def cancel_scan(self):
        if self._scan_job != -1:
            self.workers.cancel(self._scan_job)
            self.cancel_button.setEnabled(False)
            self._show_message(message("scan.cancelling"))

    def _progress(self, job, value):
        if job in self._embedding_jobs:
            if not self._closing and self._embedding_jobs[job] == (self._source_generation, self.root):
                self.model_status.setText(message('model.indexing', count=value.get('indexed', 0)))
            return
        if self._closing or job != self._scan_job or not isinstance(value, dict):
            return
        count = value.get("files", value.get("processed", 0))
        self._show_message(message("scan.progress", count=f"{count:,}".replace(",", " ")))

    def _finished(self, job, kind, value):
        if self._closing:
            return
        if kind == "search":
            generation = self._search_jobs.pop(job, -1)
            if generation != self._search_generation:
                return
            self.results.clear()
            for result in value:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, result)
                item.setToolTip(result.full_path)
                item.setText(result.name)
                self.results.addItem(item)
            count = self.results.count()
            self.pages.setCurrentIndex(1 if count else 0)
            if count:
                self.results.setCurrentRow(0)
            else:
                self.empty_title.setText(message("search.empty_title"))
                self.empty_body.setText(message("search.empty_body"))
            self.result_count.setText(message("result.count_limited" if count == 100 else "result.count", count=count))
        elif kind == "status":
            if self._status_jobs.pop(job, None) != (self._source_generation, self.root):
                return
            count = value.get('files')
            self.index_status.setText(message('index.loading') if count is None else
                message('index.count', count=f'{count:,}'.replace(',', ' ')))
            ready = bool(value.get("semantic_ready"))
            self.model_status.setText(message("model.ready") if ready else message("model.unavailable"))
            self.model_status.setToolTip(message("model.ready_tooltip") if ready else message("model.unavailable_tooltip"))
            last = value.get("last_success")
            try:
                moment = (last.astimezone() if isinstance(last, datetime) else
                          datetime.fromisoformat(last).astimezone()) if last else (
                    datetime.fromtimestamp(self._last_refresh) if self._last_refresh else None)
                self.last_updated.setText(message("updated.at", date=moment.strftime("%d.%m %H:%M")) if moment else message("updated.never"))
            except (ValueError, TypeError, OSError):
                self.last_updated.setText(message("updated.unknown"))
            self._auto_refresh()
        elif kind == "scan":
            if job != self._scan_job:
                return
            self._scan_job = -1
            self.refresh_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            if self._scan_context != (self._source_generation, self.root):
                return
            status = value.get("status", "unavailable")
            if status == "complete":
                self._last_refresh = time.time()
                self._save_runtime_state()
                self.network_status.setText(message("network.available"))
                self._show_message(message("scan.complete", date=time.strftime("%d.%m.%Y, %H:%M")))
            elif status == "partial":
                self.network_status.setText(message("network.partial"))
                self._show_message(message("scan.partial"))
            elif status == "cancelled":
                self.network_status.setText(message("network.cancelled"))
                self._show_message(message("scan.cancelled"))
            else:
                self.network_status.setText(message("network.unavailable"))
                self._show_message(message("scan.unavailable"))
            self._status()
            self._query_changed()
            if status in ('complete', 'partial') and self.service_owner is None:
                embedding_job = self.workers.start('embeddings', lambda service, progress, cancel:
                    service.index_embeddings(progress=progress, cancel=cancel))
                self._embedding_jobs[embedding_job] = (self._source_generation, self.root)
        elif kind == 'embeddings':
            if self._embedding_jobs.pop(job, None) == (self._source_generation, self.root):
                self._status()
        elif kind == "reveal":
            self._reveal_jobs.discard(job)
            self._show_message(value.message)

    def _failed(self, job, kind, error_message):
        if self._closing:
            return
        if kind == "search" and self._search_jobs.pop(job, -1) != self._search_generation:
            return
        if kind == "status" and self._status_jobs.pop(job, None) != (self._source_generation, self.root):
            return
        if kind == 'embeddings':
            if self._embedding_jobs.pop(job, None) != (self._source_generation, self.root):
                return
            self.model_status.setText(message('model.index_failed'))
            return
        if kind == "scan":
            self._scan_job = -1
            self.refresh_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            self.network_status.setText(message("network.scan_failed"))
        elif kind == "status":
            self.index_status.setText(message("index.unavailable"))
            self.model_status.setText(message("model.status_failed"))
        self._reveal_jobs.discard(job)
        self._show_message(error_message)

    @staticmethod
    def _default_reveal(path, root):
        from atom_search.windows.reveal import reveal
        return reveal(path, root)

    def reveal_selected(self, *_):
        item = self.results.currentItem()
        if item is None or self._reveal_jobs:
            return
        result = item.data(Qt.ItemDataRole.UserRole)
        self._show_message(message("reveal.started"))
        job = self.workers.start("reveal", lambda service, progress, cancel: self._reveal(result.full_path, result.root))
        self._reveal_jobs.add(job)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.search_box.clear()
                self.search_box.setFocus()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.reveal_selected()
                return True
            if watched is self.search_box and key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                count = self.results.count()
                if count:
                    delta = 1 if key == Qt.Key.Key_Down else -1
                    self.results.setCurrentRow(max(0, min(count - 1, self.results.currentRow() + delta)))
                return True
        return super().eventFilter(watched, event)

    def _show_message(self, text):
        self.message.setText(str(text))
        self.message.setVisible(bool(text))

    def open_help(self):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(resource("help.html")))):
            self._show_message(message("help.open_failed"))

    def open_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(message("settings.title"))
        dialog.setMinimumWidth(540)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        form = QFormLayout()
        root = QLineEdit(self.root)
        root.setAccessibleName(message("settings.root"))
        root.setPlaceholderText(message("settings.root_placeholder"))
        picker = QPushButton(message("settings.choose_folder"))
        def choose_folder():
            selected = QFileDialog.getExistingDirectory(dialog, message("settings.choose_folder"), root.text())
            if selected:
                root.setText(selected)
        picker.clicked.connect(choose_folder)
        folder = QHBoxLayout()
        folder.addWidget(root, 1)
        folder.addWidget(picker)
        form.addRow(message("settings.root"), folder)
        days = QSpinBox()
        days.setRange(1, 365)
        days.setValue(self.refresh_days)
        days.setSuffix(message("settings.days_suffix"))
        form.addRow(message("settings.refresh_interval"), days)
        layout.addLayout(form)
        note = QLabel(message("settings.note"))
        note.setWordWrap(True)
        note.setObjectName("subtitle")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(message("action.save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(message("action.cancel"))
        about = buttons.addButton(message("action.about"), QDialogButtonBox.ButtonRole.HelpRole)
        about.clicked.connect(self.open_about)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_settings(root.text(), days.value())

    def open_about(self):
        from atom_search import VERSION
        dialog = QDialog(self)
        dialog.setWindowTitle(message("about.title"))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 24)
        label = QLabel(message_html("about.body", version=VERSION,
            license_url=QUrl.fromLocalFile(str(resource("..", "LICENSE.txt"))).toString(),
            notices_url=QUrl.fromLocalFile(str(resource("..", "THIRD_PARTY_NOTICES.txt"))).toString()))
        label.setOpenExternalLinks(True)
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(message("action.close"))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def closeEvent(self, event):
        self._closing = True
        self.debounce.stop()
        self.daily_timer.stop()
        self.workers.shutdown()
        event.accept()
