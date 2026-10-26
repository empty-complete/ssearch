"""Public build-input and publication boundaries; run by the GitHub Full build."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load_tool('prepare_ci')
exporter = load_tool('export_ci_artifacts')


@pytest.mark.parametrize('entry', ['valid', 'wrong', 'missing', 'duplicate'])
def test_electron_preparation_requires_pinned_archive_and_matching_upstream_entry(tmp_path, entry):
    archive = tmp_path / 'electron-v45.0.0-win32-x64.zip'
    archive.write_bytes(b'unit input bytes; not an executable qualification')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    line = f'{digest} *{archive.name}\n'
    if entry == 'wrong':
        line = f'{"0" * 64} *{archive.name}\n'
    elif entry == 'missing':
        line = f'{digest} *other-platform.zip\n'
    elif entry == 'duplicate':
        line += line
    checksums = tmp_path / 'SHASUMS256.txt'
    checksums.write_text(line, encoding='utf-8')
    config = dict(version='45.0.0', platform='win32', architecture='x64',
                  archive=dict(size=archive.stat().st_size, sha256=digest),
                  checksums=dict(size=checksums.stat().st_size,
                                 sha256=hashlib.sha256(checksums.read_bytes()).hexdigest()))
    if entry == 'valid':
        prepare.verify_electron_inputs(config, archive, checksums)
        archive.write_bytes(b'corrupt cache entry')
    with pytest.raises(ValueError):
        prepare.verify_electron_inputs(config, archive, checksums)


def test_ci_pytest_preserves_module_invocation_cross_test_imports(tmp_path):
    """Match the real namespace tests/ tree, without a root test module."""
    (tmp_path / 'pytest.ini').write_text('[pytest]\ntestpaths = tests\n', encoding='utf-8')
    integration = tmp_path / 'tests/integration'
    integration.mkdir(parents=True)
    (integration / '__init__.py').write_text('', encoding='utf-8')
    (integration / 'helper.py').write_text('shared_value = 42\n', encoding='utf-8')
    (integration / 'test_sibling_import.py').write_text(
        'from tests.integration.helper import shared_value\n'
        'def test_sibling_import():\n'
        '    assert shared_value == 42\n', encoding='utf-8')
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTEST_PLUGINS', None)
    environment['PYTEST_ADDOPTS'] = ''
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    baseline = subprocess.run([sys.executable, '-u', '-m', 'pytest', '-q'], cwd=tmp_path,
                              env=environment, capture_output=True, text=True, timeout=30)
    assert baseline.returncode == 0, {'module_baseline_exit_code': baseline.returncode}
    wrapper = Path(__file__).parents[1] / 'tools/ci_pytest.py'
    result = subprocess.run([sys.executable, '-u', str(wrapper), '--timeout-seconds', '15'],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, {
        'wrapper_exit_code': result.returncode,
        'missing_tests_namespace': "No module named 'tests" in result.stdout + result.stderr}
    assert 'CI pytest starting: tests/integration/test_sibling_import.py::test_sibling_import\n' in result.stderr


@pytest.mark.parametrize('scenario, expected_exit', [('normal', 0), ('timeout', 124), ('suppressed', 1), ('full', 0)])
def test_source_pytest_reports_current_test_and_stacks_without_local_values(tmp_path, scenario, expected_exit):
    """Exercise actual threads/pytest in a subprocess, including the hard exit."""
    # Confine nested discovery even when CI's tmp_path lives under the checkout.
    (tmp_path / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    target = tmp_path / 'test_slow_source.py'
    body = ('    try:\n'
            '        raise OSError("PRIVATE_LOCAL_SENTINEL")\n'
            '    except OSError:\n'
            '        raise RuntimeError("safe_public_failure") from None\n'
            if scenario == 'suppressed' else f'    time.sleep({10 if scenario == "timeout" else 0.5})\n')
    target.write_text(
        'import time\n'
        'def test_slow_source(capfd):\n'
        '    private_value = "PRIVATE_LOCAL_SENTINEL"\n' + body, encoding='utf-8')
    wrapper = Path(__file__).parents[1] / 'tools/ci_pytest.py'
    script = (
        'import importlib.util, sys\n'
        'spec = importlib.util.spec_from_file_location("ci_pytest", sys.argv[1])\n'
        'module = importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(module)\n'
        f'raise SystemExit(module.main([sys.argv[2]], dump_after={30 if scenario == "suppressed" else 0.1}, timeout=5))\n')
    environment = os.environ.copy()
    environment['PYTEST_ADDOPTS'] = ''
    environment.pop('PYTEST_PLUGINS', None)
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    command = ([sys.executable, '-u', str(wrapper), '--timeout-seconds', '5'] if scenario == 'full' else
               [sys.executable, '-u', '-c', script, str(wrapper), str(target)])
    result = subprocess.run(command,
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == expected_exit
    starting = 'CI pytest starting: test_slow_source.py::test_slow_source\n'
    assert starting in result.stderr
    assert 'PRIVATE_LOCAL_SENTINEL' not in result.stdout + result.stderr
    assert ('CI pytest hard timeout: 5s' in result.stderr) is (scenario == 'timeout')
    if scenario == 'full':
        assert 'CI pytest budget: stack=120s timeout=5s\n' in result.stderr
    if scenario == 'suppressed':
        prefix = 'CI pytest failure: '
        reports = [json.loads(line[len(prefix):]) for line in result.stderr.splitlines() if line.startswith(prefix)]
        assert len(reports) == 1 and reports[0]['phase'] == 'call'
        chain = reports[0]['exceptions']
        assert [error['type'] for error in chain] == ['RuntimeError', 'OSError']
        locations = [location for error in chain for location in error['locations']]
        assert len(locations) <= 12
        assert any(location['file'] == target.name and location['line'] > 0 for location in locations)
        assert all('/' not in location['file'] and '\\' not in location['file'] for location in locations)
        assert str(tmp_path) not in result.stderr and str(wrapper) not in result.stderr
    elif scenario != 'full':
        assert 'CI pytest stack after: 0.1s' in result.stderr[result.stderr.index(starting):]
        assert 'in test_slow_source' in result.stderr


@pytest.mark.parametrize('child', [False, True])
def test_ci_launcher_rejects_a_process_outside_actions_before_creating_state(tmp_path, child):
    environment = os.environ.copy()
    environment['GITHUB_ACTIONS'] = 'false'
    environment['GITHUB_WORKSPACE'] = str(tmp_path)
    launcher = Path(__file__).parents[1] / 'tools/run_ci_build.ps1'
    command = ['pwsh', '-NoProfile', '-NonInteractive', '-File', str(launcher)]
    if child:
        command.append('-Child')
    result = subprocess.run(command, env=environment, cwd=tmp_path, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode != 0
    assert 'CI_ACTIONS_REQUIRED' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_ci_launcher_refuses_an_unadmitted_child_before_creating_state(tmp_path):
    environment = os.environ.copy()
    environment['GITHUB_ACTIONS'] = 'true'
    launcher = Path(__file__).parents[1] / 'tools/run_ci_build.ps1'
    result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(launcher),
                             '-Child'], env=environment, cwd=tmp_path, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode != 0
    assert 'CI_OWNED_CHILD_REQUIRED' in result.stderr
    assert list(tmp_path.iterdir()) == []


def fixture_archive(tmp_path, *, extra=None, payload=b'public runtime'):
    data = b'public runtime'
    manifest = json.dumps({'files': [{'path': 'bin/server.dll', 'size': len(data),
                                     'sha256': hashlib.sha256(data).hexdigest()}]}).encode()
    target = tmp_path / 'runtime'
    target.mkdir()
    tracked = target / 'manifest.json'
    tracked.write_bytes(manifest)
    archive = tmp_path / 'runtime.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('manifest.json', manifest)
        output.writestr('bin/server.dll', payload)
        if extra:
            output.writestr(extra, b'not allowed')
    return archive, target, hashlib.sha256(manifest).hexdigest()


def test_runtime_restores_only_pinned_inventory(tmp_path):
    archive, target, digest = fixture_archive(tmp_path)
    prepare.restore_runtime(archive, target, digest)
    assert (target / 'bin/server.dll').read_bytes() == b'public runtime'
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()) == [
        'bin/server.dll', 'manifest.json']


@pytest.mark.parametrize('extra', ['../outside.txt', 'settings.json', 'BIN/server.dll'])
def test_runtime_rejects_extra_traversal_and_case_collisions(tmp_path, extra):
    archive, target, digest = fixture_archive(tmp_path, extra=extra)
    with pytest.raises(ValueError):
        prepare.restore_runtime(archive, target, digest)
    assert not (target / 'bin/server.dll').exists()


def test_runtime_rejects_changed_payload_before_install(tmp_path):
    archive, target, digest = fixture_archive(tmp_path, payload=b'altered runtime')
    with pytest.raises(ValueError):
        prepare.restore_runtime(archive, target, digest)
    assert not (target / 'bin/server.dll').exists()


def test_runtime_preserves_conflicting_existing_file(tmp_path):
    archive, target, digest = fixture_archive(tmp_path)
    (target / 'bin').mkdir()
    existing = target / 'bin/server.dll'
    existing.write_bytes(b'existing data')
    with pytest.raises(ValueError):
        prepare.restore_runtime(archive, target, digest)
    assert existing.read_bytes() == b'existing data'


def fixture_build(tmp_path):
    commit = 'a' * 40
    base = tmp_path / 'artifacts/development' / commit
    (base / 'artifacts').mkdir(parents=True)
    (base / 'qa/profile').mkdir(parents=True)
    installer = base / 'artifacts/ssearch-Setup-0.2.0.exe'
    installer.write_bytes(b'installer')
    manifest = {'source_commit': commit, 'app_version': '0.2.0', 'test_scope': 'full',
                'test_targets': [], 'full_suite_passed': True,
                'installer': {'path': 'artifacts/ssearch-Setup-0.2.0.exe', 'size': 9,
                              'sha256': hashlib.sha256(b'installer').hexdigest()}}
    (base / 'build-manifest.json').write_text(json.dumps(manifest))
    (base / 'qa/test-receipt.json').write_text(json.dumps({
        'source_commit': commit, 'scope': 'full', 'test_targets': [],
        'full_suite_passed': True, 'exit_code': 0}))
    proof = {'ok': True, 'source_commit': commit, 'execution_mode': 'frozen',
             'private_path': 'PRIVATE_SENTINEL', 'credential': 'PRIVATE_SENTINEL'}
    proof.update(dict.fromkeys(('model_ready', 'clean_stop', 'source_clean',
        'public_identity_preserved', 'scram', 'wrong_password_rejected', 'profile_removed',
        'restart_verified', 'dpapi_verified', 'private_acl_verified',
        'application_privileges_verified'), True))
    for filename in ('frozen-self-test.json', 'postgres-runtime-proof.json'):
        (base / 'qa' / filename).write_text(json.dumps(proof))
    (base / 'qa/profile/credentials.dpapi').write_bytes(b'PRIVATE_SENTINEL')
    return commit, installer


def test_export_omits_profiles_and_private_proof_fields(tmp_path):
    commit, _ = fixture_build(tmp_path)
    output = exporter.export_artifacts(tmp_path, commit)
    assert {path.name for path in output.iterdir()} == {
        'ssearch-Setup-0.2.0.exe', 'build-manifest.json', 'test-receipt.json',
        'ci-evidence.json', 'SHA256SUMS.txt'}
    assert all(b'PRIVATE_SENTINEL' not in path.read_bytes() for path in output.iterdir())


def test_export_rejects_installer_changed_after_proof(tmp_path):
    commit, installer = fixture_build(tmp_path)
    installer.write_bytes(b'changed!!')
    with pytest.raises(ValueError, match='Installer'):
        exporter.export_artifacts(tmp_path, commit)
    assert not (tmp_path / 'artifacts/ci-export').exists()
