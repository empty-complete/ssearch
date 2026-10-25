"""Public project consumer/ranking contracts; actual SQL isolation is native PG."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from atom_search.service.application import AtomSearchService
from atom_search.service.contracts import AccessContext, SearchRequest, SearchResponse, ServiceError, SettingsSnapshot
from atom_search.service.sources import SourceRegistry
from atom_search_core.models import (CatalogFile, LexicalHit, ProjectAlias, ProjectBinding,
    ProjectDefinition, ProjectFilter, SearchScope, VectorHit)
from atom_search_core.project_context import INPUT_SCHEMA
from atom_search_core.service import SearchService

SOURCE = '00000000-0000-4000-8000-000000000041'
PROJECT = '00000000-0000-4000-8000-000000000001'
ALIAS = '00000000-0000-4000-8000-000000000011'


@pytest.fixture
def consumer():
    project = ProjectDefinition(PROJECT, 'Пермские моторы', (1,), (ProjectBinding(PROJECT, 1, '', True),))
    alias = ProjectAlias(ALIAS, PROJECT, (1,), ('ПМ',), True, 'QA confirmed')
    files = tuple(CatalogFile(i, 1, 'C:/cached', name, name, 0, 10, 'unavailable', 1)
                  for i, name in enumerate(('Спецификация.pdf', 'Расчёт.pdf', 'Пермские моторы.txt'), 1))
    class Retrieval:
        def project_candidates(self, resolution, *, scope, context_revision, limit, mode='lexical'):
            if not scope.source_ids:
                return []
            return [LexicalHit(files[0], 10, PROJECT, ('name', 'project', 'alias'))]
        def lexical_candidates(self, query, *, scope, limit, project_filter=None):
            return [LexicalHit(files[2], 999, None, ('name',))] if scope.source_ids else []
        literal_candidates = lexical_candidates
        def vector_candidates(self, vector, *, model_key, scope, limit, project_filter=None):
            self.seen = project_filter
            return [VectorHit(files[1], .99, PROJECT)]
    class Embedder:
        model_id, dimension = 'fixture', 384
        def encode(self, texts, *, kind):
            return [(1.,) + (0.,)*383 for _ in texts]
    core = SearchService(catalog=SimpleNamespace(), retrieval=Retrieval(), embedder=Embedder(),
        projection=SimpleNamespace(configuration=lambda: (7, ((project,), (alias,), {1: 'Архив'}))),
        input_schema=INPUT_SCHEMA)
    public = SimpleNamespace(resolve_sources=lambda identities: SearchScope((1,)) if identities else SearchScope(()),
        source_uuid=lambda root: SOURCE, public_file_id=lambda identity: f'00000000-0000-4000-8000-{identity:012d}')
    core.public_ids = public
    repository = SimpleNamespace(snapshot=lambda: SettingsSnapshot(2, 1, {
        'sources': [dict(id=SOURCE, path='C:/cached', label='Архив', enabled=True)]}, ()))
    owner = SimpleNamespace(core=core, repository=repository, registry=SourceRegistry(repository, public),
                            operation=nullcontext, index_generation=lambda: 19)
    return AtomSearchService(owner, AccessContext('reader', frozenset({'search'}), (SOURCE,)))


def test_auto_preferred_pool_precedes_original_literal_fallback_without_selecting_project(consumer):
    from atom_search.http.serialization import decode, encode, restore
    query = '  Пермские моторы спецификация  '
    response = consumer.search(SearchRequest(query, mode='lexical'))
    assert [row.name for row in response.results] == ['Спецификация.pdf', 'Пермские моторы.txt']
    assert response.query_interpretation.original_query == query
    assert response.query_interpretation.project_id is None
    assert response.query_interpretation.expanded_terms == ('Пермские моторы',)
    assert response.catalog_generation == 19
    assert restore(SearchResponse, decode(encode(response))) == response


def test_explicit_selection_scopes_every_channel_and_semantics_does_not_invent_field_evidence(consumer):
    response = consumer.search(SearchRequest('ПМ спецификация', project_id=PROJECT))
    assert [row.name for row in response.results] == ['Спецификация.pdf', 'Расчёт.pdf']
    assert {row.project_id for row in response.results} == {PROJECT}
    assert response.results[0].match_kind == 'lexical'
    assert response.results[1].match_kind == 'semantic' and response.results[1].matched_fields == ()
    assert response.applied_aliases[0].source_id == SOURCE
    assert consumer.owner.core.retrieval.seen == ProjectFilter((PROJECT,), 7, INPUT_SCHEMA)


def test_model_busy_returns_project_lexical_without_wait_or_inference(consumer):
    core = consumer.owner.core
    core._inference_lock.acquire()
    try:
        response = consumer.search(SearchRequest('ПМ спецификация', project_id=PROJECT))
        assert [row.name for row in response.results] == ['Спецификация.pdf']
        assert response.results[0].match_kind == 'lexical'
    finally:
        core._inference_lock.release()


def test_incompatible_project_vector_port_remains_unavailable_before_owner_operation(consumer):
    core = consumer.owner.core
    core.retrieval.vector_candidates = lambda vector, *, model_key, scope, limit: []
    def forbidden():
        pytest.fail('unsupported project search touched owner admission')
    consumer.owner.operation = forbidden
    with pytest.raises(ServiceError) as failure:
        consumer.search(SearchRequest('ПМ', project_id=PROJECT))
    assert failure.value.code == 'capability_unavailable'


def test_concurrent_context_change_refuses_old_alias_explanation(consumer):
    configuration = consumer.owner.core.projection.configuration
    reads = 0
    def changed():
        nonlocal reads
        reads += 1
        revision, value = configuration()
        return (revision if reads == 1 else revision + 1), value
    consumer.owner.core.projection.configuration = changed
    with pytest.raises(ServiceError) as failure:
        consumer.search(SearchRequest('ПМ спецификация', project_id=PROJECT, mode='lexical'))
    assert (failure.value.code, failure.value.status_code, failure.value.retryable) == ('service_unavailable', 503, True)
