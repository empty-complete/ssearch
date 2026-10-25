"""Fictional, owned service profiles; no ambient server or working documents."""
import importlib
import importlib.util
from pathlib import Path
import time
import uuid

import pytest


@pytest.fixture(scope='session')
def owner_profile(tmp_path_factory):
    name = 'atom_search.service.owner'
    assert importlib.util.find_spec(name) is not None, 'shared ServiceOwner is required'
    module = importlib.import_module(name)
    base = tmp_path_factory.mktemp('service-owner')
    owner = module.open_service_owner(base / 'profile', base / 'missing-model', automatic_catalog=False)
    snapshot = owner.repository.snapshot()
    sources = []
    for label in ('allowed', 'private'):
        directory = base / label
        directory.mkdir()
        (directory / f'{label}-drawing.pdf').write_text('CONTENT MUST NEVER BE FETCHED')
        sources.append(dict(id=str(uuid.uuid4()), path=str(directory), label=label, enabled=True))
    candidate = snapshot.settings
    candidate['sources'] = sources
    owner.coordinator.owner_apply(candidate, snapshot.revision)
    try:
        owner.start()
        for source in sources:
            owner.core.scan(source['path'])
        # Direct fixture seeding bypasses scheduler completion notifications.
        # Wait for a real background SQL observation before exposing the cached
        # status contract; status itself must remain a memory-only read.
        owner.probes.request('database')
        expected_counts = {source['id']: 1 for source in sources}
        deadline = time.monotonic() + 10
        observed_counts = {}
        observed_sources = ()
        while time.monotonic() < deadline:
            observed_sources = owner.facade(owner.owner_access).status().components.sources
            observed_counts = {source.source_id: source.catalogued_files
                               for source in observed_sources}
            if observed_counts == expected_counts and all(
                    source.catalog_counts_checked_at is not None for source in observed_sources):
                break
            time.sleep(.02)
        assert observed_counts == expected_counts
        assert all(source.catalog_counts_checked_at is not None for source in observed_sources)
        yield owner
    finally:
        owner.close()


@pytest.fixture
def owner_client(owner_profile):
    from atom_search.service.client import ServiceClient
    client = ServiceClient(owner_profile.endpoint_file, owner_profile.credential_ref)
    yield client
    client.close()


@pytest.fixture
def catalog_wave(owner_profile, monkeypatch):
    # Test-created waves may include neighboring sources. Finish the original
    # assertions first, then cancel/drain their actual workers and final writes
    # before monkeypatch teardown or another test uses the shared owner.
    yield
    try:
        assert owner_profile.scheduler.quiesce(timeout=90), 'test catalog wave did not drain'
    finally:
        owner_profile.scheduler.resume()


@pytest.fixture
def read_client(owner_profile):
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import AccessContext
    source = owner_profile.repository.snapshot().settings['sources'][0]['id']
    reference = owner_profile.issue_credential(AccessContext('reader', frozenset({'search', 'fetch', 'status', 'settings_get'}), (source,)))
    client = ServiceClient(owner_profile.endpoint_file, reference)
    yield client
    client.close()
