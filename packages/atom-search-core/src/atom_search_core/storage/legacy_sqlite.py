"""Temporary ports adapter for the unversioned SQLite v1/v2 catalog.

The revision=1 DTO sentinel is not a durable file revision or CAS guarantee.
The legacy schema and its metadata/vector reuse behavior remain unchanged.
"""
from collections.abc import Sequence
from pathlib import Path
import struct
import threading

from ..models import (CatalogFile, DirectorySnapshot, EmbeddingInput, EmbeddingWrite,
                      LexicalHit, ReconcileCounts, SearchScope, VectorHit)
from ..search import lexical_score, compact
from ..source import canonical_root
from ..store import CatalogStore


class LegacySqliteBackend:
    def __init__(self, database: Path):
        self.store = CatalogStore(database)
        self._vector_lock = threading.RLock()
        self._vector_cache = None

    @staticmethod
    def _file(row) -> CatalogFile:
        return CatalogFile(row['id'], row['root_id'], row['root'], row['relative_path'],
                           row['name'], row['size'], row['modified_ns'], row['root_status'], 1)

    def begin_root(self, root: str) -> int:
        return self.store.begin_root(root)

    def finish_root(self, root_id: int, status: str, message: str = '') -> None:
        self.store.finish_root(root_id, status, message)

    def reconcile(self, root_id: int, snapshot: DirectorySnapshot) -> ReconcileCounts:
        return ReconcileCounts(*self.store.reconcile(root_id, snapshot))

    def resolve_scope(self, root: str | None) -> SearchScope:
        target = canonical_root(root) if root is not None else None
        roots = self.store.status()['roots']
        return SearchScope(tuple(row['id'] for row in roots
                                 if target is None or canonical_root(row['path']) == target))

    def get_file(self, file_id: int, *, scope: SearchScope) -> CatalogFile | None:
        if not scope.source_ids:
            return None
        placeholders = ','.join('?' for _ in scope.source_ids)
        with self.store.connection() as connection:
            row = connection.execute(f'''SELECT f.*,r.path root,r.status root_status
                FROM files f JOIN roots r ON r.id=f.root_id
                WHERE f.id=? AND r.id IN ({placeholders})''', (file_id, *scope.source_ids)).fetchone()
            return self._file(row) if row is not None else None

    def _roots(self, scope: SearchScope) -> list[dict]:
        if not scope.source_ids:
            return []
        allowed = set(scope.source_ids)
        return [row for row in self.store.status()['roots'] if row['id'] in allowed]

    def status(self, *, scope: SearchScope) -> dict:
        roots = self._roots(scope)
        count = sum(self.store.status(root=row['path'])['files'] for row in roots)
        successes = [row['last_success'] for row in roots if row['last_success']]
        return {'files': count, 'roots': roots, 'last_success': max(successes, default=None)}

    def pending_embeddings(self, model_key: str, dimension: int, limit: int, *,
                           scope: SearchScope | None = None) -> list[EmbeddingInput]:
        if limit <= 0:
            return []
        pending = self.store.pending_embeddings(model_key, dimension, limit,
                                               source_ids=None if scope is None else scope.source_ids)
        if not pending:
            return []
        ids = [row['id'] for row in pending]
        placeholders = ','.join('?' for _ in ids)
        with self.store.connection() as connection:
            rows = connection.execute(f'''SELECT f.*,r.path root,r.status root_status
                FROM files f JOIN roots r ON r.id=f.root_id
                WHERE f.id IN ({placeholders}) ORDER BY f.id''', ids)
            result = []
            for row in rows:
                file = self._file(row)
                parent = file.relative_path.rpartition('/')[0]
                text = file.name + (' | ' + parent.replace('/', ' / ') if parent else '')
                result.append(EmbeddingInput(file, text, 'filename-parent-v1'))
            return result

    def save_embeddings(self, records: Sequence[EmbeddingWrite], model_key: str) -> int:
        return self.store.save_embeddings([
            ({'id': record.item.file.file_id, 'root_id': record.item.file.source_id,
              'relative_path': record.item.file.relative_path},
             struct.pack(f'<{len(record.vector)}f', *record.vector), record.input_hash)
            for record in records], model_key)

    def lexical_candidates(self, query: str, *, scope: SearchScope, limit: int) -> list[LexicalHit]:
        if limit <= 0:
            return []
        hits = []
        # Query each allowed root: all legacy SQL candidate caps occur after its
        # source predicate, including exact-name, FTS and substring channels.
        for root in self._roots(scope):
            for row in self.store.candidates(query, limit, root=root['path']):
                file = self._file(row)
                score = lexical_score(query, file.name, file.relative_path)
                if score:
                    hits.append(LexicalHit(file, score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.file.file_id))[:limit]

    def literal_candidates(self, query: str, *, scope: SearchScope, limit: int) -> list[LexicalHit]:
        needle = compact(query)
        if not scope.source_ids or limit <= 0 or not needle:
            return []
        placeholders = ','.join('?' for _ in scope.source_ids)
        # This compatibility adapter computes the same normalization in-process;
        # the source predicate still bounds every row before ranking/LIMIT.
        with self.store.connection() as connection:
            rows = connection.execute(f'''SELECT f.*,r.path root,r.status root_status
                FROM files f JOIN roots r ON r.id=f.root_id
                WHERE r.id IN ({placeholders})''', scope.source_ids).fetchall()
        hits = [LexicalHit(self._file(row), lexical_score(query, row['name'], row['relative_path']))
                for row in rows if needle in compact(row['name']) or needle in compact(row['relative_path'])]
        return sorted(hits, key=lambda hit: (-hit.score, hit.file.file_id))[:limit]

    def vector_candidates(self, vector: Sequence[float], *, model_key: str,
                          scope: SearchScope, limit: int) -> list[VectorHit]:
        if limit <= 0 or not scope.source_ids:
            return []
        roots = self._roots(scope)
        if not roots:
            return []
        import numpy as np
        dimension = len(vector)
        key = (self.store.generation(), model_key, dimension, tuple(row['id'] for row in roots))
        with self._vector_lock:
            if self._vector_cache is None or self._vector_cache[0] != key:
                rows = [row for root in roots for row in
                        self.store.vector_rows(model_key, dimension, root=root['path'])]
                files = [self._file(row) for row in rows]
                matrix = (np.stack([np.frombuffer(row['embedding'], dtype='<f4') for row in rows])
                          if rows else np.empty((0, dimension), dtype=np.float32))
                self._vector_cache = key, files, matrix
            _, files, matrix = self._vector_cache
        similarities = matrix @ np.asarray(vector, dtype=np.float32)
        indices = sorted((index for index in range(len(files)) if np.isfinite(similarities[index])),
                         key=lambda index: (-float(similarities[index]), files[index].file_id))[:limit]
        return [VectorHit(files[index], float(similarities[index])) for index in indices]

    def close(self) -> None:
        # CatalogStore connections are operation-scoped; release only cached data.
        with self._vector_lock:
            self._vector_cache = None
