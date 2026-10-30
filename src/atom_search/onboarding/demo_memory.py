"""Bounded read ports for the fictional eight-file catalogue; no persistence."""
from atom_search_core.models import LexicalHit, ProjectFilter, SearchScope
from atom_search_core.project_context import INPUT_SCHEMA
from atom_search_core.search import compact, lexical_score, query_terms, ranked_results, normalize


def _read_only(*args, **kwargs):
    raise RuntimeError('read_only_demo')


class ReadOnlyMemoryCatalog:
    def __init__(self, files):
        self.files = tuple(files)
        self._files = {file.file_id: file for file in self.files}

    begin_root = finish_root = reconcile = save_embeddings = _read_only

    def resolve_scope(self, root):
        return SearchScope(tuple(sorted({file.source_id for file in self.files
                                         if root is None or file.root == root})))

    def get_file(self, file_id, *, scope):
        file = self._files.get(file_id)
        return file if file is not None and file.source_id in scope.source_ids else None

    def status(self, *, scope):
        return {'files': sum(file.source_id in scope.source_ids for file in self.files)}

    def pending_embeddings(self, model_key, dimension, limit, *, scope=None):
        return []

    def close(self):
        pass


class ReadOnlyMemoryProjection:
    """An immutable current P3 context, built once from confirmed manifest bindings."""
    def __init__(self, projects, aliases, source_labels):
        self._configuration = (tuple(projects), tuple(aliases), dict(source_labels))

    def configuration(self):
        projects, aliases, labels = self._configuration
        return 1, (projects, aliases, dict(labels))

    sync = publish_batch = _read_only

    def pending_context(self, limit, *, scope=None, require_prepared=False):
        return []


class ReadOnlyMemoryRetrieval:
    def __init__(self, catalog, contexts=()):
        self.catalog = catalog
        self.contexts = {context.file_id: context for context in contexts}

    def _eligible(self, scope, project_filter):
        for file in self.catalog.files:
            if file.source_id not in scope.source_ids:
                continue
            context = self.contexts.get(file.file_id)
            if project_filter is not None:
                if (project_filter.input_schema != INPUT_SCHEMA or context is None
                        or context.context_revision != project_filter.context_revision):
                    continue
                if (project_filter.project_ids is not None
                        and context.project_id not in project_filter.project_ids):
                    continue
            yield file, context

    @staticmethod
    def _fields(query, file, context):
        values = [('name', file.name), ('path', file.relative_path)]
        if context is not None:
            values.extend((('root', file.root), ('source_label', context.source_label),
                           ('project', context.project_name or ''), ('alias', ' '.join(context.aliases))))
        terms = query_terms(query)
        return tuple(field for field, value in values if any(term in normalize(value) for term in terms))

    def lexical_candidates(self, query, *, scope, limit, project_filter=None):
        eligible = tuple(self._eligible(scope, project_filter))
        contexts = {file.file_id: context for file, context in eligible}
        files = tuple(file for file, _ in eligible)
        # Shared ranking and tie-breaking, without a second lexical engine.
        ranked = ranked_results(query, files, limit)
        return [LexicalHit(self.catalog.get_file(row.file_id, scope=scope), row.score,
                           contexts[row.file_id].project_id if contexts[row.file_id] else None,
                           self._fields(query, self.catalog.get_file(row.file_id, scope=scope), contexts[row.file_id]))
                for row in ranked]

    def literal_candidates(self, query, *, scope, limit, project_filter=None):
        needle = compact(query)
        if not needle:
            return []
        hits = [LexicalHit(file, float(lexical_score(query, file.name, file.relative_path)),
                          context.project_id if context else None, self._fields(query, file, context))
                for file, context in self._eligible(scope, project_filter)
                if needle in compact(file.name) or needle in compact(file.relative_path)]
        return sorted(hits, key=lambda hit: (-hit.score, hit.file.file_id))[:limit]

    def project_candidates(self, resolution, *, scope, context_revision, limit, mode='lexical'):
        selected = ((resolution.selected_project_id,) if resolution.selected_project_id is not None
                    else resolution.preferred_project_ids or None)
        current = ProjectFilter(selected, context_revision, INPUT_SCHEMA)
        query = resolution.residual_query if selected is not None else resolution.original_query
        if not query.strip() and selected is not None:
            return [LexicalHit(file, 0.0, context.project_id,
                               self._fields(resolution.original_query, file, context))
                    for file, context in self._eligible(scope, current)][:limit]
        candidates = self.literal_candidates if mode == 'literal' else self.lexical_candidates
        return candidates(query, scope=scope, limit=limit, project_filter=current)

    def vector_candidates(self, vector, *, model_key, scope, limit, project_filter=None):
        return []
