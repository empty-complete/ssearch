"""The shared synthetic corpus through the actual owned public PG service."""
import json


def test_public_project_golden_corpus_readiness_pending_revoke_and_restart(tmp_path):
    from atom_search.diagnostics import run_project_search_service_test
    from atom_search.project_checks import load_project_corpus, project_corpus_path
    report = tmp_path / 'golden.json'
    result = run_project_search_service_test(report, data_dir=tmp_path / 'g')
    proof = json.loads(report.read_bytes())
    assert result == 0, proof
    corpus = load_project_corpus(project_corpus_path())
    assert proof['execution_mode'] == 'source' and proof['backend'] == 'postgresql'
    assert proof['corpus_sha256'] == corpus['digest']
    assert {case['id'] for phase in proof['phases'] for case in phase['cases']} == {
        case['id'] for case in corpus['queries']}
    assert all(phase['ok'] for phase in proof['phases'])
    assert [(phase['phase'], phase['projection_state']) for phase in proof['phases']] == [
        ('baseline', 'pending'), ('pending', 'pending'), ('baseline', 'current'),
        ('revoked-pending', 'pending'), ('revoked-current', 'current'),
        ('revoked-current', 'current'), ('offline', 'current')]
    assert all(proof[name] is True for name in ('clean_stop', 'cached_readiness',
        'model_missing_lexical', 'pending_scoped_alias', 'restart_public_ids', 'no_document_reread'))
    assert proof['hardware']['processor'] and proof['hardware']['logical_cpus'] > 0
