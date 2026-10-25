"""Caller UUID scopes and owner-only, idempotent source binding.

The settings operation's before/candidate sources and prepared/committed/applied
states are the binding journal. Early settings recovery remains PG-free. Once PG
is ready, activate() binds the recovered authoritative snapshot before use.
Rollback leaves candidate identities dormant; it never deletes their catalog.
"""
from __future__ import annotations

import ntpath
from pathlib import Path
import uuid

from atom_search_core.models import SearchScope
from atom_search_core.source import canonical_root, contains_root
from atom_search.content import message
from .contracts import ServiceError, SettingChange, FieldError


def classify_source_roots(sources: tuple[dict, ...]) -> tuple[SettingChange, ...]:
    roots = [canonical_root(source['path']) for source in sources]
    overlaps = []
    for index, child in enumerate(roots):
        for parent_index, parent in enumerate(roots):
            if index == parent_index or child == parent:
                continue
            try:
                nested = ntpath.commonpath((parent, child)) == parent
            except ValueError:
                nested = False
            if nested:
                overlaps.append(SettingChange(f'sources.{index}.path', sources[parent_index]['path'],
                                               sources[index]['path']))
    return tuple(overlaps)


class SourceRegistry:
    def __init__(self, repository, identities, *, owner_id=None):
        self.repository, self.identities = repository, identities
        self._owner_id = owner_id
        self._exclusions = set()

    def register_owned_exclusion(self, path: Path, *, owner_id: str) -> None:
        if self._owner_id is None or owner_id != self._owner_id:
            raise ServiceError('access_denied', message('service.access_denied'), status_code=403)
        self._exclusions.add(Path(canonical_root(str(path))))

    def owned_exclusions(self) -> tuple[Path, ...]:
        return tuple(sorted(self._exclusions, key=str))

    def validate_sources(self, sources) -> tuple[FieldError, ...]:
        return tuple(FieldError(f'sources.{index}.path', 'owned_source_root',
                               message('settings.owned_source_root'))
                     for index, source in enumerate(sources)
                     if any(contains_root(root, source['path']) for root in self._exclusions))

    def activate(self):
        # Share both the in-process writer lock and the existing host guard.
        # Never capture a snapshot before acquiring them during slow startup.
        with self.repository._mutation_lock, self.repository.mutation():
            self.apply(self.repository.snapshot())

    def apply(self, snapshot):
        """Coordinator hook: its writer lock/host guard are already held."""
        errors = self.validate_sources(snapshot.settings['sources'])
        if errors:
            from atom_search.settings.schema import SettingsValidationError
            raise SettingsValidationError(errors)
        sources = tuple((source['id'], canonical_root(source['path']))
                        for source in snapshot.settings['sources'])
        try:
            return self.identities.bind_many(sources)
        except ValueError:
            raise ServiceError('source_binding_conflict', message('settings.source_binding_conflict'),
                               status_code=409) from None

    def bind(self, source_id: str, canonical_root: str) -> int:
        with self.repository._mutation_lock, self.repository.mutation():
            try:
                return self.identities.bind_many(((source_id, canonical_root),))[0]
            except ValueError:
                raise ServiceError('source_binding_conflict', message('settings.source_binding_conflict'),
                                   status_code=409) from None

    def select_source_id(self, root: str) -> str:
        """Owner GUI selection only; explicit owner UUIDs are never rewritten."""
        return self.identities.source_uuid(canonical_root(root)) or str(uuid.uuid4())

    def resolve(self, source_ids: tuple[str, ...] | None, *, allowed: tuple[str, ...]) -> SearchScope:
        enabled = {source['id'] for source in self.repository.snapshot().settings['sources']
                   if source['enabled']}
        permitted = enabled.intersection(allowed)
        requested = tuple(identity for identity in allowed if identity in permitted) if source_ids is None else source_ids
        if any(identity not in permitted for identity in requested):
            raise ServiceError('source_scope_denied', message('service.source_scope_denied'), status_code=403)
        # P1 resolve_scope(None) means the entire catalog. Never delegate None.
        return self.identities.resolve_sources(tuple(dict.fromkeys(requested))) if requested else SearchScope(())

    def public_file_id(self, file_id: int) -> str:
        identity = self.identities.public_file_id(file_id)
        if identity is None:
            raise ServiceError('result_not_found', message('service.result_not_found'), status_code=404)
        return identity

    def internal_file_id(self, result_id: str, *, scope: SearchScope) -> int | None:
        return self.identities.internal_file_id(result_id, scope=scope)
