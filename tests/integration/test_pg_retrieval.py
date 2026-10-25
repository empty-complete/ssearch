"""Real, owned PostgreSQL retrieval; synthetic recall is not project relevance."""
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import statistics
import time

import numpy as np
import psycopg
import pytest

from atom_search_core import SearchService
from atom_search_core.models import DirectorySnapshot, SearchScope
from atom_search_core.search import compact, lexical_score
from .test_pg_catalog import KEY, metadata, rows, write

EVIDENCE = Path(__file__).resolve().parents[2] / '.superpowers/sdd/2026-10-20-postgres-search-backend'


def retrieval(connections):
    return importlib.import_module('atom_search_core.storage.postgres_retrieval').PostgresRetrieval(
        connections['application'])


def seed_paths(catalog, paths, root='C:/synthetic'):
    source = catalog.begin_root(root)
    parents = {}
    for path in paths:
        parents.setdefault(path.rpartition('/')[0], []).append(metadata(path))
    for parent, files in parents.items():
        catalog.reconcile(source, DirectorySnapshot(parent, files=tuple(files), complete=False))
    return source


def ids(hits):
    return [hit.file.file_id for hit in hits]


def test_pg_literal_codes_and_russian_wordforms(pg_catalog, pg_connections):
    source = seed_paths(pg_catalog, ['АР-1.pdf', 'Чертежи/ПЕРМСКИЕ МОТОРЫ/план.dwg',
                                     'ЁЛОЧНЫЕ/расчёт.doc', 'ＦＡＣＡＤＥ/ＳＴＲＡＳＳＥ.txt'])
    backend = retrieval(pg_connections)
    scope = SearchScope((source,))
    for query, expected in [('АР1', 'АР-1.pdf'), ('ар1', 'АР-1.pdf'),
                            ('пермский мотор', 'план.dwg'), ('ПЕРМСКИЙ МОТОР', 'план.dwg'),
                            ('елочный', 'расчёт.doc'), ('ЁЛОЧНЫЙ', 'расчёт.doc'),
                            ('strasse', 'ＳＴＲＡＳＳＥ.txt')]:
        hits = backend.lexical_candidates(query, scope=scope, limit=10)
        assert expected in [hit.file.name for hit in hits]
    # Stemming is useful even when the old Python lexical scorer returns zero.
    assert lexical_score('пермского мотора', 'план.dwg', 'Чертежи/ПЕРМСКИЕ МОТОРЫ/план.dwg') == 0
    assert backend.lexical_candidates('пермского мотора', scope=scope, limit=10)
    assert backend.lexical_candidates('пермск', scope=scope, limit=10)


def test_pg_query_punctuation_is_safe(pg_catalog, pg_connections):
    source = seed_paths(pg_catalog, ["д'ело/ф'айл%_.dwg", 'axb.txt', 'a%b.txt', 'a_b.pdf', 'unrelated.txt'])
    backend = retrieval(pg_connections)
    scope = SearchScope((source,))
    for query in ["ф'айл%_", '"ф\'айл" -dwg', "'; DROP SCHEMA atom_search CASCADE; --", '"', '%', '_', "':* | &"]:
        backend.lexical_candidates(query, scope=scope, limit=10)
    for query in ['a%b', 'a_b']:
        hits = backend.lexical_candidates(query, scope=scope, limit=10)
        assert 'axb.txt' not in [hit.file.name for hit in hits]
        assert hits
    assert backend.lexical_candidates('%_', scope=scope, limit=10) == []
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.files') == [(5,)]


def test_pg_source_scope_precedes_every_limit(pg_catalog, pg_connections):
    a = seed_paths(pg_catalog, [f'АР1-{i:04}.txt' for i in range(2200)], 'C:/a')
    b = seed_paths(pg_catalog, ['АР-1.pdf'], 'C:/b')
    backend = retrieval(pg_connections)
    for scope in [SearchScope(()), SearchScope((999999,)), pg_catalog.resolve_scope('C:/missing')]:
        assert backend.lexical_candidates('АР1', scope=scope, limit=1) == []
        assert backend.vector_candidates((1.,) + (0.,)*383, model_key=KEY, scope=scope, limit=1) == []
    assert [hit.file.source_id for hit in backend.lexical_candidates('АР1', scope=SearchScope((b,)), limit=1)] == [b]
    pending = pg_catalog.pending_embeddings(KEY, 384, 3000)
    pg_catalog.save_embeddings([write(item, (1.,) + (0.,)*383 if item.file.source_id == a
                                            else (0.8, 0.6) + (0.,)*382) for item in pending], KEY)
    assert [hit.file.source_id for hit in backend.vector_candidates((1.,)+(0.,)*383,
            model_key=KEY, scope=SearchScope((b,)), limit=1)] == [b]


def exact(connection, vector, source_ids, limit=10):
    # Materialization prevents the distance ordering from reaching the HNSW scan.
    query = '''WITH eligible AS MATERIALIZED (
        SELECT e.file_id,e.embedding FROM atom_search.file_embeddings e
        JOIN atom_search.files f ON f.id=e.file_id AND f.revision=e.file_revision
        JOIN atom_search.sources s ON s.id=f.source_id
        JOIN atom_search.catalogs c ON c.id=s.catalog_id
        WHERE c.state='active' AND s.id=ANY(%s::bigint[]) AND e.model_key=%s)
        SELECT file_id,embedding <=> %s::vector AS distance FROM eligible
        ORDER BY distance,file_id LIMIT %s'''
    args = (list(source_ids), KEY, '[' + ','.join(map(str, vector)) + ']', limit)
    return connection.execute(query, args).fetchall(), query, args


def test_pg_cosine_hits_match_exact(pg_catalog, pg_connections):
    source = seed_paths(pg_catalog, [f'{i}.txt' for i in range(8)])
    pending = pg_catalog.pending_embeddings(KEY, 384, 8)
    pg_catalog.save_embeddings([write(item, (float(i+1), 1.)+(0.,)*382)
                               for i, item in enumerate(pending)], KEY)
    backend = retrieval(pg_connections)
    vector = (1.,)+(0.,)*383
    with pg_connections['application']() as connection:
        baseline, _, _ = exact(connection, vector, (source,))
    hits = backend.vector_candidates(vector, model_key=KEY, scope=SearchScope((source,)), limit=10)
    assert ids(hits) == [row[0] for row in baseline]
    assert [hit.similarity for hit in hits] == pytest.approx([1-row[1] for row in baseline])
    pg_catalog.save_embeddings([write(item) for item in pending], KEY)
    hits = backend.vector_candidates(vector, model_key=KEY, scope=SearchScope((source,)), limit=3)
    assert ids(hits) == sorted(item.file.file_id for item in pending)[:3]


def test_other_model_and_stale_revision_never_enter_candidates(pg_catalog, pg_connections):
    source = seed_paths(pg_catalog, ['valid.txt', 'stale.txt'])
    pending = pg_catalog.pending_embeddings(KEY, 384, 2)
    pg_catalog.save_embeddings([write(item) for item in pending], KEY)
    stale = next(item.file.file_id for item in pending if item.file.name == 'stale.txt')
    rows(pg_connections, 'UPDATE atom_search.file_embeddings SET file_revision=file_revision+1 WHERE file_id=%s', (stale,))
    rows(pg_connections, "INSERT INTO atom_search.embedding_models VALUES ('other',384,'filename-parent-v1',%s)", ('b'*64,))
    rows(pg_connections, "INSERT INTO atom_search.file_embeddings SELECT file_id,'other',file_revision,input_hash,embedding FROM atom_search.file_embeddings")
    foreign = rows(pg_connections, "INSERT INTO atom_search.catalogs(state) VALUES ('staging') RETURNING id")[0][0]
    foreign_source = rows(pg_connections, "INSERT INTO atom_search.sources(catalog_id,path,status) VALUES (%s,'foreign','new') RETURNING id", (foreign,))[0][0]
    rows(pg_connections, 'UPDATE atom_search.sources SET catalog_id=%s WHERE id=%s', (foreign, source))
    backend = retrieval(pg_connections)
    scope = SearchScope((source, foreign_source))
    vector = (1.,)+(0.,)*383
    assert backend.vector_candidates(vector, model_key=KEY, scope=scope, limit=10) == []
    assert backend.lexical_candidates('valid', scope=scope, limit=10) == []
    rows(pg_connections, "UPDATE atom_search.sources SET catalog_id=(SELECT id FROM atom_search.catalogs WHERE state='active') WHERE id=%s", (source,))
    assert backend.vector_candidates(vector, model_key='missing', scope=scope, limit=10) == []
    hits = backend.vector_candidates(vector, model_key=KEY, scope=scope, limit=10)
    assert [hit.file.name for hit in hits] == ['valid.txt']


class RecordingConnection:
    """Capture actual executed SQL for plan evidence; execute every call unchanged."""
    def __init__(self, connection, statements):
        self.connection, self.statements = connection, statements

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self.connection.execute(sql, params)


def plan_nodes(plan):
    yield plan
    for child in plan.get('Plans', []):
        yield from plan_nodes(child)


def bulk_seed(catalog, connections, vectors, *, model_key=KEY, source_count=100,
              distractor_model_key=None, preparation=None, metadata_for=None):
    sources = [catalog.begin_root(f'C:/benchmark/{i}') for i in range(source_count)]
    with connections['maintenance']() as connection:
        # Large synthetic corpora measure retrieval, not incremental indexing.
        # Reuse the exact production-created definition, including its options.
        index_ddl = connection.execute("SELECT pg_get_indexdef('atom_search.embeddings_hnsw_cosine'::regclass)").fetchone()[0]
        connection.execute('DROP INDEX atom_search.embeddings_hnsw_cosine')
        load_started = time.perf_counter()
        start = connection.execute('SELECT COALESCE(max(id),0)+1 FROM atom_search.files').fetchone()[0]
        with connection.cursor().copy('''COPY atom_search.files
            (id,source_id,relative_path,parent,name,size,modified_ns,revision,compact_name,compact_stem,compact_path,fts_russian,fts_simple)
            FROM STDIN''') as copy:
            for i in range(len(vectors)):
                name = f'fixture-{start+i}.txt'
                parent, path, _ = ('', name, name) if metadata_for is None else metadata_for(i, name)
                copy.write_row((start+i, sources[i % source_count], path, parent, name, 1, 0, 1,
                                compact(name), compact(name[:-4]), compact(path), '', ''))
        with connection.cursor().copy('''COPY atom_search.file_embeddings
            (file_id,model_key,file_revision,input_hash,embedding) FROM STDIN''') as copy:
            for i, vector in enumerate(vectors):
                name = f'fixture-{start+i}.txt'
                text = name if metadata_for is None else metadata_for(i, name)[2]
                copy.write_row((start+i, model_key, 1, hashlib.sha256(text.encode()).hexdigest(),
                                '['+','.join(map(str, vector))+']'))
        if distractor_model_key is not None:
            connection.execute('''INSERT INTO atom_search.file_embeddings
                SELECT file_id,%s,file_revision,input_hash,embedding
                FROM atom_search.file_embeddings WHERE model_key=%s''', (distractor_model_key, model_key))
        connection.execute("SELECT setval('atom_search.files_id_seq',(SELECT max(id) FROM atom_search.files))")
        load_seconds = time.perf_counter()-load_started
        build_started = time.perf_counter()
        connection.execute(index_ddl)
        build_seconds = time.perf_counter()-build_started
        for table in ('catalogs', 'sources', 'files', 'embedding_models', 'file_embeddings'):
            connection.execute('ANALYZE atom_search.'+table)
        if preparation is not None:
            preparation.update(index_ddl=index_ddl, load_seconds=load_seconds,
                               index_build_seconds=build_seconds,
                               index_bytes=connection.execute("SELECT pg_relation_size('atom_search.embeddings_hnsw_cosine')").fetchone()[0])
    return sources


def test_pg_filtered_ann_recall_and_index_plan(pg_catalog, pg_connections):
    backend = retrieval(pg_connections)
    random = np.random.default_rng(42)
    vectors = random.normal(size=(20000, 384)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    queries = random.normal(size=(90, 384)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    preparation = {}
    sources = bulk_seed(pg_catalog, pg_connections, vectors, preparation=preparation)
    statements = []

    @contextmanager
    def recording():
        with pg_connections['application']() as connection:
            yield RecordingConnection(connection, statements)

    backend = type(backend)(recording)
    report = {'seed': 42, 'vectors': 20000, 'dimensions': 384, 'queries_per_scope': 30,
              'limit': 10, 'platform': platform.platform(), 'processor': platform.processor(),
              'cpu_count': os.cpu_count(), 'preparation': preparation, 'scopes': {}}
    with pg_connections['maintenance']() as connection:
        connection.execute("SELECT '[1]'::vector <=> '[1]'::vector")
        report['server'] = connection.execute('SHOW server_version').fetchone()[0]
        report['vector'] = connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
        report['driver'] = psycopg.__version__
        report['settings'] = {setting: connection.execute('SHOW '+setting).fetchone()[0] for setting in
                              ('work_mem', 'maintenance_work_mem', 'shared_buffers', 'random_page_cost', 'hnsw.scan_mem_multiplier')}
        report['index'] = connection.execute("""SELECT pg_get_indexdef(oid),reloptions,pg_relation_size(oid)
            FROM pg_class WHERE relname='embeddings_hnsw_cosine'""").fetchone()
        for j, percent in enumerate((1, 10, 100)):
            scope = SearchScope(tuple(sources[:percent]))
            recalls, latencies, counts, query_counts, forbidden = [], [], [], [], 0
            for vector in queries[j*30:(j+1)*30]:
                baseline, exact_sql, exact_args = exact(connection, vector, scope.source_ids)
                statements.clear()
                started = time.perf_counter()
                hits = backend.vector_candidates(tuple(map(float, vector)), model_key=KEY, scope=scope, limit=10)
                latencies.append((time.perf_counter()-started)*1000)
                counts.append(len(hits))
                query_counts.append(sum('<=>' in sql for sql, _ in statements))
                forbidden += sum(hit.file.source_id not in scope.source_ids for hit in hits)
                recalls.append(len(set(ids(hits)) & {row[0] for row in baseline})/10)
            exact_plan = connection.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+exact_sql, exact_args).fetchone()[0][0]
            actual_queries = [(sql, args) for sql, args in statements if '<=>' in sql]
            plans = []
            for sql, args in statements:
                if sql.lstrip().upper().startswith('SET LOCAL'):
                    connection.execute(sql, args)
            for sql, args in actual_queries:
                plans.append(connection.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+sql, args).fetchone()[0][0])
            report['scopes'][str(percent)] = {'eligible': percent*200, 'recall_mean': statistics.mean(recalls),
                'recalls': recalls, 'counts': counts, 'forbidden': forbidden,
                'p50_ms': statistics.median(latencies), 'p95_ms': float(np.percentile(latencies, 95)),
                'latencies_ms': latencies, 'actual_distance_queries': len(actual_queries),
                'distance_queries_per_call': query_counts,
                'plans': plans, 'exact_plan': exact_plan,
                'profile': [(sql, args) for sql, args in statements if sql.lstrip().upper().startswith('SET LOCAL')]}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE/'task-3-benchmark.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    for percent, item in report['scopes'].items():
        assert not any('hnsw' in node.get('Index Name', '') for node in plan_nodes(item['exact_plan']['Plan']))
        assert item['forbidden'] == 0
        assert item['counts'] == [10]*30
        assert item['recall_mean'] >= .95, (percent, item['recall_mean'])
        if percent != '1':
            assert item['distance_queries_per_call'] == [1]*30, 'Benchmark cannot hide ANN underfill with exact fallback'
            assert any('hnsw' in node.get('Index Name', '') for plan in item['plans'] for node in plan_nodes(plan['Plan']))


def test_v1_backfill_is_atomic_and_idempotent(pg_connections):
    migration = importlib.import_module('atom_search_core.storage.migrations')
    with pg_connections['maintenance']() as connection:
        connection.execute(migration._SCHEMA_V1)
        connection.execute('INSERT INTO atom_search.schema_migrations(version) VALUES (1)')
        connection.execute("INSERT INTO atom_search.sources(catalog_id,path,status) SELECT id,'C:/old','complete' FROM atom_search.catalogs")
        connection.execute("""INSERT INTO atom_search.files(source_id,relative_path,parent,name,size,modified_ns,revision,compact_name,compact_stem)
            SELECT id,'ПЕРМСКИЕ МОТОРЫ/ЁЛКА.pdf','ПЕРМСКИЕ МОТОРЫ','ЁЛКА.pdf',1,0,1,'old','old' FROM atom_search.sources""")
        connection.execute("""CREATE FUNCTION atom_search.reject_backfill() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'backfill failure'; END $$""")
        connection.execute('CREATE TRIGGER reject_backfill BEFORE UPDATE ON atom_search.files FOR EACH ROW EXECUTE FUNCTION atom_search.reject_backfill()')
    with pytest.raises(psycopg.errors.RaiseException, match='backfill failure'):
        migration.apply_catalog_migrations(pg_connections['maintenance'])
    assert rows(pg_connections, 'SELECT version FROM atom_search.schema_migrations') == [(1,)]
    assert rows(pg_connections, "SELECT column_name FROM information_schema.columns WHERE table_schema='atom_search' AND table_name='files' AND column_name='compact_path'") == []
    rows(pg_connections, 'DROP TRIGGER reject_backfill ON atom_search.files')
    assert migration.apply_catalog_migrations(pg_connections['maintenance']) == 5
    before = rows(pg_connections, 'SELECT compact_name,compact_stem,compact_path,fts_russian::text,fts_simple::text FROM atom_search.files')
    assert before[0][:3] == ('елкаpdf', 'елка', 'пермскиемоторыелкаpdf')
    assert migration.apply_catalog_migrations(pg_connections['maintenance']) == 5
    assert rows(pg_connections, 'SELECT compact_name,compact_stem,compact_path,fts_russian::text,fts_simple::text FROM atom_search.files') == before
    scope = SearchScope(tuple(row[0] for row in rows(pg_connections, 'SELECT id FROM atom_search.sources')))
    assert retrieval(pg_connections).lexical_candidates('пермского мотора', scope=scope, limit=1)


def test_vector_timeout_keeps_lexical_and_safe_diagnostic(pg_catalog, pg_connections):
    source = seed_paths(pg_catalog, ['АР-1.pdf'])
    pg_catalog.save_embeddings([write(item) for item in pg_catalog.pending_embeddings(KEY, 384, 10)], KEY)
    backend = retrieval(pg_connections)

    class Embedder:
        model_id = 'fixture-model'
        dimension = 384

        def encode(self, texts, *, kind):
            return [(1.,)+(0.,)*383 for _ in texts]

    service = SearchService(catalog=pg_catalog, retrieval=backend, embedder=Embedder())
    with pg_connections['maintenance']() as blocker:
        blocker.execute('LOCK TABLE atom_search.file_embeddings IN ACCESS EXCLUSIVE MODE')
        started = time.perf_counter()
        hits = service.search('АР1')
        elapsed = time.perf_counter()-started
    assert [hit.name for hit in hits] == ['АР-1.pdf']
    assert 1.8 <= elapsed < 6
    diagnostic = service.status()['model_status']
    assert 'timed out' in diagnostic
    assert all(secret not in diagnostic for secret in ['SELECT', 'fixture-model', 'atom_search.', 'АР1'])
    assert backend.vector_candidates((1.,)+(0.,)*383, model_key=KEY, scope=SearchScope((source,)), limit=10)


def test_local_profile_resets_and_exact_ties_are_stable(pg_catalog, pg_connections):
    source = seed_paths(pg_catalog, ['x.txt', 'y.txt', 'z.txt'])
    pg_catalog.save_embeddings([write(item) for item in pg_catalog.pending_embeddings(KEY, 384, 10)], KEY)
    backend = retrieval(pg_connections)
    settings = ['statement_timeout', 'hnsw.ef_search', 'hnsw.iterative_scan', 'hnsw.max_scan_tuples', 'random_page_cost']
    with pg_connections['application']() as connection:
        # Loading the extension registers its GUCs in this backend process.
        connection.execute("SELECT '[1]'::vector <=> '[1]'::vector")
        before = [connection.execute('SHOW '+setting).fetchone()[0] for setting in settings]
        connection.commit()

        @contextmanager
        def reused():
            with connection.transaction():
                yield connection

        backend = type(backend)(reused)
        scope = SearchScope((source,))
        first = backend.vector_candidates((1.,)+(0.,)*383, model_key=KEY, scope=scope, limit=2)
        assert ids(backend.vector_candidates((1.,)+(0.,)*383, model_key=KEY, scope=scope, limit=2)) == ids(first)
        assert ids(first) == sorted(ids(first))
        after = [connection.execute('SHOW '+setting).fetchone()[0] for setting in settings]
        assert before == after


def test_ann_ties_mixed_models_and_bounded_underfill(pg_catalog, pg_connections):
    backend = retrieval(pg_connections)
    vectors = np.zeros((12000, 384), dtype=np.float32)
    vectors[:, 0] = 1
    rows(pg_connections, "INSERT INTO atom_search.embedding_models VALUES ('other',384,'filename-parent-v1',%s)", ('b'*64,))
    # Same-file foreign-model distractors participate in the shared HNSW graph.
    preparation = {}
    sources = bulk_seed(pg_catalog, pg_connections, vectors, source_count=1,
                        distractor_model_key='other', preparation=preparation)
    statements = []

    @contextmanager
    def recording():
        with pg_connections['application']() as connection:
            yield RecordingConnection(connection, statements)

    backend = type(backend)(recording)
    scope = SearchScope(tuple(sources))
    vector = (1.,)+(0.,)*383
    runs = [backend.vector_candidates(vector, model_key=KEY, scope=scope, limit=10) for _ in range(3)]
    assert ids(runs[0]) == ids(runs[1]) == ids(runs[2])
    assert len(set(ids(runs[0]))) == 10
    assert ids(runs[0]) == sorted(ids(runs[0]))
    assert all(hit.similarity == 1 and hit.file.source_id in sources for hit in runs[0])
    actual = [(sql, params) for sql, params in statements if '<=>' in sql]
    with pg_connections['application']() as connection:
        for sql, params in statements:
            if sql.lstrip().upper().startswith('SET LOCAL'):
                connection.execute(sql, params)
        plan = connection.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+actual[0][0], actual[0][1]).fetchone()[0][0]
        baseline, _, _ = exact(connection, vector, sources)
    assert any('hnsw' in node.get('Index Name', '') for node in plan_nodes(plan['Plan']))
    (EVIDENCE/'task-3-ties.json').write_text(json.dumps({'plan': plan, 'preparation': preparation, 'repeated_ids': [ids(run) for run in runs],
        'exact_ids': [row[0] for row in baseline], 'boundary_difference': sorted(set(ids(runs[0])) ^ {row[0] for row in baseline}),
        'forbidden': 0}, indent=2), encoding='utf-8')

    # Real concurrent deletion after the count makes ANN underfill. Observe one
    # exact retry even when the new snapshot cannot fill the requested count.
    # Neither count nor result rows are mocked; the second connection commits SQL.
    class ConcurrentDeletion(RecordingConnection):
        def execute(self, sql, params=None):
            cursor = super().execute(sql, params)
            if sql.startswith('SELECT count(*)'):
                rows(pg_connections, '''DELETE FROM atom_search.file_embeddings WHERE model_key=%s
                    AND file_id NOT IN (SELECT id FROM atom_search.files ORDER BY id LIMIT 5)''', (KEY,))
            return cursor

    @contextmanager
    def deleting():
        with pg_connections['application']() as connection:
            yield ConcurrentDeletion(connection, statements)

    statements.clear()
    backend = type(backend)(deleting)
    started = time.perf_counter()
    hits = backend.vector_candidates(vector, model_key=KEY, scope=scope, limit=10)
    assert len(hits) == 5 and len(set(ids(hits))) == 5
    assert all(hit.file.source_id in sources for hit in hits)
    assert ids(hits) == sorted(ids(hits))
    assert len([(sql, params) for sql, params in statements if '<=>' in sql]) == 2
    assert time.perf_counter()-started < 6
