"""Real PostgreSQL context migration, publication and prepared-input identity."""
from dataclasses import replace
from contextlib import nullcontext
import hashlib
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from atom_search_core.models import (DirectorySnapshot, EmbeddingModel, EmbeddingWrite,
    FileMetadata, PreparedEmbeddingInput, ProjectAlias, ProjectBinding, ProjectDefinition, SearchScope)
from atom_search_core.project_context import (INPUT_SCHEMA, build_document_context,
                                             metadata_embedding_text)
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.postgres_retrieval import PostgresRetrieval

KEY = 'context-fixture|' + INPUT_SCHEMA
MODEL = EmbeddingModel(KEY, 384, INPUT_SCHEMA, 'a' * 64)
PROJECT = '00000000-0000-4000-8000-000000000001'
VECTOR = (1.,) + (0.,) * 383
SUPPLY = '00000000-0000-4000-8000-000000000003'
OTHER = '00000000-0000-4000-8000-000000000004'
PUBLIC = tuple(f'00000000-0000-4000-8000-{i:012d}' for i in (41, 42, 43))


def sql(connections, statement, parameters=()):
    with connections['maintenance']() as connection:
        result = connection.execute(statement, parameters)
        return result.fetchall() if result.description else []


def projection_type():
    name = 'atom_search_core.storage.project_projection'
    assert importlib.util.find_spec(name), 'Versioned PG project projection is required'
    return importlib.import_module(name).ProjectProjection


def definitions(source):
    project = ProjectDefinition(PROJECT, 'Пермские моторы', (source,),
        (ProjectBinding(PROJECT, source, '', True),))
    alias = ProjectAlias('00000000-0000-4000-8000-000000000002', PROJECT,
                         (source,), ('ПМ',), True, 'Owner confirmed')
    return project, alias


def seed(connections, *, count=1):
    apply_catalog_migrations(connections['maintenance'])
    catalog = PostgresCatalog(connections['application'], model=MODEL)
    source = catalog.begin_root('C:/fictional/ПМ')
    catalog.reconcile(source, DirectorySnapshot('', files=tuple(
        FileMetadata(f'Спецификация-{index}.pdf', f'Спецификация-{index}.pdf', 0, 10)
        for index in range(count))))
    return catalog, source


def prepared(context, *, truncate=None):
    raw = metadata_embedding_text(context)
    text = raw if truncate is None else raw[:truncate]
    return PreparedEmbeddingInput(text, hashlib.sha256(text.encode('utf-8')).hexdigest(), 20)


def publish(projection, file, project, aliases, revision, *, truncate=None):
    context = build_document_context(file, source_label='Проекты', projects=(project,),
        aliases=aliases, context_revision=revision)
    assert projection.publish_context(context, expected_file_revision=file.revision,
                                      prepared=prepared(context, truncate=truncate))
    return context


def test_context_schema4_to5_preserves_metadata_ids_and_old_vectors(pg_connections):
    migration = importlib.import_module('atom_search_core.storage.migrations')
    with pg_connections['maintenance']() as connection:
        for version in range(1, 5):
            connection.execute(getattr(migration, f'_SCHEMA_V{version}'))
            if version == 2:
                migration._backfill_v2(connection)
            connection.execute('INSERT INTO atom_search.schema_migrations(version) VALUES (%s)', (version,))
        connection.execute("""INSERT INTO atom_search.sources(id,catalog_id,path,status)
            VALUES (41,1,'C:/offline/project','unavailable')""")
        connection.execute("""INSERT INTO atom_search.files(id,source_id,relative_path,parent,name,size,
            modified_ns,revision,compact_name,compact_stem,compact_path,fts_russian,fts_simple)
            VALUES (71,41,'old.pdf','','old.pdf',80,123,4,'oldpdf','old','oldpdf','','')""")
        connection.execute("""INSERT INTO atom_search.source_public_ids(source_uuid,source_id)
            VALUES ('00000000-0000-4000-8000-000000000041',41)""")
        connection.execute('INSERT INTO atom_search.file_public_ids(file_id) VALUES (71)')
        connection.execute("""INSERT INTO atom_search.embedding_models VALUES
            ('old|filename-parent-v1',384,'filename-parent-v1',%s)""", ('b' * 64,))
        connection.execute("""INSERT INTO atom_search.file_embeddings VALUES
            (71,'old|filename-parent-v1',4,%s,%s::vector)""", ('c' * 64, str(list(VECTOR))))
    before = sql(pg_connections, 'SELECT * FROM atom_search.files')
    identities = sql(pg_connections, 'SELECT * FROM atom_search.file_public_ids')
    assert apply_catalog_migrations(pg_connections['maintenance']) == 6
    assert apply_catalog_migrations(pg_connections['maintenance']) == 6
    assert sql(pg_connections, 'SELECT * FROM atom_search.files') == before
    assert sql(pg_connections, 'SELECT * FROM atom_search.file_public_ids') == identities
    assert sql(pg_connections, 'SELECT model_key,file_revision FROM atom_search.file_embeddings') == [
        ('old|filename-parent-v1', 4)]
    catalog = PostgresCatalog(pg_connections['application'], model=MODEL)
    projection = projection_type()(pg_connections['application'])
    revision = projection.sync((), (), expected_settings_revision=1, source_labels={41: 'Архив'})
    file = projection.pending_context(1000)[0]
    context = build_document_context(file, source_label='Архив', projects=(), aliases=(), context_revision=revision)
    assert projection.publish_context(context, expected_file_revision=4, prepared=prepared(context))
    assert len(catalog.pending_embeddings(KEY, 384, 16)) == 1
    assert sql(pg_connections, 'SELECT count(*) FROM atom_search.embedding_models') == [(2,)]


def test_late_context_or_embedding_cannot_publish_after_alias_revoke(pg_connections):
    Projection = projection_type()
    catalog, source = seed(pg_connections)
    project, alias = definitions(source)
    projection = Projection(pg_connections['application'])
    revision = projection.sync((project,), (alias,), expected_settings_revision=1,
                               source_labels={source: 'Проекты'})
    file = projection.pending_context(1000)[0]
    old = publish(projection, file, project, (alias,), revision)
    pending = catalog.pending_embeddings(KEY, 384, 16)
    assert len(pending) == 1 and pending[0].text == prepared(old).text
    next_revision = projection.sync((project,), (), expected_settings_revision=2,
                                   source_labels={source: 'Проекты'})
    assert next_revision > revision
    assert not projection.publish_context(old, expected_file_revision=file.revision, prepared=prepared(old))
    assert catalog.save_embeddings([EmbeddingWrite(pending[0], VECTOR, pending[0].input_hash)], KEY) == 0
    current = publish(projection, file, project, (), next_revision)
    pending = catalog.pending_embeddings(KEY, 384, 16)
    assert pending[0].text == prepared(current).text and 'aliases: ПМ' not in pending[0].text
    assert catalog.save_embeddings([EmbeddingWrite(pending[0], VECTOR, pending[0].input_hash)], KEY) == 1
    assert len(PostgresRetrieval(pg_connections['application']).vector_candidates(
        VECTOR, model_key=KEY, scope=SearchScope((source,)), limit=10)) == 1
    assert catalog.get_file(file.file_id, scope=SearchScope((source,))) == file


def test_context_revision_reuses_only_equal_prepared_v2_input_and_resumes(pg_connections):
    Projection = projection_type()
    catalog, source = seed(pg_connections)
    project, alias = definitions(source)
    projection = Projection(pg_connections['application'])
    revision = projection.sync((project,), (alias,), expected_settings_revision=1,
                               source_labels={source: 'Проекты'})
    file = projection.pending_context(1000)[0]
    publish(projection, file, project, (alias,), revision, truncate=20)
    pending = catalog.pending_embeddings(KEY, 384, 16)
    assert catalog.save_embeddings([EmbeddingWrite(pending[0], VECTOR, pending[0].input_hash)], KEY) == 1
    revision = projection.sync((project,), (), expected_settings_revision=2,
                               source_labels={source: 'Проекты'})
    assert PostgresRetrieval(pg_connections['application']).vector_candidates(
        VECTOR, model_key=KEY, scope=SearchScope((source,)), limit=10) == []
    projection = Projection(pg_connections['application'])  # Durable state, not Python progress.
    assert projection.sync((project,), (), expected_settings_revision=2,
                           source_labels={source: 'Проекты'}) == revision
    publish(projection, file, project, (), revision, truncate=20)
    assert catalog.pending_embeddings(KEY, 384, 16) == []
    assert len(PostgresRetrieval(pg_connections['application']).vector_candidates(
        VECTOR, model_key=KEY, scope=SearchScope((source,)), limit=10)) == 1
    with pytest.raises(ValueError):
        projection.sync((project,), (alias,), expected_settings_revision=1,
                        source_labels={source: 'Проекты'})


def test_context_pending_is_bounded_and_checkpoint_survives_restart_without_model(pg_connections):
    Projection = projection_type()
    catalog, source = seed(pg_connections, count=1002)
    project, alias = definitions(source)
    projection = Projection(pg_connections['application'])
    revision = projection.sync((project,), (alias,), expected_settings_revision=1,
                               source_labels={source: 'Проекты'})
    with pytest.raises(ValueError):
        projection.pending_context(1001)
    pending = projection.pending_context(1000)
    assert len(pending) == 1000
    for file in pending[:3]:
        context = build_document_context(file, source_label='Проекты', projects=(project,),
            aliases=(alias,), context_revision=revision)
        assert projection.publish_context(context, expected_file_revision=file.revision, prepared=None)
    projection = Projection(pg_connections['application'])
    assert len(projection.pending_context(1000)) == 999
    assert sql(pg_connections, 'SELECT checkpoint_file_id FROM atom_search.project_context_state') == [(pending[2].file_id,)]
    assert sql(pg_connections, "SELECT length(context_text),input_hash,fts_simple IS NOT NULL FROM atom_search.file_project_context ORDER BY file_id LIMIT 1")[0][1:] == (None, True)
    assert catalog.pending_embeddings(KEY, 384, 16) == []
    assert PostgresRetrieval(pg_connections['application']).lexical_candidates(
        'Спецификация', scope=SearchScope((source,)), limit=10)


def test_context_batch_cancel_and_file_cas_preserve_checkpoint(pg_connections):
    catalog, source = seed(pg_connections, count=3)
    projection = projection_type()(pg_connections['application'])
    revision = projection.sync((), (), expected_settings_revision=1)
    files = projection.pending_context(1000)
    records = [(build_document_context(file, source_label='', projects=(), aliases=(),
        context_revision=revision), file.revision, None) for file in files]
    class CancelAfterFirst:
        calls = 0
        def is_set(self):
            self.calls += 1
            return self.calls >= 3
    assert projection.publish_batch(records, cancel=CancelAfterFirst()) == 0
    assert sql(pg_connections, 'SELECT count(*) FROM atom_search.file_project_context') == [(0,)]
    assert sql(pg_connections, 'SELECT checkpoint_file_id FROM atom_search.project_context_state') == [(0,)]
    catalog.reconcile(source, DirectorySnapshot('', tuple(
        FileMetadata(file.relative_path, file.name, 1, 11) for file in files)))
    assert projection.publish_batch(records) == 0  # All file revisions are obsolete.
    current = projection.pending_context(1000)
    fresh = [(build_document_context(file, source_label='', projects=(), aliases=(),
        context_revision=revision), file.revision, None) for file in current]
    assert projection.publish_batch(fresh, publish_if=lambda: False) == 0
    assert projection.publish_batch(fresh) == 3
    assert projection.pending_context(1000) == []


def test_current_context_health_matches_retrieval_before_and_after_revoke(pg_connections, pg_runtime):
    from types import SimpleNamespace
    import threading
    import time
    from atom_search.service.contracts import SettingsSnapshot
    from atom_search.service.health import HealthCache, database_probe
    from atom_search_core.storage.public_ids import PostgresPublicIds
    catalog, source = seed(pg_connections)
    public = PostgresPublicIds(catalog)
    identity = '00000000-0000-4000-8000-000000000041'
    public.bind_many(((identity, 'C:/fictional/ПМ'),))
    project, alias = definitions(source)
    projection = projection_type()(pg_connections['application'])
    revision = projection.sync((project,), (alias,), expected_settings_revision=1,
                               source_labels={source: 'Проекты'})
    file = projection.pending_context(1000)[0]
    publish(projection, file, project, (alias,), revision)
    pending = catalog.pending_embeddings(KEY, 384, 16)
    catalog.save_embeddings([EmbeddingWrite(pending[0], VECTOR, pending[0].input_hash)], KEY)
    health = HealthCache()
    health.configure(SettingsSnapshot(2, 1, {'sources': [dict(id=identity, path=file.root, enabled=True)]}, ()))
    owner = SimpleNamespace(health=health, runtime=pg_runtime[0],
                            core=SimpleNamespace(catalog=catalog, _embedder=object()))
    def counts():
        return database_probe(owner, time.monotonic() + 3, threading.Event())['counts']['counts'][identity]
    assert counts() == (1, 1)

    revision = projection.sync((project,), (), expected_settings_revision=2,
                               source_labels={source: 'Проекты'})
    assert counts() == (1, 0)
    assert PostgresRetrieval(pg_connections['application']).vector_candidates(
        VECTOR, model_key=KEY, scope=SearchScope((source,)), limit=10) == []
    publish(projection, file, project, (), revision)
    assert counts() == (1, 0)  # Physically retained vector has a different prepared hash.
    pending = catalog.pending_embeddings(KEY, 384, 16)
    catalog.save_embeddings([EmbeddingWrite(pending[0], VECTOR, pending[0].input_hash)], KEY)
    assert counts() == (1, 1)

@pytest.fixture
def project_search(pg_connections):
    """Public facade over real SQL with fictional cached metadata, no source IO."""
    from atom_search.service.application import AtomSearchService
    from atom_search.service.contracts import AccessContext, SettingsSnapshot
    from atom_search.service.sources import SourceRegistry
    from atom_search_core.service import SearchService
    from atom_search_core.storage.public_ids import PostgresPublicIds
    apply_catalog_migrations(pg_connections['maintenance'])
    catalog = PostgresCatalog(pg_connections['application'], model=MODEL)
    roots = ('C:/fictional/shared', 'C:/fictional/direct', 'C:/fictional/private')
    public = PostgresPublicIds(catalog)
    public.bind_many(tuple(zip(PUBLIC, roots)))
    sources = tuple(catalog.resolve_scope(root).source_ids[0] for root in roots)
    paths = ('Чёто до/Зенитные фонари ПМ/хрень.dwg',
             'Чёто до/Зенитные фонари ПМ/Спецификация.pdf',
             'Поставка материалов/Спецификация.pdf',
             'Другой объект/Спецификация.pdf', 'Нераспределённая ведомость.xlsx')
    for source, names in zip(sources, (paths, ('Спецификация прямая.pdf',), ('Спецификация.pdf',))):
        parents = {path.rpartition('/')[0] for path in names}
        for parent in parents:
            catalog.reconcile(source, DirectorySnapshot(parent, tuple(
                FileMetadata(path, path.rsplit('/', 1)[-1], 0, 10) for path in names
                if path.rpartition('/')[0] == parent), complete=False))
        catalog.finish_root(source, 'unavailable')  # Cached/offline documents remain searchable.
    projects = (
        ProjectDefinition(PROJECT, 'Пермские моторы', sources[:2], (
            ProjectBinding(PROJECT, sources[0], 'Чёто до/Зенитные фонари ПМ', True),
            ProjectBinding(PROJECT, sources[1], '', True))),
        ProjectDefinition(SUPPLY, 'Поставка материалов', (sources[0],), (
            ProjectBinding(SUPPLY, sources[0], 'Поставка материалов', True),)),
        ProjectDefinition(OTHER, 'Закрытый объект', (sources[2],), (
            ProjectBinding(OTHER, sources[2], '', True),)))
    aliases = (
        ProjectAlias('00000000-0000-4000-8000-000000000011', PROJECT, sources[:2], ('ПМ',), True, 'QA'),
        ProjectAlias('00000000-0000-4000-8000-000000000012', SUPPLY, (sources[0],), ('ПМ',), True, 'QA'))
    projection = projection_type()(pg_connections['application'])
    revision = projection.sync(projects, aliases, expected_settings_revision=1,
                               source_labels={sources[0]: 'Проекты', sources[1]: 'Прямой корень'})
    for file in projection.pending_context(1000):
        context = build_document_context(file, source_label={sources[0]: 'Проекты', sources[1]: 'Прямой корень'}.get(file.source_id, ''), projects=projects,
            aliases=aliases, context_revision=revision)
        assert projection.publish_context(context, expected_file_revision=file.revision,
                                          prepared=prepared(context))
    core = SearchService(catalog=catalog, retrieval=PostgresRetrieval(pg_connections['application']),
                         projection=projection, input_schema=INPUT_SCHEMA)
    core.public_ids = public
    settings = {'sources': [dict(id=identity, path=root, label='', enabled=True)
                           for identity, root in zip(PUBLIC, roots)]}
    repository = SimpleNamespace(snapshot=lambda: SettingsSnapshot(2, 1, settings, ()))
    owner = SimpleNamespace(core=core, repository=repository, operation=nullcontext,
        registry=SourceRegistry(repository, public), index_generation=lambda: sql(pg_connections,
            "SELECT generation FROM atom_search.catalogs WHERE state='active'")[0][0])
    access = AccessContext('reader', frozenset({'search', 'fetch'}), PUBLIC[:2])
    yield SimpleNamespace(core=core, facade=AtomSearchService(owner, access), projects=projects,
                          aliases=aliases, sources=sources, revision=revision, owner=owner)
    core.close()


def test_full_name_finds_abbreviated_branch_without_project_preselection(project_search):
    from atom_search.service.contracts import SearchRequest
    response = project_search.facade.search(SearchRequest('  Пермские моторы зенитные фонари  ', mode='lexical'))
    assert {row.name for row in response.results} == {'хрень.dwg', 'Спецификация.pdf'}
    assert response.query_interpretation.original_query == '  Пермские моторы зенитные фонари  '
    assert response.query_interpretation.project_id is None
    assert response.query_interpretation.expanded_terms == ('Пермские моторы',)
    assert all(row.project_id == PROJECT and row.stale for row in response.results)
    assert all({'project', 'path'} <= set(row.matched_fields) for row in response.results)


def test_public_ambiguity_then_explicit_project_isolation_and_direct_root(project_search):
    from atom_search.service.contracts import SearchRequest, ServiceError
    facade = project_search.facade
    response = facade.search(SearchRequest('ПМ спецификация', source_ids=(PUBLIC[0],), mode='lexical'))
    assert response.query_interpretation.project_id is None
    assert {option.project_id for option in response.ambiguities[0].options} == {PROJECT, SUPPLY}
    assert all(option.source_ids == (PUBLIC[0],) for option in response.ambiguities[0].options)
    chosen = facade.search(SearchRequest('ПМ спецификация', project_id=PROJECT, mode='lexical'))
    assert chosen.results[0].relative_path == 'Чёто до/Зенитные фонари ПМ/Спецификация.pdf'
    assert {row.source_id for row in chosen.results} == set(PUBLIC[:2])
    assert {row.project_id for row in chosen.results} == {PROJECT}
    assert all('name' in row.matched_fields for row in chosen.results)
    assert chosen.query_interpretation.original_query == 'ПМ спецификация' and not chosen.ambiguities
    assert {row.source_id for row in chosen.applied_aliases} == set(PUBLIC[:2])
    for mode in ('literal', 'lexical', 'hybrid'):
        rows = facade.search(SearchRequest('Спецификация', project_id=PROJECT, mode=mode)).results
        assert rows and {row.project_id for row in rows} == {PROJECT}
        assert not facade.search(SearchRequest('Спецификация', source_ids=(), mode=mode)).results
    with pytest.raises(ServiceError) as failure:
        facade.search(SearchRequest('Спецификация', project_id=OTHER))
    assert failure.value.code == 'project_scope_denied' and failure.value.details == {}
    assert facade.search(SearchRequest('Нераспределённая', mode='lexical')).results[0].project_id is None


def test_alias_revoke_pending_canonical_fallback_and_offline_cache_do_not_probe_documents(project_search, monkeypatch):
    from atom_search.service.contracts import SearchRequest
    subject = project_search
    def forbidden(*args, **kwargs):
        pytest.fail('cached project search attempted a document probe')
    from atom_search_core.source import FileSystemSource
    monkeypatch.setattr(FileSystemSource, 'snapshots', forbidden)
    exists = Path.exists
    def check_exists(path):
        if 'fictional' in str(path).casefold():
            forbidden()
        return exists(path)
    monkeypatch.setattr(Path, 'exists', check_exists)
    projection = subject.core.projection
    revision = projection.sync(subject.projects, (), expected_settings_revision=2,
                               source_labels={subject.sources[0]: 'Архив'})
    # Physically retained FTS rows contain the old alias but are obsolete.
    rows = subject.facade.search(SearchRequest('Пермские моторы зенитные фонари', mode='lexical')).results
    assert {row.name for row in rows} == {'хрень.dwg', 'Спецификация.pdf'}
    assert all('alias' not in row.matched_fields for row in rows)
    assert not subject.facade.search(SearchRequest('ПМ', project_id=SUPPLY, mode='lexical')).results
    assert subject.facade.search(SearchRequest('Архив', mode='lexical')).results
    for file in projection.pending_context(1000):
        context = build_document_context(file, source_label='Архив' if file.source_id == subject.sources[0] else '', projects=subject.projects,
            aliases=(), context_revision=revision)
        assert projection.publish_context(context, expected_file_revision=file.revision, prepared=None)
    assert subject.facade.search(SearchRequest('Пермские моторы зенитные фонари', mode='lexical')).results


def test_pending_direct_root_current_confirmed_alias_remains_searchable_until_revoked(project_search, monkeypatch):
    from atom_search.service.contracts import SearchRequest
    from atom_search_core.source import FileSystemSource

    subject = project_search
    def forbidden(*args, **kwargs):
        pytest.fail('pending alias lookup attempted a document probe')
    monkeypatch.setattr(FileSystemSource, 'snapshots', forbidden)
    aliases = (replace(subject.aliases[0], variants=('ПМ', 'ДК')), subject.aliases[1])
    projection = subject.core.projection
    revision = projection.sync(subject.projects, aliases, expected_settings_revision=2,
        source_labels={subject.sources[0]: 'Проекты', subject.sources[1]: 'Прямой корень'})
    assert revision > subject.revision
    response = subject.facade.search(SearchRequest('ДК', source_ids=(PUBLIC[1],), mode='lexical'))
    assert len(response.results) == 1
    row = response.results[0]
    assert (row.name, row.project_id, row.matched_fields) == ('Спецификация прямая.pdf', PROJECT, ('alias',))
    assert response.query_interpretation.project_id is None
    assert {(alias.matched_text, alias.source_id) for alias in response.applied_aliases} == {('ДК', PUBLIC[1])}
    assert subject.facade.search(SearchRequest('Пермские моторы', source_ids=(PUBLIC[1],), mode='lexical')).results
    projection.sync(subject.projects, (), expected_settings_revision=3)
    revoked = subject.facade.search(SearchRequest('ДК', source_ids=(PUBLIC[1],), mode='lexical'))
    assert not revoked.results and not revoked.applied_aliases


def test_pending_project_membership_uses_windows_segments_not_search_normalization(project_search):
    from atom_search.service.contracts import SearchRequest
    subject = project_search
    bindings = ('Ёлка_%/ПМ', 'ＰＭ', 'Straße', 'ΟΣ', 'İ')
    paths = (
        ('ёлка_%/пм/Спецификация-yes.pdf', True),
        ('Елка_%/ПМ/Спецификация-yo-no.pdf', False),
        ('Ёлка-X/ПМ/Спецификация-wild-no.pdf', False),
        ('Ёлка_%/ПМ-2/Спецификация-bound-no.pdf', False),
        ('ＰＭ/Спецификация-wide-yes.pdf', True),
        ('PM/Спецификация-wide-no.pdf', False),
        ('straße/Спецификация-sharp-yes.pdf', True),
        ('STRASSE/Спецификация-sharp-no.pdf', False),
        ('ος/Спецификация-sigma-yes.pdf', True),
        ('i\u0307/Спецификация-dot-yes.pdf', True))
    for path, _ in paths:
        parent, _, name = path.rpartition('/')
        subject.core.catalog.reconcile(subject.sources[0], DirectorySnapshot(parent,
            (FileMetadata(path, name, 0, 0),), complete=False))
    project = replace(subject.projects[0], source_ids=(subject.sources[0],), bindings=tuple(
        ProjectBinding(PROJECT, subject.sources[0], prefix, True) for prefix in bindings))
    projects = (project, *subject.projects[1:])
    revision = subject.core.projection.sync(projects, (), expected_settings_revision=2)
    expected = {path for path, accepted in paths if accepted}
    def selected():
        return subject.facade.search(SearchRequest('Спецификация', project_id=PROJECT, mode='lexical')).results
    assert {row.relative_path for row in selected()} == expected

    for file in subject.core.projection.pending_context(1000):
        context = build_document_context(file, source_label='', projects=projects,
                                        aliases=(), context_revision=revision)
        assert subject.core.projection.publish_context(context, expected_file_revision=file.revision,
                                                        prepared=None)
    assert {row.relative_path for row in selected()} == expected

def test_same_source_foreign_project_vectors_cannot_exhaust_candidate_limit(project_search, pg_connections):
    from contextlib import contextmanager
    from atom_search_core.models import ProjectFilter
    from atom_search.service.contracts import SearchRequest
    from .test_pg_retrieval import RecordingConnection
    from atom_search_core.search import compact
    subject = project_search
    path = 'Чёто до/Зенитные фонари ПМ/needle-selected.pdf'
    subject.core.catalog.reconcile(subject.sources[0], DirectorySnapshot(path.rpartition('/')[0],
        (FileMetadata(path, 'needle-selected.pdf', 0, 0),), complete=False))
    file = subject.core.projection.pending_context(1000)[0]
    context = build_document_context(file, source_label='Проекты', projects=subject.projects,
        aliases=subject.aliases, context_revision=subject.revision)
    assert subject.core.projection.publish_context(context, expected_file_revision=file.revision,
                                                   prepared=prepared(context))
    digest = hashlib.sha256(b'foreign').hexdigest()
    with pg_connections['maintenance']() as connection:
        start = connection.execute('SELECT max(id)+1 FROM atom_search.files').fetchone()[0]
        with connection.cursor().copy('''COPY atom_search.files
            (id,source_id,relative_path,parent,name,size,modified_ns,revision,compact_name,
             compact_stem,compact_path,fts_russian,fts_simple) FROM STDIN''') as copy:
            for i in range(2000):
                name = 'needle.pdf'
                parent = f'Поставка материалов/{i}'
                path = parent + '/' + name
                copy.write_row((start+i, subject.sources[0], path, parent, name, 0, 0, 1,
                                compact(name), compact(name[:-4]), compact(path), '', ''))
        with connection.cursor().copy('''COPY atom_search.file_project_context
            (file_id,file_revision,context_revision,project_uuid,source_label,context_text,
             raw_context_hash,prepared_text,input_hash,token_count,fts_russian,fts_simple) FROM STDIN''') as copy:
            for i in range(2000):
                copy.write_row((start+i, 1, subject.revision, SUPPLY, '', 'foreign', digest,
                                'foreign', digest, 2, '', ''))
        with connection.cursor().copy('''COPY atom_search.file_embeddings
            (file_id,model_key,file_revision,input_hash,embedding) FROM STDIN''') as copy:
            for i in range(2000):
                copy.write_row((start+i, KEY, 1, digest, str(list(VECTOR))))
        connection.execute("SELECT setval('atom_search.files_id_seq',(SELECT max(id) FROM atom_search.files))")
    allowed = next(row for row in subject.core.catalog.pending_embeddings(KEY, 384, 16)
                   if row.file.name == 'needle-selected.pdf')
    subject.core.catalog.save_embeddings([EmbeddingWrite(allowed, (.99, .01) + (0.,)*382,
                                                         allowed.input_hash)], KEY)
    statements, seen = [], []
    @contextmanager
    def recording():
        with pg_connections['application']() as connection:
            yield RecordingConnection(connection, statements)
    class Retrieval(PostgresRetrieval):
        def vector_candidates(self, *args, **kwargs):
            seen.append(kwargs.get('project_filter'))
            return super().vector_candidates(*args, **kwargs)
    backend = Retrieval(recording)
    current = ProjectFilter((PROJECT,), subject.revision, INPUT_SCHEMA)
    hits = backend.vector_candidates(VECTOR, model_key=KEY, scope=SearchScope((subject.sources[0],)),
                                     limit=10, project_filter=current)
    assert [hit.file.file_id for hit in hits] == [allowed.file.file_id]
    assert sum('<=>' in statement for statement, _ in statements) == 1  # Count chose this one-row exact subset.
    for candidates in (backend.literal_candidates, backend.lexical_candidates):
        assert [hit.file.file_id for hit in candidates('needle', scope=SearchScope((subject.sources[0],)),
            limit=1, project_filter=current)] == [allowed.file.file_id]
        assert candidates('needle', scope=SearchScope((subject.sources[0],)), limit=1,
            project_filter=ProjectFilter(None, subject.revision, INPUT_SCHEMA))[0].file.name == 'needle.pdf'
    assert backend.vector_candidates(VECTOR, model_key=KEY, scope=SearchScope(subject.sources),
        limit=10, project_filter=ProjectFilter((), subject.revision, INPUT_SCHEMA)) == []
    assert backend.vector_candidates(VECTOR, model_key=KEY, scope=SearchScope(()),
        limit=10, project_filter=current) == []
    class Embedder:
        model_id, dimension = 'context-fixture', 384
        def encode(self, texts, *, kind='query'):
            return [VECTOR for _ in texts]
    subject.core._embedder, subject.core.retrieval = Embedder(), backend
    result = subject.facade.search(SearchRequest('needle', project_id=PROJECT))
    assert result.results and {row.project_id for row in result.results} == {PROJECT}
    assert seen[-1] == current and type(seen[-1]) is ProjectFilter


def test_alias_revoke_between_resolution_and_sql_never_relabels_old_expansion(project_search, monkeypatch):
    from atom_search.service.contracts import SearchRequest, ServiceError
    subject = project_search
    original = subject.core.retrieval.project_candidates
    def revoked(resolution, **kwargs):
        subject.core.projection.sync(subject.projects, (), expected_settings_revision=2)
        return original(resolution, **kwargs)
    monkeypatch.setattr(subject.core.retrieval, 'project_candidates', revoked)
    with pytest.raises(ServiceError) as failure:
        subject.facade.search(SearchRequest('ПМ спецификация', project_id=PROJECT, mode='lexical'))
    assert (failure.value.code, failure.value.retryable) == ('service_unavailable', True)
    monkeypatch.setattr(subject.core.retrieval, 'project_candidates', original)
    current = subject.facade.search(SearchRequest('ПМ спецификация', mode='lexical'))
    assert current.applied_aliases == current.ambiguities == current.query_interpretation.expanded_terms == ()
    assert current.query_interpretation.project_id is None
    assert subject.facade.search(SearchRequest('Пермские моторы зенитные фонари', mode='lexical')).results


def test_project_filter_includes_unassigned_but_rejects_wrong_prepared_payload(project_search, pg_connections):
    from atom_search_core.models import ProjectFilter
    subject = project_search
    pending = subject.core.catalog.pending_embeddings(KEY, 384, 16)
    selected = [row for row in pending if row.file.name in ('Нераспределённая ведомость.xlsx', 'хрень.dwg')]
    assert len(selected) == 2
    subject.core.catalog.save_embeddings([EmbeddingWrite(row, VECTOR, row.input_hash) for row in selected], KEY)
    backend, scope = subject.core.retrieval, SearchScope(subject.sources[:2])
    unrestricted = ProjectFilter(None, subject.revision, INPUT_SCHEMA)
    hits = backend.vector_candidates(VECTOR, model_key=KEY, scope=scope, limit=10, project_filter=unrestricted)
    assert {hit.project_id for hit in hits} == {None, PROJECT}
    sql(pg_connections, 'UPDATE atom_search.file_project_context SET prepared_text=prepared_text || %s', ('changed',))
    assert backend.vector_candidates(VECTOR, model_key=KEY, scope=scope, limit=10,
                                      project_filter=unrestricted) == []
    with pytest.raises(ValueError, match='invalid_project_filter'):
        backend.vector_candidates(VECTOR, model_key=KEY, scope=scope, limit=10,
            project_filter=ProjectFilter(None, subject.revision, 'filename-parent-v1'))


def test_compact_ascii_and_engineering_codes_have_real_field_evidence(project_search):
    from atom_search.service.contracts import SearchRequest
    subject = project_search
    subject.core.catalog.reconcile(subject.sources[0], DirectorySnapshot('', (
        FileMetadata('allowed-drawing.pdf', 'allowed-drawing.pdf', 0, 0),
        FileMetadata('КМ-101.dwg', 'КМ-101.dwg', 0, 0)), complete=False))
    def check():
        for query, expected in (('drawing', 'allowed-drawing.pdf'), ('КМ101', 'КМ-101.dwg')):
            response = subject.facade.search(SearchRequest(query, mode='lexical'))
            assert [row.name for row in response.results] == [expected]
            assert set(response.results[0].matched_fields) == {'name', 'path'}
        assert not subject.facade.search(SearchRequest('aliases', mode='lexical')).results
    check()  # Pending unassigned rows and missing model.
    for file in subject.core.projection.pending_context(1000):
        context = build_document_context(file, source_label='Проекты', projects=subject.projects,
            aliases=subject.aliases, context_revision=subject.revision)
        assert subject.core.projection.publish_context(context, expected_file_revision=file.revision, prepared=None)
    check()  # Current raw FTS technical field tags cannot fabricate evidence.


def test_project_filtered_ann_recall_and_index_plan(pg_connections):
    """The P1 20k/384D recall experiment, now with project cohorts in ONE source."""
    from contextlib import contextmanager
    import json
    import statistics
    import time
    import uuid
    import numpy as np
    from atom_search_core.models import DocumentContext, ProjectFilter
    from .test_pg_retrieval import RecordingConnection, bulk_seed, plan_nodes
    apply_catalog_migrations(pg_connections['maintenance'])
    catalog = PostgresCatalog(pg_connections['application'], model=MODEL)
    random = np.random.default_rng(42)
    vectors = random.normal(size=(20000, 384)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    queries = random.normal(size=(90, 384)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    preparation = {}
    from atom_search_core.source import canonical_root
    def metadata_for(index, name):
        group = index % 100
        parent = f'p{group}'
        path = parent + '/' + name
        context = DocumentContext(1, 1, name, path, canonical_root('C:/benchmark/0'), 'Bench',
            f'10000000-0000-4000-8000-{group:012d}', f'Project {group}', (), 1)
        return parent, path, metadata_embedding_text(context)
    source, = bulk_seed(catalog, pg_connections, vectors, model_key=KEY, source_count=1,
                        preparation=preparation, metadata_for=metadata_for)
    with pg_connections['maintenance']() as connection:
        first = connection.execute('SELECT min(id) FROM atom_search.files').fetchone()[0]
        files = connection.execute('''SELECT f.id,f.name,f.relative_path,s.path
            FROM atom_search.files f JOIN atom_search.sources s ON s.id=f.source_id ORDER BY f.id''').fetchall()
    projects = tuple(ProjectDefinition(f'10000000-0000-4000-8000-{i:012d}', f'Project {i}', (source,),
        (ProjectBinding(f'10000000-0000-4000-8000-{i:012d}', source, f'p{i}', True),)) for i in range(100))
    projection = projection_type()(pg_connections['application'])
    revision = projection.sync(projects, (), expected_settings_revision=1, source_labels={source: 'Bench'})
    assert revision == 1
    with pg_connections['maintenance']() as connection:
        with connection.cursor().copy('''COPY atom_search.file_project_context
            (file_id,file_revision,context_revision,project_uuid,source_label,context_text,
             raw_context_hash,prepared_text,input_hash,token_count,fts_russian,fts_simple) FROM STDIN''') as copy:
            for identifier, name, path, root in files:
                project = projects[(identifier-first) % 100]
                context = DocumentContext(identifier, source, name, path, root, 'Bench', project.project_id,
                                          project.canonical_name, (), revision)
                raw = metadata_embedding_text(context)
                digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
                copy.write_row((identifier, 1, revision, project.project_id, 'Bench', raw, digest,
                                raw, digest, 64, '', ''))
        for table in ('files', 'file_embeddings', 'file_project_context', 'projects', 'project_context_state'):
            connection.execute('ANALYZE atom_search.' + table)
    statements = []
    @contextmanager
    def recording():
        with pg_connections['application']() as connection:
            yield RecordingConnection(connection, statements)
    backend = PostgresRetrieval(recording)
    report = dict(seed=42, vectors=20000, dimensions=384, queries_per_cohort=30,
                  limit=10, same_source=True, preparation=preparation, cohorts={})
    with pg_connections['maintenance']() as connection:
        for group, percent in enumerate((1, 10, 100)):
            permitted = tuple(project.project_id for project in projects[:percent])
            recalls, counts, distance_queries, latencies, forbidden = [], [], [], [], 0
            for vector in queries[group*30:(group+1)*30]:
                literal = '[' + ','.join(map(str, vector)) + ']'
                # Independent exact baseline over the fixture's explicit UUID
                # cohort, never production's eligible SQL or a Python postfilter.
                exact_sql = '''WITH eligible AS MATERIALIZED (
                    SELECT e.file_id,e.embedding FROM atom_search.file_embeddings e
                    JOIN atom_search.file_project_context pc ON pc.file_id=e.file_id
                    WHERE e.model_key=%s AND pc.project_uuid=ANY(%s::uuid[]))
                    SELECT file_id FROM eligible ORDER BY embedding <=> %s::vector,file_id LIMIT 10'''
                exact_args = (KEY, list(permitted), literal)
                exact = connection.execute(exact_sql, exact_args).fetchall()
                statements.clear()
                started = time.perf_counter()
                hits = backend.vector_candidates(tuple(map(float, vector)), model_key=KEY,
                    scope=SearchScope((source,)), limit=10,
                    project_filter=ProjectFilter(permitted, revision, INPUT_SCHEMA))
                latencies.append((time.perf_counter()-started)*1000)
                counts.append(len(hits))
                distance_queries.append(sum('<=>' in statement for statement, _ in statements))
                forbidden += sum(hit.file.source_id != source or hit.project_id not in permitted for hit in hits)
                recalls.append(len({hit.file.file_id for hit in hits} & {row[0] for row in exact}) / 10)
            for statement, parameters in statements:
                if statement.lstrip().upper().startswith('SET LOCAL'):
                    connection.execute(statement, parameters)
            plans = [connection.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + statement,
                                        parameters).fetchone()[0][0]
                     for statement, parameters in statements if '<=>' in statement]
            exact_plan = connection.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + exact_sql,
                                            exact_args).fetchone()[0][0]
            report['cohorts'][str(percent)] = dict(eligible=percent*200, recalls=recalls,
                recall_mean=statistics.mean(recalls), forbidden=forbidden, counts=counts,
                distance_queries_per_call=distance_queries, plans=plans, exact_plan=exact_plan,
                p50_ms=statistics.median(latencies), p95_ms=float(np.percentile(latencies, 95)))
    evidence = Path(__file__).resolve().parents[2] / 'artifacts/ci-benchmarks/project-search'
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / ('project-ann-' + uuid.uuid4().hex[:12] + '.json')).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    for percent, result in report['cohorts'].items():
        assert result['forbidden'] == 0 and result['counts'] == [10]*30
        assert result['recall_mean'] >= .95, (percent, result['recall_mean'])
        assert result['distance_queries_per_call'] == [1]*30, 'No hidden exact retry may rescue this benchmark'
        assert not any('hnsw' in node.get('Index Name', '') for node in plan_nodes(result['exact_plan']['Plan']))
        uses_ann = any('hnsw' in node.get('Index Name', '')
                       for plan in result['plans'] for node in plan_nodes(plan['Plan']))
        assert uses_ann == (percent != '1'), (percent, result['plans'])
