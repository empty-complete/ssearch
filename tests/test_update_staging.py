"""Bounded staging admits signed bytes only into one budget-owned directory."""
import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

from atom_search.runtime import _windows
from atom_search.runtime.storage import StorageBudget
from atom_search.updates.contracts import UpdateError
from test_update_manifest import key

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
