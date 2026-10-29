"""Owner-only durable content admission and publication; no document/model I/O.

Extraction SHA-256 is computed over UTF-8 JSON ordered ``[[locator,text],...]``
using ensure_ascii=False and separators=(',', ':'). Chunk hashes cover their
exact UTF-8 text; embedding hashes cover the exact prepared passage input.

All mutations take the active catalog fence first, then the project context
fence and source/file rows. The desired state persists profile, catalog and
context identity as well as the planned versions/fingerprint: a token cannot
relabel previously admitted work. The supplied post-read stat is a host claim;
the host still owns the real before/after read, access and process proofs.
"""
import hashlib
import json

from ..content_models import (ChunkDraft, ChunkWrite, ContentPublishResult,
                              ContentWorkToken, ExtractionResult, ExtractionSegment)
from ..models import EmbeddingModel, PreparedEmbeddingInput
from ..search import normalize
from .postgres import _integer, _path, _vector_literal


_INPUT_SCHEMA = 'content-context-v1'
_PROFILES = ('light', 'balanced', 'detailed')
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_INPUT_BYTES = 100 * 1024 * 1024
_MAX_ROWS = 10000


def _text(value, *, maximum=_MAX_OUTPUT_BYTES, empty=False):
    if not isinstance(value, str) or '\x00' in value or not empty and not value:
        raise ValueError('invalid_content_text')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as error:
        raise ValueError('invalid_content_text') from error
    if size > maximum:
        raise ValueError('content_output_limit')
    return value


def _hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid_content_hash')
    return value


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _version(value):
    _text(value, maximum=256)
    if value.strip() != value:
        raise ValueError('invalid_content_version')
    return value


def _validate_token(token):
    if not isinstance(token, ContentWorkToken):
        raise ValueError('invalid_content_token')
    for value in (token.file_id, token.source_id, token.file_revision, token.content_generation, token.profile_generation):
        _integer(value)
    for value in (token.size, token.catalog_generation, token.context_revision):
        _integer(value, minimum=0)
    _integer(token.modified_ns, minimum=-(2**63))
    _path(token.relative_path)
    _text(token.relative_path, maximum=32768)
    _version(token.extractor_version)
    _version(token.chunker_version)
    if token.profile not in _PROFILES:
        raise ValueError('invalid_content_profile')


def _validate_publication(token, extraction, records, model):
    """Validate the bounded payload before opening a publishing transaction."""
    _validate_token(token)
    if (not isinstance(extraction, ExtractionResult) or extraction.status not in ('complete', 'partial')
            or extraction.extractor_version != token.extractor_version or extraction.error_code is not None
            or type(extraction.truncated) is not bool or not isinstance(extraction.segments, tuple)
            or len(extraction.segments) > _MAX_ROWS or not isinstance(extraction.warnings, tuple)
            or len(extraction.warnings) > 100):
        raise ValueError('invalid_content_extraction')
    _integer(extraction.bytes_read, minimum=0)
    if extraction.bytes_read > _MAX_INPUT_BYTES:
        raise ValueError('content_input_limit')
    for warning in extraction.warnings:
        _text(warning, maximum=1024)
    segments = {}
    pairs = []
    segment_bytes = 0
    for segment in extraction.segments:
        if not isinstance(segment, ExtractionSegment):
            raise ValueError('invalid_content_segment')
        _text(segment.locator, maximum=1024)
        _text(segment.text, empty=True)
        segment_bytes += len(segment.locator.encode('utf-8')) + len(segment.text.encode('utf-8'))
        if segment_bytes > _MAX_OUTPUT_BYTES:
            raise ValueError('content_output_limit')
        if segment.locator in segments:
            raise ValueError('duplicate_content_locator')
        segments[segment.locator] = segment.text
        pairs.append([segment.locator, segment.text])
    canonical = json.dumps(pairs, ensure_ascii=False, separators=(',', ':'))
    _text(canonical)
    if _hash(extraction.text_hash) != _digest(canonical):
        raise ValueError('content_extraction_hash_mismatch')
    if not isinstance(records, tuple) or len(records) > _MAX_ROWS:
        raise ValueError('invalid_content_chunks')
    prepared = []
    ordinals = set()
    total = len(canonical.encode('utf-8'))
    for record in records:
        if not isinstance(record, ChunkWrite) or not isinstance(record.chunk, ChunkDraft):
            raise ValueError('invalid_content_chunk')
        chunk = record.chunk
        _integer(chunk.ordinal, minimum=0)
        if chunk.ordinal >= _MAX_ROWS or chunk.ordinal in ordinals:
            raise ValueError('invalid_content_ordinal')
        ordinals.add(chunk.ordinal)
        _text(chunk.locator, maximum=1024)
        _text(chunk.text)
        if chunk.locator not in segments or chunk.text not in segments[chunk.locator]:
            raise ValueError('content_chunk_outside_extraction')
        if _hash(chunk.text_hash) != _digest(chunk.text):
            raise ValueError('content_chunk_hash_mismatch')
        total += len(chunk.text.encode('utf-8'))
        item = chunk.prepared_input
        if item is not None:
            if not isinstance(item, PreparedEmbeddingInput):
                raise ValueError('invalid_prepared_content')
            _text(item.text)
            if (not item.text.startswith('passage: ') or chunk.text not in item.text
                    or _hash(item.input_hash) != _digest(item.text)
                    or type(item.token_count) is not int or not 1 <= item.token_count <= 512):
                raise ValueError('invalid_prepared_content')
            total += len(item.text.encode('utf-8'))
        if total > _MAX_OUTPUT_BYTES:
            raise ValueError('content_output_limit')
        literal = None
        if record.vector is not None:
            if model is None or record.model_key != model.model_key or item is None:
                raise ValueError('content_embedding_model_mismatch')
            literal = _vector_literal(record.vector)
        elif record.model_key is not None:
            raise ValueError('content_embedding_without_vector')
        prepared.append((record, literal))
    return canonical, prepared


class PostgresContent:
    """Internal owner seam. Admission never authorizes reading a source file.

    prepare() persists a fresh monotonically increasing desired generation.
    Calling it deliberately invalidates older unfinished work; successful
    fingerprint scheduling/deduplication remains the existing owner's concern.
    No production worker, policy, second model queue or storage budget is wired
    by this source slice.
    """

    def __init__(self, connection_factory, *, model=None):
        self._factory = connection_factory
        self._model = model
        if model is not None:
            if (not isinstance(model, EmbeddingModel) or model.dimension != 384
                    or model.input_schema != _INPUT_SCHEMA
                    or not isinstance(model.model_key, str) or not model.model_key.endswith('|' + _INPUT_SCHEMA)):
                raise ValueError('invalid_content_model')
            _text(model.model_key, maximum=256)
            _hash(model.manifest_digest)
            with self._factory() as connection:
                if self._catalog(connection) is None:
                    raise ValueError('stale_catalog')
                connection.execute('''INSERT INTO atom_search.embedding_models(model_key,dimension,input_schema,manifest_digest)
                    VALUES (%s,%s,%s,%s) ON CONFLICT(model_key) DO NOTHING''',
                    (model.model_key, model.dimension, model.input_schema, model.manifest_digest))
                self._check_model(connection)

    @staticmethod
    def _catalog(connection):
        return connection.execute("SELECT id,generation FROM atom_search.catalogs WHERE state='active' FOR UPDATE").fetchone()

    @staticmethod
    def _context(connection):
        return connection.execute('SELECT context_revision FROM atom_search.project_context_state WHERE id=1 FOR UPDATE').fetchone()[0]

    @staticmethod
    def _file(connection, file_id, catalog_id, source_id=None):
        return connection.execute('''SELECT f.source_id,f.relative_path,f.revision,f.size,f.modified_ns,s.status
            FROM atom_search.files f JOIN atom_search.sources s ON s.id=f.source_id
            WHERE f.id=%s AND s.catalog_id=%s AND (%s::bigint IS NULL OR s.id=%s)
            FOR UPDATE OF s,f''', (file_id, catalog_id, source_id, source_id)).fetchone()

    def _check_model(self, connection):
        model = self._model
        if model is not None:
            row = connection.execute('''SELECT dimension,input_schema,manifest_digest FROM atom_search.embedding_models
                WHERE model_key=%s''', (model.model_key,)).fetchone()
            if row != (384, _INPUT_SCHEMA, model.manifest_digest):
                raise ValueError('content_model_identity_collision')

    def prepare(self, file_id, *, catalog_generation, profile, profile_generation,
                context_revision, extractor_version, chunker_version):
        """Stage work only against the current active source/file/configuration."""
        _integer(file_id)
        _integer(catalog_generation, minimum=0)
        _integer(context_revision, minimum=0)
        _integer(profile_generation)
        _version(extractor_version)
        _version(chunker_version)
        if profile not in _PROFILES:
            raise ValueError('invalid_content_profile')
        with self._factory() as connection:
            catalog = self._catalog(connection)
            if catalog is None or catalog[1] != catalog_generation:
                raise ValueError('stale_catalog')
            if self._context(connection) != context_revision:
                raise ValueError('stale_context')
            file = self._file(connection, file_id, catalog[0])
            if file is None:
                raise ValueError('stale_file')
            if file[5] == 'cancelled':
                raise ValueError('cancelled')
            previous = connection.execute('''SELECT desired_generation,active_generation,profile_generation,profile
                FROM atom_search.file_content_state WHERE file_id=%s FOR UPDATE''', (file_id,)).fetchone()
            if previous is not None and (profile_generation < previous[2]
                    or profile_generation == previous[2] and profile != previous[3]):
                raise ValueError('stale_profile')
            generation = _integer(previous[0] + 1 if previous else 1)
            if previous:
                connection.execute("""UPDATE atom_search.content_generations SET state='cancelled'
                    WHERE file_id=%s AND state='pending'""", (file_id,))
            connection.execute('''INSERT INTO atom_search.content_generations
                (file_id,content_generation,file_revision,profile_generation,state) VALUES (%s,%s,%s,%s,'pending')''',
                (file_id, generation, file[2], profile_generation))
            connection.execute('''INSERT INTO atom_search.file_content_state(file_id,desired_generation,active_generation,
                profile_generation,extractor_version,chunker_version,status,source_size,source_modified_ns,text_hash,
                last_error,profile,catalog_generation,context_revision)
                VALUES (%s,%s,NULL,%s,%s,%s,'queued',%s,%s,NULL,NULL,%s,%s,%s)
                ON CONFLICT(file_id) DO UPDATE SET desired_generation=excluded.desired_generation,
                profile_generation=excluded.profile_generation,extractor_version=excluded.extractor_version,
                chunker_version=excluded.chunker_version,status='queued',source_size=excluded.source_size,
                source_modified_ns=excluded.source_modified_ns,last_error=NULL,profile=excluded.profile,
                catalog_generation=excluded.catalog_generation,context_revision=excluded.context_revision''',
                (file_id, generation, profile_generation, extractor_version, chunker_version,
                 file[3], file[4], profile, catalog_generation, context_revision))
            return ContentWorkToken(file_id, file[0], file[1], file[2], file[3], file[4], catalog_generation,
                                    generation, profile_generation, context_revision, extractor_version, chunker_version, profile)

    def cancel_source(self, source_id):
        """Invalidate only this active source's desired work; retain last good data."""
        _integer(source_id)
        with self._factory() as connection:
            catalog = self._catalog(connection)
            if catalog is None:
                return
            self._context(connection)
            source = connection.execute('''SELECT id FROM atom_search.sources
                WHERE id=%s AND catalog_id=%s FOR UPDATE''', (source_id, catalog[0])).fetchone()
            if source is None:
                return
            states = connection.execute('''SELECT st.file_id,st.desired_generation FROM atom_search.file_content_state st
                JOIN atom_search.files f ON f.id=st.file_id WHERE f.source_id=%s ORDER BY f.id FOR UPDATE OF f,st''',
                (source_id,)).fetchall()
            for file_id, desired in states:
                next_generation = _integer(desired + 1)
                connection.execute("""UPDATE atom_search.content_generations SET state='cancelled'
                    WHERE file_id=%s AND state='pending'""", (file_id,))
                connection.execute("""UPDATE atom_search.file_content_state SET desired_generation=%s,status='cancelled'
                    WHERE file_id=%s""", (next_generation, file_id))

    def publish(self, token, extraction, records, *, current_stat):
        """Publish an entire generation, or leave every old row unchanged."""
        canonical, prepared = _validate_publication(token, extraction, records, self._model)
        if not isinstance(current_stat, tuple) or len(current_stat) != 2:
            raise ValueError('invalid_content_stat')
        _integer(current_stat[0], minimum=0)
        _integer(current_stat[1], minimum=-(2**63))

        def rejected(reason):
            return ContentPublishResult(False, 0, token.content_generation, reason)

        if current_stat != (token.size, token.modified_ns):
            return rejected('stale_file')
        with self._factory() as connection:
            catalog = self._catalog(connection)
            if catalog is None:
                return rejected('stale_catalog')
            # Context updates also advance the catalog generation. Report their
            # stronger fence first, while observing the shared writer lock order.
            if self._context(connection) != token.context_revision:
                return rejected('stale_context')
            file = self._file(connection, token.file_id, catalog[0], token.source_id)
            if file is None or file[1:5] != (token.relative_path, token.file_revision, token.size, token.modified_ns):
                return rejected('stale_file')
            if file[5] == 'cancelled':
                return rejected('cancelled')
            if catalog[1] != token.catalog_generation:
                return rejected('stale_catalog')
            state = connection.execute('''SELECT desired_generation,active_generation,profile_generation,extractor_version,
                chunker_version,status,source_size,source_modified_ns,profile,catalog_generation,context_revision
                FROM atom_search.file_content_state WHERE file_id=%s FOR UPDATE''', (token.file_id,)).fetchone()
            if state is None:
                return rejected('stale_file')
            if state[5] == 'cancelled':
                return rejected('cancelled')
            if (state[0], state[2], state[3], state[4], state[8]) != (
                    token.content_generation, token.profile_generation, token.extractor_version, token.chunker_version, token.profile):
                return rejected('stale_profile')
            if state[9] != token.catalog_generation:
                return rejected('stale_catalog')
            if state[10] != token.context_revision:
                return rejected('stale_context')
            if state[6:8] != (token.size, token.modified_ns):
                return rejected('stale_file')
            parent = connection.execute('''SELECT file_revision,profile_generation,state FROM atom_search.content_generations
                WHERE file_id=%s AND content_generation=%s FOR UPDATE''', (token.file_id, token.content_generation)).fetchone()
            if state[5] != 'queued' or parent != (token.file_revision, token.profile_generation, 'pending'):
                return rejected('stale_profile')
            self._check_model(connection)
            connection.execute('''INSERT INTO atom_search.content_extractions
                (file_id,content_generation,extractor_version,text_hash,segments_json) VALUES (%s,%s,%s,%s,%s::jsonb)''',
                (token.file_id, token.content_generation, token.extractor_version, extraction.text_hash, canonical))
            for record, literal in prepared:
                chunk = record.chunk
                item = chunk.prepared_input
                chunk_id = connection.execute('''INSERT INTO atom_search.content_chunks(file_id,file_revision,content_generation,
                    profile_generation,extractor_version,chunker_version,ordinal,locator,text,text_hash,input_hash,input_schema,
                    context_revision,fts_russian,fts_simple) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    to_tsvector('russian',%s),to_tsvector('simple',%s)) RETURNING id''',
                    (token.file_id, token.file_revision, token.content_generation, token.profile_generation,
                     token.extractor_version, token.chunker_version, chunk.ordinal, chunk.locator, chunk.text, chunk.text_hash,
                     item.input_hash if item else None, _INPUT_SCHEMA if item else None, token.context_revision,
                     normalize(chunk.text), normalize(chunk.text))).fetchone()[0]
                if literal is not None:
                    connection.execute('''INSERT INTO atom_search.chunk_embeddings(chunk_id,model_key,embedding,input_hash)
                        VALUES (%s,%s,%s::public.vector,%s)''', (chunk_id, record.model_key, literal, item.input_hash))
            if state[1] is not None:
                connection.execute("""UPDATE atom_search.content_generations SET state='retired'
                    WHERE file_id=%s AND content_generation=%s""", (token.file_id, state[1]))
            connection.execute("""UPDATE atom_search.content_generations SET state='active'
                WHERE file_id=%s AND content_generation=%s""", (token.file_id, token.content_generation))
            connection.execute('''UPDATE atom_search.file_content_state SET active_generation=%s,status=%s,text_hash=%s,
                last_error=NULL WHERE file_id=%s''', (token.content_generation, extraction.status, extraction.text_hash, token.file_id))
            return ContentPublishResult(True, len(prepared), token.content_generation, None)
