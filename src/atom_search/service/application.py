"""Lightweight caller views of one host. Scope is resolved before retrieval."""
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import PureWindowsPath

from atom_search_core.search import normalize, compact
from atom_search_core.query_resolution import resolve_project_query
from atom_search.http.security import error, scope_tuple, public_uuid
from .contracts import (SearchRequest, SearchResponse, ResultMetadata, QueryInterpretation,
    SettingsSnapshot, HealthSnapshot)
from .projects import project_resolution_dtos


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class AtomSearchService:
    def __init__(self, owner, access):
        self.owner, self.access = owner, access

    def _require(self, capability):
        if capability not in self.access.capabilities and 'owner' not in self.access.capabilities:
            raise error('access_denied', 403)

    def _allowed(self):
        sources = self.owner.repository.snapshot().settings['sources']
        if 'owner' in self.access.capabilities:
            return tuple(source['id'] for source in sources)
        return self.access.source_ids

    def _scope(self, requested):
        return self.owner.registry.resolve(scope_tuple(requested), allowed=self._allowed())

    def _metadata(self, file, *, score=None, kind='metadata', matched=()):
        source_id = self.owner.core.public_ids.source_uuid(file.root)
        return ResultMetadata(self.owner.registry.public_file_id(file.file_id), source_id,
            file.name, file.relative_path, str(PureWindowsPath(file.root).joinpath(*file.relative_path.split('/'))),
            file.size, file.modified_ns, getattr(file, 'stale',
                getattr(file, 'source_status', 'complete') not in ('complete', 'scanning')),
            getattr(file, 'project_id', None), matched, score, kind)

    def search(self, request: SearchRequest) -> SearchResponse:
        self._require('search')
        if (not isinstance(request, SearchRequest) or type(request.query) is not str
                or not 1 <= len(request.query.strip()) <= 512 or type(request.limit) is not int
                or not 1 <= request.limit <= 100 or request.mode not in ('literal', 'lexical', 'hybrid')):
            raise error('invalid_request')
        core = getattr(self.owner, 'core', None)
        project_ready = (getattr(core, 'project_search_supported', False) is True
                         and callable(getattr(core, 'search_project', None)))
        if request.project_id is not None and not project_ready:
            raise error('capability_unavailable')
        with self.owner.operation():
            scope = self._scope(request.source_ids)
            if project_ready:
                # One durable read binds the resolver configuration to the SQL
                # context fence. Do not mix an old settings snapshot/new revision.
                revision, (projects, aliases, _) = core.projection.configuration()
                try:
                    resolution = resolve_project_query(request.query, scope=scope,
                        projects=projects, aliases=aliases, project_id=request.project_id)
                except ValueError as failure:
                    if str(failure) == 'project_scope_denied':
                        raise error('project_scope_denied', 403) from None
                    raise
                public = tuple(source['id'] for source in self.owner.repository.snapshot().settings['sources'])
                internal = core.public_ids.resolve_sources(public).source_ids
                if len(internal) != len(public):
                    raise error('project_scope_denied', 403)
                interpretation, applied, ambiguities = project_resolution_dtos(resolution,
                    scope=scope, projects=projects, source_ids=dict(zip(public, internal)))
                results = core.search_project(request.query, scope=scope, resolution=resolution,
                    context_revision=revision, limit=request.limit, mode=request.mode)
                if core.projection.configuration()[0] != revision:
                    # SQL has already rejected the obsolete cohort. Refuse its
                    # old alias explanation as well; a fresh request can resolve
                    # against the new configuration without holding writer locks.
                    raise error('service_unavailable', 503, retryable=True)
                metadata = tuple(self._metadata(item, score=item.score, kind=item.match_kind,
                                                matched=item.matched_fields) for item in results)
                return SearchResponse(metadata, interpretation, applied, ambiguities,
                                      self.owner.index_generation(), ())
            results = self.owner.core.search_scoped(request.query.strip(), scope=scope,
                                                    limit=request.limit, mode=request.mode)
            kind = request.mode if request.mode != 'hybrid' or self.owner.core._embedder is not None else 'lexical'
            metadata = tuple(self._metadata(item, score=item.score, kind=kind,
                matched=tuple(field for field, value in (('name', item.name), ('path', item.relative_path))
                              if compact(request.query) in compact(value))) for item in results)
            return SearchResponse(metadata, QueryInterpretation(request.query, normalize(request.query), None, ()),
                                  (), (), self.owner.index_generation(), ())

    def fetch(self, result_id: str, *, source_ids=None) -> ResultMetadata:
        self._require('fetch')
        with self.owner.operation():
            scope = self._scope(source_ids)
            identity = self.owner.registry.internal_file_id(result_id, scope=scope)
            file = self.owner.core.catalog.get_file(identity, scope=scope) if identity is not None else None
            if file is None:
                raise error('result_not_found', 404)
            return self._metadata(file)

    def settings_get(self) -> SettingsSnapshot:
        self._require('settings_get')
        snapshot = self.owner.repository.snapshot()
        if 'owner' in self.access.capabilities:
            return snapshot
        # A deliberately incomplete read projection. Closed/security sections
        # and credential references are absent, never filled with defaults.
        value = snapshot.settings
        enabled = {source['id'] for source in value['sources'] if source['enabled']}
        allowed = set(self._allowed()) & enabled
        projection = {key: value[key] for key in ('schema_version', 'revision', 'ui', 'onboarding')}
        projection['sources'] = [source for source in value['sources'] if source['id'] in allowed]
        projection['search'] = {'default_mode': value['search']['default_mode']}
        return SettingsSnapshot(snapshot.schema_version, snapshot.revision, projection, ())

    def settings_validate(self, candidate: dict, expected_revision: int):
        self._require('owner')
        return self.owner.coordinator.validate(candidate, expected_revision)

    def settings_apply(self, candidate: dict, expected_revision: int):
        self._require('owner')
        with self.owner.operation():
            return self.owner.coordinator.owner_apply(candidate, expected_revision)

    def start_catalog(self, source_ids=None):
        self._require('owner')
        with self.owner.operation():
            if not self._scope(source_ids).source_ids:
                return ()
            enabled = {source['id'] for source in self.owner.repository.snapshot().settings['sources']
                       if source['enabled']}
            requested = tuple(identity for identity in self._allowed() if identity in enabled) if source_ids is None else tuple(source_ids)
            return self.owner.scheduler.start_catalog(requested)

    def cancel_job(self, job_id: str):
        self._require('owner')
        with self.owner.operation():
            return self.owner.scheduler.cancel(public_uuid(job_id))

    def status(self) -> HealthSnapshot:
        self._require('status')
        with self.owner.operation():
            return self.owner.health.snapshot(now=datetime.fromisoformat(utc_now()), access=self.access)

    def close(self):
        """A caller view has no ownership of the host."""
