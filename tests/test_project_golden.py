"""One honest corpus drives public source and frozen project acceptance."""
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import pytest

from atom_search.service.contracts import SearchResponse, QueryInterpretation, ResultMetadata

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/fixtures/project-search-v2'


def checks():
    name = 'atom_search.project_checks'
    assert importlib.util.find_spec(name), 'Shared public project corpus runner is missing'
    return importlib.import_module(name)


def test_project_v2_golden_queries_cover_every_acceptance_case():
    corpus = checks().load_project_corpus(CORPUS)
    cases = {case['id']: case for case in corpus['queries']}
    assert {'reverse', 'full-name', 'short-scoped', 'ambiguous', 'selected', 'direct-root',
            'unknown', 'unconfirmed', 'same-name', 'offline', 'revoked', 'order', 'period',
            'location', 'instructions', 'empty-scope', 'forbidden-source', 'forbidden-project'} <= cases.keys()
    assert cases['reverse']['query'] == '  Пермские моторы зенитные фонари  '
    assert cases['reverse'].get('project_id') is None
    assert cases['selected']['query'] == cases['ambiguous']['query'] == 'ПМ спецификация'
    assert all(item['kind'] == 'fictional_txt' and item['path'].endswith('.txt')
               for item in corpus['manifest']['files'])
    assert len(corpus['digest']) == 64


@pytest.mark.parametrize('fault', [None, 'source', 'query', 'first', 'fields', 'options',
    'duplicate', 'stale', 'alias-missing', 'alias-id', 'alias-text', 'alias-name'])
def test_shared_runner_rejects_wrong_public_results_or_explanations(tmp_path, fault):
    operation = checks().run_project_search_checks
    shutil.copytree(CORPUS, tmp_path / 'corpus')
    folder = tmp_path / 'corpus'
    queries = json.loads((folder / 'queries.json').read_text(encoding='utf-8'))
    key = 'short-scoped' if fault and fault.startswith('alias-') else 'reverse'
    queries['queries'] = [case for case in queries['queries'] if case['id'] == key]
    (folder / 'queries.json').write_text(json.dumps(queries, ensure_ascii=False), encoding='utf-8')
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    files = {item['id']: item for item in manifest['files']}
    case = queries['queries'][0]
    rows = tuple(ResultMetadata(f'00000000-0000-4000-8000-{i:012d}', files[key]['source_id'],
        Path(files[key]['path']).name, files[key]['path'], 'C:/fictional/' + files[key]['path'],
        1, 1, False, case['expected_project_id'], ('path', 'project'), 1., 'lexical')
        for i, key in enumerate(case['expected_results'], 1))
    if fault == 'source': rows = (replace(rows[0], source_id=manifest['sources'][2]['id']), *rows[1:])
    if fault == 'first': rows = rows[::-1]
    if fault == 'fields': rows = tuple(replace(row, matched_fields=()) for row in rows)
    if fault == 'stale': rows = tuple(replace(row, stale=True) for row in rows)
    if key == 'short-scoped':
        rows = tuple(replace(row, matched_fields=('alias',)) for row in rows)
    if fault == 'duplicate': rows = (*rows, rows[0])
    response = SearchResponse(rows, QueryInterpretation(
        'rewritten' if fault == 'query' else case['query'], 'normalized', None, ()), (), (), 1, ())
    if key == 'short-scoped':
        from atom_search.service.contracts import AppliedAlias
        alias = AppliedAlias(**case['expected_aliases'][0])
        if fault == 'alias-id': alias = replace(alias, alias_id='00000000-0000-4000-8000-000000000999')
        if fault == 'alias-text': alias = replace(alias, matched_text='П')
        if fault == 'alias-name': alias = replace(alias, canonical_name='Invented')
        response = replace(response, applied_aliases=() if fault == 'alias-missing' else (alias,))
    if fault == 'options':
        from atom_search.service.contracts import QueryAmbiguity, ProjectOption
        response = replace(response, ambiguities=(QueryAmbiguity('ПМ', (
            ProjectOption(manifest['projects'][-1]['id'], 'Private', (manifest['sources'][2]['id'],)),)),))
    def fetch(identity):
        if fault is None:
            time.sleep(.01)  # Query latency must exclude QA roundtrip checks.
        return replace(next(row for row in rows if row.result_id == identity),
                       project_id=None, matched_fields=(), score=None, match_kind='metadata')
    report = operation(SimpleNamespace(search=lambda request: response, fetch=fetch), folder)
    assert report['ok'] is (fault is None)
    assert report['failed_cases'] == ([] if fault is None else [key])
    if fault:
        check = {'source': 'scope', 'query': 'original_query', 'first': 'ranking',
                 'fields': 'fields', 'options': 'options', 'duplicate': 'results',
                 'stale': 'freshness',
                 'alias-missing': 'aliases', 'alias-id': 'aliases', 'alias-text': 'aliases',
                 'alias-name': 'aliases'}[fault]
        assert report['cases'][0]['checks'][check] is False
    else:
        row = report['cases'][0]
        assert row['check_elapsed_ms'] >= row['search_latency_ms'] + 15


@pytest.mark.parametrize('projection_state,returned_state', [
    ('pending', 'pending'), ('current', 'current'),
    ('pending', 'current'), ('current', 'pending')])
def test_ambiguous_literal_has_exact_projection_specific_expectations(tmp_path, projection_state, returned_state):
    from atom_search.service.contracts import ProjectOption, QueryAmbiguity
    shutil.copytree(CORPUS, tmp_path / 'corpus')
    folder = tmp_path / 'corpus'
    queries = json.loads((folder / 'queries.json').read_bytes())
    case = next(case for case in queries['queries'] if case['id'] == 'ambiguous')
    queries['queries'] = [case]
    (folder / 'queries.json').write_bytes(json.dumps(queries, ensure_ascii=False).encode('utf-8'))
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    files = {item['id']: item for item in manifest['files']}
    projects = {item['id']: item for item in manifest['projects']}
    rows = tuple(ResultMetadata(f'00000000-0000-4000-8000-{i:012d}', files[key]['source_id'],
        Path(files[key]['path']).name, files[key]['path'], 'C:/fictional/' + files[key]['path'],
        1, 1, False, files[key]['project_id'], ('name', 'path', 'alias'), 1., 'lexical')
        for i, key in enumerate(case['expected_results_by_projection'][returned_state], 1))
    options = tuple(ProjectOption(key, projects[key]['canonical_name'], tuple(case['source_ids']))
                    for key in case['expected_options'])
    response = SearchResponse(rows, QueryInterpretation(case['query'], case['query'].casefold(), None, ()),
        (), (QueryAmbiguity('ПМ', options),), 1, ())
    def fetch(identity):
        return replace(next(row for row in rows if row.result_id == identity),
                       project_id=None, matched_fields=(), score=None, match_kind='metadata')
    report = checks().run_project_search_checks(SimpleNamespace(search=lambda request: response, fetch=fetch),
        folder, projection_state=projection_state)
    assert report['projection_state'] == projection_state
    assert report['ok'] is (projection_state == returned_state)
    assert report['cases'][0]['checks']['results'] is (projection_state == returned_state)


@pytest.mark.parametrize('fault', ['hash', 'escape'])
def test_corpus_refuses_changed_bytes_or_escape_before_owner_creation(tmp_path, fault):
    shutil.copytree(CORPUS, tmp_path / 'corpus')
    folder = tmp_path / 'corpus'
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if fault == 'hash':
        (folder / manifest['files'][0]['file']).write_bytes(b'changed')
    else:
        manifest['files'][0]['file'] = '../outside.txt'
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='project_corpus'):
        checks().load_project_corpus(folder)


def test_project_corpus_is_an_explicit_frozen_input():
    spec = importlib.util.spec_from_file_location('project_build_support', ROOT / 'packaging/build_support.py')
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    assert (str(CORPUS), 'resources/project-search-v2') in support.collect_datas(ROOT)


def test_golden_cli_refuses_existing_profile_before_owner_or_qt(tmp_path, monkeypatch):
    from atom_search.desktop.app import main
    import atom_search.service.owner as owner
    def forbidden(*args, **kwargs):
        pytest.fail('golden gate touched an existing profile')
    monkeypatch.setattr(owner, 'open_service_owner', forbidden)
    folder = tmp_path / 'present'
    folder.mkdir()
    report = tmp_path / 'golden.json'
    assert main(['--service-test', str(report), '--service-test-scenario', 'project-golden',
                 '--data-dir', str(folder)]) == 1
    assert json.loads(report.read_bytes())['error_code'] == 'disposable_profile_must_be_new'
    assert list(folder.iterdir()) == []
