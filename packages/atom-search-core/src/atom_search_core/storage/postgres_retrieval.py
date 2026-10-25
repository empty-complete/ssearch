"""Scoped SQL candidates; caller owns connections and the PostgreSQL runtime."""
from collections.abc import Callable, Sequence
from typing import Any
import uuid

from ..models import CatalogFile, LexicalHit, ProjectFilter, ProjectResolution, SearchScope, VectorHit
from ..search import compact, normalize, query_terms
from ..project_context import INPUT_SCHEMA
from .postgres import _FILE_COLUMNS, _integer, _vector_literal


_JOINS = """FROM atom_search.file_embeddings e
    JOIN atom_search.files f ON f.id=e.file_id AND f.revision=e.file_revision
    JOIN atom_search.sources s ON s.id=f.source_id
    JOIN atom_search.catalogs c ON c.id=s.catalog_id
    WHERE c.state='active' AND s.id=ANY(%s::bigint[]) AND e.model_key=%s"""

_CURRENT_CONTEXT = ''' AND EXISTS (SELECT 1 FROM atom_search.file_project_context pc
    JOIN atom_search.project_context_state st ON st.id=1 AND st.context_revision=pc.context_revision
    WHERE pc.file_id=f.id AND pc.file_revision=f.revision AND pc.input_hash=e.input_hash)'''


def _fold(expression):
    # Search normalization is separate from Windows path membership below.
    return f"replace(casefold(normalize({expression},NFKC) COLLATE pg_unicode_fast),'ё','е')"


def _project_ids(project_filter):
    if (not isinstance(project_filter, ProjectFilter) or project_filter.input_schema != INPUT_SCHEMA
            or type(project_filter.context_revision) is not int):
        raise ValueError('invalid_project_filter')
    _integer(project_filter.context_revision)
    if project_filter.project_ids is None:
        return None
    if type(project_filter.project_ids) is not tuple:
        raise ValueError('invalid_project_filter')
    try:
        if any(type(value) is not str or str(uuid.UUID(value)) != value for value in project_filter.project_ids):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid_project_filter') from None
    return list(dict.fromkeys(project_filter.project_ids))


# Only confirmed CURRENT bindings can supply pending membership. Full Unicode
# lower (PG18 builtin, independent of the database's C locale) matches the pure
# path segment contract; neither NFKC/ё folding nor LIKE wildcards belong here.
_PROJECT_FROM = '''FROM atom_search.files f
    JOIN atom_search.sources s ON s.id=f.source_id
    JOIN atom_search.catalogs c ON c.id=s.catalog_id
    CROSS JOIN atom_search.project_context_state st
    LEFT JOIN atom_search.file_project_context pc ON pc.file_id=f.id
        AND pc.file_revision=f.revision AND pc.context_revision=st.context_revision
    LEFT JOIN LATERAL (
        SELECT DISTINCT b.project_uuid FROM atom_search.project_bindings b
        JOIN atom_search.projects current_project ON current_project.project_uuid=b.project_uuid
            AND current_project.context_revision=st.context_revision
        WHERE pc.file_id IS NULL AND b.source_id=f.source_id AND (b.path_prefix=''
            OR (string_to_array(lower(f.relative_path COLLATE pg_unicode_fast),'/'))[
                1:cardinality(string_to_array(lower(b.path_prefix COLLATE pg_unicode_fast),'/'))]
                =string_to_array(lower(b.path_prefix COLLATE pg_unicode_fast),'/'))
    ) pending ON true
    LEFT JOIN atom_search.projects p ON p.project_uuid=COALESCE(pc.project_uuid,pending.project_uuid)
        AND p.context_revision=st.context_revision
    LEFT JOIN LATERAL (
        SELECT string_agg(variant,' ') AS text FROM jsonb_array_elements(
            COALESCE(st.configuration->'aliases','[]'::jsonb)) a(value)
        CROSS JOIN LATERAL jsonb_array_elements_text(a.value->'variants') variants(variant)
        WHERE a.value->>'confirmed'='true'
            AND (a.value->>'project_id')::uuid=p.project_uuid
            AND (a.value->'source_ids') @> to_jsonb(f.source_id)
    ) aliases ON true'''

_PROJECT_WHERE = '''c.state='active' AND s.id=ANY(%s::bigint[])
    AND st.id=1 AND st.context_revision=%s
    AND (%s::uuid[] IS NULL OR p.project_uuid=ANY(%s::uuid[]))'''


class PostgresRetrieval:
    def __init__(self, connection_factory: Callable[[], Any]):
        self._factory = connection_factory

    def literal_candidates(self, query: str, *, scope: SearchScope, limit: int,
                           project_filter: ProjectFilter | None = None) -> list[LexicalHit]:
        if project_filter is not None:
            return self._project_lexical(query, scope=scope, limit=limit,
                                         project_filter=project_filter, literal=True)
        sources = [_integer(source) for source in scope.source_ids]
        needle = compact(query)
        if not sources or limit <= 0 or not needle:
            return []
        _integer(limit)
        with self._factory() as connection:
            rows = connection.execute(f'''WITH q AS (SELECT %s::text AS needle)
                SELECT {_FILE_COLUMNS},
                    CASE WHEN f.compact_name=q.needle THEN 250 ELSE 0 END
                    + CASE WHEN f.compact_stem=q.needle THEN 200 ELSE 0 END
                    + CASE WHEN strpos(f.compact_name,q.needle)>0 THEN 90 ELSE 0 END
                    + CASE WHEN strpos(f.compact_path,q.needle)>0 THEN 15 ELSE 0 END AS score
                FROM atom_search.files f JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id CROSS JOIN q
                WHERE c.state='active' AND s.id=ANY(%s::bigint[])
                  AND (strpos(f.compact_name,q.needle)>0 OR strpos(f.compact_path,q.needle)>0)
                ORDER BY score DESC,f.id ASC LIMIT %s''', (needle, sources, limit)).fetchall()
        return [LexicalHit(CatalogFile(*row[:9]), float(row[9])) for row in rows]

    def lexical_candidates(self, query: str, *, scope: SearchScope, limit: int,
                           project_filter: ProjectFilter | None = None) -> list[LexicalHit]:
        if project_filter is not None:
            return self._project_lexical(query, scope=scope, limit=limit,
                                         project_filter=project_filter)
        sources = [_integer(source) for source in scope.source_ids]
        terms = query_terms(query)
        if not sources or limit <= 0 or not terms:
            return []
        _integer(limit)
        # query_terms contains only Unicode letters/numbers. Operators never
        # come from raw input; plain text and prefix syntax are bound separately.
        prefix = ' & '.join(term + ':*' for term in terms)
        with self._factory() as connection:
            result = connection.execute(f"""WITH q AS (
                SELECT %s::text AS compact, plainto_tsquery('russian',%s) AS ru,
                       plainto_tsquery('simple',%s) AS simple, to_tsquery('simple',%s) AS prefix)
                SELECT {_FILE_COLUMNS},
                    CASE WHEN f.compact_name=q.compact THEN 250 ELSE 0 END
                    + CASE WHEN f.compact_stem=q.compact THEN 200 ELSE 0 END
                    + CASE WHEN strpos(f.compact_name,q.compact)>0 THEN 90 ELSE 0 END
                    + CASE WHEN strpos(f.compact_path,q.compact)>0 THEN 15 ELSE 0 END
                    + 100*ts_rank_cd(f.fts_russian,q.ru)
                    + 50*ts_rank_cd(f.fts_simple,q.simple)
                    + 10*ts_rank_cd(f.fts_simple,q.prefix) AS score
                FROM atom_search.files f JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id CROSS JOIN q
                WHERE c.state='active' AND s.id=ANY(%s::bigint[])
                    AND (f.compact_name=q.compact OR f.compact_stem=q.compact
                         OR strpos(f.compact_name,q.compact)>0 OR strpos(f.compact_path,q.compact)>0
                         OR f.fts_russian @@ q.ru OR f.fts_simple @@ q.simple OR f.fts_simple @@ q.prefix)
                ORDER BY score DESC,f.id ASC LIMIT %s""",
                (compact(query), normalize(query), normalize(query), prefix, sources, limit)).fetchall()
        return [LexicalHit(CatalogFile(*row[:9]), float(row[9])) for row in result]

    def project_candidates(self, resolution: ProjectResolution, *, scope: SearchScope,
                           context_revision: int, limit: int, mode: str = 'lexical') -> list[LexicalHit]:
        if not isinstance(resolution, ProjectResolution) or mode not in ('literal', 'lexical', 'hybrid'):
            raise ValueError('invalid_search_request')
        selected = ((resolution.selected_project_id,) if resolution.selected_project_id is not None
                    else resolution.preferred_project_ids or None)
        query = resolution.residual_query if selected is not None else resolution.original_query
        return self._project_lexical(query, scope=scope, limit=limit,
            project_filter=ProjectFilter(selected, context_revision, INPUT_SCHEMA),
            literal=mode == 'literal', evidence_query=resolution.original_query)

    def _project_lexical(self, query, *, scope, limit, project_filter, literal=False, evidence_query=None):
        sources = [_integer(source) for source in scope.source_ids]
        projects = _project_ids(project_filter)
        if not sources or projects == [] or limit <= 0:
            return []
        _integer(limit)
        terms = query_terms(query)
        if not terms and (query.strip() or projects is None):
            return []
        prefix = ' & '.join(term + ':*' for term in terms)
        fields = [('name', 'f.name'), ('path', 'f.relative_path'), ('root', 's.path'),
                  ('source_label', "COALESCE(st.configuration->'source_labels'->>f.source_id::text,'')"),
                  ('project', "COALESCE(p.canonical_name,'')"), ('alias', "COALESCE(aliases.text,'')")]
        values = ','.join(f"('{name}',{_fold(value)})" for name, value in fields)
        fallback_text = ' || E\'\\n\' || '.join(_fold(value) for _, value in fields[:-1])
        # The stored projection is usable only at the current file/context CAS.
        # Pending FTS uses canonical binding + metadata. Current confirmed,
        # scoped aliases can explain an already resolved cohort before backfill.
        ru = f"CASE WHEN pc.file_id IS NOT NULL THEN pc.fts_russian ELSE to_tsvector('russian',{fallback_text}) END"
        simple = f"CASE WHEN pc.file_id IS NOT NULL THEN pc.fts_simple ELSE to_tsvector('simple',{fallback_text}) END"
        full_text = '' if literal else f'''OR ({ru}) @@ q.ru
            OR ({simple}) @@ q.simple OR ({simple}) @@ q.prefix'''
        with self._factory() as connection:
            result = connection.execute(f'''WITH q AS (SELECT %s::text AS compact,
                plainto_tsquery('russian',%s) AS ru, plainto_tsquery('simple',%s) AS simple,
                CASE WHEN %s::text='' THEN NULL ELSE to_tsquery('simple',%s) END AS prefix,
                %s::text[] AS terms)
                SELECT {_FILE_COLUMNS},
                    CASE WHEN f.compact_name=q.compact THEN 250 ELSE 0 END
                    + CASE WHEN f.compact_stem=q.compact THEN 200 ELSE 0 END
                    + CASE WHEN q.compact<>'' AND strpos(f.compact_name,q.compact)>0 THEN 90 ELSE 0 END
                    + CASE WHEN q.compact<>'' AND strpos(f.compact_path,q.compact)>0 THEN 15 ELSE 0 END
                    + COALESCE(100*ts_rank_cd(({ru}),q.ru),0)
                    + COALESCE(50*ts_rank_cd(({simple}),q.simple),0)
                    + COALESCE(10*ts_rank_cd(({simple}),q.prefix),0) AS score,
                    p.project_uuid::text,evidence.fields
                {_PROJECT_FROM} CROSS JOIN q
                CROSS JOIN LATERAL (SELECT ARRAY(SELECT field FROM (VALUES {values}) actual(field,value)
                    WHERE (q.compact<>'' AND strpos(regexp_replace(value COLLATE pg_unicode_fast,
                        '[^[:alnum:]]','','g'),q.compact)>0)
                    OR EXISTS (SELECT 1 FROM unnest(q.terms) term
                        WHERE to_tsvector('russian',value) @@ plainto_tsquery('russian',term)
                            OR to_tsvector('simple',value) @@ to_tsquery('simple',term || ':*'))
                    ) AS fields) evidence
                WHERE {_PROJECT_WHERE} AND cardinality(evidence.fields)>0
                    AND (q.compact='' OR strpos(f.compact_name,q.compact)>0
                        OR strpos(f.compact_path,q.compact)>0 {full_text})
                ORDER BY score DESC,f.id ASC LIMIT %s''',
                (compact(query), normalize(query), normalize(query), prefix, prefix,
                 query_terms(query if evidence_query is None else evidence_query), sources,
                 project_filter.context_revision, projects, projects, limit)).fetchall()
        return [LexicalHit(CatalogFile(*row[:9]), float(row[9]), row[10], tuple(row[11])) for row in result]

    def vector_candidates(self, vector: Sequence[float], *, model_key: str,
                          scope: SearchScope, limit: int,
                          project_filter: ProjectFilter | None = None) -> list[VectorHit]:
        sources = [_integer(source) for source in scope.source_ids]
        projects = _project_ids(project_filter) if project_filter is not None else None
        if projects == []:
            return []
        if not sources or limit <= 0:
            return []
        _integer(limit)
        literal = _vector_literal(vector)
        joins = _JOINS + (_CURRENT_CONTEXT if model_key.endswith('|' + INPUT_SCHEMA) else '')
        parameters = (sources, model_key)
        project_column = 'NULL::text'
        if project_filter is not None:
            if not model_key.endswith('|' + project_filter.input_schema):
                raise ValueError('invalid_project_filter')
            joins = '''FROM atom_search.file_embeddings e
                JOIN atom_search.files f ON f.id=e.file_id AND f.revision=e.file_revision
                JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id
                JOIN LATERAL (
                    SELECT vctx.project_uuid FROM atom_search.file_project_context vctx
                    JOIN atom_search.project_context_state st ON st.id=1 AND st.context_revision=vctx.context_revision
                    JOIN atom_search.embedding_models m ON m.model_key=e.model_key
                    WHERE vctx.file_id=f.id AND vctx.file_revision=f.revision
                        AND st.context_revision=%s AND m.input_schema=%s
                        AND vctx.input_hash=e.input_hash AND vctx.prepared_text IS NOT NULL
                        AND vctx.input_hash=encode(sha256(convert_to(vctx.prepared_text,'UTF8')),'hex')
                        AND (vctx.project_uuid IS NULL OR EXISTS (SELECT 1 FROM atom_search.projects p
                            WHERE p.project_uuid=vctx.project_uuid AND p.context_revision=st.context_revision))
                        AND (%s::uuid[] IS NULL OR vctx.project_uuid=ANY(%s::uuid[]))
                    OFFSET 0
                ) pc ON true
                WHERE c.state='active' AND s.id=ANY(%s::bigint[]) AND e.model_key=%s'''
            # Keep current metadata eligibility correlated to each ordered
            # vector. Pulling the project table ahead of this scan made large
            # cohorts silently use a full exact Sort (measured native EXPLAIN).
            # OFFSET 0 is a planner fence, never a candidate LIMIT/postfilter.
            parameters = (project_filter.context_revision, project_filter.input_schema, projects, projects,
                          sources, model_key)
            project_column = 'pc.project_uuid::text'
        try:
            with self._factory() as connection:
                connection.execute("SET LOCAL statement_timeout='2000ms'")
                connection.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
                # Measured together with migration v2's graph on the synthetic
                # 20k/384D recall fixture. Cost tuning is local, not a host claim.
                connection.execute('SET LOCAL hnsw.ef_search=400')
                connection.execute('SET LOCAL hnsw.max_scan_tuples=20000')
                connection.execute('SET LOCAL random_page_cost=1.1')
                # Only the threshold and requested count matter. Never count an
                # entire large catalog just to select exact versus approximate.
                eligible = connection.execute(f"""SELECT count(*) FROM (
                    SELECT 1 {joins} LIMIT %s) eligible""",
                    (*parameters, max(1001, limit))).fetchone()[0]
                if not eligible:
                    return []
                if eligible <= 1000:
                    result = self._exact(connection, literal, sources, model_key, limit,
                        joins=joins, parameters=parameters, project_column=project_column)
                else:
                    # PostgreSQL 18 retains the HNSW path with Incremental Sort
                    # for the secondary id key (verified on the native fixture).
                    result = connection.execute(f"""SELECT {_FILE_COLUMNS},
                        e.embedding <=> %s::vector AS distance,{project_column} {joins}
                        ORDER BY e.embedding <=> %s::vector,f.id LIMIT %s""",
                        (literal, *parameters, literal, limit)).fetchall()
                    if len(result) < min(limit, eligible):
                        result = self._exact(connection, literal, sources, model_key, limit,
                            joins=joins, parameters=parameters, project_column=project_column)
        except Exception as error:
            if getattr(error, 'sqlstate', None) == '57014':
                # SearchService already preserves lexical results and exposes
                # exception text. Do not forward SQL, paths or bound values.
                raise TimeoutError('PostgreSQL vector search timed out') from None
            raise
        return [VectorHit(CatalogFile(*row[:9]), 1.0-float(row[9]), row[10]) for row in result]

    @staticmethod
    def _exact(connection, literal, sources, model_key, limit, *, joins=_JOINS,
               parameters=None, project_column='NULL::text'):
        parameters = (sources, model_key) if parameters is None else parameters
        return connection.execute(f"""WITH eligible AS MATERIALIZED (
            SELECT {_FILE_COLUMNS},e.embedding,{project_column} AS project_id {joins})
            SELECT id,source_id,path,relative_path,name,size,modified_ns,status,revision,
                embedding <=> %s::vector AS distance,project_id FROM eligible
            ORDER BY distance,id LIMIT %s""", (*parameters, literal, limit)).fetchall()
