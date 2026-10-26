"""Publisher contracts use synthetic public receipts; no native build or release calls."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


HEAD = 'a' * 40
MERGE = 'b' * 40
TREE = 'c' * 40
REPO = 'empty-complete/ssearch'


@pytest.fixture
def publisher():
    path = Path(__file__).parents[1] / 'tools/ci_publish.py'
    assert path.is_file(), 'Canonical publisher contract is not implemented yet'
    spec = importlib.util.spec_from_file_location('ci_publish', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pull():
    return {'number': 1, 'merged': True, 'merged_at': '2026-10-26T00:00:00Z',
            'merge_commit_sha': MERGE,
            'head': {'sha': HEAD, 'ref': 'codex/public-windows', 'repo': {'full_name': REPO}},
            'base': {'ref': 'main', 'repo': {'full_name': REPO}}}


def run():
    return {'id': 12, 'workflow_id': 34, 'path': '.github/workflows/windows-build.yml',
            'event': 'pull_request', 'status': 'completed', 'conclusion': 'success',
            'head_sha': HEAD, 'head_branch': 'codex/public-windows',
            'head_repository': {'full_name': REPO}, 'pull_requests': []}


def test_empty_run_pr_list_uses_independent_exact_commit_association(publisher):
    publisher.validate_pr(pull(), REPO, 1)
    publisher.validate_run(run(), pull(), REPO, 34, [pull()])


@pytest.mark.parametrize('field,value', [
    ('head_sha', 'd' * 40), ('head_branch', 'other'), ('workflow_id', 35),
    ('path', '.github/workflows/other.yml'), ('event', 'workflow_dispatch'),
    ('status', 'in_progress'), ('conclusion', 'failure'),
    ('head_repository', {'full_name': 'foreign/repo'}),
    ('pull_requests', [{'number': 2}]),
])
def test_run_identity_cannot_be_substituted(publisher, field, value):
    candidate = run()
    candidate[field] = value
    with pytest.raises(publisher.PublishError):
        publisher.validate_run(candidate, pull(), REPO, 34, [pull()])


@pytest.mark.parametrize('mutation', ['head', 'base', 'repository', 'unmerged', 'association'])
def test_authoritative_pr_and_commit_association_are_both_required(publisher, mutation):
    authoritative, associated = pull(), pull()
    if mutation == 'head':
        associated['head']['sha'] = 'd' * 40
    elif mutation == 'base':
        authoritative['base']['ref'] = 'other'
    elif mutation == 'repository':
        associated['base']['repo']['full_name'] = 'foreign/repo'
    elif mutation == 'unmerged':
        authoritative['merged'] = False
    else:
        associated['number'] = 2
    with pytest.raises(publisher.PublishError):
        publisher.validate_pr(authoritative, REPO, 1)
        publisher.validate_run(run(), authoritative, REPO, 34, [associated])


def package(tmp_path):
    directory = tmp_path / 'package'
    directory.mkdir()
    installer = directory / 'ssearch-Setup-0.2.0.exe'
    installer.write_bytes(b'tested installer')
    manifest = {'source_commit': HEAD, 'app_version': '0.2.0', 'core_version': '0.2.0',
                'test_scope': 'full', 'test_targets': [], 'full_suite_passed': True,
                'catalog': {'backend': 'postgresql', 'schema_version': 5},
                'capabilities': {'postgresql': 'application_verified'}, 'windows_validation': [],
                'installer': {'path': 'artifacts/' + installer.name, 'size': installer.stat().st_size,
                              'sha256': hashlib.sha256(installer.read_bytes()).hexdigest()}}
    receipt = {'source_commit': HEAD, 'scope': 'full', 'test_targets': [],
               'full_suite_passed': True, 'exit_code': 0}
    checks = {
        'application': ['model_ready', 'clean_stop', 'source_clean', 'public_identity_preserved'],
        'postgresql': ['scram', 'wrong_password_rejected', 'clean_stop', 'profile_removed',
                       'restart_verified', 'dpapi_verified', 'private_acl_verified',
                       'application_privileges_verified']}
    evidence = {'schema_version': 1, 'source_commit': HEAD, 'test_scope': 'full',
                'full_suite_passed': True, 'runner_platform': 'Windows Server 2025 x64',
                'windows_validation': [], 'release_channel': 'development-prerelease',
                'frozen_proofs': {name: {'passed': True, 'execution_mode': 'frozen',
                    'original_receipt_sha256': 'e' * 64, 'checks': dict.fromkeys(flags, True)}
                    for name, flags in checks.items()}}
    for name, value in [('build-manifest.json', manifest), ('test-receipt.json', receipt),
                        ('ci-evidence.json', evidence)]:
        (directory / name).write_text(json.dumps(value), encoding='utf-8')
    write_sums(directory)
    return directory


def write_sums(directory):
    (directory / 'SHA256SUMS.txt').write_text(''.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
        for path in sorted(directory.iterdir()) if path.name != 'SHA256SUMS.txt'), encoding='utf-8')


def test_only_original_full_public_files_qualify(publisher, tmp_path):
    directory = package(tmp_path)
    result = publisher.validate_package(directory, HEAD)
    assert result['app_version'] == '0.2.0'
    assert len(list(directory.iterdir())) == 5


@pytest.mark.parametrize('mutation', ['installer', 'targeted', 'core', 'receipt', 'frozen', 'extra', 'sums'])
def test_package_cannot_transfer_or_weaken_proof(publisher, tmp_path, mutation):
    directory = package(tmp_path)
    if mutation == 'installer':
        (directory / 'ssearch-Setup-0.2.0.exe').write_bytes(b'substitute')
    elif mutation == 'extra':
        (directory / 'private-profile.json').write_text('private')
    elif mutation == 'sums':
        (directory / 'SHA256SUMS.txt').write_text('')
    else:
        filename = {'targeted': 'build-manifest.json', 'core': 'build-manifest.json',
                    'receipt': 'test-receipt.json', 'frozen': 'ci-evidence.json'}[mutation]
        value = json.loads((directory / filename).read_text())
        if mutation == 'targeted':
            value['test_scope'] = 'targeted'
        elif mutation == 'core':
            value['core_version'] = '0.1.0'
        elif mutation == 'receipt':
            value['source_commit'] = MERGE
        else:
            value['frozen_proofs']['postgresql']['checks']['dpapi_verified'] = False
        (directory / filename).write_text(json.dumps(value))
        write_sums(directory)
    with pytest.raises(publisher.PublishError):
        publisher.validate_package(directory, HEAD)


@pytest.mark.parametrize('name', ['../escape', 'nested/file', 'BUILD-MANIFEST.JSON', 'private.txt'])
def test_archive_inventory_rejects_unsafe_or_unexpected_entries(publisher, tmp_path, name):
    directory = package(tmp_path)
    archive = tmp_path / 'artifact.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        for path in directory.iterdir():
            output.write(path, path.name)
        output.writestr(name, 'unexpected')
    metadata = {'size_in_bytes': archive.stat().st_size,
                'digest': 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()}
    with pytest.raises(publisher.PublishError):
        publisher.extract_artifact(archive, metadata, tmp_path / 'extracted')
    assert not (tmp_path / 'extracted').exists()


def test_archive_digest_precedes_extraction(publisher, tmp_path):
    archive = tmp_path / 'artifact.zip'
    archive.write_bytes(b'not a zip')
    with pytest.raises(publisher.PublishError):
        publisher.extract_artifact(archive, {'size_in_bytes': 9, 'digest': 'sha256:' + '0' * 64},
                                   tmp_path / 'extracted')
    assert not (tmp_path / 'extracted').exists()


def workflow():
    return 'name: Windows build\non: push\npermissions:\n  contents: read\njobs:\n  changes:\n    old: true\n  build:\n    steps:\n      - run: immutable build\n\n  publish:\n    old: true\n'


def test_publisher_only_change_preserves_exact_binary_boundary(publisher):
    original = workflow()
    changed = original.replace('old: true', 'new: true')
    paths = ['.github/workflows/windows-build.yml', 'tools/ci_publish.py',
             'tests/test_ci_publish.py', '.github/workflows/publish-existing.yml',
             '.github/workflows/publisher-checks.yml']
    assert publisher.requires_build(paths, original, changed) is False


@pytest.mark.parametrize('mutation', ['build', 'header', 'unknown', 'renamed_previous'])
def test_publisher_exemption_fails_closed(publisher, mutation):
    original = workflow()
    changed = original
    paths = ['tools/ci_publish.py']
    if mutation == 'build':
        changed = changed.replace('immutable build', 'different build')
    elif mutation == 'header':
        changed = changed.replace('on: push', 'on: pull_request')
    else:
        paths.append('src/product.py' if mutation == 'unknown' else 'tools/build.ps1')
    assert publisher.requires_build(paths, original, changed) is True


def test_existing_documentation_and_frontend_exemptions_remain(publisher):
    assert not publisher.requires_build(['README.md', 'frontend/src/view.tsx'], workflow(), workflow())
    assert publisher.requires_build(['tools/ci_publish.py', 'README.md'], workflow(), workflow())


def test_existing_release_requires_exact_target_and_all_asset_hashes(publisher, tmp_path):
    directory = package(tmp_path)
    release = {'tag_name': 'dev-' + MERGE, 'target_commitish': MERGE,
               'prerelease': True, 'draft': False,
               'assets': [{'name': path.name, 'size': path.stat().st_size,
                           'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path in directory.iterdir()]}
    publisher.validate_existing_release(release, MERGE, directory)
    release['assets'][0]['digest'] = 'sha256:' + '0' * 64
    with pytest.raises(publisher.PublishError):
        publisher.validate_existing_release(release, MERGE, directory)
