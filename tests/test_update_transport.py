"""Owner-explicit transport and signature-before-artifact admission."""
from pathlib import Path
import os
import subprocess
import sys
import json
from datetime import datetime, timedelta, timezone

import pytest

from atom_search.updates.contracts import UpdateError, UpdateVersions
from test_update_manifest import NOW, encode, key, versions
from test_update_staging import api, release
from test_update_staging import _schedule_initial_root_swap
import shutil


def test_local_source_requires_explicit_fixture_mode(release):
    with pytest.raises(UpdateError, match='update_source'):
        api('transports.unc').UNCTransport(release.source)


def test_bad_signature_stops_before_inventory_or_artifact(release):
    transport = release.transport()
    (release.source / 'update-manifest.json').write_bytes(
        release.key.envelope(encode(release.document), signature=b'\0' * 64))
    events = []
    transport.read_inventory = lambda *_args, **_kwargs: events.append('inventory')
    transport.open_artifact = lambda *_args: events.append('artifact')
    with pytest.raises(UpdateError, match='update_signature'):
        api('transports.base').read_verified_manifest(
            transport, release.checker, channel='stable',
            installed=UpdateVersions(**versions()), now=NOW)
    assert events == []


def test_unc_fixture_manifest_and_inventory_are_bounded(release):
    transport = release.transport()
    manifest_raw = (release.source / 'update-manifest.json').read_bytes()
    assert transport.read_manifest(max_bytes=len(manifest_raw)) == manifest_raw
    with pytest.raises(UpdateError, match='update_limit'):
        transport.read_manifest(max_bytes=len(manifest_raw) - 1)
    manifest = release.verified()
    assert transport.read_inventory(manifest, 'application', max_bytes=len(release.inventory)) == release.inventory
    with pytest.raises(UpdateError, match='update_limit'):
        transport.read_inventory(manifest, 'application', max_bytes=len(release.inventory) - 1)


def test_artifact_handle_rejects_unsigned_input_and_unknown_id(release):
    transport = release.transport()
    with pytest.raises(UpdateError):
        transport.open_artifact(release.document, 'application')
    with pytest.raises(UpdateError):
        transport.open_artifact(release.verified(), '../escape')


def test_open_handle_denies_file_or_ancestor_replacement_and_writes(release):
    transport = release.transport()
    manifest = release.verified()
    archive = release.source / manifest.artifacts[0].relative_path
    with transport.open_artifact(manifest, 'application') as stream:
        with pytest.raises(OSError):
            archive.rename(archive.with_name('replaced.zip'))
        with pytest.raises(OSError):
            archive.parent.rename(archive.parent.with_name('moved-parent'))
        with pytest.raises(OSError):
            with archive.open('wb') as writer:
                writer.write(b'replacement')
        assert stream.read() == release.archive


def test_source_hardlink_is_denied_without_changing_any_alias(release):
    manifest = release.verified()
    archive = release.source / manifest.artifacts[0].relative_path
    other = release.base / 'foreign-hardlink.bin'
    os.link(archive, other)
    with pytest.raises(UpdateError, match='update_path'):
        release.transport().open_artifact(manifest, 'application')
    assert archive.read_bytes() == other.read_bytes() == release.archive


def test_source_junction_is_denied_before_inventory_read(release):
    original = release.source / 'payload'
    foreign = release.base / 'foreign-payload'
    original.rename(foreign)
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(original), str(foreign)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, 'required disposable junction could not be created'
    try:
        with pytest.raises(UpdateError, match='update_path'):
            release.transport().read_inventory(release.verified(), 'application', max_bytes=len(release.inventory))
        assert (foreign / 'application.zip').read_bytes() == release.archive
    finally:
        os.rmdir(original)


def _cli(release, profile):
    installed = release.base / 'fixture-installed.json'
    installed.write_bytes(encode(versions()))
    arguments = [sys.executable, str(Path(__file__).resolve().parents[1] / 'tools/check_updates.py'),
        '--transport', 'fixture', '--release-root', str(release.source), '--qa-profile', str(profile),
        '--staging-dir', str(profile / 'engine/staging'), '--trust-store', str(release.base / 'fixture-trust.json'),
        '--installed-versions', str(installed), '--channel', 'stable', '--quota-bytes', str(16 * 1024 * 1024),
        '--report', str(profile / 'receipt.json')]
    return subprocess.run(arguments, capture_output=True, env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
                          timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)


def test_cli_fixture_proof_explicitly_remains_source_only(release):
    now = datetime.now(timezone.utc)
    release.document.update(issued_at=(now - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                            expires_at=(now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
    release.write()
    profile = release.base / 'isolated-qa'
    result = _cli(release, profile)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    receipt = json.loads((profile / 'receipt.json').read_bytes())
    assert receipt['ok'] and receipt['mode'] == 'source' and receipt['staged_files'] == 2
    assert not receipt['activation'] and not receipt['frozen_proof'] and not receipt['actual_unc_qualified']


def test_cli_refuses_non_qa_profile_without_writing_a_receipt(release):
    from atom_search.runtime import _windows
    profile = _windows.private_directory(release.base / 'foreign-profile')
    marker = profile / 'settings.json'
    marker.write_bytes(b'foreign settings')
    result = _cli(release, profile)
    assert result.returncode == 1
    assert list(profile.iterdir()) == [marker] and marker.read_bytes() == b'foreign settings'


def test_initial_source_root_swap_never_adopts_foreign_directory(release, monkeypatch):
    route = release.base / 'source-route'
    route.mkdir()
    requested = route / 'release'
    release.source.rename(requested)
    release.source = requested
    foreign = release.base / 'foreign-source-route'
    shutil.copytree(route, foreign)
    state = _schedule_initial_root_swap(monkeypatch, requested, route, foreign,
                                       release.base / 'retired-source-route')
    try:
        with release.transport().open_artifact(release.verified(), 'application') as stream:
            raw = stream.read()
    except UpdateError as error:
        assert error.code == 'update_path'
    else:
        assert state['blocked'] and raw == release.archive
    assert state['attempted']
