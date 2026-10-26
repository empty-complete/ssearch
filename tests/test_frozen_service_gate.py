"""Controller failure/receipt guards only; fakes never qualify a frozen service."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location('ssearch_frozen_service_gate', ROOT / 'tools/check_frozen_service.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(gate, monkeypatch, tmp_path):
    executable = tmp_path / 'service/ssearch-service.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'unit fixture, not an executable')
    qa, report = tmp_path / 'qa', tmp_path / 'receipt.json'
    monkeypatch.setattr(gate, '_verify_service_bundle', lambda path: None)
    def private(path):
        path.mkdir(parents=True)
        return path
    monkeypatch.setattr(gate, '_windows', SimpleNamespace(reject_reparse=lambda path: Path(path),
                                                        private_directory=private))
    monkeypatch.setattr(gate, '_child_environment', lambda: {})
    arguments = ['--service-executable', str(executable), '--qa-directory', str(qa),
                 '--source-commit', 'a' * 40, '--app-version', '0.2.0', '--report', str(report)]
    return SimpleNamespace(executable=executable, qa=qa, report=report, arguments=arguments)


@pytest.mark.parametrize('field,value', [
    ('--service-executable', 'relative.exe'), ('--qa-directory', 'relative-qa'),
    ('--report', 'relative.json'), ('--source-commit', 'PRIVATE_SENTINEL'),
    ('--app-version', 'PRIVATE_SENTINEL'),
])
def test_public_input_rejection_precedes_processes_and_receipts(gate, prepared, monkeypatch, capsys, field, value):
    arguments = prepared.arguments.copy()
    arguments[arguments.index(field) + 1] = value
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *a, **k: pytest.fail('invalid input spawned a process'))
    assert gate.main(arguments) != 0
    assert not prepared.qa.exists() and not prepared.report.exists()
    assert 'PRIVATE_SENTINEL' not in ''.join(capsys.readouterr())


@pytest.mark.parametrize('existing', ['qa', 'report'])
def test_existing_qa_or_receipt_is_never_overwritten(gate, prepared, monkeypatch, existing):
    if existing == 'qa':
        prepared.qa.mkdir()
        sentinel = prepared.qa / 'keep'
    else:
        sentinel = prepared.report
    sentinel.write_bytes(b'preserve existing input')
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *a, **k: pytest.fail('existing input spawned a process'))
    assert gate.main(prepared.arguments) != 0
    assert sentinel.read_bytes() == b'preserve existing input'


def test_missing_closure_cannot_be_replaced_by_source_fallback(gate, prepared, monkeypatch, capsys):
    def reject(path):
        raise RuntimeError('PRIVATE_SENTINEL missing native closure')
    monkeypatch.setattr(gate, '_verify_service_bundle', reject)
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *a, **k: pytest.fail('invalid bundle spawned a process'))
    assert gate.main(prepared.arguments) == 1
    assert not prepared.qa.exists() and not prepared.report.exists()
    assert 'PRIVATE_SENTINEL' not in ''.join(capsys.readouterr())


def test_controller_rejects_non_windows_before_native_work(gate, prepared, monkeypatch):
    monkeypatch.setattr(gate, 'os', SimpleNamespace(name='posix'))
    assert gate.main(prepared.arguments) != 0
    assert not prepared.qa.exists() and not prepared.report.exists()


def test_child_environment_restricts_lookup_without_rebinding_user_folders(gate, monkeypatch, tmp_path):
    system = tmp_path / 'Windows'
    (system / 'System32').mkdir(parents=True)
    original = dict(SystemRoot=str(system), USERPROFILE='retained-user', LOCALAPPDATA='retained-local',
                    APPDATA='retained-roaming', PATH='source lookup', Path='alternate lookup',
                    PYTHONPATH='private-source', __PYVENV_LAUNCHER__='external-python',
                    VIRTUAL_ENV='external-venv', NODE_OPTIONS='external-node',
                    ELECTRON_RUN_AS_NODE='1', SSEARCH_SERVICE_STAGE_DIR='private-source',
                    SOURCE_SHA='caller-value', PGSERVICE='ambient-server')
    monkeypatch.setattr(gate, 'os', SimpleNamespace(environ=original))
    child = gate._child_environment()
    assert child == dict(SystemRoot=str(system), USERPROFILE='retained-user', LOCALAPPDATA='retained-local',
                         APPDATA='retained-roaming', PATH=str(system / 'System32'))
    assert original['PATH'] == 'source lookup', 'parent environment must remain unchanged'


@pytest.mark.parametrize('failure', ['exception', 'incomplete', 'cleanup', 'changed-image'])
def test_failed_proof_never_emits_success_and_finalizes_retained_children(gate, prepared, monkeypatch, capsys, failure):
    cleaned = []
    class Child:
        def cleanup(self):
            cleaned.append(self)
            return failure != 'cleanup'
    def scenario(executable, profile, qa, label, environment, version, children):
        children.append(Child())
        if failure == 'exception':
            raise RuntimeError('PRIVATE_SENTINEL credentials and paths')
        if failure == 'changed-image':
            executable.write_bytes(b'changed unit fixture')
        return {} if failure == 'incomplete' else dict.fromkeys(gate.FLAGS, True)
    monkeypatch.setattr(gate, '_scenario', scenario)
    assert gate.main(prepared.arguments) == 1
    assert cleaned and not prepared.report.exists()
    assert 'PRIVATE_SENTINEL' not in ''.join(capsys.readouterr())


def test_receipt_waits_for_cleanup_and_does_not_claim_observed_source_sha(gate, prepared, monkeypatch):
    labels, cleaned = [], []
    class Child:
        def cleanup(self):
            assert not prepared.report.exists(), 'receipt preceded child cleanup'
            cleaned.append(True)
            return True
    def scenario(executable, profile, qa, label, environment, version, children):
        labels.append(label)
        children.append(Child())
        return dict.fromkeys(gate.FLAGS, True)
    monkeypatch.setattr(gate, '_scenario', scenario)
    assert gate.main(prepared.arguments) == 0
    assert labels == ['ascii', 'unicode'] and len(cleaned) == 2
    receipt = json.loads(prepared.report.read_text(encoding='utf-8'))
    assert receipt['source_commit'] == 'a' * 40
    assert receipt['source_commit_observed'] is False
    assert receipt['source_identity_binding'] == 'external-frozen-application-proof'
    assert receipt['service_sha256_before'] == receipt['service_sha256_after']
    assert receipt['windows_validation'] == []


def test_owned_child_timeout_uses_only_retained_object_and_remains_failure(gate, monkeypatch, tmp_path):
    import subprocess
    import io
    calls = []
    class Process:
        pid = 123
        stdin = io.BytesIO()
        def poll(self):
            return None
        def wait(self, timeout):
            calls.append(('wait', timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('PRIVATE_SENTINEL', timeout)
            return -1
        def kill(self):
            calls.append(('kill-retained',))
    process = Process()
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *a, **k: process)
    child = gate._Child(tmp_path / 'ssearch-service.exe', tmp_path / 'profile', tmp_path, 'unit', {})
    assert child.cleanup() is False, 'forced termination is not clean lifecycle proof'
    assert child.cleanup() is False, 'repeated cleanup cannot erase uncertainty'
    assert ('kill-retained',) in calls


def test_discovered_owner_pid_cannot_replace_the_retained_child(gate, monkeypatch):
    identity = SimpleNamespace(pid=123)
    child = SimpleNamespace(identity=identity, process=SimpleNamespace(pid=123, poll=lambda: None))
    client = SimpleNamespace(endpoint_file=Path('private-endpoint'), descriptor={'pid': 999},
                             status=lambda: SimpleNamespace(app_version='0.2.0', service_version='0.2.0', api_version='1'))
    monkeypatch.setattr(gate, 'read_owner_state', lambda path: ({'state': 'ready'}, identity))
    with pytest.raises(gate.GateFailure) as failure:
        gate._owner(client, child, '0.2.0')
    assert failure.value.code == 'owner_not_retained_child'


def test_child_output_cannot_become_a_clean_stop_or_leak_into_diagnostics(gate, monkeypatch, tmp_path, capsys):
    from contextlib import nullcontext
    import io
    class Process:
        pid = 123
        stdin = io.BytesIO()
        def poll(self):
            return None
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr(gate, '_windows', SimpleNamespace(verified_process=lambda identity: nullcontext()))
    child = gate._Child(tmp_path / 'ssearch-service.exe', tmp_path / 'profile', tmp_path, 'unit', {})
    child.logs[0].write_bytes(b'PRIVATE_SENTINEL')
    try:
        with pytest.raises(gate.GateFailure) as failure:
            child.stop()
        assert failure.value.code == 'child_output_leak'
    finally:
        child.cleanup()
    assert capsys.readouterr() == ('', '')


@pytest.fixture
def output_failure(gate, prepared, monkeypatch, capsys):
    """Real controller/stop/cleanup, with only the retained OS child faked."""
    from contextlib import nullcontext
    import io

    def exercise(label, stream, payload, *, during_exit=False, unsafe=None):
        cleaned = []
        process = SimpleNamespace(exited=False)
        original_open = Path.open

        def checked_open(path, mode='r', *args, **kwargs):
            if 'r' in mode and path.suffix in ('.stdout', '.stderr'):
                assert process.exited, 'capture content was read before child exit'
                assert cleaned, 'diagnostics preceded retained child cleanup'
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, 'open', checked_open)
        windows = gate._windows

        def reject(path):
            if cleaned and unsafe == 'reparse' and Path(path).suffix == '.' + stream:
                raise RuntimeError('PRIVATE_SENTINEL unsafe capture')
            return Path(path)

        def private(path):
            if cleaned and unsafe == 'parent' and Path(path) == prepared.qa:
                raise RuntimeError('PRIVATE_SENTINEL unsafe parent')

        monkeypatch.setattr(gate, '_windows', SimpleNamespace(
            private_directory=windows.private_directory, reject_reparse=reject,
            assert_private=private, verified_process=lambda identity: nullcontext()))

        class Process:
            pid = 123

            def __init__(self, **kwargs):
                self.stdin = io.BytesIO()
                self.output = kwargs[stream]
                if not during_exit:
                    self.output.write(payload)

            def poll(self):
                return 0 if process.exited else None

            def wait(self, timeout):
                if not process.exited and during_exit:
                    self.output.write(payload)
                process.exited = True
                return 0

        monkeypatch.setattr(gate.subprocess, 'Popen', lambda *args, **kwargs: Process(**kwargs))

        def scenario(executable, profile, qa, scenario_label, environment, version, children):
            child = gate._Child(executable, profile, qa, label, environment)
            children.append(child)
            original_cleanup = child.cleanup

            def cleanup():
                result = original_cleanup()
                cleaned.append(True)
                path = child.logs[stream == 'stderr']
                if unsafe == 'missing':
                    path.unlink()
                elif unsafe == 'replaced':
                    replacement = path.with_suffix('.replacement')
                    replacement.write_bytes(b'PRIVATE_SENTINEL replacement')
                    path.unlink()
                    replacement.rename(path)
                return result

            child.cleanup = cleanup
            child.stop()
            pytest.fail('nonempty output became a passing scenario')

        monkeypatch.setattr(gate, '_scenario', scenario)
        assert gate.main(prepared.arguments) == 1
        assert cleaned == [True] and not prepared.report.exists()
        output, error = capsys.readouterr()
        assert output == ''
        lines = error.splitlines()
        assert len(lines) == 2 and lines[-1] == 'child_output_leak'
        diagnostic = json.loads(lines[0])
        assert set(diagnostic) == {'diagnostic_only', 'code', 'child', 'checkpoints', 'streams'}
        assert diagnostic['diagnostic_only'] is True and diagnostic['code'] == 'child_output_leak'
        assert 'PRIVATE_SENTINEL' not in error and str(prepared.qa) not in error
        return diagnostic

    return exercise


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('label,category,marker', [
    ('ascii-primary', 'traceback', b'Traceback (most recent call last):'),
    ('ascii-attached', 'thread', b'Exception in thread PRIVATE_SENTINEL:'),
    ('ascii-restart', 'unraisable', b'Exception ignored in: PRIVATE_SENTINEL'),
    ('unicode-primary', 'warning', b'UserWarning: PRIVATE_SENTINEL'),
    ('unicode-attached', 'onnxruntime', b'[W:onnxruntime:PRIVATE_SENTINEL]'),
    ('unicode-restart', 'pyinstaller', b'[PYI-123:WARNING] PRIVATE_SENTINEL'),
    ('ascii-primary', 'unknown', b'PRIVATE_SENTINEL unrecognized output'),
])
def test_output_failure_classifies_both_streams_without_exporting_payload(output_failure, stream, label, category, marker):
    payload = marker + b' PRIVATE_SENTINEL C:\\private\\credentials.json\n'
    diagnostic = output_failure(label, stream, payload)
    assert diagnostic['child'] == label
    for checkpoint in ('beforeEOF', 'afterExit'):
        assert diagnostic['checkpoints'][checkpoint][stream] == {'bytes': len(payload), 'oversize': False}
    observed = diagnostic['streams'][stream]
    assert observed['state'] == 'read' and observed['bytes'] == len(payload)
    assert observed['oversize'] is observed['truncated'] is False
    assert observed['categories'] == {name: name == category for name in (
        'traceback', 'thread', 'unraisable', 'warning', 'onnxruntime', 'pyinstaller', 'unknown',
        'ort_windows_release_check')}
    other = diagnostic['streams']['stderr' if stream == 'stdout' else 'stdout']
    assert other['bytes'] == 0 and not any(other['categories'].values())


def test_output_failure_separates_eof_output_and_bounds_capture_inspection(output_failure):
    # A recognisable marker beyond 64 KiB must not be inspected/classified.
    payload = b'UserWarning: PRIVATE_SENTINEL\n'.ljust(65536, b'x') + b'[PYI-123:WARNING]'
    diagnostic = output_failure('unicode-restart', 'stderr', payload, during_exit=True)
    assert diagnostic['checkpoints']['beforeEOF']['stderr'] == {'bytes': 0, 'oversize': False}
    assert diagnostic['checkpoints']['afterExit']['stderr'] == {'bytes': 65537, 'oversize': True}
    observed = diagnostic['streams']['stderr']
    assert observed['bytes'] == 65537 and observed['oversize'] is observed['truncated'] is True
    assert observed['categories']['warning'] is True and observed['categories']['pyinstaller'] is False


@pytest.mark.parametrize('unsafe', ['missing', 'replaced', 'reparse', 'parent'])
def test_output_failure_unsafe_capture_is_unavailable_and_still_fails(output_failure, unsafe):
    diagnostic = output_failure('ascii-attached', 'stdout', b'PRIVATE_SENTINEL', unsafe=unsafe)
    assert diagnostic['streams']['stdout'] == {'state': 'unavailable'}


def test_output_failure_unknown_child_label_is_fixed_and_redacted(output_failure):
    diagnostic = output_failure('PRIVATE_SENTINEL', 'stderr', b'PRIVATE_SENTINEL')
    assert diagnostic['child'] == 'unknown'


@pytest.mark.parametrize('change,expected', [
    ('exact', True), ('filename', False), ('category', False),
    ('template', False), ('extra', False), ('release', False),
])
def test_output_failure_identifies_only_complete_public_ort_windows_warning(output_failure, change, expected):
    # ORT v1.22.1 onnxruntime/python/onnxruntime_validation.py, blob
    # 9f290da51fe8a757136898a35cc9795f93ec958d: check_distro_info().
    payload = (b'onnxruntime\\capi\\onnxruntime_validation.py:27: UserWarning: '
               b'Unsupported Windows version (2025server). ONNX Runtime supports Windows 10 and above, only.\r\n')
    if change == 'filename':
        payload = payload.replace(b'onnxruntime_validation.py', b'PRIVATE_SENTINEL.py')
    elif change == 'category':
        payload = payload.replace(b'UserWarning', b'RuntimeWarning')
    elif change == 'template':
        payload = payload.replace(b'Windows 10 and above', b'PRIVATE_SENTINEL')
    elif change == 'extra':
        payload += b'PRIVATE_SENTINEL C:\\private\\credentials.json\r\n'
    elif change == 'release':
        payload = payload.replace(b'2025server', b'2022server')
    diagnostic = output_failure('ascii-primary', 'stderr', payload)
    assert diagnostic['streams']['stderr']['categories']['ort_windows_release_check'] is expected


def test_pinned_ort_fresh_import_classifies_public_windows_warning_without_model():
    """A producer check only; it never qualifies the frozen capture or model/PG."""
    import subprocess
    import sys

    script = r'''
import json, platform, warnings
result = dict(version_matches=False, release_family='other', warning_matches=False,
              warning_count=0, probe_ok=False)
try:
    release = platform.release().lower()
    result['release_family'] = ('client' if release in ('10', '11') else
                                'server' if release.endswith('server') else 'other')
    with warnings.catch_warnings(record=True) as observed:
        import onnxruntime
    result['version_matches'] = onnxruntime.__version__ == '1.22.1'
    expected = ('Unsupported Windows version (' + release + '). '
                'ONNX Runtime supports Windows 10 and above, only.')
    result['warning_count'] = min(len(observed), 2)
    result['warning_matches'] = bool(observed) and all(
        item.category is UserWarning
        and item.filename.replace('\\', '/').endswith('/onnxruntime/capi/onnxruntime_validation.py')
        and str(item.message) == expected for item in observed)
    result['probe_ok'] = platform.system() == 'Windows'
except BaseException:
    pass
print(json.dumps(result, ensure_ascii=True))
'''
    try:
        result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=60, shell=False)
    except (OSError, subprocess.SubprocessError):
        pytest.fail('ort_warning_producer_unavailable', pytrace=False)
    streams_clean = result.returncode == 0 and result.stderr == b''
    assert streams_clean, 'ort_warning_producer_failed'
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        pytest.fail('ort_warning_producer_result_unavailable', pytrace=False)
    valid_contract = (type(observed) is dict
        and set(observed) == {'version_matches', 'release_family', 'warning_matches', 'warning_count', 'probe_ok'}
        and observed['probe_ok'] is observed['version_matches'] is True
        and observed['release_family'] in ('server', 'client', 'other')
        and observed['warning_matches'] is (observed['release_family'] != 'client')
        and type(observed['warning_count']) is int
        and observed['warning_count'] == (0 if observed['release_family'] == 'client' else 1))
    assert valid_contract, 'ort_warning_producer_contract_failed'
