"""Explicit demo search over the same P1/P3 pipeline and P2 response contract."""
from pathlib import Path
import threading
import uuid

from atom_search_core.models import CatalogFile, ProjectAlias, ProjectBinding, ProjectDefinition, SearchScope
from atom_search_core.project_context import INPUT_SCHEMA, build_document_context
from atom_search_core.query_resolution import resolve_project_query
from atom_search_core.search import compact, normalize
from atom_search_core.service import SearchService
from atom_search.http.security import error, public_uuid, scope_tuple
from atom_search.service.contracts import QueryInterpretation, ResultMetadata, SearchRequest, SearchResponse
from atom_search.service.projects import project_resolution_dtos
from .demo_memory import ReadOnlyMemoryCatalog, ReadOnlyMemoryProjection, ReadOnlyMemoryRetrieval


# This versioned compatibility recipe was specified for manifest v2. Changing it
# would break persisted tutorial result/project identities, so retain its URI.
_ID_PREFIX = 'urn:atom:onboarding-demo:v2:'


def demo_id(kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _ID_PREFIX + kind))


class DemoSearchProvider:
    def __init__(self, root, manifest, capabilities, *, validate_file):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._closed = False
        self._validate_file = validate_file
        self.source_id = demo_id('source')
        self.capabilities = tuple(dict.fromkeys(capability for capability in capabilities
                                               if capability in ('project_search', 'project_aliases')))
        self._project_ready = bool(self.capabilities)
        projects = tuple(ProjectDefinition(demo_id('project:' + item['id']), item['label'], (1,),
            tuple(ProjectBinding(demo_id('project:' + item['id']), 1, path, True)
                  for path in item['path_prefixes'])) for item in manifest['projects'])
        aliases = tuple(ProjectAlias(demo_id('alias:' + str(index)),
            demo_id('project:' + item['scope']['project_id']), (1,), (item['alias'],), True, 'manifest-v2')
            for index, item in enumerate(manifest['project_aliases']['entries']))
        if 'project_aliases' not in self.capabilities:
            aliases = ()
        files = []
        self._result_ids = {}
        for index, relative in enumerate(manifest['files'], 1):
            path = self.root.joinpath(*relative.split('/'))
            info = path.stat()
            files.append(CatalogFile(index, 1, str(self.root), relative, path.name,
                                     info.st_size, info.st_mtime_ns, 'complete', 1))
            self._result_ids[demo_id('file:' + relative)] = index
        self.catalog = ReadOnlyMemoryCatalog(files)
        self._projects, self._aliases = projects, aliases
        contexts = tuple(build_document_context(file, source_label=manifest['label'], projects=projects,
                                                aliases=aliases, context_revision=1) for file in files)
        retrieval = ReadOnlyMemoryRetrieval(self.catalog, contexts)
        projection = ReadOnlyMemoryProjection(projects, aliases, {1: manifest['label']})
        self._core = SearchService(catalog=self.catalog, retrieval=retrieval,
                                   projection=projection, input_schema=INPUT_SCHEMA)

    def _open(self):
        if self._closed:
            raise RuntimeError('demo_session_closed')

    def close(self):
        # Wait for current search/fetch before allowing the host to clean files.
        with self._lock:
            self._closed = True

    def _scope(self, source_ids):
        requested = scope_tuple(source_ids)
        if requested is not None and any(identity != self.source_id for identity in requested):
            raise error('source_scope_denied', 403)
        return SearchScope((1,) if requested is None or requested else ())

    def _metadata(self, file, *, score=None, kind='metadata', matched=()):
        if not self._validate_file(file.relative_path):
            raise error('result_not_found', 404)
        return ResultMetadata(demo_id('file:' + file.relative_path), self.source_id, file.name,
            file.relative_path, str(self.root.joinpath(*file.relative_path.split('/'))), file.size,
            file.modified_ns, False, getattr(file, 'project_id', None), matched, score, kind)

    def search(self, request):
        with self._lock:
            self._open()
            if (not isinstance(request, SearchRequest) or type(request.query) is not str
                    or not 1 <= len(request.query.strip()) <= 512 or type(request.limit) is not int
                    or not 1 <= request.limit <= 100 or request.mode not in ('literal', 'lexical', 'hybrid')):
                raise error('invalid_request')
            scope = self._scope(request.source_ids)
            if request.project_id is not None:
                public_uuid(request.project_id)
                if not self._project_ready:
                    raise error('capability_unavailable')
            if self._project_ready:
                try:
                    resolution = resolve_project_query(request.query, scope=scope, projects=self._projects,
                                                       aliases=self._aliases, project_id=request.project_id)
                except ValueError as failure:
                    if str(failure) == 'project_scope_denied':
                        raise error('project_scope_denied', 403) from None
                    raise
                interpretation, applied, ambiguities = project_resolution_dtos(resolution,
                    scope=scope, projects=self._projects, source_ids={self.source_id: 1})
                rows = self._core.search_project(request.query, scope=scope, resolution=resolution,
                                                context_revision=1, limit=request.limit, mode=request.mode)
                results = tuple(self._metadata(row, score=row.score, kind=row.match_kind,
                                               matched=row.matched_fields) for row in rows)
                return SearchResponse(results, interpretation, applied, ambiguities, 1, ())
            rows = self._core.search_scoped(request.query.strip(), scope=scope,
                                           limit=request.limit, mode=request.mode)
            kind = 'lexical' if request.mode == 'hybrid' else request.mode
            results = tuple(self._metadata(row, score=row.score, kind=kind,
                matched=tuple(field for field, value in (('name', row.name), ('path', row.relative_path))
                              if compact(request.query) in compact(value))) for row in rows)
            return SearchResponse(results, QueryInterpretation(request.query, normalize(request.query), None, ()),
                                  (), (), 1, ())

    def fetch(self, result_id, *, source_ids=None):
        with self._lock:
            self._open()
            scope = self._scope(source_ids)
            identifier = self._result_ids.get(result_id) if type(result_id) is str else None
            file = self.catalog.get_file(identifier, scope=scope)
            if file is None:
                raise error('result_not_found', 404)
            return self._metadata(file)
