"""Public search materialization against owned, verified PostgreSQL connections."""
from contextlib import contextmanager, nullcontext
from pathlib import PureWindowsPath
import time
from types import SimpleNamespace
from uuid import UUID

import pytest

from atom_search.service.application import AtomSearchService
from atom_search.service.contracts import AccessContext, SearchRequest, ServiceError, SettingsSnapshot
from atom_search.service.sources import SourceRegistry
from atom_search_core.models import DirectorySnapshot, EmbeddingModel, FileMetadata
from atom_search_core.project_context import INPUT_SCHEMA, build_document_context
from atom_search_core.service import SearchService
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
from atom_search_core.storage.project_projection import ProjectProjection
from atom_search_core.storage.public_ids import PostgresPublicIds


VISIBLE = 'a0000000-0000-4000-8000-000000000001'
PRIVATE = 'a0000000-0000-4000-8000-000000000002'


def sql(connections, statement, parameters=()):
    with connections['maintenance']() as connection:
        result = connection.execute(statement, parameters)
        return result.fetchall() if result.description is not None else []


def identities(connections):
    return tuple(sql(connections, f'SELECT * FROM atom_search.{table} ORDER BY 1')
                 for table in ('source_public_ids', 'file_public_ids'))


class VerifiedReadMeter:
    """Observe the real runtime factory; never replace its identity checks or SQL."""
    def __init__(self, factory):
        self.factory = factory
        self.enabled = False
        self.phase = 'retrieval'
        self.reads = []
        self.after_identity_read = None

    @contextmanager
    def __call__(self):
        started = time.perf_counter()
        record = dict(phase=self.phase, elapsed=0.) if self.enabled else None
        if record is not None:
            self.reads.append(record)
        try:
            with self.factory() as connection:
                yield ObservedConnection(connection, self)
        finally:
            if record is not None:
                record['elapsed'] = time.perf_counter() - started


class ObservedConnection:
    def __init__(self, connection, meter):
        self.connection, self.meter = connection, meter

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=None):
        result = self.connection.execute(statement, parameters)
        # Run a genuine second transaction after the UUID SELECT has executed,
        # before the read transaction/metadata assembly has returned its rows.
        if self.meter.after_identity_read is not None and 'file_public_ids' in statement:
            action, self.meter.after_identity_read = self.meter.after_identity_read, None
            action()
        return result


@pytest.fixture
def materialization_search(pg_connections, request):
    count = getattr(request, 'param', 4)
    apply_catalog_migrations(pg_connections['maintenance'])
    meter = VerifiedReadMeter(pg_connections['application'])
    catalog = PostgresCatalog(meter, model=EmbeddingModel(
        'materialization-fixture|' + INPUT_SCHEMA, 384, INPUT_SCHEMA, 'a' * 64))
    public = PostgresPublicIds(catalog)
    roots = ('C:/fictional/batch-visible', 'D:/fictional/batch-private')
    sources = public.bind_many(((VISIBLE, roots[0]), (PRIVATE, roots[1])))
    for source, names in zip(sources, (
            tuple(f'needle-{index:03}.pdf' for index in range(count)),
            ('needle-private-000.pdf', 'needle-private-001.pdf'))):
        catalog.reconcile(source, DirectorySnapshot('Measured', files=tuple(
            FileMetadata('Measured/' + name, name, index + 1, 123 + index)
            for index, name in enumerate(names))))
        catalog.finish_root(source, 'unavailable')
    labels = {sources[0]: 'Visible', sources[1]: 'Private'}
    projection = ProjectProjection(meter)
    revision = projection.sync((), (), expected_settings_revision=1, source_labels=labels)
    for file in projection.pending_context(1000):
        context = build_document_context(file, source_label=labels[file.source_id],
            projects=(), aliases=(), context_revision=revision)
        assert projection.publish_context(context, expected_file_revision=file.revision, prepared=None)
    core = SearchService(catalog=catalog, retrieval=PostgresRetrieval(meter),
                         projection=projection, input_schema=INPUT_SCHEMA)
    core.public_ids = public
    settings = dict(sources=[dict(id=identity, path=root, enabled=True, label=label)
                            for identity, root, label in zip((VISIBLE, PRIVATE), roots, ('Visible', 'Private'))])
    repository = SimpleNamespace(snapshot=lambda: SettingsSnapshot(3, 1, settings, ()))

    def generation():
        with meter() as connection:
            return connection.execute("SELECT generation FROM atom_search.catalogs WHERE state='active'").fetchone()[0]

    owner = SimpleNamespace(core=core, repository=repository, operation=nullcontext,
        registry=SourceRegistry(repository, public), index_generation=generation)
    facade = AtomSearchService(owner, AccessContext('reader', frozenset({'search', 'fetch'}), (VISIBLE,)))
    # Independently read durable UUIDs, not the materialization helpers under test.
    expected = sql(pg_connections, '''SELECT f.id,p.file_uuid::text,s.source_uuid::text,
        f.name,f.relative_path,source.path FROM atom_search.files f
        JOIN atom_search.file_public_ids p ON p.file_id=f.id
        JOIN atom_search.source_public_ids s ON s.source_id=f.source_id
        JOIN atom_search.sources source ON source.id=f.source_id
        WHERE f.source_id=%s ORDER BY f.relative_path''', (sources[0],))
    try:
        yield SimpleNamespace(core=core, facade=facade, meter=meter, sources=sources,
                              expected=expected, revision=revision, roots=roots)
    finally:
        core.close()


@pytest.mark.parametrize('branch', ['project', 'metadata'])
@pytest.mark.parametrize('materialization_search', [100], indirect=True)
def test_public_search_has_constant_verified_reads_for_two_and_hundred_results(
        materialization_search, pg_connections, monkeypatch, capfd, branch):
    subject = materialization_search
    if branch == 'metadata':
        subject.core.projection = None  # The supported non-project PG API path.
    name = 'search_project' if branch == 'project' else 'search_scoped'
    original = getattr(subject.core, name)

    def retrieved(*args, **kwargs):
        result = original(*args, **kwargs)
        subject.meter.phase = 'post_retrieval'
        return result

    monkeypatch.setattr(subject.core, name, retrieved)
    before = identities(pg_connections)
    measured = []
    for limit in (2, 100):
        subject.meter.reads.clear()
        subject.meter.phase = 'retrieval'
        subject.meter.enabled = True
        started = time.perf_counter()
        try:
            response = subject.facade.search(SearchRequest('needle', limit=limit, mode='lexical'))
        finally:
            elapsed = time.perf_counter() - started
            subject.meter.enabled = False
        reads = len(subject.meter.reads)
        tail = [item for item in subject.meter.reads if item['phase'] == 'post_retrieval']
        measured.append(reads)
        # Visible on both RED and GREEN without exporting SQL parameters/paths.
        with capfd.disabled():
            print(f'P5_MATERIALIZATION branch={branch} mode=lexical limit={limit} '
                  f'results={len(response.results)} verified_reads={reads} '
                  f'elapsed_s={elapsed:.6f} post_retrieval_reads={len(tail)} '
                  f'post_retrieval_connection_s={sum(item["elapsed"] for item in tail):.6f}', flush=True)
        expected = subject.expected[:limit]
        assert [row.result_id for row in response.results] == [row[1] for row in expected]
        assert [row.relative_path for row in response.results] == [row[4] for row in expected]
        for row, stored in zip(response.results, expected):
            assert str(UUID(row.result_id)) == stored[1]
            assert row.source_id == VISIBLE and row.name == stored[3]
            assert row.full_path == str(PureWindowsPath(stored[5], *stored[4].split('/')))
            assert row.matched_fields == ('name', 'path') and row.match_kind == 'lexical'
            assert row.project_id is None and row.stale and row.score > 0
        fetched = subject.facade.fetch(response.results[0].result_id)
        assert fetched.result_id == expected[0][1] and fetched.source_id == VISIBLE
        assert fetched.relative_path == expected[0][4] and fetched.score is None
    assert identities(pg_connections) == before  # Reads never mint replacement UUIDs.
    assert measured[0] == measured[1], f'verified reads grew with result count: {measured}'
    assert measured[1] <= 9, f'public search exceeded constant verified-read budget: {measured}'


def after_retrieval(subject, monkeypatch, action):
    original = subject.core.search_project

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result
        action(result)
        return result

    monkeypatch.setattr(subject.core, 'search_project', changed)


@pytest.mark.parametrize('table,column', [('file_public_ids', 'file_id'), ('source_public_ids', 'source_id')])
def test_missing_mapping_during_materialization_fails_closed_without_minting_uuid(
        materialization_search, pg_connections, monkeypatch, table, column):
    subject = materialization_search
    remaining = []

    def remove(result):
        identity = result[0].file_id if column == 'file_id' else subject.sources[0]
        sql(pg_connections, f'DELETE FROM atom_search.{table} WHERE {column}=%s', (identity,))
        remaining.append(identities(pg_connections))

    after_retrieval(subject, monkeypatch, remove)
    failure = None
    try:
        subject.facade.search(SearchRequest('needle', mode='lexical'))
    except ServiceError as caught:
        failure = caught
    assert remaining and identities(pg_connections) == remaining[0]
    assert failure is not None, 'search exposed metadata with a missing durable UUID mapping'
    assert failure.code == 'result_not_found' and failure.status_code == 404


@pytest.mark.parametrize('change', ['moved_out_of_scope', 'retired_catalogue'])
def test_materialization_rechecks_captured_scope_and_active_catalogue(
        materialization_search, pg_connections, monkeypatch, change):
    subject = materialization_search

    def move_or_retire(result):
        if change == 'moved_out_of_scope':
            sql(pg_connections, 'UPDATE atom_search.files SET source_id=%s WHERE id=%s',
                (subject.sources[1], result[0].file_id))
        else:
            with pg_connections['maintenance']() as connection:
                connection.execute("UPDATE atom_search.catalogs SET state='retired' WHERE state='active'")
                connection.execute("INSERT INTO atom_search.catalogs(state) VALUES ('active')")

    after_retrieval(subject, monkeypatch, move_or_retire)
    before = identities(pg_connections)
    with pytest.raises(ServiceError) as failure:
        subject.facade.search(SearchRequest('needle', mode='lexical'))
    assert failure.value.code == 'result_not_found' and failure.value.status_code == 404
    assert identities(pg_connections) == before


def test_revision_change_during_uuid_read_rejects_obsolete_response(
        materialization_search, pg_connections):
    subject = materialization_search
    revisions = []

    @contextmanager
    def writer():
        with pg_connections['application']() as connection:
            connection.execute("SET LOCAL lock_timeout='2s'")
            yield connection

    def revise():
        revisions.append(ProjectProjection(writer).sync((), (), expected_settings_revision=2,
            source_labels={subject.sources[0]: 'Revised', subject.sources[1]: 'Private'}))

    subject.meter.after_identity_read = revise
    with pytest.raises(ServiceError) as failure:
        subject.facade.search(SearchRequest('needle', mode='lexical'))
    assert revisions == [subject.revision + 1]
    assert (failure.value.code, failure.value.retryable) == ('service_unavailable', True)


def test_scope_empty_forbidden_and_fetch_identity_stay_distinct(materialization_search, pg_connections):
    subject = materialization_search
    assert subject.facade.search(SearchRequest('needle', source_ids=(), mode='lexical')).results == ()
    assert len(subject.facade.search(SearchRequest('needle', source_ids=None, mode='lexical')).results) == 4
    with pytest.raises(ServiceError) as forbidden:
        subject.facade.search(SearchRequest('needle', source_ids=(PRIVATE,), mode='lexical'))
    assert (forbidden.value.code, forbidden.value.status_code) == ('source_scope_denied', 403)
    result_id = sql(pg_connections, '''SELECT p.file_uuid::text FROM atom_search.file_public_ids p
        JOIN atom_search.files f ON f.id=p.file_id WHERE f.source_id=%s ORDER BY f.id LIMIT 1''',
        (subject.sources[1],))[0][0]
    for identity, scope in ((result_id, None), (subject.expected[0][1], ())):
        with pytest.raises(ServiceError) as hidden:
            subject.facade.fetch(identity, source_ids=scope)
        assert (hidden.value.code, hidden.value.status_code) == ('result_not_found', 404)
