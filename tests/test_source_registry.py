"""Caller UUID scopes and lexical root relationships, without network probes."""
from copy import deepcopy
import importlib
import uuid

import pytest

from atom_search_core.models import SearchScope
from atom_search.settings.repository import SettingsRepository
from atom_search.service.contracts import ServiceError

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
C = '33333333-3333-4333-8333-333333333333'


class MemoryIdentities:
    """In-memory storage port for registry unit/desktop tests only."""
    def __init__(self):
        self.sources = {}

    def bind_many(self, sources):
        from atom_search_core.source import canonical_root
        candidate = dict(self.sources)
        for identity, root in sources:
            root = canonical_root(root)
            if any((key == identity) != (path == root) for key, (_, path) in candidate.items()):
                raise ValueError('source_binding_conflict')
            candidate.setdefault(identity, (len(candidate) + 41, root))
        self.sources = candidate
        return tuple(candidate[identity][0] for identity, _ in sources)

    def source_uuid(self, root):
        from atom_search_core.source import canonical_root
        return next((identity for identity, (_, path) in self.sources.items()
                     if path == canonical_root(root)), None)

    def resolve_sources(self, identities):
        return SearchScope(tuple(self.sources[identity][0] for identity in identities
                                 if identity in self.sources))


def registry(tmp_path):
    module = importlib.import_module('atom_search.service.sources')
    repo = SettingsRepository(tmp_path)
    snapshot = repo.load()
    settings = snapshot.settings
    settings['sources'] = [dict(id=A, path='C:/docs', label='A', enabled=True),
                           dict(id=B, path='D:/docs', label='B', enabled=True),
                           dict(id=C, path='E:/docs', label='C', enabled=False)]
    repo.commit(settings, snapshot.revision)
    identities = MemoryIdentities()
    result = module.SourceRegistry(repo, identities)
    result.activate()
    return repo, identities, result


def test_none_empty_unknown_and_forbidden_scope_are_distinct(tmp_path):
    repo, identities, subject = registry(tmp_path)
    assert subject.resolve(None, allowed=(B,)) == SearchScope((42,))
    assert subject.resolve((), allowed=(A, B)) == SearchScope(())
    assert subject.resolve(None, allowed=(A, B, C)) == SearchScope((41, 42))
    for requested in ((A,), (C,), (str(uuid.uuid4()),), ('41',)):
        with pytest.raises(ServiceError) as failure:
            subject.resolve(requested, allowed=(B,))
        assert failure.value.code == 'source_scope_denied'
        assert failure.value.status_code == 403 and failure.value.details == {}


def test_duplicate_canonical_roots_rejected_and_nested_roots_warn(tmp_path, monkeypatch):
    module = importlib.import_module('atom_search.service.sources')
    from atom_search.settings.migration import default_settings
    from atom_search.settings.schema import validate_settings
    settings = default_settings()
    settings['sources'] = [dict(id=A, path=r'\\server\share\a', label='', enabled=True),
                           dict(id=B, path=r'//SERVER/share/a/child', label='', enabled=True),
                           dict(id=C, path=r'\\server\share\ab', label='', enabled=True)]
    monkeypatch.setattr('os.stat', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network probe')))
    before = deepcopy(settings)
    overlaps = module.classify_source_roots(tuple(settings['sources']))
    assert len(overlaps) == 1
    assert overlaps[0].path == 'sources.1.path'
    assert settings == before
    settings['sources'][1]['path'] = r'//SERVER/share/A/'
    with pytest.raises(ServiceError) as failure:
        validate_settings(settings)
    assert any(error.code == 'duplicate_root' for error in failure.value.errors)


def test_root_revisit_reuses_uuid_and_conflicting_owner_uuid_is_not_rewritten(tmp_path):
    repo, identities, subject = registry(tmp_path)
    assert subject.select_source_id('c:\\DOCS\\') == A
    fresh = subject.select_source_id('F:/new')
    assert str(uuid.UUID(fresh)) == fresh and fresh not in {A, B, C}
    candidate = repo.snapshot().settings
    candidate['sources'][0]['path'] = 'F:/new'
    from atom_search.service.mutations import MutationCoordinator
    with pytest.raises(ServiceError) as failure:
        MutationCoordinator(repo, apply=subject.apply).owner_apply(candidate, repo.snapshot().revision)
    assert failure.value.code == 'settings_apply_failed'
    assert repo.snapshot().settings['sources'][0]['id'] == A
    assert subject.resolve((A,), allowed=(A,)) == SearchScope((41,))
