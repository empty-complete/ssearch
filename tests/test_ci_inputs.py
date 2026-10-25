"""Public build-input and publication boundaries; run by the GitHub Full build."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load_tool('prepare_ci')
exporter = load_tool('export_ci_artifacts')


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
