"""Bounded staging admits signed bytes only into one budget-owned directory."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import shutil
import zlib
import zipfile

import pytest

from atom_search.runtime import _windows
from atom_search.runtime.storage import StorageBudget
from atom_search.updates.contracts import UpdateError
from test_update_manifest import key
from test_update_manifest import encode

_spec = importlib.util.spec_from_file_location('update_release_fixture', Path(__file__).parent / 'fixtures/update_release.py')
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
api = _fixtures.api


@pytest.fixture
def release(tmp_path, key):
    return _fixtures.FixtureRelease(tmp_path, key)


def stage(release, transport=None, **kwargs):
    return api('staging').stage_release(release.verified(), transport or release.transport(),
                                       staging_dir=release.staging, budget=release.budget, **kwargs)


def test_verified_archive_stages_exact_files_without_modifying_source(release):
    before = release.source_bytes()
    result = stage(release)
    assert result.directory.is_relative_to(release.staging)
    assert result.manifest.release_id == 'fixture-release'
    assert {str(path.relative_to(result.directory)).replace('\\', '/'): path.read_bytes()
            for path in result.files} == release.files
    assert release.source_bytes() == before


@pytest.mark.parametrize('fault', ['hash', 'count', 'sum'])
def test_signed_inventory_checked_before_archive_read(release, fault):
    if fault == 'hash':
        release.inventory += b' '
    else:
        reference = release.document['artifacts'][0]['inventory']
        reference['entry_count' if fault == 'count' else 'unpacked_bytes'] += 1
    release.write()
    transport = release.transport()
    reads = []
    original = transport.open_artifact
    def observed(*args, **kwargs):
        reads.append('archive')
        return original(*args, **kwargs)
    transport.open_artifact = observed
    with pytest.raises(UpdateError):
        stage(release, transport)
    assert reads == []
    assert not release.staging.exists() or not list(release.staging.iterdir())


@pytest.mark.parametrize('fault', ['short', 'extra', 'changed', 'disconnect'])
def test_staging_stops_at_declared_size_and_deletes_partial_only(release, fault):
    before = release.source_bytes()
    transport = release.transport()
    raw = {'short': release.archive[:-1], 'extra': release.archive + b'!',
           'changed': b'!' + release.archive[1:], 'disconnect': release.archive}[fault]
    class Interrupted(io.BytesIO):
        def read(self, amount=-1):
            if self.tell() > 0:
                raise OSError('fictional disconnected source')
            return super().read(min(amount, 8))
    transport.open_artifact = lambda *_: Interrupted(raw) if fault == 'disconnect' else io.BytesIO(raw)
    foreign = release.data / 'foreign'
    foreign.mkdir()
    marker = foreign / 'keep.txt'
    marker.write_bytes(b'foreign data')
    with pytest.raises(UpdateError):
        stage(release, transport)
    assert marker.read_bytes() == b'foreign data'
    assert release.source_bytes() == before
    assert not release.staging.exists() or not list(release.staging.iterdir())


def test_real_storage_budget_denial_happens_before_staging_writes(release):
    old = release.data / 'old-install.bin'
    old.write_bytes(b'old application and profile')
    physical = _windows.allocated_bytes(release.budget.roots)
    item = release.document['artifacts'][0]
    peak = item['size'] + item['inventory']['size'] + item['inventory']['unpacked_bytes']
    release.budget = StorageBudget(release.budget.roots, quota_bytes=physical + peak - 1, min_free_bytes=0)
    with pytest.raises(UpdateError, match='update_storage'):
        stage(release)
    assert not release.staging.exists()
    assert old.read_bytes() == b'old application and profile'


def test_staging_must_be_strictly_inside_supplied_budget_roots(release, tmp_path):
    release.staging = tmp_path / 'unaccounted'
    with pytest.raises(UpdateError, match='update_path'):
        stage(release)
    assert not release.staging.exists()


def _no_archive_reads(release):
    transport = release.transport()
    reads = []
    original = transport.open_artifact
    def observed(*args):
        reads.append(args[-1])
        return original(*args)
    transport.open_artifact = observed
    return transport, reads


@pytest.mark.parametrize('fault', ['missing-service', 'wrong-executable-hash', 'file-zip-collision'])
def test_executable_membership_and_global_fileset_before_archive_fetch(release, fault):
    if fault == 'file-zip-collision':
        release.document['artifacts'].append(dict(id='collision', relative_path='desktop/ssearch.exe', size=0,
            sha256=hashlib.sha256(b'').hexdigest(), kind='extension', format='file', inventory=None))
    else:
        entries = json.loads(release.inventory)['entries']
        if fault == 'missing-service':
            entries = entries[:1]
        else:
            entries[0]['sha256'] = '0' * 64
        release.rebuild(inventory_entries=entries)
    release.write()
    transport, reads = _no_archive_reads(release)
    with pytest.raises(UpdateError, match='update_inventory'):
        stage(release, transport)
    assert reads == []


def test_all_inventories_precede_first_archive_fetch(release):
    from copy import deepcopy
    other = deepcopy(release.document['artifacts'][0])
    other.update(id='second', kind='extension', relative_path='payload/second.zip')
    other['inventory']['relative_path'] = 'payload/second.inventory'
    release.document['artifacts'].append(other)
    (release.source / 'payload/second.inventory').write_bytes(release.inventory + b'!')
    release.write()
    transport, reads = _no_archive_reads(release)
    with pytest.raises(UpdateError):
        stage(release, transport)
    assert reads == []


@pytest.mark.parametrize('fault', ['unlisted', 'missing', 'size', 'case-collision', 'escape', 'ads'])
def test_zip_whitelist_requires_exact_safe_file_set(release, fault):
    entries = list(release.files.items())
    if fault == 'unlisted': entries.append(('unexpected.txt', b'!'))
    if fault == 'missing': entries.pop()
    if fault == 'size': entries[0] = (entries[0][0], entries[0][1] + b'!')
    if fault == 'case-collision': entries.append((entries[0][0].upper(), entries[0][1]))
    if fault == 'escape': entries.append(('../outside.txt', b'!'))
    if fault == 'ads': entries.append(('desktop/ssearch.exe:stream', b'!'))
    release.rebuild(zip_entries=entries)
    before = release.source_bytes()
    with pytest.raises(UpdateError):
        stage(release)
    assert release.source_bytes() == before
    assert not release.staging.exists() or not list(release.staging.iterdir())


def _sign_archive(release, raw):
    release.archive = bytes(raw)
    release.document['artifacts'][0].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    release.write()


@pytest.mark.parametrize('fault', ['symlink', 'fifo', 'device', 'reparse', 'encrypted', 'descriptor',
                                  'extra', 'comment', 'trailing', 'sfx', 'huge-central'])
def test_unsupported_or_ambiguous_zip_features_fail_closed(release, fault):
    raw = bytearray(release.archive)
    central = struct.unpack_from('<I', raw, len(raw) - 6)[0]
    if fault in ('symlink', 'fifo', 'device'):
        mode = {'symlink': stat.S_IFLNK, 'fifo': stat.S_IFIFO, 'device': stat.S_IFCHR}[fault]
        struct.pack_into('<I', raw, central + 38, (mode | 0o600) << 16)
    if fault == 'reparse':
        value = struct.unpack_from('<I', raw, central + 38)[0]
        struct.pack_into('<I', raw, central + 38, value | 0x400)
    if fault in ('encrypted', 'descriptor'):
        flag = 1 if fault == 'encrypted' else 8
        struct.pack_into('<H', raw, 6, flag)
        struct.pack_into('<H', raw, central + 8, flag)
    if fault == 'extra': struct.pack_into('<H', raw, central + 30, 1)
    if fault == 'comment': struct.pack_into('<H', raw, len(raw) - 2, 1)
    if fault == 'trailing': raw += b'unlisted tail'
    if fault == 'sfx': raw = bytearray(b'MZ') + raw
    if fault == 'huge-central':
        struct.pack_into('<HHI', raw, len(raw) - 14, 65534, 65534, 0xf0000000)
    _sign_archive(release, raw)
    with pytest.raises(UpdateError, match='update_archive'):
        stage(release)


def test_actual_deflate_extra_decoded_byte_is_rejected_even_if_headers_describe_valid_prefix(release):
    original = release.files['desktop/ssearch.exe']
    release.rebuild(zip_entries=[('desktop/ssearch.exe', original + b'!'),
                                ('service/ssearch-service.exe', release.files['service/ssearch-service.exe'])])
    raw = bytearray(release.archive)
    central = struct.unpack_from('<I', raw, len(raw) - 6)[0]
    for offset, crc_at, size_at in ((0, 14, 22), (central, 16, 24)):
        struct.pack_into('<I', raw, offset + crc_at, zlib.crc32(original))
        struct.pack_into('<I', raw, offset + size_at, len(original))
    _sign_archive(release, raw)
    with pytest.raises(UpdateError, match='update_archive'):
        stage(release)
    assert not list(release.staging.iterdir())


def test_deflate_exact_compressed_span_rejects_unconsumed_tail(release):
    raw = bytearray(release.archive)
    central = struct.unpack_from('<I', raw, len(raw) - 6)[0]
    first_name = struct.unpack_from('<H', raw, central + 28)[0]
    second = central + 46 + first_name
    local = struct.unpack_from('<I', raw, second + 42)[0]
    compressed = struct.unpack_from('<I', raw, second + 20)[0]
    struct.pack_into('<I', raw, local + 18, compressed + 1)
    raw[central:central] = b'!'
    second += 1
    struct.pack_into('<I', raw, second + 20, compressed + 1)
    struct.pack_into('<I', raw, len(raw) - 6, central + 1)
    _sign_archive(release, raw)
    with pytest.raises(UpdateError, match='update_archive'):
        stage(release)


def test_cancellation_removes_only_unique_owned_partial_directory(release):
    private = _windows.private_directory(release.staging)
    foreign = private / 'foreign.txt'
    foreign.write_bytes(b'keep foreign staging')
    calls = []
    def cancelled():
        calls.append(1)
        return len(calls) > 7
    with pytest.raises(UpdateError, match='update_cancelled'):
        stage(release, cancelled=cancelled)
    assert foreign.read_bytes() == b'keep foreign staging'
    assert list(release.staging.iterdir()) == [foreign]


def test_plain_file_is_staged_without_archive_interpretation(release):
    raw = b'plain extension file, not a ZIP'
    release.document['artifacts'] = [dict(id='extension', relative_path='extension.bin', size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(), kind='extension', format='file', inventory=None)]
    (release.source / 'extension.bin').write_bytes(raw)
    result = stage(release)
    assert len(result.files) == 1 and result.files[0].read_bytes() == raw


def test_real_ensure_remaining_refusal_keeps_old_install_and_releases_reservation(release):
    old = release.data / 'old.bin'
    old.write_bytes(b'old index/settings/install')
    transport = release.transport()
    opening = transport.open_artifact
    def disk_growth(*args):
        # An actual new allocation in the shared domain after initial reserve.
        with (release.data / 'unexpected-growth.bin').open('wb') as stream:
            stream.write(b'x' * (2 * 1024 * 1024))
            stream.flush()
            os.fsync(stream.fileno())
        return opening(*args)
    transport.open_artifact = disk_growth
    release.budget = StorageBudget(release.budget.roots, quota_bytes=1024 * 1024, min_free_bytes=0)
    with pytest.raises(UpdateError, match='update_storage'):
        stage(release, transport)
    assert old.read_bytes() == b'old index/settings/install'
    ledger = release.budget.ledger
    assert json.loads(ledger.read_bytes()) == []
    assert not release.staging.exists() or not list(release.staging.iterdir())


def test_actual_peer_reservation_blocks_staging_and_is_released(release):
    ready, done = release.owned / 'peer.ready', release.owned / 'peer.done'
    script = '''import sys,time
from pathlib import Path
from atom_search.runtime.storage import StorageBudget
root,ready,done=map(Path,sys.argv[1:4])
budget=StorageBudget((root,),quota_bytes=1048576,min_free_bytes=0)
with budget.reserve(1000000,'fictional-peer'):
 ready.write_text('reserved')
 deadline=time.monotonic()+30
 while not done.exists() and time.monotonic()<deadline: time.sleep(.02)
'''
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    peer = subprocess.Popen([sys.executable, '-c', script, str(release.data), str(ready), str(done)],
                            env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        import time
        deadline = time.monotonic() + 20
        while not ready.exists() and peer.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), 'actual peer failed to acquire its shared reservation'
        release.budget = StorageBudget(release.budget.roots, quota_bytes=1048576, min_free_bytes=0)
        with pytest.raises(UpdateError, match='update_storage'):
            stage(release)
        assert not release.staging.exists()
    finally:
        done.write_text('release')
        assert peer.wait(timeout=35) == 0
    assert json.loads(release.budget.ledger.read_bytes()) == []


def _inventory(raw, count, unpacked):
    from atom_search.updates.contracts import ArchiveInventoryRef
    reference = ArchiveInventoryRef('fixture.inventory', len(raw), hashlib.sha256(raw).hexdigest(), count, unpacked)
    return api('inventory').verify_inventory(reference, raw)


@pytest.mark.parametrize('fault', ['duplicate', 'case', 'prefix', 'parent-interleaved', 'negative', 'bool', 'float',
                                  'traversal', 'device', 'ads', 'surrogate', 'unknown'])
def test_inventory_rejects_unsafe_or_ambiguous_entries(fault):
    entry = dict(path='safe/file.bin', size=0, sha256=hashlib.sha256(b'').hexdigest())
    entries = [entry]
    if fault in ('duplicate', 'case'): entries.append(entry | dict(path=entry['path'].upper() if fault == 'case' else entry['path']))
    if fault == 'prefix': entries = [entry | dict(path='a'), entry | dict(path='a/b')]
    if fault == 'parent-interleaved': entries = [entry | dict(path=name) for name in ('a', 'a-b', 'a/x')]
    if fault == 'negative': entry['size'] = -1
    if fault == 'bool': entry['size'] = True
    if fault == 'float': entry['size'] = 0.5
    if fault == 'traversal': entry['path'] = '../outside'
    if fault == 'device': entry['path'] = 'folder/COM¹.bin'
    if fault == 'ads': entry['path'] = 'folder/a:stream'
    if fault == 'surrogate': entry['path'] = 'folder/\ud800'
    if fault == 'unknown': entry['unrecognized'] = 'value'
    raw = json.dumps(dict(format_version=1, entries=entries), separators=(',', ':')).encode()
    with pytest.raises(UpdateError):
        _inventory(raw, len(entries), 0)


@pytest.mark.parametrize('raw', [b'{"format_version":1,"entries":[],"entries":[]}',
                               b'{"format_version":1,"entries":NaN}', b'\xff'])
def test_inventory_strict_json_is_checked_after_exact_hash(raw):
    with pytest.raises(UpdateError, match='update_inventory'):
        _inventory(raw, 0, 0)


def test_inventory_exact_byte_entry_path_and_unpacked_limits():
    empty = encode(dict(format_version=1, entries=[]))
    exact = empty + b' ' * (4194304 - len(empty))
    assert _inventory(exact, 0, 0).entries == ()
    with pytest.raises(UpdateError, match='update_limit'):
        _inventory(exact + b' ', 0, 0)
    entry = dict(path='files/00000.bin', size=0, sha256=hashlib.sha256(b'').hexdigest())
    entries = [entry | dict(path=f'files/{index:05}.bin') for index in range(20000)]
    assert len(_inventory(encode(dict(format_version=1, entries=entries)), 20000, 0).entries) == 20000
    with pytest.raises(UpdateError, match='update_limit'):
        _inventory(encode(dict(format_version=1, entries=entries + [entry])), 20001, 0)
    path = '/'.join(('a' * 200, 'b' * 200, 'c' * 110))
    assert len(path) == 512
    assert _inventory(encode(dict(format_version=1, entries=[entry | dict(path=path)])), 1, 0).entries[0].path == path
    with pytest.raises(UpdateError):
        _inventory(encode(dict(format_version=1, entries=[entry | dict(path=path + 'x')])), 1, 0)
    large = entry | dict(size=34359738368)
    assert _inventory(encode(dict(format_version=1, entries=[large])), 1, large['size']).unpacked_bytes == large['size']
    with pytest.raises(UpdateError, match='update_limit'):
        _inventory(encode(dict(format_version=1, entries=[large | dict(size=large['size'] + 1)])), 1, large['size'] + 1)


@pytest.mark.parametrize('fault', ['local-crc', 'local-name', 'overlap', 'duplicate-offset', 'truncated', 'wrong-crc', 'wrong-sha'])
def test_archive_header_span_crc_and_sha_disagreements_are_rejected(release, fault):
    raw = bytearray(release.archive)
    central = struct.unpack_from('<I', raw, len(raw) - 6)[0]
    second = central + 46 + struct.unpack_from('<H', raw, central + 28)[0]
    if fault == 'local-crc': struct.pack_into('<I', raw, 14, 0)
    if fault == 'local-name': raw[30] ^= 1
    if fault in ('overlap', 'duplicate-offset'):
        struct.pack_into('<I', raw, second + 42, 1 if fault == 'overlap' else 0)
    if fault == 'truncated': raw = raw[:-1]
    if fault == 'wrong-crc':
        for offset in (14, central + 16): struct.pack_into('<I', raw, offset, 0)
    if fault == 'wrong-sha':
        changed = dict(release.files)
        changed['service/ssearch-service.exe'] = b'x' * len(changed['service/ssearch-service.exe'])
        release.rebuild(zip_entries=list(changed.items()))
        raw = bytearray(release.archive)
    _sign_archive(release, raw)
    with pytest.raises(UpdateError):
        stage(release)
    assert not release.staging.exists() or not list(release.staging.iterdir())


def test_source_and_foreign_staging_roots_are_never_adopted(release):
    release.staging = release.source
    before = release.source_bytes()
    with pytest.raises(UpdateError, match='update_path'):
        stage(release)
    assert release.source_bytes() == before


def test_staging_junction_is_rejected_without_touching_its_target(release):
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(release.staging), str(release.source)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, 'required disposable junction could not be created'
    before = release.source_bytes()
    try:
        with pytest.raises(UpdateError, match='update_path'):
            stage(release)
        assert release.source_bytes() == before
    finally:
        os.rmdir(release.staging)


def test_reservation_counts_only_new_peak_and_explicit_headroom(release, monkeypatch):
    old = release.data / 'old-index.bin'
    old.write_bytes(b'x' * (1024 * 1024))
    reserved = []
    original = release.budget.reserve
    def observed(extra, operation):
        reserved.append(extra)
        return original(extra, operation)
    monkeypatch.setattr(release.budget, 'reserve', observed)
    result = stage(release, headroom_bytes=12345)
    assert len(reserved) == 1 and reserved[0] >= 12345
    assert reserved[0] < old.stat().st_size  # Existing physical bytes were not reserved again.
    assert old.read_bytes() == b'x' * (1024 * 1024)
    assert len(result.files) == 2


def test_supported_stored_zip_and_parent_directories_stage_exact_files(release):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name in ('desktop/', 'service/'):
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = ((stat.S_IFDIR | 0o700) << 16) | 0x10
            archive.writestr(entry, b'')
        for name, raw in release.files.items():
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, raw)
    _sign_archive(release, output.getvalue())
    result = stage(release)
    assert {path.relative_to(result.directory).as_posix(): path.read_bytes() for path in result.files} == release.files


def test_zero_byte_files_have_exact_stream_end_and_hash(release):
    release.files = {name: b'' for name in release.files}
    release.rebuild()
    result = stage(release)
    assert len(result.files) == 2 and all(path.read_bytes() == b'' for path in result.files)


def _schedule_initial_root_swap(monkeypatch, requested_root, ancestor, foreign, retired):
    """Schedule real filesystem replacement immediately before the native open."""
    boundary = api('transports.unc')
    native_call = boundary._call
    state = dict(attempted=False, swapped=False, blocked=False)

    def schedule(dll, name, arguments, *rest, **kwargs):
        native = native_call(dll, name, arguments, *rest, **kwargs)
        if name != 'CreateFileW':
            return native
        def opened(*values):
            text = str(values[0])
            if text.startswith('\\\\?\\'):
                text = text[4:]
            if state['attempted'] or Path(text) != requested_root:
                return native(*values)
            state['attempted'] = True
            try:
                ancestor.rename(retired)
            except OSError:
                state['blocked'] = True
                return native(*values)
            linked = False
            try:
                result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(ancestor), str(foreign)],
                                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                assert result.returncode == 0, 'required race junction could not be created'
                linked = True
                state['swapped'] = True
                return native(*values)
            finally:
                if linked:
                    os.rmdir(ancestor)
                retired.rename(ancestor)
        return opened
    monkeypatch.setattr(boundary, '_call', schedule)
    return state


def test_initial_destination_root_swap_never_writes_outside_budget(release, monkeypatch):
    foreign = _windows.private_directory(release.base / 'foreign-private')
    foreign_data = _windows.private_directory(foreign / 'data')
    marker = foreign_data / 'keep.txt'
    marker.write_bytes(b'foreign destination')
    state = _schedule_initial_root_swap(monkeypatch, release.data, release.owned,
                                       foreign, release.base / 'retired-private')
    try:
        result = stage(release)
    except UpdateError as error:
        assert error.code == 'update_path'
    else:
        assert state['blocked'] and result.directory.is_relative_to(release.data)
    assert state['attempted']
    assert list(foreign_data.iterdir()) == [marker]
    assert marker.read_bytes() == b'foreign destination'


@pytest.mark.parametrize('mode', ['failure', 'cancel'])
def test_cleanup_never_deletes_foreign_private_replacement(release, monkeypatch, mode):
    original_assert = _windows.assert_private
    original_api = _windows._api
    state = dict(armed=False, attempted=False, blocked=False, replaced=False, path=None)
    marker = b'foreign replacement must survive cleanup'

    def replace_after_real_acl(path):
        path = Path(path)
        if state['armed'] and not state['attempted'] and path.suffix == '.inventory':
            state['attempted'], state['path'] = True, path
            try:
                path.rename(release.data / 'retired-own-inventory')
            except OSError:
                state['blocked'] = True
                return
            path.write_bytes(marker)
            state['replaced'] = True
    def schedule(path):
        original_assert(path)  # A real ACL check; its result is never fabricated.
        replace_after_real_acl(path)
    def schedule_handle(dll, name, arguments, *rest, **kwargs):
        native = original_api(dll, name, arguments, *rest, **kwargs)
        if name != 'GetSecurityInfo':
            return native
        def checked(*values):
            result = native(*values)
            if result == 0 and values[1] == 1 and state['armed']:
                replace_after_real_acl(api('transports.unc')._final_path(values[0]))
            return result
        return checked
    monkeypatch.setattr(_windows, 'assert_private', schedule)
    monkeypatch.setattr(_windows, '_api', schedule_handle)
    transport = release.transport()
    original_open = transport.open_artifact
    def interrupted(*args):
        state['armed'] = True
        if mode == 'failure':
            raise OSError('fictional source disconnect')
        return original_open(*args)
    transport.open_artifact = interrupted
    with pytest.raises(UpdateError):
        stage(release, transport, cancelled=(lambda: state['armed']) if mode == 'cancel' else None)
    assert state['attempted'], 'cleanup never reached the scheduled ownership boundary'
    if state['replaced']:
        assert state['path'].is_file() and state['path'].read_bytes() == marker
    else:
        assert state['blocked']
