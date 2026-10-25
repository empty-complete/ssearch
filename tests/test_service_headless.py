"""Source serve proof: real PostgreSQL/IPC; no Qt or fake runtime."""
import os
from pathlib import Path
import subprocess
import sys
import time
import re

import pytest


def test_startup_dependency_import_is_inside_the_deadline(monkeypatch, tmp_path):
    import builtins
    import threading
    from atom_search.service.headless import serve
    entered, release, finished = (threading.Event() for _ in range(3))
    original = builtins.__import__

    def delayed(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'owner' and level == 1 and (globals or {}).get('__package__') == 'atom_search.service':
            entered.set()
            try:
                release.wait(10)
                raise ImportError('controlled dependency startup failure')
            finally:
                finished.set()
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', delayed)
    started = time.monotonic()
    try:
        assert serve(tmp_path / 'profile', startup_timeout=.2, shutdown_timeout=.1) == 1
        assert entered.is_set() and time.monotonic() - started < 2
    finally:
        release.set()
        assert finished.wait(10)


def test_stop_during_blocked_start_uses_shutdown_budget(tmp_path, monkeypatch):
    import threading
    from atom_search.service.headless import serve
    from atom_search.service.owner import ServiceOwner
    entered, release, cleaned, stop = (threading.Event() for _ in range(4))
    observed = []
    original_close = ServiceOwner.close

    def blocked_start(self):
        entered.set()
        release.wait(10)
        raise RuntimeError('controlled startup failure')

    def close(self):
        try:
            return original_close(self)
        finally:
            cleaned.set()

    monkeypatch.setattr(ServiceOwner, 'start', blocked_start)
    monkeypatch.setattr(ServiceOwner, 'close', close)

    def request_stop():
        if entered.wait(10):
            observed.append(time.monotonic())
            stop.set()

    stopper = threading.Thread(target=request_stop, daemon=True)
    stopper.start()
    try:
        result = serve(tmp_path / 'profile', model_dir=tmp_path / 'missing', stop=stop,
                       startup_timeout=10, shutdown_timeout=.1)
        assert entered.is_set() and observed
        assert result == 1 and time.monotonic() - observed[0] < 2
        assert not cleaned.is_set()  # No false clean-stop receipt while startup is uncertain.
    finally:
        release.set()
        stopper.join(10)
        assert cleaned.wait(10)


def test_control_reader_failure_is_redacted_and_stops_cleanly(tmp_path, monkeypatch, capsys):
    from atom_search.service.headless import main

    class BrokenInput:
        def fileno(self):
            raise OSError('private control pipe secret/path')

    monkeypatch.setattr(sys, 'stdin', BrokenInput())
    assert main(['--data-dir', str(tmp_path / 'profile'), '--model-dir', str(tmp_path / 'missing'),
                 '--control-stdin']) == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def source_environment():
    return dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path('src').absolute()),
                str(Path('packages/atom-search-core/src').absolute()))))


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
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=10,
                            env=source_environment())
    assert result.returncode == 0, result.stderr.decode(errors='replace')


@pytest.mark.parametrize('profile_name', ['headless profile ASCII', 'headless profile Кириллица'],
                         ids=['ascii', 'unicode'])
def test_source_serve_two_clients_private_apply_and_clean_stop(tmp_path, profile_name):
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import SearchRequest, ServiceError
    profile = tmp_path / profile_name
    endpoint = profile / 'engine' / 'runtime' / 'endpoint.json'
    environment = source_environment()
    args = [sys.executable, '-m', 'atom_search.service.headless', '--data-dir', str(profile),
            '--model-dir', str(tmp_path / 'missing-model'), '--control-stdin']
    stack_file = tmp_path / 'source-startup-stack.txt'
    # Execute the real entry, adding only a bounded, test-owned child stack dump.
    # Its raw file stays in the disposable fixture; failures expose basenames and
    # line numbers only, never profile paths, locals, credentials or raw output.
    entry = '''
import faulthandler, runpy, sys
trace = open(sys.argv.pop(1), 'wb')
faulthandler.dump_traceback_later(60, file=trace)
try:
    runpy.run_module('atom_search.service.headless', run_name='__main__', alter_sys=True)
finally:
    faulthandler.cancel_dump_traceback_later()
    trace.close()
'''
    observed_args = [sys.executable, '-c', entry, str(stack_file), *args[3:]]
    process = subprocess.Popen(observed_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    attached = None
    try:
        deadline = time.monotonic() + 120
        first = None
        last_failure = None
        while time.monotonic() < deadline and process.poll() is None:
            if endpoint.exists():
                try:
                    candidate_client = ServiceClient(endpoint, None)
                    candidate_client.status()
                    first = candidate_client
                    break
                except ServiceError as failure:
                    last_failure = failure
            time.sleep(.05)
        if first is None or process.poll() is not None:
            # Preserve the client exception context for CI's safe type/file/line
            # chain, without exporting the private profile or subprocess output.
            frames = []
            if stack_file.is_file():
                text = stack_file.read_text(encoding='utf-8', errors='replace')
                frames = [(Path(path).name, int(line)) for path, line in
                          re.findall(r'  File "([^"]+)", line ([0-9]+)', text)
                          if not path.startswith('<frozen ')][:48]
            raise AssertionError('source serve did not become ready; '
                f'endpoint_exists={endpoint.exists()} child_exited={process.poll() is not None} '
                f'child_frames={frames}') from last_failure
        second = ServiceClient(endpoint, None)
        assert first.descriptor == second.descriptor
        assert first.descriptor['pid'] == process.pid
        import http.client
        from urllib.parse import urlsplit
        address = urlsplit(first.descriptor['base_url'])
        unauthenticated = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            unauthenticated.request('GET', '/v1/status')
            assert unauthenticated.getresponse().status == 401
        finally:
            unauthenticated.close()
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
