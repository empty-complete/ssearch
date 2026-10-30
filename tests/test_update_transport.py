"""Owner-explicit transport and signature-before-artifact admission."""
from pathlib import Path

import pytest

from atom_search.updates.contracts import UpdateError, UpdateVersions
from test_update_manifest import NOW, encode, key, versions
from test_update_staging import api, release


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
