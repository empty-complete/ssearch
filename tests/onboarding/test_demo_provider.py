"""Offline demo uses real metadata and the public service DTOs."""
import builtins
import importlib
import importlib.util
import json
from pathlib import Path
import socket
import uuid

import pytest

from atom_search.service.contracts import ResultMetadata, SearchRequest, SearchResponse, ServiceError


MANIFEST = Path(__file__).resolve().parents[2] / 'examples/onboarding-demo/manifest.json'
MANIFEST_DATA = json.loads(MANIFEST.read_text(encoding='utf-8'))
SOURCE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'urn:atom:onboarding-demo:v2:source'))


def api():
    assert importlib.util.find_spec('atom_search.onboarding') is not None, 'offline onboarding provider is required'
    return importlib.import_module('atom_search.onboarding')


def assert_error(code, operation):
    with pytest.raises(ServiceError) as failure:
        operation()
    assert failure.value.code == code


@pytest.fixture
def session(tmp_path):
    module = api()
    opened = module.open_demo_session(MANIFEST, tmp_path / 'demo-cache', ())
    try:
        yield opened
    finally:
        module.close_demo_session(opened.session_id)


def test_demo_search_before_pg_model_network_uses_manifest_files(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(('psycopg', 'onnxruntime', 'PySide6', 'atom_search.service.owner',
                                    'atom_search_core.storage', 'atom_search_core.embeddings'))
        return original_import(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        pytest.fail('offline demo attempted network access')

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(socket, 'socket', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    module = api()
    opened = module.open_demo_session(MANIFEST, tmp_path / 'demo-cache', ())
    try:
        assert opened.version == 2
        assert opened.capabilities == ()
        for lesson in MANIFEST_DATA['lessons']:
            if lesson.get('required_capabilities'):
                continue
            response = opened.provider.search(SearchRequest(lesson['query'], mode='lexical'))
            assert isinstance(response, SearchResponse)
            expected = set(lesson['expected_files'])
            assert {row.relative_path for row in response.results[:len(expected)]} == expected
            assert all(Path(row.full_path).is_file() for row in response.results)
            assert all(row.match_kind in {'literal', 'lexical'} for row in response.results)
            assert all(isinstance(row.score, float) for row in response.results)
            if 'selected_file' in lesson:
                assert lesson['selected_file'] in {row.relative_path for row in response.results}
    finally:
        module.close_demo_session(opened.session_id)


def test_demo_scope_and_ids_are_public_dto(session):
    response = session.provider.search(SearchRequest('Пермские моторы'))
    assert response.results
    for row in response.results:
        assert isinstance(row, ResultMetadata)
        assert row.source_id == SOURCE_ID
        assert str(uuid.UUID(row.result_id)) == row.result_id
        expected = uuid.uuid5(uuid.NAMESPACE_URL, 'urn:atom:onboarding-demo:v2:file:' + row.relative_path)
        assert row.result_id == str(expected)
        assert session.provider.fetch(row.result_id) == row
    assert session.provider.search(SearchRequest('Пермские моторы', source_ids=())).results == ()
    row = response.results[0]
    assert_error('result_not_found', lambda: session.provider.fetch(row.result_id, source_ids=()))
    assert_error('result_not_found', lambda: session.provider.fetch(str(uuid.uuid4())))
    forbidden = (str(uuid.uuid4()),)
    assert_error('source_scope_denied', lambda: session.provider.search(SearchRequest('ПМ', source_ids=forbidden)))
    assert_error('source_scope_denied', lambda: session.provider.fetch(row.result_id, source_ids=forbidden))


def test_demo_project_pipeline_includes_short_branch_and_explains_ambiguity(tmp_path):
    module = api()
    opened = module.open_demo_session(MANIFEST, tmp_path / 'demo-cache', ('project_search', 'project_aliases', 'unknown'))
    try:
        assert set(opened.capabilities) == {'project_search', 'project_aliases'}
        project = str(uuid.uuid5(uuid.NAMESPACE_URL, 'urn:atom:onboarding-demo:v2:project:perm-motors'))
        for lesson_id in ('future_full_project_to_short_branch', 'future_scoped_project_alias'):
            lesson = next(item for item in MANIFEST_DATA['lessons'] if item['id'] == lesson_id)
            selected = project if lesson_id == 'future_scoped_project_alias' else None
            response = opened.provider.search(SearchRequest(lesson['query'], project_id=selected))
            assert {row.relative_path for row in response.results[:len(lesson['expected_files'])]} == set(lesson['expected_files'])
            assert all(row.project_id == project for row in response.results[:len(lesson['expected_files'])])
            assert response.query_interpretation.original_query == lesson['query']
            if selected:
                assert response.applied_aliases
                assert all(str(uuid.UUID(item.alias_id)) == item.alias_id for item in response.applied_aliases)
        ambiguous = opened.provider.search(SearchRequest('ПМ'))
        assert ambiguous.ambiguities
        assert len(ambiguous.ambiguities[0].options) == 2
        assert ambiguous.query_interpretation.project_id is None
        assert not ambiguous.applied_aliases
        assert {item.canonical_name for item in ambiguous.ambiguities[0].options} == {'Пермские моторы', 'Полярный музей'}
        assert_error('project_scope_denied', lambda: opened.provider.search(SearchRequest('ПМ', project_id=str(uuid.uuid4()))))
    finally:
        module.close_demo_session(opened.session_id)


def test_replay_stable_ids_and_no_work_state_changes(tmp_path, monkeypatch):
    work = tmp_path / 'work-profile'
    work.mkdir()
    files = {'settings.json': b'{"sources":[],"revision":7}', 'catalog': b'working metadata', 'stats': b'files=42;ready=false'}
    for name, content in files.items():
        (work / name).write_bytes(content)
    module = api()
    seen = []
    for _ in range(2):
        opened = module.open_demo_session(MANIFEST, tmp_path / 'demo-cache', ())
        try:
            seen.append(tuple(row.result_id for row in opened.provider.search(SearchRequest('спецификация')).results))
            assert all((work / name).read_bytes() == content for name, content in files.items())
        finally:
            module.close_demo_session(opened.session_id)
    assert seen[0] == seen[1]
    assert all((work / name).read_bytes() == content for name, content in files.items())


@pytest.mark.parametrize('request', [SearchRequest(''), SearchRequest('a', limit=0), SearchRequest('a', mode='unknown')])
def test_invalid_requests_fail_explicitly(session, request):
    assert_error('invalid_search_request', lambda: session.provider.search(request))


def test_memory_ports_reject_writes_and_have_no_vector_channel():
    api()
    from atom_search.onboarding.demo_memory import ReadOnlyMemoryCatalog, ReadOnlyMemoryRetrieval
    from atom_search_core.models import DirectorySnapshot, SearchScope

    catalog = ReadOnlyMemoryCatalog(())
    retrieval = ReadOnlyMemoryRetrieval(catalog)
    assert catalog.get_file(1, scope=SearchScope(())) is None
    assert retrieval.vector_candidates((), model_key='absent', scope=SearchScope((1,)), limit=10) == []
    for operation in (lambda: catalog.begin_root('work'),
                      lambda: catalog.finish_root(1, 'complete'),
                      lambda: catalog.reconcile(1, DirectorySnapshot('')),
                      lambda: catalog.save_embeddings((), 'absent')):
        with pytest.raises(RuntimeError, match='read_only_demo'):
            operation()
