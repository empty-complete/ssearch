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
