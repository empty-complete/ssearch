"""Application use cases shared by the desktop shell and a future AtomDB service."""
from pathlib import Path
from collections.abc import Callable
import hashlib
import inspect
import math
import threading

from .models import EmbeddingWrite, ProjectFilter, ProjectResolution, SearchResult, SearchScope
from .contracts import (Cancellation, CatalogBackend, ContextProjection, Embedder, EmbeddingPreparer, ProgressCallback,
                        RetrievalBackend, Source)
from .source import FileSystemSource, canonical_root
from .project_context import INPUT_SCHEMA, build_document_context, metadata_embedding_text


class SearchService:
    def __init__(self, database: Path | None = None, model_dir: Path | None = None, *,
                 catalog: CatalogBackend | None = None, retrieval: RetrievalBackend | None = None,
                 source: Source | None = None, embedder: Embedder | None = None,
                 projection: ContextProjection | None = None, preparer: EmbeddingPreparer | None = None,
                 input_schema='filename-parent-v1',
                 on_close: Callable[[], None] | None = None):
        if database is not None:
            if catalog is not None or retrieval is not None:
                raise ValueError('Choose a database or both storage ports, not both.')
        elif catalog is None or retrieval is None:
            raise ValueError('Supply a database or both catalog and retrieval ports.')
        if database is not None:
            from .storage.legacy_sqlite import LegacySqliteBackend
            catalog = retrieval = LegacySqliteBackend(database)
            self.store = catalog.store  # Temporary legacy-client compatibility only.
        self.catalog = catalog
        self.retrieval = retrieval
        self.source = source if source is not None else FileSystemSource()
        self.model_dir = Path(model_dir) if model_dir is not None else None
        self._scan_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._on_close = on_close
        self._embedder = embedder
        self.projection, self._preparer, self.input_schema = projection, preparer, input_schema
        self._model_status = 'Модель не установлена; работает поиск по названиям и путям'
        if self._embedder is None and self.model_dir is not None:
            try:
                from .embeddings import OnnxEmbedder
                self._embedder = OnnxEmbedder(self.model_dir)
            except Exception as exc:
                self._model_status = f'Модель недоступна: {exc}'
        if self._embedder is not None:
            self._model_status = 'Локальная смысловая модель готова'

    @property
    def _model_key(self):
        return self._embedder.model_id + '|' + self.input_schema

    @property
    def _dimension(self):
        return getattr(self._embedder, 'dimension', 384)

    def search(self, query: str, limit: int = 100, *, root: str | None = None) -> list[SearchResult]:
        query = query.strip()[:512]
        limit = max(1, min(int(limit), 500))
        if not query:
            return []
        root = canonical_root(root) if root is not None else None
        return self.search_scoped(query, scope=self.catalog.resolve_scope(root), limit=limit)

    def search_scoped(self, query: str, *, scope: SearchScope, limit: int = 50,
                      mode: str = 'hybrid') -> list[SearchResult]:
        if (type(query) is not str or not 1 <= len(query.strip()) <= 512
                or type(limit) is not int or not 1 <= limit <= 500
                or mode not in ('literal', 'lexical', 'hybrid') or not isinstance(scope, SearchScope)):
            raise ValueError('invalid_search_request')
        query = query.strip()
        if not scope.source_ids:
            return []
        candidates = (self.retrieval.literal_candidates if mode == 'literal'
                      else self.retrieval.lexical_candidates)
        hits = candidates(query, scope=scope, limit=max(500, limit * 5))
        lexical = sorted((self._result(hit.file, hit.score) for hit in hits),
                         key=lambda item: (-item.score, item.relative_path.casefold(), item.file_id))[:limit]
        return self._with_semantic(query, lexical, limit, scope=scope, mode=mode)

    @property
    def project_search_supported(self):
        """Structural compatibility, without invoking a port or claiming readiness."""
        if self.projection is None or self.input_schema != INPUT_SCHEMA:
            return False
        calls = {
            'project_candidates': ((), dict(resolution=None, scope=None, context_revision=1, limit=1, mode='lexical')),
            'lexical_candidates': (('',), dict(scope=None, limit=1, project_filter=None)),
            'literal_candidates': (('',), dict(scope=None, limit=1, project_filter=None)),
            'vector_candidates': ((), dict(vector=(), model_key='', scope=None, limit=1, project_filter=None)),
        }
        try:
            for name, (args, kwargs) in calls.items():
                inspect.signature(getattr(self.retrieval, name)).bind(*args, **kwargs)
        except (TypeError, ValueError, AttributeError):
            return False
        return True

    def search_project(self, query: str, *, scope: SearchScope, resolution: ProjectResolution,
                       context_revision: int, limit: int = 50, mode: str = 'hybrid') -> list[SearchResult]:
        if (type(query) is not str or not 1 <= len(query.strip()) <= 512
                or type(limit) is not int or not 1 <= limit <= 500
                or mode not in ('literal', 'lexical', 'hybrid') or not isinstance(scope, SearchScope)
                or not isinstance(resolution, ProjectResolution) or resolution.original_query != query):
            raise ValueError('invalid_search_request')
        if not self.project_search_supported:
            raise ValueError('capability_unavailable')
        if not scope.source_ids:
            return []
        cohort = ((resolution.selected_project_id,) if resolution.selected_project_id is not None
                  else resolution.preferred_project_ids or None)
        current = ProjectFilter(cohort, context_revision, self.input_schema)
        hits = self.retrieval.project_candidates(resolution, scope=scope,
            context_revision=context_revision, limit=max(500, limit * 5), mode=mode)
        lexical = self._project_results(hits, limit, 'literal' if mode == 'literal' else 'lexical')
        preferred = self._with_semantic(query.strip(), lexical, limit, scope=scope,
                                         mode=mode, project_filter=current)
        if (resolution.selected_project_id is not None or not resolution.preferred_project_ids
                or len(preferred) >= limit):
            return preferred
        # Auto preference is a distinct bounded pool, never a synthetic score
        # boost. Retain the original literal/lexical channel after that pool.
        original = (self.retrieval.literal_candidates if mode == 'literal'
                    else self.retrieval.lexical_candidates)
        fallback = self._project_results(original(query.strip(), scope=scope, limit=max(500, limit * 5),
            project_filter=ProjectFilter(None, context_revision, self.input_schema)),
            limit, 'literal' if mode == 'literal' else 'lexical')
        seen = {item.file_id for item in preferred}
        return (preferred + [item for item in fallback if item.file_id not in seen])[:limit]

    def _project_results(self, hits, limit, kind):
        return sorted((self._result(hit.file, hit.score, project_id=hit.project_id,
            matched_fields=hit.matched_fields, match_kind=kind) for hit in hits),
            key=lambda item: (-item.score, item.relative_path.casefold(), item.file_id))[:limit]

    def _with_semantic(self, query, lexical, limit, *, scope, mode, project_filter=None):
        if mode != 'hybrid' or self._embedder is None:
            return lexical
        if not self._inference_lock.acquire(blocking=False):
            return lexical
        try:
            return self._hybrid(query, lexical, limit, scope=scope, project_filter=project_filter)
        except Exception as exc:
            self._model_status = f'Смысловой поиск недоступен; работает обычный поиск: {exc}'
            return lexical
        finally:
            self._inference_lock.release()

    @staticmethod
    def _result(file, score, *, project_id=None, matched_fields=(), match_kind=None):
        return SearchResult(file.file_id, file.root, file.relative_path, file.name, file.size,
                            file.modified_ns, score, file.source_status not in ('complete', 'scanning'),
                            project_id, matched_fields, match_kind)

    def _hybrid(self, query, lexical, limit, *, scope, project_filter=None):
        vector = self._embedder.encode([query], kind='query')[0]
        options = {} if project_filter is None else dict(project_filter=project_filter)
        hits = self.retrieval.vector_candidates(vector, model_key=self._model_key,
                                                scope=scope, limit=max(100, limit * 3), **options)
        if not hits:
            return lexical
        items = {item.file_id: item for item in lexical}
        scores = {item.file_id: 2.0 / (60 + rank) for rank, item in enumerate(lexical, 1)}
        for rank, hit in enumerate(hits, 1):
            if not math.isfinite(hit.similarity) or hit.similarity < 0.3:
                continue
            identifier = hit.file.file_id
            if project_filter is not None:
                previous = items.get(identifier)
                items[identifier] = (self._result(hit.file, 0, project_id=hit.project_id, match_kind='semantic')
                    if previous is None else SearchResult(previous.file_id, previous.root, previous.relative_path,
                        previous.name, previous.size, previous.modified_ns, previous.score, previous.stale,
                        previous.project_id, previous.matched_fields, 'hybrid'))
            else:
                items.setdefault(identifier, self._result(hit.file, 0))
            scores[identifier] = scores.get(identifier, 0) + 1.0 / (60 + rank)
        result = []
        for identifier, item in items.items():
            result.append(SearchResult(item.file_id, item.root, item.relative_path, item.name,
                                       item.size, item.modified_ns, scores[identifier], item.stale,
                                       item.project_id, item.matched_fields, item.match_kind))
        return sorted(result, key=lambda item: (-item.score, item.relative_path.casefold(), item.file_id))[:limit]

    def index_embeddings(self, *, progress: ProgressCallback | None = None,
                         cancel: Cancellation | None = None) -> dict:
        cancel = cancel if cancel is not None else threading.Event()
        count = 0
        while not cancel.is_set():
            report = self.index_embedding_batch(cancel=cancel)
            if not report['attempted'] and not report.get('context_attempted'):
                break
            count += report['indexed']
            if progress:
                progress({'stage': 'embeddings', 'indexed': count,
                          'files': self.catalog.status(scope=self.catalog.resolve_scope(None))['files']})
        return {'indexed': count, 'cancelled': cancel.is_set()}

    def configure_context(self, projects, aliases, *, settings_revision, source_labels):
        if self.projection is None:
            raise ValueError('context_projection_unavailable')
        return self.projection.sync(projects, aliases, expected_settings_revision=settings_revision,
                                    source_labels=source_labels)

    def index_context_batch(self, *, scope=None, cancel=None, publish_if=None):
        """Prepare metadata only; a transaction acknowledges at most 1000 rows."""
        cancel = cancel if cancel is not None else threading.Event()
        report = dict(attempted=0, published=0, cancelled=cancel.is_set())
        if self.projection is None or cancel.is_set():
            return report
        revision, configuration = self.projection.configuration()
        if revision == 0:
            self.configure_context((), (), settings_revision=1, source_labels={})
            revision, configuration = self.projection.configuration()
        projects, aliases, labels = configuration
        files = self.projection.pending_context(1000, scope=scope,
                                                require_prepared=self._preparer is not None)
        records = []
        for file in files:
            if cancel.is_set():
                return dict(report, cancelled=True)
            context = build_document_context(file, source_label=labels.get(file.source_id, ''),
                projects=projects, aliases=aliases, context_revision=revision)
            prepared = None
            if self._preparer is not None:
                try:
                    prepared = self._preparer.prepare_input(metadata_embedding_text(context), kind='passage')
                except (ValueError, RuntimeError, OSError, ImportError):
                    # Raw FTS remains publishable without a usable tokenizer.
                    self._preparer = None
            records.append((context, file.revision, prepared))
        if cancel.is_set() or publish_if is not None and not publish_if():
            return dict(report, cancelled=True)
        if records:
            report['attempted'] = len(records)
            report['published'] = self.projection.publish_batch(records, cancel=cancel, publish_if=publish_if)
        report['cancelled'] = cancel.is_set() or bool(publish_if is not None and not publish_if())
        return report

    def index_embedding_batch(self, *, scope: SearchScope | None = None,
                              cancel: Cancellation | None = None,
                              publish_if: Callable[[], bool] | None = None) -> dict:
        """One bounded batch on the existing session; SQL scope precedes LIMIT."""
        cancel = cancel if cancel is not None else threading.Event()
        empty = {'indexed': 0, 'attempted': 0, 'cancelled': cancel.is_set()}
        if self.projection is not None:
            context = self.index_context_batch(scope=scope, cancel=cancel, publish_if=publish_if)
            if context['attempted'] or context['cancelled']:
                return dict(empty, context_attempted=context['attempted'], cancelled=context['cancelled'])
        if self._embedder is None or cancel.is_set():
            return empty
        with self._inference_lock:
            if cancel.is_set() or publish_if is not None and not publish_if():
                return dict(empty, cancelled=True)
            rows = (self.catalog.pending_embeddings(self._model_key, self._dimension, 16)
                    if scope is None else self.catalog.pending_embeddings(
                        self._model_key, self._dimension, 16, scope=scope))
            if not rows:
                return empty
            vectors = [tuple(float(value) for value in vector)
                       for vector in self._embedder.encode([row.text for row in rows], kind='passage')]
            if len(vectors) != len(rows) or any(
                    len(vector) != self._dimension or not all(math.isfinite(value) for value in vector)
                    for vector in vectors):
                raise ValueError('Модель вернула некорректные векторы.')
            if cancel.is_set() or publish_if is not None and not publish_if():
                return dict(empty, attempted=len(rows), cancelled=True)
            saved = self.catalog.save_embeddings([
                EmbeddingWrite(row, vector, row.input_hash or hashlib.sha256(row.text.encode('utf-8')).hexdigest())
                for row, vector in zip(rows, vectors)], self._model_key)
            return {'indexed': saved, 'attempted': len(rows), 'cancelled': False}

    def scan(self, root: str, progress: ProgressCallback | None = None,
             cancel: Cancellation | None = None, *, excluded_roots: tuple[Path, ...] = (),
             publish_if: Callable[[], bool] | None = None) -> dict:
        cancel = cancel if cancel is not None else threading.Event()
        report = {'status': 'complete', 'files': 0, 'directories': 0, 'added': 0,
                  'updated': 0, 'removed': 0, 'skipped_links': 0, 'errors': []}
        if not self._scan_lock.acquire(blocking=False):
            return dict(report, status='busy')
        root_id = None
        try:
            root = canonical_root(root)
            root_id = self.catalog.begin_root(root)
            if not cancel.is_set():
                for snapshot in self.source.snapshots(root, cancel, excluded_roots=excluded_roots):
                    if cancel.is_set() or publish_if is not None and not publish_if():
                        report['status'] = 'cancelled'
                        break
                    counts = self.catalog.reconcile(root_id, snapshot)
                    report['files'] += len(snapshot.files)
                    report['directories'] += 1
                    report['added'] += counts.added
                    report['updated'] += counts.updated
                    report['removed'] += counts.removed
                    report['errors'].extend(snapshot.errors)
                    report['skipped_links'] += snapshot.skipped_links
                    if not snapshot.complete or snapshot.errors or snapshot.skipped_links:
                        report['status'] = ('unavailable' if snapshot.relative_path == '' and
                                            not snapshot.complete and not snapshot.files else 'partial')
                    if progress:
                        progress(dict(report, current_path=snapshot.relative_path))
            if cancel.is_set():
                report['status'] = 'cancelled'
        except OSError as exc:
            report['errors'].append({'path': root, 'operation': 'scan', 'message': str(exc),
                'error_code': 'access_denied' if isinstance(exc, PermissionError) else 'source_unavailable'})
            report['status'] = 'unavailable' if not report['files'] else 'partial'
        except BaseException:
            report['status'] = 'partial'
            # A quota/SQL failure must survive a second failure while publishing
            # status. The original exception remains the caller's diagnostic.
            if root_id is not None:
                try:
                    self.catalog.finish_root(root_id, 'partial')
                except Exception:
                    pass
                root_id = None
            raise
        finally:
            try:
                if root_id is not None:
                    self.catalog.finish_root(root_id, report['status'],
                                           '; '.join(item['message'] for item in report['errors'][:5]))
            finally:
                self._scan_lock.release()
        return report

    def status(self, *, root: str | None = None) -> dict:
        root = canonical_root(root) if root is not None else None
        status = self.catalog.status(scope=self.catalog.resolve_scope(root))
        metadata_ready = bool(status['roots']) and all(
            row.get('status') == 'complete' for row in status['roots'])
        pending = (self.catalog.pending_embeddings(self._model_key, self._dimension, 1)
                   if self._embedder is not None else [])
        context_pending = (self.projection.pending_context(1, require_prepared=True)
                           if self.projection is not None else [])
        return dict(status, backend=getattr(self.catalog, 'backend', 'sqlite-legacy'),
                    metadata_ready=metadata_ready and not status.get('storage_blocked', False),
                    embeddings_ready=self._embedder is not None and not pending and not context_pending,
                    semantic_ready=self._embedder is not None,
                    model_status=self._model_status)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.catalog.close()
        finally:
            if self._on_close is not None:
                self._on_close()
