"""Shared synthetic acceptance data and public checks, never a search backend."""
from __future__ import annotations

import hashlib
from dataclasses import asdict
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import time
import uuid

from .service.contracts import SearchRequest, ServiceError


def project_corpus_path() -> Path:
    if getattr(sys, 'frozen', False):
        from .paths import resource_path
        return resource_path('project-search-v2')
    return Path(__file__).resolve().parents[2] / 'tests/fixtures/project-search-v2'


def _relative(value):
    if (type(value) is not str or not value or '\\' in value or ':' in value
            or PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('invalid_project_corpus_path')
    return value


def _uuid(value):
    return type(value) is str and str(uuid.UUID(value)) == value


def load_project_corpus(directory: Path) -> dict:
    """Validate all inert input bytes before a QA scenario creates its profile."""
    directory = Path(directory).resolve(strict=True)
    raw = {}
    def read(name, limit):
        target = directory / _relative(name)
        if not target.resolve(strict=True).is_relative_to(directory) or target.is_symlink():
            raise ValueError('invalid_project_corpus_path')
        with target.open('rb') as source:
            value = source.read(limit + 1)
        if len(value) > limit:
            raise ValueError('project_corpus_too_large')
        raw[name] = value
        return value
    manifest = json.loads(read('manifest.json', 65536))
    queries = json.loads(read('queries.json', 65536))
    if (manifest.get('schema_version') != 1 or manifest.get('fictional') is not True
            or manifest.get('metadata_only') is not True or queries.get('schema_version') != 1):
        raise ValueError('invalid_project_corpus')
    sources = {source['id']: source for source in manifest['sources']}
    projects = {project['id']: project for project in manifest['projects']}
    if (len(sources) != len(manifest['sources']) or not 1 <= len(sources) <= 16
            or any(not _uuid(key) for key in (*sources, *projects))
            or not set(manifest['allowed_source_ids']) <= sources.keys()):
        raise ValueError('invalid_project_corpus')
    for source in sources.values():
        _relative(source['directory'])
    files = manifest['files']
    identities = {item['id'] for item in files}
    paths = {(item['source_id'], item['path']) for item in files}
    if not 1 <= len(files) <= 1000 or len(identities) != len(files) or len(paths) != len(files):
        raise ValueError('invalid_project_corpus')
    for item in files:
        if (item['source_id'] not in sources or item['kind'] != 'fictional_txt'
                or not item['path'].endswith('.txt') or item['project_id'] not in (None, *projects)):
            raise ValueError('invalid_project_corpus')
        _relative(item['path'])
        if item['file'] != 'files/' + sources[item['source_id']]['directory'] + '/' + item['path']:
            raise ValueError('invalid_project_corpus_path')
        body = read(item['file'], 16384)
        body.decode('utf-8')
        if hashlib.sha256(body).hexdigest() != item['sha256']:
            raise ValueError('project_corpus_hash_mismatch')
    cases = queries['queries']
    if not 1 <= len(cases) <= 1000 or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('invalid_project_corpus')
    for case in cases:
        if (not isinstance(case['query'], str) or not 1 <= len(case['query'].strip()) <= 512
                or not set(case['expected_results']) <= identities
                or not set(case['expected_options']) <= projects.keys()
                or not set(case.get('source_ids', sources)) <= sources.keys()):
            raise ValueError('invalid_project_corpus')
        variants = case.get('expected_results_by_projection', {})
        if (set(variants) - {'pending', 'current'}
                or any(not set(value) <= identities for value in variants.values())):
            raise ValueError('invalid_project_corpus')
    digest = hashlib.sha256()
    for name, value in sorted(raw.items()):
        digest.update(name.encode('utf-8') + b'\0' + hashlib.sha256(value).digest())
    return dict(manifest=manifest, queries=cases, digest=digest.hexdigest())


def run_project_search_checks(service, fixture_dir: Path, *, phase='baseline',
                              projection_state='current') -> dict:
    """Assert the same public expectations for a facade or authenticated client.

    Fixture IDs are logical names. Published UUIDs are discovered through the
    actual source+relative-path row and checked by fetch and repeated search.
    """
    if projection_state not in ('pending', 'current'):
        raise ValueError('invalid_project_projection_state')
    corpus = load_project_corpus(fixture_dir)
    manifest = corpus['manifest']
    files = {item['id']: item for item in manifest['files']}
    identity = {(item['source_id'], item['path']): item['id'] for item in files.values()}
    project_sources = {item['id']: set(item['source_ids']) for item in manifest['projects']}
    rows = []
    for case in corpus['queries']:
        if case['phase'] != phase:
            continue
        expected_results = case.get('expected_results_by_projection', {}).get(
            projection_state, case['expected_results'])
        allowed = set(case.get('source_ids', manifest['allowed_source_ids']))
        checks = {}
        started = time.perf_counter()
        report = dict(id=case['id'], original_query=case['query'])
        request = SearchRequest(case['query'], source_ids=tuple(case['source_ids'])
            if 'source_ids' in case else None, project_id=case.get('project_id'),
            mode=case['mode'], limit=10)
        try:
            response = service.search(request)
            report['search_latency_ms'] = round((time.perf_counter() - started)*1000, 3)
            checks['expected_response'] = 'expected_error' not in case
            actual = [identity.get((row.source_id, row.relative_path)) for row in response.results]
            checks['scope'] = all(row.source_id in allowed for row in response.results)
            checks['results'] = (None not in actual and len(actual) == len(set(actual))
                and len({row.result_id for row in response.results}) == len(actual)
                and set(actual) == set(expected_results))
            checks['ranking'] = not case.get('expected_first') or actual[:1] == [case['expected_first']]
            checks['original_query'] = response.query_interpretation.original_query == case['query']
            checks['selected_project'] = response.query_interpretation.project_id == case.get('project_id')
            checks['projects'] = all(row.project_id == files[key]['project_id']
                for row, key in zip(response.results, actual) if key is not None)
            checks['fields'] = all(set(case['required_fields']) <= set(row.matched_fields)
                and set(row.matched_fields) <= {'name', 'path', 'root', 'source_label', 'project', 'alias'}
                for row in response.results)
            options = [option for ambiguity in response.ambiguities for option in ambiguity.options]
            checks['options'] = (set(option.project_id for option in options) == set(case['expected_options'])
                and all(set(option.source_ids) and set(option.source_ids) <= allowed
                    and set(option.source_ids) <= project_sources.get(option.project_id, set()) for option in options))
            actual_aliases = [asdict(alias) for alias in response.applied_aliases]
            checks['aliases'] = (sorted(actual_aliases, key=lambda value: json.dumps(value, sort_keys=True))
                == sorted(case['expected_aliases'], key=lambda value: json.dumps(value, sort_keys=True))) and all(alias.source_id in allowed
                and alias.source_id in project_sources.get(alias.project_id, set())
                and alias.matched_text in case['query'] for alias in response.applied_aliases)
            if phase.startswith('revoked'):
                checks['aliases'] &= not response.applied_aliases
            checks['freshness'] = all(row.stale is case['expected_stale'] for row in response.results)
            checks['public_ids'] = all(_uuid(row.result_id) for row in response.results)
            checks['fetch_roundtrip'] = all(service.fetch(row.result_id) == replace_metadata(row)
                                            for row in response.results)
            repeated = service.search(request)
            checks['stable_ids'] = [row.result_id for row in repeated.results] == [row.result_id for row in response.results]
            expected = set(expected_results)
            report.update(top10=actual, public_result_ids=[row.result_id for row in response.results],
                recall_at_10=len(set(actual) & expected) / len(expected) if expected else None,
                matched_fields=[list(row.matched_fields) for row in response.results])
        except ServiceError as failure:
            report['search_latency_ms'] = round((time.perf_counter() - started)*1000, 3)
            checks['expected_error'] = failure.code == case.get('expected_error')
            checks['no_disclosure'] = failure.details == {}
        report.update(checks=checks, passed=bool(checks) and all(checks.values()),
                      check_elapsed_ms=round((time.perf_counter() - started)*1000, 3))
        rows.append(report)
    return dict(ok=bool(rows) and all(row['passed'] for row in rows), phase=phase,
        projection_state=projection_state,
        corpus_sha256=corpus['digest'], cases=rows,
        failed_cases=[row['id'] for row in rows if not row['passed']])


def replace_metadata(row):
    # fetch intentionally has no query attribution/score. Compare immutable
    # public identity and metadata, not a fabricated persisted search score.
    from dataclasses import replace
    return replace(row, project_id=None, matched_fields=(), score=None, match_kind='metadata')
