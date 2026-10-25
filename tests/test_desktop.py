"""Exercise the real offscreen window without contacting corporate sources."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import pytest
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from atom_search_core.service import SearchService
from atom_search.desktop.window import SearchWindow
from atom_search.content import message


APP = QApplication.instance() or QApplication([])


@dataclass
class Result:
    file_id: int = 1
    root: str = r"C:\work"
    relative_path: str = r"Чертежи\КМ-101.pdf"
    name: str = "КМ-101.pdf"
    size: int = 2048
    modified_ns: int = 0
    score: float = 1.0
    stale: bool = False

    @property
    def full_path(self):
        return self.root + "\\" + self.relative_path


class Service:
    def __init__(self):
        from test_source_registry import MemoryIdentities
        self.public_ids = MemoryIdentities()
        self.query_threads = []
        self.closed = False
        self.block_scan = threading.Event()
        self.scanning = threading.Event()
        self.cancellation = None

    def search(self, query, limit=100, *, root=None):
        self.query_threads.append(threading.get_ident())
        if query == "старый":
            time.sleep(.25)
        return [Result(name=query + ".pdf")]

    def status(self, *, root=None):
        return {"files": 1, "roots": [r"C:\work"], "last_success": None,
                "semantic_ready": False, "model_status": "missing"}

    def scan(self, root, progress=None, cancel=None):
        self.cancellation = cancel
        self.scanning.set()
        if progress:
            progress({"files": 1})
        self.block_scan.wait(1.5)
        return {"status": "cancelled" if cancel.is_set() else "complete",
                "files": 1, "added": 1, "updated": 0, "removed": 0, "errors": []}

    def close(self):
        self.closed = True

    def index_embeddings(self, *, progress=None, cancel=None):
        return {'indexed': 0, 'cancelled': cancel.is_set()}


def spin_until(predicate, timeout=2000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "Window did not reach the expected state"


def make_window(tmp_path, service=None, **kwargs):
    service = service or Service()
    # These interaction fixtures start with an already selected folder. The
    # separate --root scenario below covers its journaled startup selection.
    if not (tmp_path / 'settings.json').exists():
        (tmp_path / 'settings.json').write_text(json.dumps({'root': r'C:\work'}))
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: service,
                          root=r"C:\work", auto_scan=False, **kwargs)
    window.show()
    spin_until(lambda: "1" in window.index_status.text())
    return window, service


def test_folder_change_waits_for_registry_without_mutating_settings(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'root': 'C:/chosen'}), encoding='utf-8')
    release = threading.Event()
    service = Service()
    def blocked_factory():
        release.wait(3)
        return service
    window = SearchWindow(data_dir=tmp_path, service_factory=blocked_factory, auto_scan=False)
    window.show()
    before = (tmp_path / 'settings.json').read_bytes()
    try:
        assert window.save_settings('D:/new', 7) is False
        assert (tmp_path / 'settings.json').read_bytes() == before
        assert window.message.text() == message('settings.registry_warming')
        assert window.save_settings(window.root, 7) is True
        release.set()
        spin_until(lambda: getattr(window, 'source_registry', None) is not None)
        assert window.save_settings('D:/new', 7) is True
    finally:
        release.set(); window.close(); window.workers.wait()


def test_cli_root_warming_cannot_restore_persisted_root_behind_factory(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'root': 'C:/persisted'}))
    release = threading.Event()
    service = Service()
    def factory():
        release.wait(10)
        return service
    window = SearchWindow(data_dir=tmp_path, service_factory=factory, root='D:/requested', auto_scan=False)
    window.show()
    before = (tmp_path / 'settings.json').read_bytes()
    try:
        assert window.save_settings('C:/persisted', 7) is False
        assert (tmp_path / 'settings.json').read_bytes() == before
        assert window.root == 'D:/requested'
        assert window.message.text() == message('settings.registry_warming')
        release.set()
        spin_until(lambda: window.source_registry is not None, timeout=10000)
        selected = window.settings_repository.snapshot().settings['sources'][0]
        assert selected['path'] == window.root == 'D:/requested'
        assert service.public_ids.source_uuid(window.root) == selected['id']
    finally:
        release.set(); window.close(); window.workers.wait()


def test_folder_switch_and_revisit_preserve_separate_source_uuids(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'root': 'C:/work'}))
    window, service = make_window(tmp_path)
    try:
        first = window.settings_repository.snapshot().settings['sources'][0]['id']
        assert window.save_settings('D:/new', 7) is True
        second = window.settings_repository.snapshot().settings['sources'][0]['id']
        assert second != first
        assert window.save_settings('c:\\WORK\\', 7) is True
        assert window.settings_repository.snapshot().settings['sources'][0]['id'] == first
        assert len(service.public_ids.sources) == 2
    finally:
        window.close(); window.workers.wait()


def test_metadata_ready_before_blocked_embedding(tmp_path):
    class Blocked(Service):
        def __init__(self):
            super().__init__()
            self.embedding_started = threading.Event()
            self.release = threading.Event()

        def index_embeddings(self, *, progress=None, cancel=None):
            self.embedding_started.set()
            self.release.wait(2)
            return {'indexed': 1, 'cancelled': cancel.is_set()}
    service = Blocked()
    window, _ = make_window(tmp_path, service)
    try:
        service.block_scan.set()
        window.refresh()
        spin_until(service.embedding_started.is_set)
        assert window.refresh_button.isEnabled()
        window.search_box.setText('available')
        spin_until(lambda: window.results.count() == 1)
        assert not service.release.is_set()
    finally:
        service.release.set()
        window.close()


def test_old_embedding_progress_cannot_replace_current_source_status(tmp_path):
    window, _ = make_window(tmp_path)
    try:
        assert hasattr(window, '_embedding_jobs'), 'embedding generation ownership required'
        window._embedding_jobs[900] = (window._source_generation, window.root)
        window.save_settings(r'D:\new-source', 1)
        before = window.message.text()
        window._progress(900, {'stage': 'embeddings', 'indexed': 999})
        window._finished(900, 'embeddings', {'indexed': 999})
        assert window.message.text() == before
    finally:
        window.close()


def test_close_runs_runtime_cleanup_once_after_workers(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    marker = tmp_path / 'cleanup.txt'
    script = '''import sys, threading, time
from pathlib import Path
from PySide6.QtCore import QObject
from atom_search.desktop.workers import Workers
marker = Path(sys.argv[1])
started = threading.Event()
class Service:
 def close(self):
  time.sleep(.2)
  marker.write_text(marker.read_text() + '|closed' if marker.exists() else 'closed')
def operation(service, progress, cancel):
 started.set()
 cancel.wait(3)
 time.sleep(.2)
 marker.write_text('worker-ended')
owner = QObject()
workers = Workers(Service, owner)
workers.start('scan', operation)
assert started.wait(3)
workers.shutdown()
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-c', script, str(marker)], capture_output=True,
                            timeout=10, env=environment)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), 'process must wait for worker and runtime cleanup'
    assert marker.read_text() == 'worker-ended|closed'


def test_debounced_search_runs_off_gui_and_ignores_old_results(tmp_path):
    window, service = make_window(tmp_path)
    try:
        window.search_box.setText("старый")
        QTest.qWait(200)
        window.search_box.setText("новый")
        spin_until(lambda: window.results.count() == 1 and
                   window.results.item(0).data(Qt.ItemDataRole.UserRole).name == "новый.pdf")
        QTest.qWait(300)
        APP.processEvents()
        assert window.results.item(0).data(Qt.ItemDataRole.UserRole).name == "новый.pdf"
        assert service.query_threads and all(t != threading.get_ident() for t in service.query_threads)
    finally:
        window.close()


def test_enter_reveals_selected_result_and_escape_clears(tmp_path):
    reveal_calls = []

    def reveal(path, root):
        reveal_calls.append((path, root, threading.get_ident()))
        return type("Reveal", (), {"opened": True, "selected": False,
                    "folder": r"C:\work\Чертежи", "message": "Файл перемещён. Открыта папка."})()

    window, _ = make_window(tmp_path, reveal_func=reveal)
    try:
        window.search_box.setText("КМ")
        spin_until(lambda: window.results.count() == 1)
        QTest.keyClick(window.search_box, Qt.Key.Key_Return)
        spin_until(lambda: "Файл перемещён" in window.message.text())
        assert reveal_calls[0][:2] == (r"C:\work\Чертежи\КМ-101.pdf", r"C:\work")
        assert reveal_calls[0][2] != threading.get_ident()
        QTest.keyClick(window.search_box, Qt.Key.Key_Escape)
        assert window.search_box.text() == ""
        assert window.results.count() == 0
    finally:
        window.close()


def test_close_cancels_scan_without_waiting_for_network_and_closes_when_idle(tmp_path):
    window, service = make_window(tmp_path)
    window.refresh()
    spin_until(service.scanning.is_set)
    assert window.cancel_button.isEnabled()
    began = time.monotonic()
    window.close()
    assert time.monotonic() - began < .2
    assert service.cancellation.is_set()
    assert not service.closed
    service.block_scan.set()
    spin_until(lambda: service.closed)


def test_settings_are_local_and_next_session_recovers_source(tmp_path):
    (tmp_path / 'settings.json').write_text('{"root":"C:/work","storage":{"quota_bytes":987654321,"min_free_bytes":1234}}')
    window, _ = make_window(tmp_path)
    window.save_settings(r"D:\Документы", 3)
    window.close()
    next_window = SearchWindow(data_dir=tmp_path, service_factory=Service, auto_scan=False)
    try:
        assert next_window.root == r"D:\Документы"
        assert next_window.refresh_days == 3
        assert (tmp_path / "settings.json").exists()
        import json
        assert json.loads((tmp_path / 'settings.json').read_text())['storage']['quota_bytes'] == 987654321
    finally:
        next_window.close()


def settings_window_state(window):
    return (
        window.root, window.refresh_days, window._last_attempt, window._last_refresh,
        window._source_generation, window._search_generation,
        window.source_label.text(), window.source_label.toolTip(),
        window.network_status.text(), window.index_status.text(),
        window.model_status.text(), window.last_updated.text(),
        window.result_count.text(), window.pages.currentIndex(),
        window.results.currentItem(), window.results.count(),
        dict(window._search_jobs), dict(window._status_jobs),
        dict(window._embedding_jobs), window._scan_job, window._scan_context,
        window.debounce.isActive(), window.daily_timer.isActive(),
    )


@pytest.mark.parametrize('new_root', [r'D:\Документы', r'C:\work'])
def test_save_settings_replace_failure_preserves_live_window_and_file(tmp_path, monkeypatch, new_root):
    target = tmp_path / 'settings.json'
    original = b'{"root":"C:\\\\work","refresh_days":3,"last_attempt":123,"last_refresh":100}'
    target.write_bytes(original)
    window, _ = make_window(tmp_path)
    original = target.read_bytes()  # Startup has migrated the legacy fixture.
    release = threading.Event()
    started = threading.Event()
    cancellations = []

    def embedding(service, progress, cancel):
        cancellations.append(cancel)
        started.set()
        release.wait(3)
        return {'indexed': 0, 'cancelled': cancel.is_set()}

    try:
        window.search_box.setText('чертёж')
        spin_until(lambda: window.results.count() == 1)
        job = window.workers.start('embeddings', embedding)
        window._embedding_jobs[job] = (window._source_generation, window.root)
        spin_until(started.is_set)
        before = settings_window_state(window)

        def denied(source, destination):
            if destination == target:
                raise PermissionError('settings locked')
            real_replace(source, destination)

        real_replace = os.replace
        monkeypatch.setattr(os, 'replace', denied)
        assert window.save_settings(new_root, 7) is False
        assert target.read_bytes() == original
        assert settings_window_state(window) == before
        assert not cancellations[0].is_set()
        assert window.message.text() == message('settings.save_failed')
        assert not window.message.isHidden()
    finally:
        release.set()
        window.close()
        window.workers.wait()


def test_successful_save_publishes_window_after_replace(tmp_path, monkeypatch):
    target = tmp_path / 'settings.json'
    target.write_text(json.dumps({'root': r'C:\work', 'refresh_days': 3,
                                 'last_attempt': 123, 'last_refresh': 100,
                                 'storage': {'quota_bytes': 987654321}}), encoding='utf-8')
    window, service = make_window(tmp_path)
    updates = []
    try:
        window.search_box.setText('чертёж')
        spin_until(lambda: window.results.count() == 1)
        before = settings_window_state(window)
        real_replace = os.replace
        real_query_changed, real_status = window._query_changed, window._status

        def replace(source, destination):
            if destination == target:
                assert settings_window_state(window) == before
                assert updates == []
            real_replace(source, destination)

        def query_changed():
            updates.append('query')
            real_query_changed()

        def status():
            updates.append('status')
            real_status()

        monkeypatch.setattr(os, 'replace', replace)
        monkeypatch.setattr(window, '_query_changed', query_changed)
        monkeypatch.setattr(window, '_status', status)
        assert window.save_settings(r' D:\Документы ', 7)
        assert window.root == r'D:\Документы'
        assert window.refresh_days == 7
        assert window._last_attempt == window._last_refresh == 0
        assert window._source_generation > before[4]
        assert window._search_generation > before[5]
        assert window.source_label.text() == window.source_label.toolTip() == r'D:\Документы'
        assert window.results.count() == 0
        assert window.pages.currentIndex() == 0
        assert window.network_status.text() == message('network.unchecked')
        assert updates == ['query', 'status']
        assert not service.scanning.is_set()
        persisted = json.loads(target.read_text(encoding='utf-8'))
        assert persisted['sources'][0]['path'] == r'D:\Документы'
        assert persisted['indexing']['interval_seconds'] == 604800
        assert persisted['storage'] == {'quota_bytes': 987654321, 'min_free_bytes': 2147483648}
        assert persisted['revision'] == 2
        assert 'last_attempt' not in persisted
    finally:
        window.close()
        window.workers.wait()


def test_same_source_interval_save_preserves_refresh_times_and_search(tmp_path):
    target = tmp_path / 'settings.json'
    target.write_text(json.dumps({'root': r'C:\work', 'refresh_days': 3,
                                 'last_attempt': 123, 'last_refresh': 100}), encoding='utf-8')
    window, service = make_window(tmp_path)
    try:
        window.search_box.setText('чертёж')
        spin_until(lambda: window.results.count() == 1)
        before = settings_window_state(window)
        assert window.save_settings(r'C:\work', 7)
        after = settings_window_state(window)
        assert after[:1] + after[2:] == before[:1] + before[2:]
        assert window.refresh_days == 7
        persisted = json.loads(target.read_text(encoding='utf-8'))
        assert persisted['sources'][0]['path'] == r'C:\work'
        assert persisted['indexing']['interval_seconds'] == 604800 and persisted['revision'] == 2
        runtime = json.loads((tmp_path / 'engine/runtime/desktop-state.json').read_bytes())
        assert runtime['last_attempt'] == 123 and runtime['last_refresh'] == 100
        assert not service.scanning.is_set()
    finally:
        window.close()
        window.workers.wait()


def test_switching_source_limits_real_window_search_and_count_to_current_root(tmp_path):
    first = tmp_path / "source-a"
    second = tmp_path / "source-b"
    first.mkdir()
    second.mkdir()
    (first / "onlyA.pdf").touch()
    (first / "anotherA.pdf").touch()
    (second / "onlyB.pdf").touch()
    service = SearchService(tmp_path / "catalog.sqlite3")
    from test_source_registry import MemoryIdentities
    service.public_ids = MemoryIdentities()
    assert service.scan(str(first))["status"] == "complete"
    window = SearchWindow(data_dir=tmp_path / "settings", service_factory=lambda: service,
                          root=str(first), auto_scan=False)
    try:
        window.show()
        spin_until(lambda: "2" in window.index_status.text(), timeout=10000)
        selected = window.settings_repository.snapshot().settings['sources'][0]
        assert selected['path'] == str(first)
        assert service.public_ids.source_uuid(str(first)) == selected['id']
        assert window._settings_snapshot.settings['sources'][0] == selected
        window.search_box.setText("onlyA")
        spin_until(lambda: window.results.count() == 1)
        assert window.results.item(0).data(Qt.ItemDataRole.UserRole).name == "onlyA.pdf"

        assert window.save_settings(str(second), 1)
        window.refresh()
        spin_until(lambda: window._scan_job == -1 and not window.workers._jobs)
        APP.processEvents()
        assert window.source_label.text() == str(second)
        assert window.index_status.text() == "В индексе: 1 файлов"

        window.search_box.setText("onlyB")
        spin_until(lambda: window.results.count() == 1 and
                   window.results.item(0).data(Qt.ItemDataRole.UserRole).name == "onlyB.pdf")
        window.search_box.setText("onlyA")
        spin_until(lambda: not window.debounce.isActive() and not window._search_jobs)
        assert window.results.count() == 0
        assert window.result_count.text() == "Найдено: 0"
    finally:
        window.close()
        spin_until(lambda: not window.workers._jobs)


def test_status_from_previous_source_cannot_replace_current_source_status(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'root': r'C:\work'}))
    old_status_started = threading.Event()
    release_old_status = threading.Event()
    old_status_finished = threading.Event()

    class DelayedStatusService(Service):
        def status(self, *, root=None):
            if not old_status_started.is_set():
                old_status_started.set()
                release_old_status.wait(2)
                old_status_finished.set()
                return dict(super().status(root=root), files=17)
            return dict(super().status(root=root), files=3)

    service = DelayedStatusService()
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: service,
                          root=r"C:\work", auto_scan=False)
    try:
        window.show()
        spin_until(old_status_started.is_set)
        assert window.save_settings(r"D:\current", 1)
        window._status()
        spin_until(lambda: "3" in window.index_status.text())
        release_old_status.set()
        spin_until(old_status_finished.is_set)
        spin_until(lambda: not window.workers._jobs)
        APP.processEvents()
        assert window.source_label.text() == r"D:\current"
        assert window.index_status.text() == "В индексе: 3 файлов"
    finally:
        release_old_status.set()
        window.close()
        spin_until(lambda: not window.workers._jobs)


def test_long_source_path_does_not_force_window_beyond_screen(tmp_path):
    window = SearchWindow(data_dir=tmp_path, service_factory=Service,
                          root="C:\\" + "Длинное имя папки\\" * 25, auto_scan=False)
    try:
        window.show()
        APP.processEvents()
        assert window.width() <= 980
        assert window.source_label.toolTip().endswith("Длинное имя папки\\")
    finally:
        window.close()


def test_keyboard_moves_selection_without_losing_query_focus(tmp_path):
    service = Service()
    service.search = lambda query, limit=100, root=None: [Result(name="Первый.pdf"), Result(file_id=2, name="Второй.pdf")]
    window, _ = make_window(tmp_path, service)
    try:
        window.search_box.setText("чертеж")
        spin_until(lambda: window.results.count() == 2)
        QTest.keyClick(window.search_box, Qt.Key.Key_Down)
        assert window.results.currentItem().data(Qt.ItemDataRole.UserRole).name == "Второй.pdf"
        assert window.search_box.hasFocus()
        QTest.keyClick(window.search_box, Qt.Key.Key_Up)
        assert window.results.currentRow() == 0
    finally:
        window.close()


def test_daily_refresh_does_not_repeat_after_unavailable_source(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'root': r'C:\work'}))
    service = Service()
    attempts = []

    def unavailable(root, progress=None, cancel=None):
        attempts.append(root)
        return {"status": "unavailable", "files": 0, "added": 0,
                "updated": 0, "removed": 0, "errors": []}

    service.scan = unavailable
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: service,
                          root=r"C:\work", auto_scan=True)
    try:
        window.show()
        spin_until(lambda: "недоступна" in window.network_status.text())
        window._auto_refresh()
        QTest.qWait(30)
        APP.processEvents()
        assert attempts == [r"C:\work"]
        assert "сохранённому списку" in window.message.text()
        assert window.search_box.isEnabled()
    finally:
        window.close()


def test_scan_runtime_observations_do_not_change_settings_revision(tmp_path):
    window, service = make_window(tmp_path)
    try:
        before = (tmp_path / 'settings.json').read_bytes()
        service.block_scan.set()
        window.refresh()
        spin_until(lambda: window._scan_job == -1 and not window.workers._jobs)
        assert (tmp_path / 'settings.json').read_bytes() == before
        runtime = json.loads((tmp_path / 'engine/runtime/desktop-state.json').read_bytes())
        assert runtime['last_attempt'] > 0 and runtime['last_refresh'] > 0
    finally:
        window.close()
        window.workers.wait()


def test_single_source_dialog_save_preserves_other_configured_sources(tmp_path):
    import uuid
    from test_settings_schema import repository
    repo = repository(tmp_path)
    initial = repo.load()
    settings = initial.settings
    settings['sources'][0]['path'] = r'C:\work'
    second = {'id': str(uuid.uuid4()), 'path': r'D:\Second', 'label': 'Вторая', 'enabled': False}
    settings['sources'].append(second)
    repo.commit(settings, initial.revision)
    window, _ = make_window(tmp_path)
    try:
        assert window.save_settings(r'D:\First moved', 7)
        persisted = json.loads((tmp_path / 'settings.json').read_bytes())
        assert persisted['sources'][1] == second
        assert persisted['indexing']['interval_seconds'] == 604800
    finally:
        window.close()
        window.workers.wait()


def test_disabled_source_does_not_search_or_start_manual_catalog(tmp_path):
    from atom_search.settings.repository import SettingsRepository
    repo = SettingsRepository(tmp_path)
    initial = repo.load()
    settings = initial.settings
    import uuid
    settings['sources'] = [dict(id=str(uuid.uuid4()), path='C:/work', label='', enabled=False)]
    repo.commit(settings, initial.revision)
    service = Service()
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: service, auto_scan=True)
    window.show()
    try:
        spin_until(lambda: window.source_registry is not None)
        window.search_box.setText('private')
        window._search()
        window.refresh()
        QTest.qWait(200); APP.processEvents()
        assert service.query_threads == []
        assert not service.scanning.is_set()
        assert window.results.count() == 0
        assert window.message.text() == message('settings.source_disabled')
    finally:
        window.close(); window.workers.wait()


def test_startup_real_quota_failure_returns_error_and_preserves_settings(tmp_path, capsys, caplog):
    from atom_search.desktop.app import main
    target = tmp_path / 'settings.json'
    original = json.dumps({'root': r'C:\Saved', 'refresh_days': 7,
                           'storage': {'quota_bytes': 1, 'min_free_bytes': 0}}).encode()
    target.write_bytes(original)
    assert main(['--smoke-test', '--no-auto-scan', '--data-dir', str(tmp_path),
                 '--model-dir', str(tmp_path / 'missing')]) == 1
    assert target.read_bytes() == original
    assert message('settings.preparation_failed') in capsys.readouterr().err
    assert any(getattr(record, 'error_code', None) == 'quota_exceeded' for record in caplog.records)


@pytest.mark.parametrize('kind', ['filesystem', 'windows', 'postgres'])
def test_startup_reports_expected_preparation_failure_types(tmp_path, monkeypatch, capsys, kind):
    from atom_search.desktop import app, window
    from atom_search.runtime._windows import WindowsError as RuntimeWindowsError
    from atom_search.runtime.postgres import PostgresRuntimeError
    failures = {'filesystem': PermissionError(13, 'never echo this secret'),
                'windows': RuntimeWindowsError('profile_acl_not_private'),
                'postgres': PostgresRuntimeError('host_lifecycle_invalid')}
    def blocked(**kwargs):
        raise failures[kind]
    monkeypatch.setattr(window, 'SearchWindow', blocked)
    assert app.main(['--smoke-test', '--data-dir', str(tmp_path)]) == 1
    stderr = capsys.readouterr().err
    assert message('settings.preparation_failed') in stderr and 'never echo' not in stderr


def test_startup_does_not_swallow_unexpected_programming_errors(tmp_path, monkeypatch):
    from atom_search.desktop import app, window
    def broken(**kwargs):
        raise RuntimeError('controlled programming defect')
    monkeypatch.setattr(window, 'SearchWindow', broken)
    with pytest.raises(RuntimeError, match='controlled programming defect'):
        app.main(['--smoke-test', '--data-dir', str(tmp_path)])


@pytest.mark.parametrize('phase', ['factory', 'close'])
def test_desktop_exit_refuses_uncertain_factory_or_cleanup(tmp_path, monkeypatch, phase):
    from atom_search.desktop.app import main
    import atom_search.search_runtime as host
    calls = []
    class FailedClose(Service):
        def close(self):
            calls.append('close')
            raise RuntimeError('injected_stop_unknown')
    def factory(*args, **kwargs):
        if phase == 'factory':
            calls.append('factory')
            raise RuntimeError('injected_factory_cleanup_unknown')
        return FailedClose()
    monkeypatch.setattr(host, 'open_search_service', factory)
    result = main(['--smoke-test', '--data-dir', str(tmp_path / phase)])
    assert result == 1, 'uncertain Runtime cleanup must not become desktop exit zero'
    assert calls == [phase]


def test_empty_profile_window_does_not_search_or_scan_implicit_root(tmp_path, monkeypatch):
    service = Service()
    monkeypatch.setattr(service, 'search', lambda *a, **k: pytest.fail('empty profile searched'))
    monkeypatch.setattr(service, 'scan', lambda *a, **k: pytest.fail('empty profile scanned'))
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: service, auto_scan=True)
    try:
        assert window.root == ''
        assert window._settings_snapshot.settings['sources'] == []
        assert not window.refresh_button.isEnabled()
        window.search_box.setText('drawing')
        window._search()
        window.refresh()
        window._auto_refresh()
        assert window._scan_job == -1
    finally:
        window.close()


def test_first_explicit_folder_from_empty_profile_persists_uuid_and_restarts(tmp_path):
    from atom_search.settings.repository import SettingsRepository
    service = Service()
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: service, auto_scan=False)
    try:
        spin_until(lambda: window.source_registry is not None)
        assert window.save_settings(r'C:\Chosen', 7)
        selected = window.settings_repository.snapshot()
        assert len(selected.settings['sources']) == 1
        source = selected.settings['sources'][0]
        assert source['path'] == r'C:\Chosen'
        assert service.public_ids.source_uuid(window.root) == source['id']
        assert window.refresh_button.isEnabled()
    finally:
        window.close()
        window.workers.wait()
    assert SettingsRepository(tmp_path).load() == selected
