"""Read-only SQLite backup and maintenance-only, resumable catalog migration.

The owner holds its existing StorageReservation from BEFORE backup through
activation. Original SQLite/WAL, snapshot (retained as the single bounded backup)
and PostgreSQL allocations must share the owner's accounting domain. Already
allocated bytes are counted there; callbacks here reserve only future peak.
No source scanning, source cleanup, Runtime ownership or optional imports occur.
"""
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
import time
from typing import Any, Literal

from ..search import compact, normalize
from ..source import canonical_root
from .public_ids import ensure_file_id, transfer_catalog_ids


_VERSION = 3
_IMPORT_LOCK = 0x41544F4D494D50
_INPUT = 'filename-parent-v1'
_MARGIN = 128 * 1024**2
_REGISTRY = ('catalog_id,last_file_id,state,validated,target_model_key,'
             'target_manifest_digest,history_fingerprint,files_imported,vectors_reused')


@dataclass(frozen=True)
class MigrationReport:
    status: Literal['staged', 'active', 'failed']
    source_digest: str
    catalog_id: int | None
    files: int
    vectors_reused: int
    vectors_pending: int
    error_code: str | None


class _ImportError(ValueError):
    """Only fixed machine codes cross the report boundary."""


def _sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _snapshot_digest(path):
    # The import identity hashes the completed backup, never a main-file-only
    # view of a live WAL producer. The owner must retain exclusive ownership of
    # this snapshot throughout staging; check before reading and before commit.
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise _ImportError('legacy_snapshot_not_frozen')
    return _sha256(path)


def _json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _reader(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _check_sqlite(connection):
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version not in (1, 2):
        raise _ImportError('legacy_schema_unsupported')
    if [row[0] for row in connection.execute('PRAGMA integrity_check')] != ['ok']:
        raise _ImportError('legacy_integrity_failed')
    required = {
        'roots': {'id', 'path', 'last_attempt', 'last_success', 'status', 'error'},
        'files': {'id', 'root_id', 'relative_path', 'parent', 'name', 'size', 'modified_ns',
                  'compact_name', 'compact_path', 'embedding', 'embedding_model', 'input_hash'},
    }
    if version == 2:
        required['files'].add('compact_stem')
        required['directories'] = {'root_id', 'relative_path', 'parent'}
        required['catalog_state'] = {'name', 'value'}
    for table, columns in required.items():
        actual = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
        if not columns <= actual:
            raise _ImportError('legacy_schema_invalid')
    if connection.execute('''SELECT 1 FROM files f LEFT JOIN roots r ON r.id=f.root_id
        WHERE r.id IS NULL LIMIT 1''').fetchone():
        raise _ImportError('legacy_rows_invalid')
    if version == 2 and connection.execute('''SELECT 1 FROM directories d LEFT JOIN roots r ON r.id=d.root_id
        WHERE r.id IS NULL LIMIT 1''').fetchone():
        raise _ImportError('legacy_rows_invalid')
    return version


def snapshot_legacy_database(source: Path, destination: Path) -> str:
    """Backup a live WAL database, never overwrite a caller's existing path.

    Read-only SQLite may access SHM bookkeeping; it does not checkpoint, change
    journal mode, mutate source documents or use immutable=1. The owner reserves
    space before calling and retains the successful snapshot as its bounded backup.
    """
    source, destination = Path(source), Path(destination)
    # Exclusive create also refuses source==destination and hardlink aliases.
    with destination.open('xb'):
        pass
    started = time.monotonic()
    last_progress, copied_pages = started, 0

    def progress(_status, remaining, total):
        nonlocal last_progress, copied_pages
        now = time.monotonic()
        copied = total - remaining
        if copied > copied_pages:
            last_progress = now
        if now - last_progress > 15 or now - started > 300:
            raise _ImportError('legacy_snapshot_busy')
        copied_pages = copied

    try:
        with closing(_reader(source)) as old, closing(sqlite3.connect(destination)) as new:
            old.backup(new, pages=256, progress=progress, sleep=.05)
        with closing(_reader(destination)) as check:
            _check_sqlite(check)
        return _sha256(destination)
    except BaseException:
        # Only the file exclusively created above belongs to this operation.
        destination.unlink(missing_ok=True)
        raise


def _encoding(manifest):
    """Relevant producer identity only; prose is not historical provenance."""
    try:
        assets = {item['path']: item['sha256'] for item in manifest['files']}
        onnx, tokenizer = assets[manifest['onnx_file']], assets[manifest['tokenizer_file']]
        revision, key, dimension = manifest['revision'], manifest['model_id'], manifest['dimension']
        if (not isinstance(revision, str) or not revision or not isinstance(key, str) or not key
                or type(dimension) is not int or any(not isinstance(h, str) or len(h) != 64
                    or any(c not in '0123456789abcdef' for c in h) for h in (onnx, tokenizer))):
            return None
        return (key, revision, onnx, tokenizer, dimension, _INPUT)
    except (KeyError, TypeError):
        return None


def _target(manifest):
    if manifest is None:
        return None, None, None
    encoding = _encoding(manifest)
    if encoding is None or encoding[4] != 384:
        raise _ImportError('target_manifest_invalid')
    return encoding[0] + '|' + _INPUT, _json_digest(manifest), encoding


def _report(digest, row=None, error=None):
    count, reused = (row[7], row[8]) if row else (0, 0)
    status = 'failed' if error else ('active' if row[2] == 'active' else 'staged')
    return MigrationReport(status, digest, row[0] if row else None, count, reused, count - reused, error)


def _load(connection, digest):
    return connection.execute(f'''SELECT {_REGISTRY} FROM atom_search.legacy_imports
        WHERE snapshot_digest=%s AND target_schema_version=%s''', (digest, _VERSION)).fetchone()


def _reserve(reserve_bytes, legacy, checkpoint, *, include_sources=False):
    # 32 KiB per row covers tuples/maps, index entries and WAL amplification;
    # large UTF-8 metadata and embeddings additionally scale the bound. The
    # runtime's 128 MiB margin remains reserved through ANALYZE and activation.
    rows, payload = legacy.execute('''SELECT count(*),COALESCE(sum(
        length(CAST(relative_path AS BLOB))+length(CAST(name AS BLOB))+
        COALESCE(length(embedding),0)),0) FROM files WHERE ? IS NULL OR id>?''',
        (checkpoint, checkpoint)).fetchone()
    metadata = 0
    if include_sources:
        metadata = legacy.execute('SELECT COALESCE(sum(length(CAST(path AS BLOB))+4096),0) FROM roots').fetchone()[0]
        if legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='directories'").fetchone():
            metadata += legacy.execute('SELECT COALESCE(sum(length(CAST(relative_path AS BLOB))+4096),0) FROM directories').fetchone()[0]
    try:
        reserve_bytes(_MARGIN + rows * 32768 + payload * 8 + metadata * 8)
    except Exception:
        raise _ImportError('storage_reservation_failed') from None


def _path(value, *, empty=False):
    if (not isinstance(value, str) or '\x00' in value or '\\' in value
            or (value != '' or not empty) and any(part in ('', '.', '..') for part in value.split('/'))):
        raise _ImportError('legacy_rows_invalid')
    return value


def _copy_sources(connection, legacy, digest, catalog_id, version):
    for source in legacy.execute('SELECT * FROM roots ORDER BY id'):
        path = canonical_root(source['path'])
        new_id = connection.execute('''INSERT INTO atom_search.sources
            (catalog_id,path,last_attempt,last_success,status,error) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
            (catalog_id, path, source['last_attempt'], source['last_success'], source['status'], source['error'])).fetchone()[0]
        connection.execute('INSERT INTO atom_search.legacy_source_map VALUES (%s,%s,%s,%s)',
                           (digest, _VERSION, source['id'], new_id))
        if version == 2:
            for directory in legacy.execute('SELECT relative_path FROM directories WHERE root_id=?', (source['id'],)):
                connection.execute('INSERT INTO atom_search.directories VALUES (%s,%s)',
                                   (new_id, _path(directory[0], empty=True)))


def _reusable(row, model_key, reuse):
    if not reuse or row['embedding_model'] != model_key:
        return None
    parent = row['relative_path'].rpartition('/')[0]
    passage = row['name'] + (' | ' + parent.replace('/', ' / ') if parent else '')
    if row['input_hash'] != hashlib.sha256(passage.encode('utf-8')).hexdigest():
        return None
    blob = row['embedding']
    if not isinstance(blob, bytes) or len(blob) != 1536:
        return None
    vector = struct.unpack('<384f', blob)
    if not all(math.isfinite(value) for value in vector) or not any(vector):
        return None
    return '[' + ','.join(repr(value) for value in vector) + ']'


def _copy_batch(connection, batch, digest, model_key, reuse):
    reused = 0
    for row in batch:
        path, name = _path(row['relative_path']), _path(row['name'])
        if '/' in name or path.rsplit('/', 1)[-1] != name:
            raise _ImportError('legacy_rows_invalid')
        if type(row['size']) is not int or row['size'] < 0 or type(row['modified_ns']) is not int:
            raise _ImportError('legacy_rows_invalid')
        source = connection.execute('''SELECT new_source_id FROM atom_search.legacy_source_map
            WHERE snapshot_digest=%s AND target_schema_version=%s AND legacy_source_id=%s''',
            (digest, _VERSION, row['root_id'])).fetchone()
        if source is None:
            raise _ImportError('legacy_rows_invalid')
        new_id = connection.execute('''INSERT INTO atom_search.files
            (source_id,relative_path,parent,name,size,modified_ns,revision,compact_name,compact_stem,
             compact_path,fts_russian,fts_simple) VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,
             setweight(to_tsvector('russian',%s),'A') || setweight(to_tsvector('russian',%s),'B'),
             setweight(to_tsvector('simple',%s),'A') || setweight(to_tsvector('simple',%s),'B')) RETURNING id''',
            (source[0], path, path.rpartition('/')[0], name, row['size'], row['modified_ns'],
             compact(name), compact(name.rsplit('.', 1)[0]), compact(path),
             normalize(name), normalize(path), normalize(name), normalize(path))).fetchone()[0]
        ensure_file_id(connection, new_id)
        literal = _reusable(row, model_key, reuse)
        if literal is not None:
            connection.execute('''INSERT INTO atom_search.file_embeddings
                (file_id,model_key,file_revision,input_hash,embedding) VALUES (%s,%s,1,%s,%s::public.vector)''',
                (new_id, model_key, row['input_hash'], literal))
            reused += 1
        connection.execute('INSERT INTO atom_search.legacy_file_map VALUES (%s,%s,%s,%s)',
                           (digest, _VERSION, row['id'], new_id))
    connection.execute('''UPDATE atom_search.legacy_imports SET last_file_id=%s,
        files_imported=files_imported+%s,vectors_reused=vectors_reused+%s
        WHERE snapshot_digest=%s AND target_schema_version=%s''',
        (batch[-1]['id'], len(batch), reused, digest, _VERSION))


def _validate(connection, legacy, digest, catalog_id):
    for source in legacy.execute('SELECT id FROM roots'):
        expected = legacy.execute('SELECT count(*),count(DISTINCT relative_path) FROM files WHERE root_id=?', (source[0],)).fetchone()
        actual = connection.execute('''SELECT count(f.id),count(DISTINCT f.relative_path),count(m.new_file_id)
            FROM atom_search.legacy_source_map s LEFT JOIN atom_search.files f ON f.source_id=s.new_source_id
            LEFT JOIN atom_search.legacy_file_map m ON m.new_file_id=f.id
                AND m.snapshot_digest=s.snapshot_digest AND m.target_schema_version=s.target_schema_version
            WHERE s.snapshot_digest=%s AND s.target_schema_version=%s AND s.legacy_source_id=%s''',
            (digest, _VERSION, source[0])).fetchone()
        if tuple(expected) != (expected[0], expected[0]) or actual != (expected[0],) * 3:
            raise _ImportError('legacy_validation_failed')
    total = legacy.execute('SELECT count(*) FROM files').fetchone()[0]
    if _load(connection, digest)[7] != total:
        raise _ImportError('legacy_validation_failed')
    connection.execute('ANALYZE atom_search.files')
    connection.execute('ANALYZE atom_search.file_embeddings')
    connection.execute('ANALYZE atom_search.sources')
    connection.execute('''UPDATE atom_search.legacy_imports SET state='validated',validated=true
        WHERE snapshot_digest=%s AND target_schema_version=%s AND catalog_id=%s''', (digest, _VERSION, catalog_id))


def stage_legacy_catalog(snapshot: Path, maintenance_factory: Callable[[], Any], *,
                         model_manifest: dict[str, Any] | None, reserve_bytes: Callable[[int], None],
                         legacy_model_manifest: dict[str, Any] | None = None) -> MigrationReport:
    """Copy batches of 1000; checkpoint/maps/counts commit with each batch.

    The snapshot must be the completed owner-held backup, not a live producer.
    Non-None historical manifest is the caller's attestation that it belongs to
    this producer/profile. Core checks identities, but cannot prove association.
    Absent history never permits reuse. Explicit target None imports metadata
    only. Both choices persist across retries; target changes fail closed.
    """
    digest, registry = '', None
    try:
        digest = _snapshot_digest(snapshot)
        model_key, model_digest, encoding = _target(model_manifest)
        history = _encoding(legacy_model_manifest) if encoding is not None else None
        fingerprint = _json_digest(history) if history is not None else None
        with closing(_reader(snapshot)) as legacy:
            legacy.execute('BEGIN')
            version = _check_sqlite(legacy)
            if _snapshot_digest(snapshot) != digest:
                raise _ImportError('legacy_snapshot_changed')
            with maintenance_factory() as connection:
                # A session lock spans batch commits and is released on disconnect
                # (including process death). Activation uses the same lock.
                connection.execute('SELECT pg_advisory_lock(%s)', (_IMPORT_LOCK,))
                try:
                    # Registry target 3 records the historical import contract.
                    # Additive schemas 4/5/6 preserve this v3/v1 import contract.
                    if connection.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone()[0] not in (4, 5, 6):
                        raise _ImportError('target_schema_unsupported')
                    registry = _load(connection, digest)
                    if registry:
                        if registry[4:6] != (model_key, model_digest):
                            raise _ImportError('target_identity_conflict')
                        if registry[2] in ('active', 'validated'):
                            return _report(digest, registry)
                        if registry[6] is not None and registry[6] != fingerprint:
                            raise _ImportError('legacy_provenance_conflict')
                        # Original None cannot be upgraded on resume.
                        if registry[6] is None:
                            history = None
                    _reserve(reserve_bytes, legacy, registry[1] if registry else None, include_sources=registry is None)
                    if not registry:
                        if model_key is not None:
                            connection.execute('''INSERT INTO atom_search.embedding_models VALUES (%s,384,%s,%s)
                                ON CONFLICT(model_key) DO NOTHING''', (model_key, _INPUT, model_digest))
                            actual = connection.execute('SELECT dimension,input_schema,manifest_digest FROM atom_search.embedding_models WHERE model_key=%s', (model_key,)).fetchone()
                            if actual != (384, _INPUT, model_digest):
                                raise _ImportError('target_identity_conflict')
                        catalog_id = connection.execute("INSERT INTO atom_search.catalogs(state) VALUES ('staging') RETURNING id").fetchone()[0]
                        connection.execute('''INSERT INTO atom_search.legacy_imports
                            (snapshot_digest,target_schema_version,catalog_id,state,target_model_key,target_manifest_digest,history_fingerprint)
                            VALUES (%s,%s,%s,'copying',%s,%s,%s)''',
                            (digest, _VERSION, catalog_id, model_key, model_digest, fingerprint))
                        _copy_sources(connection, legacy, digest, catalog_id, version)
                    else:
                        connection.execute("UPDATE atom_search.legacy_imports SET state='copying',validated=false WHERE snapshot_digest=%s AND target_schema_version=%s", (digest, _VERSION))
                    connection.commit()
                    registry = _load(connection, digest)
                    while True:
                        batch = legacy.execute('SELECT * FROM files WHERE ? IS NULL OR id>? ORDER BY id LIMIT 1000', (registry[1], registry[1])).fetchall()
                        if not batch:
                            break
                        _reserve(reserve_bytes, legacy, registry[1])
                        _copy_batch(connection, batch, digest, model_key, history is not None and history == encoding)
                        connection.commit()
                        registry = _load(connection, digest)
                    _reserve(reserve_bytes, legacy, registry[1])
                    if _snapshot_digest(snapshot) != digest:
                        raise _ImportError('legacy_snapshot_changed')
                    _validate(connection, legacy, digest, registry[0])
                    connection.commit()
                    return _report(digest, _load(connection, digest))
                except Exception as error:
                    connection.rollback()
                    registry = _load(connection, digest)
                    # A conflicting retry must not rewrite the accepted decision.
                    if registry and registry[2] not in ('active', 'validated'):
                        connection.execute("UPDATE atom_search.legacy_imports SET state='failed',validated=false WHERE snapshot_digest=%s AND target_schema_version=%s", (digest, _VERSION))
                        connection.commit()
                    code = str(error) if isinstance(error, _ImportError) else 'legacy_import_failed'
                    return _report(digest, registry, code)
                finally:
                    connection.execute('SELECT pg_advisory_unlock(%s)', (_IMPORT_LOCK,))
    except Exception as error:
        code = str(error) if isinstance(error, _ImportError) else 'legacy_import_failed'
        return _report(digest, registry, code)


def activate_catalog(catalog_id: int, maintenance_factory: Callable[[], Any]) -> None:
    """Atomically switch only a validated import, under the shared import lock.

    Lock active catalog before import/target rows, respecting live writer order.
    A repeated activation of the current active import is harmless. Retired
    imports cannot be reactivated by replay. The owner still holds reservation.
    """
    if type(catalog_id) is not int or catalog_id <= 0:
        raise ValueError('legacy_catalog_invalid')
    with maintenance_factory() as connection:
        connection.execute('SELECT pg_advisory_xact_lock(%s)', (_IMPORT_LOCK,))
        active = connection.execute("SELECT id FROM atom_search.catalogs WHERE state='active' FOR UPDATE").fetchone()
        row = connection.execute('''SELECT i.state,i.validated,c.state FROM atom_search.legacy_imports i
            JOIN atom_search.catalogs c ON c.id=i.catalog_id WHERE i.catalog_id=%s FOR UPDATE OF i,c''', (catalog_id,)).fetchone()
        if row == ('active', True, 'active') and active == (catalog_id,):
            return
        if row != ('validated', True, 'staging'):
            raise ValueError('legacy_catalog_not_validated')
        if active is not None:
            transfer_catalog_ids(connection, active[0], catalog_id)
        connection.execute("UPDATE atom_search.catalogs SET state='retired' WHERE state='active'")
        connection.execute("UPDATE atom_search.catalogs SET state='active' WHERE id=%s", (catalog_id,))
        connection.execute("UPDATE atom_search.legacy_imports SET state='active' WHERE catalog_id=%s", (catalog_id,))
