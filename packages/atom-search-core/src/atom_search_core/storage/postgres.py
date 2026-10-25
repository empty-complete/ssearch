"""Atomic metadata catalog; no runtime ownership or retrieval implementation.

Every mutation locks the active catalog before source/file rows. This intentionally
conservative order serializes reconciliation and embedding CAS, including across
adapter instances, and prevents activation from racing authorization.
"""
from collections.abc import Callable, Sequence
from contextlib import contextmanager
import hashlib
import math
from numbers import Real
import struct
import threading
from typing import Any

from ..models import (CatalogFile, DirectorySnapshot, EmbeddingInput, EmbeddingModel,
                      EmbeddingWrite, ReconcileCounts, SearchScope)
from ..search import compact, normalize
from ..source import canonical_root
from ..project_context import INPUT_SCHEMA
from .public_ids import ensure_file_id


_MAX_INT64 = 2**63 - 1
_SCHEMA = "filename-parent-v1"
_STATUSES = {"new", "scanning", "complete", "partial", "unavailable", "cancelled"}
_FILE_COLUMNS = "f.id,f.source_id,s.path,f.relative_path,f.name,f.size,f.modified_ns,s.status,f.revision"


def _integer(value: int, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_INT64:
        raise ValueError("Value is outside the required int64 range")
    return value


def _path(value: str, *, root: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("Expected a normalized relative path")
    if value == "" and root:
        return value
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Expected a normalized relative path")
    return value


def _name(value: str) -> str:
    _path(value)
    if "/" in value:
        raise ValueError("Expected an immediate basename")
    return value


def _passage(file: CatalogFile) -> str:
    parent = file.relative_path.rpartition("/")[0]
    return file.name + (" | " + parent.replace("/", " / ") if parent else "")


def _vector_literal(vector: Sequence[float]) -> str:
    if len(vector) != 384 or any(not isinstance(value, Real) or isinstance(value, bool) for value in vector):
        raise ValueError("Embedding must contain 384 real numbers")
    try:
        values = struct.unpack("<384f", struct.pack("<384f", *vector))
    except (OverflowError, struct.error, TypeError) as error:
        raise ValueError("Embedding is not representable as float32") from error
    if not all(math.isfinite(value) for value in values) or not any(values):
        raise ValueError("Embedding must have a finite, nonzero float32 norm")
    return "[" + ",".join(repr(value) for value in values) + "]"


class PostgresCatalog:
    def __init__(self, connection_factory: Callable[[], Any], *, model: EmbeddingModel | None = None):
        self._factory = connection_factory
        self._model = model
        self._lock = threading.RLock()
        self._closed = False
        if model is not None:
            if (model.dimension != 384 or model.input_schema not in (_SCHEMA, INPUT_SCHEMA) or not model.model_key
                    or model.input_schema == INPUT_SCHEMA and not model.model_key.endswith('|' + INPUT_SCHEMA)
                    or not isinstance(model.manifest_digest, str) or len(model.manifest_digest) != 64
                    or any(char not in "0123456789abcdef" for char in model.manifest_digest)):
                raise ValueError("Unsupported embedding model identity")
            with self._connection() as connection:
                self._active(connection)
                connection.execute("""INSERT INTO atom_search.embedding_models
                    (model_key,dimension,input_schema,manifest_digest) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(model_key) DO NOTHING""",
                                   (model.model_key, model.dimension, model.input_schema, model.manifest_digest))
                self._check_model(connection)

    @contextmanager
    def _connection(self):
        # Operations own and close their connection; close() waits only for this
        # adapter's operations, never stops the caller's shared runtime.
        with self._lock:
            if self._closed:
                raise ValueError("Catalog is closed")
            with self._factory() as connection:
                yield connection

    @staticmethod
    def _active(connection):
        row = connection.execute("SELECT id FROM atom_search.catalogs WHERE state='active' FOR UPDATE").fetchone()
        if row is None:
            raise ValueError("No active catalog")
        return row[0]

    @staticmethod
    def _source(connection, catalog_id, root_id):
        row = connection.execute("""SELECT s.id FROM atom_search.sources s
            JOIN atom_search.catalogs c ON c.id=s.catalog_id
            WHERE s.id=%s AND c.id=%s AND c.state='active' FOR UPDATE OF s""", (root_id, catalog_id)).fetchone()
        if row is None:
            raise ValueError("Source is not in the active catalog")

    def _check_model(self, connection):
        model = self._model
        row = connection.execute("""SELECT dimension,input_schema,manifest_digest
            FROM atom_search.embedding_models WHERE model_key=%s""", (model.model_key,)).fetchone()
        if row != (model.dimension, model.input_schema, model.manifest_digest):
            raise ValueError("Embedding model identity collision")

    def _model_for(self, model_key, dimension=384):
        if self._model is None or (model_key, dimension) != (self._model.model_key, self._model.dimension):
            raise ValueError("Embedding model is not registered on this catalog")
        return self._model

    def begin_root(self, root: str) -> int:
        path = canonical_root(root)
        with self._connection() as connection:
            catalog_id = self._active(connection)
            return connection.execute("""INSERT INTO atom_search.sources(catalog_id,path,last_attempt,status)
                VALUES (%s,%s,now(),'scanning') ON CONFLICT(catalog_id,path)
                DO UPDATE SET last_attempt=now(),status='scanning',error=NULL RETURNING id""",
                                      (catalog_id, path)).fetchone()[0]

    def finish_root(self, root_id: int, status: str, message: str = "") -> None:
        _integer(root_id)
        if status not in _STATUSES:
            raise ValueError("Unknown source status")
        with self._connection() as connection:
            catalog_id = self._active(connection)
            self._source(connection, catalog_id, root_id)
            connection.execute("""UPDATE atom_search.sources SET status=%s,error=%s,
                last_success=CASE WHEN %s='complete' THEN now() ELSE last_success END WHERE id=%s""",
                               (status, message, status, root_id))

    def reconcile(self, root_id: int, snapshot: DirectorySnapshot) -> ReconcileCounts:
        _integer(root_id)
        path = _path(snapshot.relative_path, root=True)
        prefix = path + "/" if path else ""
        observed = {}
        directories = {_name(name) for name in snapshot.directories}
        protected = {_name(name) for name in snapshot.protected_names}
        for file in snapshot.files:
            _path(file.relative_path)
            _name(file.name)
            if file.relative_path != prefix + file.name or file.name in observed or file.name in directories:
                raise ValueError("Snapshot files must be unique immediate children")
            _integer(file.size, minimum=0)
            _integer(file.modified_ns, minimum=-(2**63))
            observed[file.name] = file
        with self._connection() as connection:
            catalog_id = self._active(connection)
            self._source(connection, catalog_id, root_id)
            connection.execute("""INSERT INTO atom_search.directories(source_id,relative_path)
                VALUES (%s,%s) ON CONFLICT DO NOTHING""", (root_id, path))
            for name in sorted(directories):
                connection.execute("""INSERT INTO atom_search.directories(source_id,relative_path)
                    VALUES (%s,%s) ON CONFLICT DO NOTHING""", (root_id, prefix + name))
            added = updated = removed = 0
            for file in observed.values():
                current = connection.execute("""SELECT id,name,size,modified_ns FROM atom_search.files
                    WHERE source_id=%s AND relative_path=%s FOR UPDATE""", (root_id, file.relative_path)).fetchone()
                compact_name = compact(file.name)
                compact_stem = compact(file.name.rsplit(".", 1)[0])
                search_values = (compact(file.relative_path), normalize(file.name), normalize(file.relative_path),
                                 normalize(file.name), normalize(file.relative_path))
                if current is None:
                    file_id = connection.execute("""INSERT INTO atom_search.files
                        (source_id,relative_path,parent,name,size,modified_ns,revision,compact_name,compact_stem,
                         compact_path,fts_russian,fts_simple)
                        VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,
                            setweight(to_tsvector('russian',%s),'A') || setweight(to_tsvector('russian',%s),'B'),
                            setweight(to_tsvector('simple',%s),'A') || setweight(to_tsvector('simple',%s),'B')) RETURNING id""",
                                       (root_id, file.relative_path, path, file.name, file.size,
                                        file.modified_ns, compact_name, compact_stem, *search_values)).fetchone()[0]
                    ensure_file_id(connection, file_id)
                    added += 1
                elif current[1:] != (file.name, file.size, file.modified_ns):
                    connection.execute("""UPDATE atom_search.files SET name=%s,size=%s,modified_ns=%s,
                        revision=revision+1,compact_name=%s,compact_stem=%s,compact_path=%s,
                        fts_russian=setweight(to_tsvector('russian',%s),'A') || setweight(to_tsvector('russian',%s),'B'),
                        fts_simple=setweight(to_tsvector('simple',%s),'A') || setweight(to_tsvector('simple',%s),'B')
                        WHERE id=%s""",
                                       (file.name, file.size, file.modified_ns, compact_name, compact_stem,
                                        *search_values, current[0]))
                    connection.execute("DELETE FROM atom_search.file_embeddings WHERE file_id=%s", (current[0],))
                    updated += 1
            if snapshot.complete:
                # Compare literal prefixes, never LIKE patterns: % and _ are valid names.
                candidates = connection.execute("""SELECT id,relative_path FROM atom_search.files
                    WHERE source_id=%s AND left(relative_path,length(%s))=%s""", (root_id, prefix, prefix)).fetchall()
                delete_ids = []
                for file_id, relative in candidates:
                    branch, separator, _ = relative[len(prefix):].partition("/")
                    retained = directories if separator else observed
                    if branch not in retained and branch not in protected:
                        delete_ids.append(file_id)
                if delete_ids:
                    removed = connection.execute("DELETE FROM atom_search.files WHERE id=ANY(%s)", (delete_ids,)).rowcount
                existing = connection.execute("""SELECT relative_path FROM atom_search.directories
                    WHERE source_id=%s AND relative_path<>%s AND left(relative_path,length(%s))=%s""",
                                              (root_id, path, prefix, prefix)).fetchall()
                delete_paths = [relative for (relative,) in existing
                                if relative[len(prefix):].split("/", 1)[0] not in directories | protected]
                if delete_paths:
                    connection.execute("DELETE FROM atom_search.directories WHERE source_id=%s AND relative_path=ANY(%s)",
                                       (root_id, delete_paths))
            connection.execute("UPDATE atom_search.catalogs SET generation=generation+1 WHERE id=%s", (catalog_id,))
            return ReconcileCounts(added, updated, removed)

    def resolve_scope(self, root: str | None) -> SearchScope:
        path = canonical_root(root) if root is not None else None
        with self._connection() as connection:
            result = connection.execute("""SELECT s.id FROM atom_search.sources s
                JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND (%s::text IS NULL OR s.path=%s) ORDER BY s.id""", (path, path))
            return SearchScope(tuple(row[0] for row in result))

    def get_file(self, file_id: int, *, scope: SearchScope) -> CatalogFile | None:
        _integer(file_id)
        ids = [_integer(identity) for identity in scope.source_ids]
        if not ids:
            return None
        with self._connection() as connection:
            row = connection.execute(f"""SELECT {_FILE_COLUMNS} FROM atom_search.files f
                JOIN atom_search.sources s ON s.id=f.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND f.id=%s AND s.id=ANY(%s)""", (file_id, ids)).fetchone()
            return CatalogFile(*row) if row is not None else None

    def status(self, *, scope: SearchScope) -> dict[str, Any]:
        ids = [_integer(source_id) for source_id in scope.source_ids]
        with self._connection() as connection:
            result = connection.execute("""SELECT s.id,s.path,s.last_attempt,s.last_success,s.status,s.error,count(f.id)
                FROM atom_search.sources s JOIN atom_search.catalogs c ON c.id=s.catalog_id
                LEFT JOIN atom_search.files f ON f.source_id=s.id
                WHERE c.state='active' AND s.id=ANY(%s) GROUP BY s.id ORDER BY s.id""", (ids,)).fetchall()
            roots = [dict(zip(("id", "path", "last_attempt", "last_success", "status", "error"), row[:6])) for row in result]
            successes = [root["last_success"] for root in roots if root["last_success"] is not None]
            return {"files": sum(row[6] for row in result), "roots": roots, "last_success": max(successes, default=None)}

    def pending_embeddings(self, model_key: str, dimension: int, limit: int, *,
                           scope: SearchScope | None = None) -> list[EmbeddingInput]:
        if self._model is None:
            return []
        self._model_for(model_key, dimension)
        if limit <= 0:
            return []
        _integer(limit)
        ids = None if scope is None else [_integer(identity) for identity in scope.source_ids]
        if ids == []:
            return []
        with self._connection() as connection:
            self._check_model(connection)
            if self._model.input_schema == INPUT_SCHEMA:
                result = connection.execute(f'''SELECT {_FILE_COLUMNS},pc.prepared_text,
                    pc.context_revision,pc.input_hash FROM atom_search.files f
                    JOIN atom_search.sources s ON s.id=f.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                    JOIN atom_search.file_project_context pc ON pc.file_id=f.id AND pc.file_revision=f.revision
                    JOIN atom_search.project_context_state st ON st.id=1 AND st.context_revision=pc.context_revision
                    LEFT JOIN atom_search.file_embeddings e ON e.file_id=f.id AND e.model_key=%s
                    WHERE c.state='active' AND (%s::bigint[] IS NULL OR s.id=ANY(%s::bigint[]))
                    AND pc.input_hash IS NOT NULL
                    AND (e.file_id IS NULL OR e.file_revision<>f.revision OR e.input_hash<>pc.input_hash)
                    ORDER BY f.id LIMIT %s''', (model_key, ids, ids, limit)).fetchall()
                return [EmbeddingInput(CatalogFile(*row[:9]), row[9], INPUT_SCHEMA, row[10], row[11])
                        for row in result]
            result = connection.execute(f"""SELECT {_FILE_COLUMNS} FROM atom_search.files f
                JOIN atom_search.sources s ON s.id=f.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                LEFT JOIN atom_search.file_embeddings e ON e.file_id=f.id AND e.model_key=%s
                WHERE c.state='active' AND (%s::bigint[] IS NULL OR s.id=ANY(%s::bigint[]))
                AND (e.file_id IS NULL OR e.file_revision<>f.revision)
                ORDER BY f.id LIMIT %s""", (model_key, ids, ids, limit)).fetchall()
            files = [CatalogFile(*row) for row in result]
            return [EmbeddingInput(file, _passage(file), _SCHEMA) for file in files]

    def save_embeddings(self, records: Sequence[EmbeddingWrite], model_key: str) -> int:
        model = self._model_for(model_key)
        prepared = []
        for record in records:
            file = record.item.file
            for value in (file.file_id, file.source_id, file.revision):
                _integer(value)
            _path(file.relative_path)
            if (record.item.input_schema != model.input_schema
                    or model.input_schema == _SCHEMA and record.item.text != _passage(file)
                    or record.input_hash != hashlib.sha256(record.item.text.encode("utf-8")).hexdigest()):
                raise ValueError("Embedding passage/schema/hash does not match metadata")
            if model.input_schema == INPUT_SCHEMA:
                _integer(record.item.context_revision)
                if record.item.input_hash != record.input_hash:
                    raise ValueError('Prepared embedding input hash mismatch')
            prepared.append((record, _vector_literal(record.vector)))
        with self._connection() as connection:
            # No active catalog means all CAS candidates are stale, not writable.
            active = connection.execute("SELECT id FROM atom_search.catalogs WHERE state='active' FOR UPDATE").fetchone()
            if active is None:
                return 0
            self._check_model(connection)
            accepted = 0
            for record, literal in prepared:
                expected = record.item.file
                current = connection.execute(f"""SELECT {_FILE_COLUMNS} FROM atom_search.files f
                    JOIN atom_search.sources s ON s.id=f.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                    WHERE f.id=%s AND f.source_id=%s AND f.relative_path=%s AND f.revision=%s
                    AND c.id=%s AND c.state='active' FOR UPDATE OF s,f""",
                                             (expected.file_id, expected.source_id, expected.relative_path, expected.revision, active[0])).fetchone()
                if current is None:
                    continue
                file = CatalogFile(*current)
                if model.input_schema == INPUT_SCHEMA:
                    projection = connection.execute('''SELECT 1 FROM atom_search.file_project_context pc
                        JOIN atom_search.project_context_state st ON st.id=1 AND st.context_revision=pc.context_revision
                        WHERE pc.file_id=%s AND pc.file_revision=%s AND pc.context_revision=%s
                        AND pc.prepared_text=%s AND pc.input_hash=%s''',
                        (file.file_id, file.revision, record.item.context_revision,
                         record.item.text, record.input_hash)).fetchone()
                    if projection is None:
                        continue
                elif record.item.text != _passage(file):
                    raise ValueError("Embedding passage does not match current metadata")
                accepted += connection.execute("""INSERT INTO atom_search.file_embeddings
                    (file_id,model_key,file_revision,input_hash,embedding) VALUES (%s,%s,%s,%s,%s::public.vector)
                    ON CONFLICT(file_id,model_key) DO UPDATE SET file_revision=excluded.file_revision,
                    input_hash=excluded.input_hash,embedding=excluded.embedding""",
                                               (file.file_id, model_key, file.revision, record.input_hash, literal)).rowcount
            return accepted

    def close(self) -> None:
        with self._lock:
            self._closed = True
