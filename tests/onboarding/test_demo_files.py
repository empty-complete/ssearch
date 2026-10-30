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


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows sharing contract')
def test_busy_owned_file_is_deferred_and_cleanup_retry_is_safe(tmp_path):
    import ctypes as c
    from ctypes import wintypes as w

    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    relative = json.loads(MANIFEST.read_text(encoding='utf-8'))['files'][0]
    busy = session.root / relative
    kernel = c.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [w.HANDLE]
    close.restype = w.BOOL
    handle = create(str(busy), 0x80000000, 1, None, 3, 0x200000, None)
    assert handle and handle != c.c_void_p(-1).value, c.WinError(c.get_last_error())
    try:
        result = module.close_demo_session(session.session_id)
        assert busy in result.deferred
    finally:
        close(handle)
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


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows handle race contract')
def test_cleanup_leaf_swap_during_fingerprint_never_deletes_replacement(tmp_path, monkeypatch):
    import hashlib
    from atom_search.onboarding import demo_files

    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    relative = json.loads(MANIFEST.read_text(encoding='utf-8'))['files'][0]
    leaf = session.root / relative
    content = leaf.read_bytes()
    original_hash = hashlib.sha256
    attempted = []
    replaced = []

    def racing_hash(data=b'', *args, **kwargs):
        digest = original_hash(data, *args, **kwargs)
        if data == content and not attempted:
            attempted.append(True)
            try:
                leaf.unlink()
                leaf.write_text('foreign replacement during cleanup', encoding='utf-8')
                replaced.append(True)
            except OSError:
                pass  # A held Windows leaf handle must deny replacement.
        return digest

    with monkeypatch.context() as patch:
        patch.setattr(demo_files.hashlib, 'sha256', racing_hash)
        module.close_demo_session(session.session_id)
    assert attempted, 'race must run at the actual fingerprint boundary'
    if replaced:
        assert leaf.read_text(encoding='utf-8') == 'foreign replacement during cleanup'


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows handle race contract')
def test_creation_receipt_never_adopts_replacement_after_write(tmp_path, monkeypatch):
    import hashlib
    from atom_search.onboarding import demo_files

    module = api()
    owned = tmp_path / 'owned'
    relative = json.loads(MANIFEST.read_text(encoding='utf-8'))['files'][0]
    content = (MANIFEST.parent / relative).read_bytes()
    original_hash = hashlib.sha256
    occurrences, attempted, replacements = [], [], []

    def racing_hash(data=b'', *args, **kwargs):
        digest = original_hash(data, *args, **kwargs)
        if data == content:
            occurrences.append(True)
            if len(occurrences) == 2:
                attempted.append(True)
                leaf, = tuple(owned.glob('v2-*/' + relative))
                try:
                    leaf.unlink()
                    leaf.write_text('foreign replacement before receipt', encoding='utf-8')
                    replacements.append(leaf)
                except OSError:
                    pass  # Receipt must be computed while the created handle is held.
        return digest

    with monkeypatch.context() as patch:
        patch.setattr(demo_files.hashlib, 'sha256', racing_hash)
        session = module.open_demo_session(MANIFEST, owned, ())
    assert attempted, 'race must run at the created file fingerprint boundary'
    assert not replacements, 'the created file handle must deny replacement until its receipt is retained'
    module.close_demo_session(session.session_id)
    for leaf in replacements:
        assert leaf.read_text(encoding='utf-8') == 'foreign replacement before receipt'


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows handle race contract')
def test_cleanup_parent_junction_swap_at_fingerprint_boundary_is_blocked_or_preserved(tmp_path, monkeypatch):
    import hashlib
    import subprocess
    from atom_search.onboarding import demo_files

    module = api()
    session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    relative = json.loads(MANIFEST.read_text(encoding='utf-8'))['files'][0]
    leaf = session.root / relative
    branch = leaf.parent
    detached = tmp_path / 'detached-race'
    target = tmp_path / 'external'
    target.mkdir()
    sentinel = target / leaf.name
    sentinel.write_text('external parent-race sentinel', encoding='utf-8')
    content = leaf.read_bytes()
    original_hash = hashlib.sha256
    attempted, moved = [], []

    def racing_hash(data=b'', *args, **kwargs):
        digest = original_hash(data, *args, **kwargs)
        if data == content and not attempted:
            attempted.append(True)
            try:
                assert branch.resolve().is_relative_to(tmp_path.resolve())
                assert detached.resolve().is_relative_to(tmp_path.resolve())
                branch.rename(detached)
            except OSError:
                pass
            else:
                moved.append(True)
                subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(branch), str(target)],
                               check=True, capture_output=True)
        return digest

    try:
        with monkeypatch.context() as patch:
            patch.setattr(demo_files.hashlib, 'sha256', racing_hash)
            module.close_demo_session(session.session_id)
        assert attempted, 'parent race must run at the actual fingerprint boundary'
        assert sentinel.read_text(encoding='utf-8') == 'external parent-race sentinel'
        if moved:
            assert branch.is_junction()
            assert (detached / leaf.name).is_file()
    finally:
        if branch.is_junction():
            branch.rmdir()


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows handle creation contract')
def test_new_session_directory_identity_is_retained_before_receipt(tmp_path, monkeypatch):
    from atom_search.onboarding._windows_files import WindowsFiles

    module = api()
    owned = tmp_path / 'owned'
    detached = tmp_path / 'detached-directory-race'
    original_identity = WindowsFiles._identity
    attempted, moved = [], []

    def racing_identity(self, handle, *, directory):
        identity = original_identity(self, handle, directory=directory)
        roots = tuple(owned.glob('v2-*')) if owned.exists() else ()
        if directory and roots and not attempted:
            attempted.append(True)
            root, = roots
            try:
                assert root.resolve().is_relative_to(tmp_path.resolve())
                assert detached.resolve().is_relative_to(tmp_path.resolve())
                root.rename(detached)
            except OSError:
                pass
            else:
                moved.append(True)
                root.mkdir()
                (root / 'foreign.txt').write_text('replacement directory', encoding='utf-8')
        return identity

    with monkeypatch.context() as patch:
        patch.setattr(WindowsFiles, '_identity', racing_identity)
        session = module.open_demo_session(MANIFEST, owned, ())
    assert attempted, 'directory race must run while the newly created identity is observed'
    assert not moved, 'new directory must be pinned by its same creation handle'
    module.close_demo_session(session.session_id)


def test_unsupported_platform_never_falls_back_to_path_based_mutation(tmp_path, monkeypatch):
    from atom_search.onboarding import demo_files

    owned = tmp_path / 'owned'
    with monkeypatch.context() as patch:
        patch.setattr(demo_files.os, 'name', 'posix')
        with pytest.raises(RuntimeError, match='unsupported_demo_platform'):
            demo_files.open_demo_session(MANIFEST, owned, ())
    assert not owned.exists()


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows local-volume contract')
@pytest.mark.parametrize('drive_type', [4, 2, 0])
def test_mapped_or_nonfixed_owned_volume_is_rejected_before_any_anchor_open(tmp_path, monkeypatch, drive_type):
    from contextlib import contextmanager
    from atom_search.onboarding import demo_files
    from atom_search.onboarding import _windows_files

    module = api()
    original_library = _windows_files.c.WinDLL
    original_handle = _windows_files.WindowsFiles._handle
    observed, touched = [], []

    class DriveType:
        def __init__(self, kernel):
            self.actual = kernel.GetDriveTypeW
            self.actual.argtypes = [_windows_files.w.LPCWSTR]
            self.actual.restype = _windows_files.w.UINT

        def __call__(self, anchor):
            if str(anchor).upper().startswith('Z:'):
                observed.append(str(anchor))
                return drive_type
            return self.actual(anchor)

    class Kernel:
        def __init__(self, actual):
            self.actual = actual
            self.GetDriveTypeW = DriveType(actual)

        def __getattr__(self, name):
            return getattr(self.actual, name)

    def library(name, *args, **kwargs):
        actual = original_library(name, *args, **kwargs)
        return Kernel(actual) if str(name).lower() == 'kernel32' else actual

    @contextmanager
    def reject_mapped_file_operation(self, parent, name, **kwargs):
        if kwargs.get('anchor') and 'Z:' in name.upper():
            touched.append(name)
            pytest.fail('mapped drive anchor was opened before rejecting its nonlocal volume')
        with original_handle(self, parent, name, **kwargs) as handle:
            yield handle

    with monkeypatch.context() as patch:
        patch.setattr(_windows_files.c, 'WinDLL', library)
        patch.setattr(_windows_files.WindowsFiles, '_handle', reject_mapped_file_operation)
        with pytest.raises(ValueError, match='unsafe_demo_volume'):
            demo_files.open_demo_session(MANIFEST, Path('Z:/ssearch-owned-demo'), ())
    assert observed, 'drive classification must precede native mapped-anchor operations'
    assert touched == []


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows local-volume contract')
@pytest.mark.parametrize('target', ['', r'\Device\Mup\server\share', r'\Device\LanmanRedirector\mapped', r'\??\C:\alias'])
def test_fixed_letter_with_nonlocal_or_alias_device_is_rejected_before_native_open(tmp_path, monkeypatch, target):
    from contextlib import contextmanager
    from atom_search.onboarding import demo_files
    from atom_search.onboarding import _windows_files

    original_library = _windows_files.c.WinDLL
    observed, touched = [], []

    class Device:
        def __call__(self, name, buffer, size):
            observed.append(str(name))
            buffer.value = target
            return len(target) + 2 if target else 0

    class Kernel:
        def __init__(self, actual):
            self.actual = actual
            self.QueryDosDeviceW = Device()

        def __getattr__(self, name):
            return getattr(self.actual, name)

    def library(name, *args, **kwargs):
        actual = original_library(name, *args, **kwargs)
        return Kernel(actual) if str(name).lower() == 'kernel32' else actual

    @contextmanager
    def no_native_open(self, parent, name, **kwargs):
        touched.append(name)
        pytest.fail('untrusted DOS device mapping reached native filesystem operations')
        yield

    with monkeypatch.context() as patch:
        patch.setattr(_windows_files.c, 'WinDLL', library)
        patch.setattr(_windows_files.WindowsFiles, '_handle', no_native_open)
        with pytest.raises(ValueError, match='unsafe_demo_volume'):
            demo_files.open_demo_session(MANIFEST, tmp_path / 'owned', ())
    assert observed
    assert touched == []


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows local-volume contract')
def test_native_anchor_is_pinned_local_device_instead_of_mutable_drive_letter(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import re
    from atom_search.onboarding import _windows_files

    module = api()
    original_handle = _windows_files.WindowsFiles._handle
    anchors = []

    @contextmanager
    def record_anchor(self, parent, name, **kwargs):
        if kwargs.get('anchor'):
            anchors.append(name)
        with original_handle(self, parent, name, **kwargs) as handle:
            yield handle

    with monkeypatch.context() as patch:
        patch.setattr(_windows_files.WindowsFiles, '_handle', record_anchor)
        session = module.open_demo_session(MANIFEST, tmp_path / 'owned', ())
        module.close_demo_session(session.session_id)
    assert anchors
    assert all(re.fullmatch(r'\\Device\\HarddiskVolume[0-9]+\\', anchor, re.IGNORECASE) for anchor in anchors)
