"""Durable current metadata projection; no document filesystem or model session."""
from dataclasses import asdict
from contextlib import nullcontext
import hashlib
import json

from ..models import CatalogFile, ProjectAlias, ProjectBinding, ProjectDefinition
from ..project_context import build_document_context, metadata_embedding_text, metadata_input_hash
from ..search import normalize
from .postgres import _FILE_COLUMNS, _integer, PostgresCatalog


def _configuration(projects, aliases, labels):
    return {'projects': [dict(project_id=p.project_id, canonical_name=p.canonical_name,
        source_ids=list(p.source_ids), bindings=[asdict(b) for b in p.bindings if b.confirmed])
        for p in sorted(projects, key=lambda p: p.project_id)],
        'aliases': [asdict(a) for a in sorted(aliases, key=lambda a: a.alias_id) if a.confirmed],
        'source_labels': {str(k): v for k, v in sorted(labels.items())}}


def _encoded_configuration(projects, aliases, labels):
    encoded = json.dumps(_configuration(projects, aliases, labels), sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False)
    return encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def context_configuration(value):
    projects = tuple(ProjectDefinition(p['project_id'], p['canonical_name'], tuple(p['source_ids']),
        tuple(ProjectBinding(**b) for b in p['bindings'])) for p in value.get('projects', ()))
    aliases = tuple(ProjectAlias(a['alias_id'], a['project_id'], tuple(a['source_ids']),
        tuple(a['variants']), a['confirmed'], a['basis']) for a in value.get('aliases', ()))
    return projects, aliases, {int(k): v for k, v in value.get('source_labels', {}).items()}


class ProjectProjection:
    def __init__(self, connection_factory):
        self._factory = connection_factory

    def configuration_matches(self, projects, aliases, *, source_labels=None):
        _, digest = _encoded_configuration(projects, aliases, source_labels or {})
        with self._factory() as connection:
            return connection.execute('SELECT configuration_hash FROM atom_search.project_context_state WHERE id=1').fetchone()[0] == digest

    def sync(self, projects, aliases, *, expected_settings_revision, source_labels=None,
             unchanged_only=False):
        _integer(expected_settings_revision)
        encoded, digest = _encoded_configuration(projects, aliases, source_labels or {})
        with self._factory() as connection:
            active = PostgresCatalog._active(connection)
            sources = {source for project in projects for source in project.source_ids}
            sources.update(binding.source_id for project in projects for binding in project.bindings)
            sources.update(source for alias in aliases for source in alias.source_ids)
            sources.update((source_labels or {}).keys())
            if sources:
                for source in sources:
                    _integer(source)
                available = {row[0] for row in connection.execute(
                    'SELECT id FROM atom_search.sources WHERE catalog_id=%s AND id=ANY(%s::bigint[])',
                    (active, list(sources))).fetchall()}
                if available != sources:
                    raise ValueError('project_source_outside_active_catalog')
            row = connection.execute('''SELECT context_revision,settings_revision,configuration_hash
                FROM atom_search.project_context_state WHERE id=1 FOR UPDATE''').fetchone()
            revision, settings_revision, previous = row
            if expected_settings_revision < settings_revision:
                raise ValueError('project_settings_stale')
            if expected_settings_revision == settings_revision and previous and digest != previous:
                raise ValueError('project_settings_revision_conflict')
            if unchanged_only and digest != previous:
                raise ValueError('project_context_changed')
            if previous == digest:
                if expected_settings_revision != settings_revision:
                    connection.execute('UPDATE atom_search.project_context_state SET settings_revision=%s WHERE id=1',
                                       (expected_settings_revision,))
                return revision
            revision = _integer(revision + 1)
            for project in projects:
                connection.execute('''INSERT INTO atom_search.projects(project_uuid,canonical_name,context_revision)
                    VALUES (%s,%s,%s) ON CONFLICT(project_uuid) DO UPDATE SET
                    canonical_name=excluded.canonical_name,context_revision=excluded.context_revision''',
                    (project.project_id, project.canonical_name, revision))
            connection.execute('DELETE FROM atom_search.project_bindings')
            for project in projects:
                for binding in project.bindings:
                    if binding.confirmed:
                        connection.execute('''INSERT INTO atom_search.project_bindings
                            (project_uuid,source_id,path_prefix) VALUES (%s,%s,%s)''',
                            (project.project_id, binding.source_id, binding.path_prefix))
            connection.execute('''UPDATE atom_search.project_context_state SET context_revision=%s,
                settings_revision=%s,configuration_hash=%s,configuration=%s::jsonb,checkpoint_file_id=0 WHERE id=1''',
                (revision, expected_settings_revision, digest, encoded))
            connection.execute('UPDATE atom_search.catalogs SET generation=generation+1 WHERE id=%s', (active,))
            return revision

    def pending_context(self, limit, *, scope=None, require_prepared=False):
        _integer(limit)
        if limit > 1000:
            raise ValueError('context_batch_exceeds_1000')
        ids = None if scope is None else [_integer(i) for i in scope.source_ids]
        if ids == []:
            return []
        with self._factory() as connection:
            rows = connection.execute(f'''SELECT {_FILE_COLUMNS} FROM atom_search.files f
                JOIN atom_search.sources s ON s.id=f.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                CROSS JOIN atom_search.project_context_state st
                LEFT JOIN atom_search.file_project_context pc ON pc.file_id=f.id
                WHERE c.state='active' AND st.id=1
                AND (%s::bigint[] IS NULL OR f.source_id=ANY(%s::bigint[]))
                AND (pc.file_id IS NULL OR pc.file_revision<>f.revision
                    OR pc.context_revision<>st.context_revision OR (%s AND pc.input_hash IS NULL))
                ORDER BY f.id LIMIT %s''', (ids, ids, require_prepared, limit)).fetchall()
            return [CatalogFile(*row) for row in rows]

    def configuration(self):
        with self._factory() as connection:
            row = connection.execute('''SELECT context_revision,configuration
                FROM atom_search.project_context_state WHERE id=1''').fetchone()
        return row[0], context_configuration(row[1])

    def publish_context(self, context, *, expected_file_revision, prepared):
        _integer(expected_file_revision)
        raw = metadata_embedding_text(context)
        if prepared is not None and (not raw.startswith(prepared.text)
                or prepared.input_hash != hashlib.sha256(prepared.text.encode('utf-8')).hexdigest()
                or type(prepared.token_count) is not int or not 1 <= prepared.token_count <= 512):
            raise ValueError('invalid_prepared_context')
        with self._factory() as connection:
            active = PostgresCatalog._active(connection)
            state = connection.execute('''SELECT context_revision,configuration
                FROM atom_search.project_context_state WHERE id=1 FOR UPDATE''').fetchone()
            if context.context_revision != state[0]:
                return False
            row = connection.execute(f'''SELECT {_FILE_COLUMNS} FROM atom_search.files f
                JOIN atom_search.sources s ON s.id=f.source_id WHERE f.id=%s AND f.source_id=%s
                AND f.revision=%s AND s.catalog_id=%s FOR UPDATE OF s,f''',
                (context.file_id, context.source_id, expected_file_revision, active)).fetchone()
            if row is None:
                return False
            projects, aliases, labels = context_configuration(state[1])
            expected = build_document_context(CatalogFile(*row), source_label=labels.get(context.source_id, ''),
                projects=projects, aliases=aliases, context_revision=state[0])
            if context != expected:
                return False
            connection.execute('''INSERT INTO atom_search.file_project_context(file_id,file_revision,
                context_revision,project_uuid,source_label,context_text,raw_context_hash,prepared_text,
                input_hash,token_count,fts_russian,fts_simple)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('russian',%s),to_tsvector('simple',%s))
                ON CONFLICT(file_id) DO UPDATE SET file_revision=excluded.file_revision,
                context_revision=excluded.context_revision,project_uuid=excluded.project_uuid,
                source_label=excluded.source_label,context_text=excluded.context_text,
                raw_context_hash=excluded.raw_context_hash,prepared_text=excluded.prepared_text,
                input_hash=excluded.input_hash,token_count=excluded.token_count,
                fts_russian=excluded.fts_russian,fts_simple=excluded.fts_simple''',
                (context.file_id, expected_file_revision, state[0], context.project_id, context.source_label,
                 raw, metadata_input_hash(context), prepared.text if prepared else None,
                 prepared.input_hash if prepared else None, prepared.token_count if prepared else None,
                 normalize(raw), normalize(raw)))
            connection.execute('''UPDATE atom_search.project_context_state
                SET checkpoint_file_id=GREATEST(checkpoint_file_id,%s) WHERE id=1''', (context.file_id,))
            return True

    def publish_batch(self, records, *, cancel=None, publish_if=None):
        """One bounded transaction/checkpoint; cancellation rolls back the batch."""
        records = tuple(records)
        if len(records) > 1000:
            raise ValueError('context_batch_exceeds_1000')
        class Cancelled(Exception):
            pass
        def check():
            if cancel is not None and cancel.is_set():
                raise Cancelled
        try:
            check()
            if publish_if is not None and not publish_if():
                return 0
            with self._factory() as connection:
                batch = ProjectProjection(lambda: nullcontext(connection))
                accepted = 0
                for context, revision, prepared in records:
                    check()
                    accepted += batch.publish_context(context, expected_file_revision=revision, prepared=prepared)
                check()
                if publish_if is not None and not publish_if():
                    raise Cancelled
            return accepted
        except Cancelled:
            return 0
