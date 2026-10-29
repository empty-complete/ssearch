"""Owned real TXT lifecycle never deletes caller or foreign files."""
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import threading

import pytest

from atom_search.service.contracts import SearchRequest


MANIFEST = Path(__file__).resolve().parents[2] / 'examples/onboarding-demo/manifest.json'


def api():
    assert importlib.util.find_spec('atom_search.onboarding') is not None, 'owned demo session lifecycle is required'
    return importlib.import_module('atom_search.onboarding')


def copied_manifest(tmp_path):
    target = tmp_path / 'dataset'
    shutil.copytree(MANIFEST.parent, target)
    return target / 'manifest.json'


def test_materializes_only_manifest_owned_utf8_txt_and_close_is_idempotent(tmp_path):
    module = api()
    owned = tmp_path / 'owned'
    owned.mkdir()
    foreign = owned / 'user-notes.txt'
    foreign.write_text('keep me', encoding='utf-8')
    session = module.open_demo_session(MANIFEST, owned, ())
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    root = session.root
    assert root != owned and root.is_relative_to(owned)
    assert {str(path.relative_to(root)).replace('\\', '/') for path in root.rglob('*.txt')} == set(data['files'])
    for relative in data['files']:
        text = (root / relative).read_text(encoding='utf-8')
        assert 'Учебные данные.' in text and 'Вымышленный' in text
    module.close_demo_session(session.session_id)
    module.close_demo_session(session.session_id)
    assert foreign.read_text(encoding='utf-8') == 'keep me'
    assert not any((root / relative).exists() for relative in data['files'])


def test_cleanup_preserves_unknown_and_modified_files(tmp_path):
    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    unknown = session.root / 'personal.txt'
    unknown.write_text('foreign', encoding='utf-8')
    modified = session.root / data['files'][0]
    modified.write_text('user replaced this file', encoding='utf-8')
    module.close_demo_session(session.session_id)
    assert unknown.read_text(encoding='utf-8') == 'foreign'
    assert modified.read_text(encoding='utf-8') == 'user replaced this file'


@pytest.mark.parametrize('bad_path', ['../escape.txt', '/escape.txt', 'C:/escape.txt', 'folder/../../escape.txt',
                                     'folder\\escape.txt', 'file.pdf', 'file.txt:stream', 'folder/CON.txt'])
def test_manifest_rejects_unsafe_paths_before_materialization(tmp_path, bad_path):
    module = api()
    manifest = copied_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    data['files'][0] = bad_path
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    owned = tmp_path / 'owned'
    with pytest.raises(ValueError, match='invalid_demo_manifest'):
        module.open_demo_session(manifest, owned, ())
    assert not owned.exists() or not list(owned.iterdir())


@pytest.mark.parametrize('change', ['duplicate', 'wrong_version', 'not_fictional', 'too_many_files', 'invalid_utf8', 'missing_marker'])
def test_manifest_rejects_invalid_dataset_before_materialization(tmp_path, change):
    module = api()
    manifest = copied_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if change == 'duplicate':
        data['files'][1] = data['files'][0].upper()
    elif change == 'wrong_version':
        data['version'] = 1
    elif change == 'not_fictional':
        data['fictional'] = False
    elif change == 'too_many_files':
        data['files'].append('extra.txt')
    elif change == 'invalid_utf8':
        (manifest.parent / data['files'][0]).write_bytes(b'\xff\xfe')
    elif change == 'missing_marker':
        (manifest.parent / data['files'][0]).write_text('ordinary document', encoding='utf-8')
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    owned = tmp_path / 'owned'
    with pytest.raises(ValueError, match='invalid_demo_manifest'):
        module.open_demo_session(manifest, owned, ())
    assert not owned.exists() or not list(owned.iterdir())


def test_cleanup_refuses_symlink_escape_and_preserves_external_sentinels(tmp_path):
    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    target = tmp_path / 'external'
    target.mkdir()
    sentinel = target / 'Спецификация.txt'
    sentinel.write_text('must survive', encoding='utf-8')
    branch = session.root / 'Учебный архив/Пермские моторы/Зенитные фонари'
    shutil.rmtree(branch)
    try:
        branch.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'host cannot create symlinks: {error}')
    module.close_demo_session(session.session_id)
    assert sentinel.read_text(encoding='utf-8') == 'must survive'
    assert branch.is_symlink()


def test_manifest_source_symlink_is_rejected(tmp_path):
    module = api()
    manifest = copied_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    file = manifest.parent / data['files'][0]
    file.unlink()
    target = tmp_path / 'external.txt'
    target.write_text('Учебные данные. Вымышленный example', encoding='utf-8')
    try:
        file.symlink_to(target)
    except OSError as error:
        pytest.skip(f'host cannot create symlinks: {error}')
    with pytest.raises(ValueError, match='invalid_demo_manifest'):
        module.open_demo_session(manifest, tmp_path / 'owned', ())
    assert target.exists()


def test_source_parent_excludes_demo_and_install_cache_before_descent(tmp_path):
    from atom_search.service.sources import SourceRegistry
    from atom_search_core.source import FileSystemSource

    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    try:
        work_file = tmp_path / 'work.txt'
        work_file.write_text('work', encoding='utf-8')
        staging = tmp_path / 'install-cache'
        staging.mkdir()
        (staging / 'setup.bin').write_bytes(b'installer')
        registry = SourceRegistry(None, None, owner_id='host-owner')
        registry.register_owned_exclusion(session.root.parent, owner_id='host-owner')
        registry.register_owned_exclusion(staging, owner_id='host-owner')
        snapshots = tuple(FileSystemSource().snapshots(str(tmp_path), threading.Event(),
                                                       excluded_roots=registry.owned_exclusions()))
        assert [file.relative_path for snapshot in snapshots for file in snapshot.files] == ['work.txt']
        assert not any('Учебный архив' in snapshot.relative_path or 'install-cache' in snapshot.relative_path for snapshot in snapshots)
    finally:
        module.close_demo_session(session.session_id)


def test_closed_session_provider_cannot_return_paths_for_deleted_files(tmp_path):
    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    module.close_demo_session(session.session_id)
    with pytest.raises(RuntimeError, match='demo_session_closed'):
        session.provider.search(SearchRequest('спецификация'))


def test_busy_owned_file_is_deferred_and_cleanup_retry_is_safe(tmp_path, monkeypatch):
    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    relative = json.loads(MANIFEST.read_text(encoding='utf-8'))['files'][0]
    busy = session.root / relative
    original_unlink = Path.unlink

    def occupied(path, *args, **kwargs):
        if path == busy:
            raise PermissionError('file is occupied')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', occupied)
        module.close_demo_session(session.session_id)
    assert busy.is_file()
    module.close_demo_session(session.session_id)
    assert not busy.exists()


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows junction contract')
def test_windows_junction_cleanup_never_follows_external_target(tmp_path):
    import subprocess

    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    target = tmp_path / 'external'
    target.mkdir()
    sentinel = target / 'Спецификация.txt'
    sentinel.write_text('external sentinel', encoding='utf-8')
    branch = session.root / 'Учебный архив/Пермские моторы/Зенитные фонари'
    assert branch.resolve().is_relative_to(session.root.resolve())
    shutil.rmtree(branch)
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(branch), str(target)],
                   check=True, capture_output=True)
    try:
        result = module.close_demo_session(session.session_id)
        assert sentinel.read_text(encoding='utf-8') == 'external sentinel'
        assert result.preserved
        assert branch.is_junction()
    finally:
        branch.rmdir()  # Remove only the test-created junction, never its target.


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows junction contract')
def test_windows_junction_manifest_source_is_rejected_without_copying(tmp_path):
    import subprocess

    module = api()
    manifest = copied_manifest(tmp_path)
    branch = manifest.parent / 'Учебный архив/Пермские моторы/Зенитные фонари'
    assert branch.resolve().is_relative_to(manifest.parent.resolve())
    shutil.rmtree(branch)
    target = tmp_path / 'external'
    target.mkdir()
    sentinel = target / 'Спецификация.txt'
    sentinel.write_text('Учебные данные. Вымышленный external sentinel', encoding='utf-8')
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(branch), str(target)],
                   check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='invalid_demo_manifest'):
            module.open_demo_session(manifest, tmp_path / 'owned', ())
        assert sentinel.read_text(encoding='utf-8') == 'Учебные данные. Вымышленный external sentinel'
        assert not (tmp_path / 'owned').exists()
    finally:
        branch.rmdir()


def test_replaced_session_root_is_not_adopted_for_search_or_cleanup(tmp_path):
    from atom_search.service.contracts import ServiceError

    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    detached = tmp_path / 'detached-original'
    assert session.root.resolve().is_relative_to(tmp_path.resolve())
    assert detached.resolve().is_relative_to(tmp_path.resolve())
    session.root.rename(detached)
    session.root.mkdir()
    relative = json.loads(MANIFEST.read_text(encoding='utf-8'))['files'][0]
    substitute = session.root / relative
    substitute.parent.mkdir(parents=True)
    substitute.write_bytes((detached / relative).read_bytes())
    with pytest.raises(ServiceError) as failure:
        session.provider.search(SearchRequest('Пермские моторы'))
    assert failure.value.code == 'result_not_found'
    result = module.close_demo_session(session.session_id)
    assert substitute.is_file()
    assert (detached / relative).is_file()
    assert result.preserved
