"""Check the actual frozen shared corpus on a fresh disposable profile."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from atom_search.project_checks import (load_project_corpus, project_corpus_path,
                                       run_project_search_checks)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    executable, directory, report = (path.absolute() for path in
                                    (args.executable, args.data_dir, args.report))
    if (directory.exists() or report.exists() or report.is_relative_to(directory)
            or not executable.is_file()):
        parser.error('executable required; profile/report must be new and separate')
    corpus = load_project_corpus(project_corpus_path())
    before = hashlib.sha256(executable.read_bytes()).hexdigest()
    result = subprocess.run([str(executable), '--service-test', str(report),
        '--service-test-scenario', 'project-golden', '--data-dir', str(directory)],
        timeout=600, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not report.is_file():
        return 1
    proof = json.loads(report.read_bytes())
    phases = proof.get('phases', [])
    expected = {case['id'] for case in corpus['queries']}
    observed = {case['id'] for phase in phases for case in phase.get('cases', [])}
    expected_phases = [('baseline', 'pending'), ('pending', 'pending'), ('baseline', 'current'),
        ('revoked-pending', 'pending'), ('revoked-current', 'current'),
        ('revoked-current', 'current'), ('offline', 'current')]
    valid = (hashlib.sha256(executable.read_bytes()).hexdigest() == before
        and proof.get('execution_mode') == 'frozen' and proof.get('scenario') == 'project-golden'
        and proof.get('backend') == 'postgresql' and proof.get('factory') == 'open_service_owner'
        and re.fullmatch('[0-9a-f]{40}', proof.get('source_commit') or '')
        and proof.get('source_clean') is True and proof.get('corpus_sha256') == corpus['digest']
        and proof.get('corpus_queries') == len(expected) and observed == expected
        and [(phase.get('phase'), phase.get('projection_state')) for phase in phases] == expected_phases
        and proof.get('catalog_schema') == 5 and proof.get('settings_schema') == 3
        and all(proof.get(name) is True for name in ('ok', 'clean_stop', 'model_missing_lexical',
            'cached_readiness', 'pending_scoped_alias', 'restart_public_ids', 'no_document_reread'))
        and phases and all(phase.get('ok') is True and phase.get('corpus_sha256') == corpus['digest']
            and not phase.get('failed_cases') and phase.get('cases')
            and all(case.get('passed') is True and case.get('checks')
                and all(value is True for value in case['checks'].values())
                for case in phase['cases']) for phase in phases))
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())
