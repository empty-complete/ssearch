"""Source serve proof: real PostgreSQL/IPC; no Qt or fake runtime."""
import os
from pathlib import Path
import subprocess
import sys
import time


def test_cold_entries_import_without_gui_or_composition():
    script = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('PySide6', 'PyQt6', 'PyQt5') or fullname in (
                'atom_search.service.owner', 'atom_search.search_runtime'):
            raise AssertionError('unexpected composition or GUI import: ' + fullname)
sys.meta_path.insert(0, Guard())
import atom_search.service.headless
import atom_search.service.desktop_client
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def test_source_serve_two_clients_private_apply_and_clean_stop(tmp_path):
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import SearchRequest, ServiceError
    import pytest
    profile = tmp_path / 'headless profile Кириллица'
    endpoint = profile / 'engine' / 'runtime' / 'endpoint.json'
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path('src').absolute()),
                        str(Path('packages/atom-search-core/src').absolute()))))
    args = [sys.executable, '-m', 'atom_search.service.headless', '--data-dir', str(profile),
            '--model-dir', str(tmp_path / 'missing-model'), '--control-stdin']
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    attached = None
    try:
        deadline = time.monotonic() + 120
        first = None
        while time.monotonic() < deadline and process.poll() is None:
            if endpoint.exists():
                try:
                    first = ServiceClient(endpoint, None)
                    first.status()
                    break
                except ServiceError:
                    pass
            time.sleep(.05)
        assert first is not None and process.poll() is None, 'source serve did not become ready'
        second = ServiceClient(endpoint, None)
        assert first.descriptor == second.descriptor
        assert first.descriptor['pid'] == process.pid
        assert first.search(SearchRequest('drawing', source_ids=(), mode='literal')).results == ()
        snapshot = second.settings_get()
        assert snapshot.settings['sources'] == []
        candidate = snapshot.settings
        candidate['ui']['theme'] = 'dark'
        assert first.settings_validate(candidate, snapshot.revision).valid
        applied = first.settings_apply(candidate, snapshot.revision)
        assert second.settings_get().revision == applied.revision == snapshot.revision + 1
        with pytest.raises(ServiceError) as conflict:
            second.settings_apply(candidate, snapshot.revision)
        assert conflict.value.code == 'revision_conflict'
        assert first.start_catalog([]) == ()
        # A second source launch attaches to exactly the same live owner.
        attached = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(.5)
        assert attached.poll() is None
        attached.stdin.close()
        assert attached.wait(50) == 0
        assert second.status() is not None
        assert second.descriptor['pid'] == process.pid
        process.stdin.close()
        assert process.wait(50) == 0
        assert not endpoint.exists()
        from atom_search.http.serialization import decode
        assert decode((endpoint.parent / 'service-state.json').read_bytes())['state'] == 'stopped'
        assert process.stdout.read() == b'' and process.stderr.read() == b''
    finally:
        for child in (attached, process):
            if child is not None:
                if child.stdin and not child.stdin.closed:
                    child.stdin.close()
                try:
                    child.wait(50)
                except subprocess.TimeoutExpired:
                    child.kill()  # Only this test's own child; never discovered processes.
                    child.wait(10)

