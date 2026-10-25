"""Source serve proof: real PostgreSQL/IPC; no Qt or fake runtime."""
import os
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest


_SOURCE_FAILURE_OBSERVER = r'''
import json, pathlib, runpy, sys, threading
destination = pathlib.Path(sys.argv.pop(1))
records = []
record_lock = threading.Lock()
codes = frozenset(('initdb_failed', 'runtime_start_failed', 'runtime_validation_failed',
    'process_timeout', 'startup_unresolved', 'startup_not_ready', 'incomplete_pgdata',
    'process_identity_unavailable', 'service_unavailable', 'service_identity_mismatch',
    'service_stopping', 'profile_owner_mismatch', 'credential_decryption_failed'))

def observe(frame, event, argument):
    if (event != 'exception' or len(records) >= 3 or
            pathlib.Path(frame.f_code.co_filename).name != 'headless.py' or
            frame.f_code.co_name not in ('lifecycle', 'control', 'main')):
        return observe
    try:
        error, trace = argument[1], argument[2]
        chain, seen = [], set()
        while error is not None and id(error) not in seen and len(chain) < 4:
            seen.add(id(error))
            locations = []
            while trace is not None:
                locations.append(dict(file=pathlib.Path(trace.tb_frame.f_code.co_filename).name[:64],
                                      line=trace.tb_lineno))
                locations = locations[-3:]
                trace = trace.tb_next
            code = getattr(error, 'code', None)
            chain.append(dict(type=type(error).__name__[:64], code=code if code in codes else None,
                              locations=locations))
            cause = error.__cause__
            error = cause if cause is not None else error.__context__
            trace = error.__traceback__ if error is not None else None
        with record_lock:
            if len(records) < 3:
                records.append(dict(site=frame.f_code.co_name, exceptions=chain))
                temporary = destination.with_suffix('.tmp')
                temporary.write_text(json.dumps(records, ensure_ascii=True, separators=(',', ':')),
                                     encoding='utf-8')
                temporary.replace(destination)
    except BaseException:
        pass  # Observing an exception must not replace it or change service cleanup.
    return observe

sys.settrace(observe)
threading.settrace(observe)
try:
    runpy.run_module('atom_search.service.headless', run_name='__main__', alter_sys=True)
finally:
    threading.settrace(None)
    sys.settrace(None)
'''


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
    executable = sys.executable
    base_executable = getattr(sys, '_base_executable', None) or executable
    if os.name == 'nt' and os.path.normcase(base_executable) != os.path.normcase(executable):
        # Match CPython's multiprocessing Windows venv launch: retain the venv,
        # but own the interpreter PID directly instead of its redirector PID.
        environment['__PYVENV_LAUNCHER__'] = executable
        executable = base_executable
    args = [executable, '-m', 'atom_search.service.headless', '--data-dir', str(profile),
            '--model-dir', str(tmp_path / 'missing-model'), '--control-stdin']
    diagnostic_file = tmp_path / 'source-failure.json'
    # Temporary observer for the unresolved Unicode exit. It neither replaces
    # composition nor changes return values; this route is not normal -m proof.
    observed_args = ([executable, '-c', _SOURCE_FAILURE_OBSERVER, str(diagnostic_file), *args[3:]]
                     if profile_name == 'headless profile Кириллица' else args)
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
            diagnostic = []
            if diagnostic_file.is_file():
                with diagnostic_file.open('rb') as stream:
                    content = stream.read(8193)
                if len(content) <= 8192:
                    diagnostic = json.loads(content)
            raise AssertionError('source serve did not become ready; '
                f'endpoint_exists={endpoint.exists()} child_exit_code={process.poll()} '
                f'cause={diagnostic}') from last_failure
        second = ServiceClient(endpoint, None)
        assert first.descriptor == second.descriptor
        spawned_pid = process.pid
        assert first.descriptor['pid'] == spawned_pid
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
        assert second.descriptor['pid'] == spawned_pid
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
