"""Content generation CAS on the fixture-owned PostgreSQL; no document I/O."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
import threading

import psycopg
import pytest

from atom_search_core.content_models import ChunkDraft, ChunkWrite, ExtractionResult, ExtractionSegment
from atom_search_core.models import DirectorySnapshot, EmbeddingModel, EmbeddingWrite, FileMetadata, PreparedEmbeddingInput
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.project_projection import ProjectProjection


MODEL = EmbeddingModel('fixture|content-context-v1', 384, 'content-context-v1', 'b' * 64)
METADATA_MODEL = EmbeddingModel('fixture|filename-parent-v1', 384, 'filename-parent-v1', 'a' * 64)
VECTOR = (1.,) + (0.,) * 383
TABLES = ('file_content_state', 'content_generations', 'content_extractions', 'content_chunks', 'chunk_embeddings')


def sql(connections, statement, parameters=(), *, role='maintenance'):
    with connections[role]() as connection:
        cursor = connection.execute(statement, parameters)
        return cursor.fetchall() if cursor.description else []


def storage_type():
    name = 'atom_search_core.storage.content'
    assert importlib.util.find_spec(name), 'Durable PostgreSQL content generation storage is required'
    module = importlib.import_module(name)
    assert hasattr(module, 'PostgresContent'), 'PostgresContent publication contract is required'
    return module.PostgresContent


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def extraction(text='Согласованный бюджет', *, status='complete'):
    segments = (ExtractionSegment('page:1', text),)
    canonical = json.dumps([[s.locator, s.text] for s in segments], ensure_ascii=False, separators=(',', ':'))
    return ExtractionResult(status, segments, digest(canonical), 'fixture-extractor-v1', (), None, 120, False)


def records(text='Согласованный бюджет', *, vector=False):
    prepared = PreparedEmbeddingInput('passage: ' + text, digest('passage: ' + text), 12) if vector else None
    return (ChunkWrite(ChunkDraft(0, 'page:1', text, digest(text), prepared),
                       MODEL.model_key if vector else None, VECTOR if vector else None),)


def seed(connections, *, vector=False):
    apply_catalog_migrations(connections['maintenance'])
    catalog = PostgresCatalog(connections['application'], model=METADATA_MODEL)
    source = catalog.begin_root('C:/fictional/content-fixture')
    catalog.reconcile(source, DirectorySnapshot('', files=(FileMetadata('budget.pdf', 'budget.pdf', 120, 123),)))
    pending, = catalog.pending_embeddings(METADATA_MODEL.model_key, 384, 10)
    assert catalog.save_embeddings((EmbeddingWrite(pending, VECTOR, digest(pending.text)),), METADATA_MODEL.model_key) == 1
    file_id = pending.file.file_id
    content = storage_type()(connections['application'], model=MODEL if vector else None)
    return catalog, source, file_id, content


def prepare(connections, content, file_id, **overrides):
    generation, = sql(connections, "SELECT generation FROM atom_search.catalogs WHERE state='active'")[0]
    revision, = sql(connections, 'SELECT context_revision FROM atom_search.project_context_state WHERE id=1')[0]
    arguments = dict(catalog_generation=generation, profile='balanced', profile_generation=1,
                     context_revision=revision, extractor_version='fixture-extractor-v1', chunker_version='fixture-chunker-v1')
    arguments.update(overrides)
    return content.prepare(file_id, **arguments)


def snapshot(connections):
    return {table: sql(connections, f'SELECT * FROM atom_search.{table} ORDER BY 1,2') for table in TABLES}


def active(connections):
    return sql(connections, '''SELECT st.active_generation,c.text,c.content_generation FROM atom_search.file_content_state st
        JOIN atom_search.content_chunks c ON c.file_id=st.file_id AND c.content_generation=st.active_generation
        ORDER BY c.ordinal''')


def publish(content, token, *, text='Согласованный бюджет', vector=False):
    return content.publish(token, extraction(text), records(text, vector=vector), current_stat=(token.size, token.modified_ns))


def test_schema5_to6_preserves_metadata_public_ids_and_vectors_and_is_idempotent(pg_connections):
    migration = importlib.import_module('atom_search_core.storage.migrations')
    with pg_connections['maintenance']() as connection:
        for version in range(1, 6):
            connection.execute(getattr(migration, f'_SCHEMA_V{version}'))
            if version == 2:
                migration._backfill_v2(connection)
            connection.execute('INSERT INTO atom_search.schema_migrations(version) VALUES (%s)', (version,))
        connection.execute(migration._GRANTS)
    catalog = PostgresCatalog(pg_connections['application'], model=METADATA_MODEL)
    source = catalog.begin_root('C:/fictional/old-v5')
    catalog.reconcile(source, DirectorySnapshot('', files=(FileMetadata('old.pdf', 'old.pdf', 120, 123),)))
    pending, = catalog.pending_embeddings(METADATA_MODEL.model_key, 384, 10)
    catalog.save_embeddings((EmbeddingWrite(pending, VECTOR, digest(pending.text)),), METADATA_MODEL.model_key)
    before = {table: sql(pg_connections, f'SELECT * FROM atom_search.{table} ORDER BY 1')
              for table in ('files', 'sources', 'file_public_ids', 'file_embeddings', 'embedding_models', 'project_context_state')}
    # Deliberately a live migration assertion: v5 code fails here, not at import/collection.
    assert apply_catalog_migrations(pg_connections['maintenance']) == 6
    assert apply_catalog_migrations(pg_connections['maintenance']) == 6
    assert {table: sql(pg_connections, f'SELECT * FROM atom_search.{table} ORDER BY 1') for table in before} == before
    assert all(not values for values in snapshot(pg_connections).values())
    assert sql(pg_connections, 'SELECT version FROM atom_search.schema_migrations ORDER BY version') == [(i,) for i in range(1, 7)]
    for table in TABLES:
        for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
            assert sql(pg_connections, "SELECT has_table_privilege('atom_app',%s,%s)",
                       ('atom_search.' + table, privilege)) == [(True,)]
    assert sql(pg_connections, "SELECT has_sequence_privilege('atom_app','atom_search.content_chunks_id_seq','USAGE')") == [(True,)]
    with pg_connections['application']() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('ALTER TABLE atom_search.content_chunks ADD COLUMN forbidden text')
        connection.rollback()


def test_lexical_only_publication_and_monotonic_desired_generation_survive_new_adapter(pg_connections):
    catalog, source, file_id, content = seed(pg_connections)
    token = prepare(pg_connections, content, file_id)
    result = publish(content, token)
    assert (result.accepted, result.chunks_written, result.generation, result.reason) == (True, 1, token.content_generation, None)
    assert active(pg_connections) == [(token.content_generation, 'Согласованный бюджет', token.content_generation)]
    assert sql(pg_connections, 'SELECT count(*) FROM atom_search.chunk_embeddings') == [(0,)]
    assert sql(pg_connections, "SELECT count(*) FROM atom_search.content_chunks WHERE fts_russian @@ plainto_tsquery('russian','бюджет')") == [(1,)]
    before = sql(pg_connections, 'SELECT * FROM atom_search.file_embeddings')
    other = storage_type()(pg_connections['application'])
    next_token = prepare(pg_connections, other, file_id)
    assert next_token.content_generation > token.content_generation
    rejected = publish(content, token, text='Устаревшее')
    assert not rejected.accepted and rejected.chunks_written == 0
    assert publish(other, next_token, text='Новый бюджет').accepted
    assert active(pg_connections) == [(next_token.content_generation, 'Новый бюджет', next_token.content_generation)]
    assert sql(pg_connections, 'SELECT * FROM atom_search.file_embeddings') == before
    assert catalog.status(scope=catalog.resolve_scope(None))['files'] == 1


@pytest.mark.parametrize('change,reason', [
    ('revision', 'stale_file'), ('size', 'stale_file'), ('mtime', 'stale_file'),
    ('path', 'stale_file'), ('source', 'stale_file'), ('catalog', 'stale_catalog'),
    ('profile', 'stale_profile'), ('context', 'stale_context'), ('cancel', 'cancelled'),
    ('extractor', 'stale_profile'), ('chunker', 'stale_profile'), ('post_size', 'stale_file'), ('post_mtime', 'stale_file')])
def test_stale_publication_preserves_last_good_chunks_and_current_metadata(pg_connections, change, reason):
    catalog, source, file_id, content = seed(pg_connections)
    first = prepare(pg_connections, content, file_id)
    assert publish(content, first).accepted
    token = prepare(pg_connections, content, file_id)
    if change in ('revision', 'size', 'mtime', 'path'):
        assignment = dict(revision='revision=revision+1', size='size=size+1', mtime='modified_ns=modified_ns+1', path="relative_path='renamed.pdf'")[change]
        sql(pg_connections, f'UPDATE atom_search.files SET {assignment} WHERE id=%s', (file_id,))
    elif change == 'source':
        token = replace(token, source_id=source + 100)
    elif change == 'catalog':
        sql(pg_connections, 'UPDATE atom_search.catalogs SET generation=generation+1')
    elif change == 'profile':
        prepare(pg_connections, content, file_id, profile='detailed', profile_generation=2)
    elif change == 'context':
        ProjectProjection(pg_connections['application']).sync((), (), expected_settings_revision=1, source_labels={source: 'Обновлённый'})
    elif change == 'cancel':
        content.cancel_source(source)
    elif change in ('extractor', 'chunker'):
        token = replace(token, **{change + '_version': 'other-version'})
    current_stat = (token.size + (change == 'post_size'), token.modified_ns + (change == 'post_mtime'))
    before = snapshot(pg_connections)
    metadata = sql(pg_connections, 'SELECT * FROM atom_search.files')
    old_vectors = sql(pg_connections, 'SELECT * FROM atom_search.file_embeddings')
    data = replace(extraction('Устаревшее'), extractor_version=token.extractor_version)
    result = content.publish(token, data, records('Устаревшее'), current_stat=current_stat)
    assert (result.accepted, result.chunks_written, result.reason) == (False, 0, reason)
    assert snapshot(pg_connections) == before
    assert active(pg_connections) == [(first.content_generation, 'Согласованный бюджет', first.content_generation)]
    assert sql(pg_connections, 'SELECT * FROM atom_search.files') == metadata
    assert sql(pg_connections, 'SELECT * FROM atom_search.file_embeddings') == old_vectors


@pytest.mark.parametrize('change', ['deleted', 'replaced', 'retired'])
def test_deleted_replaced_or_retired_file_cannot_be_resurrected(pg_connections, change):
    catalog, source, file_id, content = seed(pg_connections)
    token = prepare(pg_connections, content, file_id)
    assert publish(content, token).accepted
    token = prepare(pg_connections, content, file_id)
    if change == 'retired':
        sql(pg_connections, "UPDATE atom_search.catalogs SET state='retired'")
        sql(pg_connections, "INSERT INTO atom_search.catalogs(state,generation) VALUES ('active',0)")
    else:
        catalog.reconcile(source, DirectorySnapshot(''))
        if change == 'replaced':
            catalog.reconcile(source, DirectorySnapshot('', files=(FileMetadata('budget.pdf', 'budget.pdf', 120, 123),)))
            assert sql(pg_connections, 'SELECT id FROM atom_search.files')[0][0] != file_id
    before = snapshot(pg_connections)
    result = publish(content, token)
    assert not result.accepted and result.chunks_written == 0
    assert snapshot(pg_connections) == before


def test_cancel_source_is_durable_and_does_not_cancel_another_source(pg_connections):
    catalog, source, file_id, content = seed(pg_connections)
    first = prepare(pg_connections, content, file_id)
    assert publish(content, first).accepted
    old = prepare(pg_connections, content, file_id)
    other_source = catalog.begin_root('D:/fictional/other-content')
    catalog.reconcile(other_source, DirectorySnapshot('', files=(FileMetadata('other.pdf', 'other.pdf', 120, 123),)))
    other_id = sql(pg_connections, 'SELECT id FROM atom_search.files WHERE source_id=%s', (other_source,))[0][0]
    old = prepare(pg_connections, content, file_id)
    other = prepare(pg_connections, content, other_id)
    content.cancel_source(source)
    restarted = storage_type()(pg_connections['application'])
    assert not publish(restarted, old).accepted
    assert publish(restarted, other).accepted
    assert first.content_generation in [row[0] for row in active(pg_connections)]
    fresh = prepare(pg_connections, restarted, file_id)
    assert fresh.content_generation > old.content_generation
    assert publish(restarted, fresh).accepted


def test_atomic_switch_failure_rolls_back_chunks_cache_and_vectors(pg_connections):
    _, _, file_id, content = seed(pg_connections, vector=True)
    old = prepare(pg_connections, content, file_id)
    assert publish(content, old, vector=True).accepted
    token = prepare(pg_connections, content, file_id)
    sql(pg_connections, '''CREATE FUNCTION atom_search.reject_content_switch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.active_generation IS DISTINCT FROM OLD.active_generation THEN RAISE EXCEPTION 'injected content switch'; END IF;
        RETURN NEW; END $$''')
    sql(pg_connections, '''CREATE TRIGGER reject_content_switch BEFORE UPDATE ON atom_search.file_content_state
        FOR EACH ROW EXECUTE FUNCTION atom_search.reject_content_switch()''')
    before = snapshot(pg_connections)
    with pytest.raises(psycopg.errors.RaiseException, match='injected content switch'):
        publish(content, token, text='Новый бюджет', vector=True)
    assert snapshot(pg_connections) == before
    assert active(pg_connections) == [(old.content_generation, 'Согласованный бюджет', old.content_generation)]
    sql(pg_connections, 'DROP TRIGGER reject_content_switch ON atom_search.file_content_state')
    assert publish(content, token, text='Новый бюджет', vector=True).accepted
    assert active(pg_connections) == [(token.content_generation, 'Новый бюджет', token.content_generation)]


def test_extraction_cache_is_optional_sibling_and_source_delete_cascades_every_generation(pg_connections):
    _, source, file_id, content = seed(pg_connections, vector=True)
    token = prepare(pg_connections, content, file_id)
    assert publish(content, token, vector=True).accepted
    chunks = sql(pg_connections, 'SELECT * FROM atom_search.content_chunks')
    vectors = sql(pg_connections, 'SELECT * FROM atom_search.chunk_embeddings')
    sql(pg_connections, 'DELETE FROM atom_search.content_extractions WHERE file_id=%s', (file_id,), role='application')
    assert sql(pg_connections, 'SELECT * FROM atom_search.content_chunks') == chunks
    assert sql(pg_connections, 'SELECT * FROM atom_search.chunk_embeddings') == vectors
    assert active(pg_connections)
    fresh = prepare(pg_connections, content, file_id)
    assert publish(content, fresh, vector=True).accepted
    sql(pg_connections, 'DELETE FROM atom_search.sources WHERE id=%s', (source,), role='application')
    assert all(not values for values in snapshot(pg_connections).values())
    assert sql(pg_connections, 'SELECT * FROM atom_search.file_embeddings') == []


def test_active_pointer_and_chunk_parent_are_enforced_by_database(pg_connections):
    _, _, file_id, content = seed(pg_connections)
    token = prepare(pg_connections, content, file_id)
    assert publish(content, token).accepted
    before = snapshot(pg_connections)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql(pg_connections, 'UPDATE atom_search.file_content_state SET active_generation=99999 WHERE file_id=%s', (file_id,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql(pg_connections, 'DELETE FROM atom_search.content_generations WHERE file_id=%s', (file_id,))
    assert snapshot(pg_connections) == before
    assert sql(pg_connections, '''SELECT condeferrable FROM pg_constraint WHERE conrelid='atom_search.file_content_state'::regclass
        AND contype='f' AND confrelid='atom_search.content_generations'::regclass''') == [(True,)]


@pytest.mark.parametrize('bad', ['extraction_hash', 'chunk_hash', 'input_hash', 'vector_size', 'vector_nan', 'vector_zero',
                                 'missing_model', 'missing_prepared', 'token_budget', 'duplicate_ordinal', 'unsafe_text',
                                 'oversize', 'extraction_version', 'failed', 'foreign_text'])
def test_invalid_extraction_and_vectors_leave_no_partial_state(pg_connections, bad):
    _, _, file_id, content = seed(pg_connections, vector=True)
    token = prepare(pg_connections, content, file_id)
    data, writes = extraction(), records(vector=True)
    write, = writes
    if bad == 'extraction_hash': data = replace(data, text_hash='0' * 64)
    elif bad == 'chunk_hash': write = replace(write, chunk=replace(write.chunk, text_hash='0' * 64))
    elif bad == 'input_hash': write = replace(write, chunk=replace(write.chunk, prepared_input=replace(write.chunk.prepared_input, input_hash='0' * 64)))
    elif bad == 'vector_size': write = replace(write, vector=(1.,))
    elif bad == 'vector_nan': write = replace(write, vector=(float('nan'),) + VECTOR[1:])
    elif bad == 'vector_zero': write = replace(write, vector=(0.,) * 384)
    elif bad == 'missing_model': write = replace(write, model_key=None)
    elif bad == 'missing_prepared': write = replace(write, chunk=replace(write.chunk, prepared_input=None))
    elif bad == 'token_budget': write = replace(write, chunk=replace(write.chunk, prepared_input=replace(write.chunk.prepared_input, token_count=513)))
    elif bad == 'unsafe_text': data, writes = extraction('bad\x00text'), records('bad\x00text', vector=True)
    elif bad == 'oversize': data, writes = extraction('x' * (8 * 1024 * 1024 + 1)), records('x' * (8 * 1024 * 1024 + 1), vector=True)
    elif bad == 'extraction_version': data = replace(data, extractor_version='other')
    elif bad == 'failed': data = replace(data, status='failed', error_code='parse_failed')
    elif bad == 'foreign_text': writes = records('Чужой текст', vector=True)
    if bad not in ('unsafe_text', 'oversize', 'foreign_text'):
        writes = (write, write) if bad == 'duplicate_ordinal' else (write,)
    before = snapshot(pg_connections)
    with pytest.raises(ValueError):
        content.publish(token, data, writes, current_stat=(120, 123))
    assert snapshot(pg_connections) == before


def test_content_model_registry_rejects_metadata_schema_and_identity_collision(pg_connections):
    _, _, _, content = seed(pg_connections, vector=True)
    before = sql(pg_connections, 'SELECT * FROM atom_search.embedding_models ORDER BY model_key')
    with pytest.raises(ValueError):
        storage_type()(pg_connections['application'], model=METADATA_MODEL)
    with pytest.raises(ValueError):
        storage_type()(pg_connections['application'], model=replace(MODEL, manifest_digest='c' * 64))
    assert sql(pg_connections, 'SELECT * FROM atom_search.embedding_models ORDER BY model_key') == before


@pytest.mark.parametrize('change', ['catalog', 'context', 'cancelled_source', 'profile_downgrade', 'deleted'])
def test_prepare_rejects_stale_owner_admission_without_allocating_generation(pg_connections, change):
    catalog, source, file_id, content = seed(pg_connections)
    prepare(pg_connections, content, file_id, profile_generation=2)
    arguments = dict(profile_generation=2)
    if change == 'catalog': arguments['catalog_generation'] = 0
    elif change == 'context': arguments['context_revision'] = 99
    elif change == 'cancelled_source': catalog.finish_root(source, 'cancelled')
    elif change == 'profile_downgrade': arguments['profile_generation'] = 1
    elif change == 'deleted': catalog.reconcile(source, DirectorySnapshot(''))
    before = snapshot(pg_connections)
    with pytest.raises(ValueError):
        prepare(pg_connections, content, file_id, **arguments)
    assert snapshot(pg_connections) == before


@pytest.mark.parametrize('operation', ['prepare', 'publish', 'cancel'])
def test_content_mutations_wait_for_catalog_fence(pg_connections, operation):
    _, source, file_id, content = seed(pg_connections)
    token = prepare(pg_connections, content, file_id)
    started = threading.Event()
    def action():
        started.set()
        if operation == 'prepare': return prepare(pg_connections, content, file_id)
        if operation == 'publish': return publish(content, token)
        return content.cancel_source(source)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pg_connections['maintenance']() as locked:
            locked.execute("SELECT id FROM atom_search.catalogs WHERE state='active' FOR UPDATE")
            future = pool.submit(action)
            assert started.wait(5)
            # Observe a real server lock wait instead of relying on a scheduling sleep.
            for _ in range(100):
                waiting = locked.execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND pid<>pg_backend_pid()").fetchone()[0]
                if waiting: break
                locked.execute('SELECT pg_sleep(0.01)')
            assert waiting and not future.done()
        future.result(timeout=10)
