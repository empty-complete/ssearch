"""Pure project interpretation; original text remains an independent channel."""
import re
import unicodedata

from .models import (AliasUse, AmbiguousProject, ProjectAlias, ProjectDefinition,
                     ProjectResolution, SearchScope)
from .search import normalize


def _normalized_spans(query):
    # NFKC can compose multiple source characters or expand a ligature. Track
    # the rewritten suffix before casefold/ё->е, which can hide that composition.
    text, spans = '', []
    for end in range(1, len(query) + 1):
        current = unicodedata.normalize('NFKC', query[:end])
        common = 0
        while common < min(len(text), len(current)) and text[common] == current[common]:
            common += 1
        start = min((span[0] for span in spans[common:]), default=end - 1)
        spans[common:] = [(start, end)] * (len(current) - common)
        text = current
    folded, offsets = [], []
    for character, span in zip(text, spans):
        value = character.casefold().replace('ё', 'е')
        folded.append(value)
        offsets.extend([span] * len(value))
    return ''.join(folded), offsets


def _word(character):
    return unicodedata.category(character)[0] in ('L', 'N', 'M')


def _matches(term, query, normalized, offsets):
    value = normalize(term).strip()
    if not value:
        return
    pattern = re.compile(''.join(r'\s+' if part.isspace() else re.escape(part)
                                 for part in re.split(r'(\s+)', value)))
    for match in pattern.finditer(normalized):
        begin, end = match.span()
        if (begin and _word(normalized[begin - 1]) or
                end < len(normalized) and _word(normalized[end])):
            continue
        start = min(span[0] for span in offsets[begin:end])
        stop = max(span[1] for span in offsets[begin:end])
        fragment = query[start:stop]
        # A match inside a single compatibility expansion is not that source
        # fragment. Only complete original fragments may explain an alias use.
        if pattern.fullmatch(normalize(fragment)) is not None:
            yield (begin, end), fragment


def resolve_project_query(query: str, *, scope: SearchScope,
                          projects: tuple[ProjectDefinition, ...],
                          aliases: tuple[ProjectAlias, ...],
                          project_id: str | None = None) -> ProjectResolution:
    if (type(query) is not str or not 1 <= len(query.strip()) <= 512
            or not isinstance(scope, SearchScope)):
        raise ValueError('invalid_search_request')
    sources = set(scope.source_ids)
    permitted = {project.project_id: project for project in projects
                 if sources.intersection(project.source_ids)}
    if project_id is not None and (type(project_id) is not str or project_id not in permitted):
        raise ValueError('project_scope_denied')
    if project_id is not None:
        permitted = {project_id: permitted[project_id]}
    key = lambda identity: (permitted[identity].canonical_name.casefold(), identity)
    # The existing bound applies after trim. Only this <=512-character window
    # needs provenance tracking; the complete original/normalized query survives.
    text = query.strip()
    normalized, offsets = _normalized_spans(text)
    literal = normalize(query)
    mentions, fragments = {}, {}

    def add(term, project, alias=None, allowed=()):
        for span, fragment in _matches(term, text, normalized, offsets):
            uses = mentions.setdefault(span, {}).setdefault(project.project_id, set())
            fragments[span] = fragment
            if alias is not None:
                uses.update(AliasUse(alias.alias_id, project.project_id, source, fragment,
                                     project.canonical_name) for source in allowed)

    for project in permitted.values():
        add(project.canonical_name, project)
    for alias in aliases:
        project = permitted.get(alias.project_id)
        if not alias.confirmed or project is None:
            continue
        allowed = sources.intersection(project.source_ids, alias.source_ids)
        if allowed:
            for variant in alias.variants:
                add(variant, project, alias, sorted(allowed))
    # A configured whole phrase/code takes precedence over its shorter pieces.
    chosen = []
    for span in sorted(mentions, key=lambda span: (span[0] - span[1], span[0], span[1])):
        if not any(span[0] < old[1] and old[0] < span[1] for old in chosen):
            chosen.append(span)
    preferred = {project_id} if project_id is not None else set()
    ambiguities, applied, removed = [], [], []
    for span in sorted(chosen):
        identities = tuple(sorted(mentions[span], key=key))
        if len(identities) > 1:
            ambiguities.append(AmbiguousProject(fragments[span], identities))
        else:
            identity, = identities
            preferred.add(identity)
            applied.extend(sorted(mentions[span][identity],
                key=lambda item: (item.alias_id, item.source_id)))
            removed.append(span)
    residual = literal
    if removed:
        pieces, cursor = [], 0
        for begin, end in sorted(removed):
            pieces.extend((normalized[cursor:begin], ' '))
            cursor = end
        pieces.append(normalized[cursor:])
        residual = ' '.join(''.join(pieces).split())
    ordered = tuple(sorted(preferred, key=key))
    return ProjectResolution(query, literal, residual, project_id, ordered,
        tuple(dict.fromkeys(permitted[identity].canonical_name for identity in ordered)),
        tuple(applied), tuple(ambiguities))
