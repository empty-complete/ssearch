"""Validated public UUID settings translated through persisted source IDs."""
import uuid

from atom_search_core.models import (ProjectAlias, ProjectBinding, ProjectDefinition,
                                     ProjectResolution, SearchScope)

from atom_search.settings.schema import SettingsValidationError, _error, validate_settings
from atom_search.http.security import error
from .contracts import AppliedAlias, ProjectOption, QueryAmbiguity, QueryInterpretation


def parse_project_settings(settings: dict, source_ids: dict[str, int]
                           ) -> tuple[tuple[ProjectDefinition, ...], tuple[ProjectAlias, ...]]:
    value = validate_settings(settings)
    used = {identity for project in value['projects'] for identity in project['source_ids']}
    missing = [identity for identity in used if type(source_ids.get(identity)) is not int
               or source_ids[identity] <= 0]
    if missing:
        raise SettingsValidationError((_error('projects', 'unknown_source_mapping'),))
    projects = tuple(ProjectDefinition(project['id'], project['canonical_name'],
        tuple(source_ids[identity] for identity in project['source_ids']),
        tuple(ProjectBinding(project['id'], source_ids[binding['source_id']],
                             binding['path_prefix'], binding['confirmed'])
              for binding in project['bindings'])) for project in value['projects'])
    aliases = tuple(ProjectAlias(alias['id'], alias['project_id'],
        tuple(source_ids[identity] for identity in alias['source_ids']), tuple(alias['variants']),
        alias['confirmed'], alias['basis']) for alias in value['search']['project_aliases'])
    return projects, aliases


def configure_project_context(core, snapshot, *, reservation=None):
    """Use persisted UUID mappings, including disabled configured sources."""
    if getattr(core, 'projection', None) is None:
        return
    identities = tuple(source['id'] for source in snapshot.settings['sources'])
    internal = core.public_ids.resolve_sources(identities).source_ids
    if len(internal) != len(identities):
        raise ValueError('unknown_source_mapping')
    mapping = dict(zip(identities, internal))
    projects, aliases = parse_project_settings(snapshot.settings, mapping)
    labels = {mapping[source['id']]: source['label'] for source in snapshot.settings['sources']}
    if reservation is not None:
        return core.projection.sync(projects, aliases, expected_settings_revision=snapshot.revision,
                                    source_labels=labels, settings_reservation=reservation)
    return core.configure_context(projects, aliases, settings_revision=snapshot.revision, source_labels=labels)


def project_resolution_dtos(resolution: ProjectResolution, *, scope: SearchScope,
                            projects: tuple[ProjectDefinition, ...], source_ids: dict[str, int]
                            ) -> tuple[QueryInterpretation, tuple[AppliedAlias, ...], tuple[QueryAmbiguity, ...]]:
    """Map an internal interpretation with the caller's persisted UUID scope."""
    allowed = set(scope.source_ids)
    permitted = {project.project_id: project for project in projects
                 if allowed.intersection(project.source_ids)}
    referenced = {*resolution.preferred_project_ids,
                  *(item.project_id for item in resolution.applied_aliases),
                  *(identity for item in resolution.ambiguities for identity in item.project_ids)}
    if resolution.selected_project_id is not None:
        referenced.add(resolution.selected_project_id)
    if not referenced.issubset(permitted):
        raise error('project_scope_denied', 403)

    def public(source):
        matches = [identity for identity, internal in source_ids.items()
                   if type(internal) is int and internal == source]
        try:
            if (source not in allowed or len(matches) != 1 or type(matches[0]) is not str
                    or str(uuid.UUID(matches[0])) != matches[0]):
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise error('project_scope_denied', 403) from None
        return matches[0]

    applied = []
    for item in resolution.applied_aliases:
        project = permitted[item.project_id]
        if item.source_id not in project.source_ids:
            raise error('project_scope_denied', 403)
        applied.append(AppliedAlias(item.alias_id, item.project_id, public(item.source_id),
                                    item.matched_text, project.canonical_name))
    ambiguities = tuple(QueryAmbiguity(item.term, tuple(
        ProjectOption(project.project_id, project.canonical_name,
                      tuple(sorted(public(source) for source in set(project.source_ids) & allowed)))
        for project in sorted((permitted[identity] for identity in item.project_ids),
                              key=lambda project: (project.canonical_name.casefold(), project.project_id))))
        for item in resolution.ambiguities)
    return (QueryInterpretation(resolution.original_query, resolution.normalized_query,
                                resolution.selected_project_id, resolution.expanded_terms),
            tuple(applied), ambiguities)
