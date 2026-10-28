"""Review the unchanged P4 candidate with real exact-head source checks.

Historical Quick/installation proofs retain their original source identities.
This policy neither builds nor publishes a binary or grants platform acceptance.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = 'e4e93eebfa51c53ea1703d9a39a34e7c667828ba'
INPUT = 'resources/ci/installer-proof-input.json'
CONFIG = '.github/p4-candidate.json'
CANDIDATE = 'frontend/compatibility/candidate-versions.json'
LOCKS = ('frontend/package-lock.json', 'desktop/package-lock.json')
INSTALLER_ANCHOR = '6a380230b8f26cf5df1798b396c850ab59484c7d'
FULL_CONFIG = '.github/p4-full-installer.json'
ALLOWED = frozenset((CONFIG, '.github/workflows/windows-build.yml',
    'tools/ci_p4_candidate.py', 'tests/test_ci_p4_candidate.py',
    'tools/ci_publish.py', 'tests/test_ci_publish.py'))
INSTALLER_ALLOWED = frozenset((FULL_CONFIG, '.github/workflows/windows-build.yml',
    'tools/ci_installer.py', 'tools/check_installer.ps1', 'tools/run_ci_build.ps1',
    'tools/ci_p4_candidate.py', 'tests/test_ci_p4_candidate.py', 'tests/test_installer_qa.py',
    'tests/test_ci_inputs.py', 'tools/ci_publish.py', 'tests/test_ci_publish.py'))
OPEN = frozenset(('qt_upgrade', 'native_windows_11', 'native_visual_accessibility',
    'windows_10_final', 'full_p4_acceptance', 'stable', 'long_qa_paths'))


class CandidateError(RuntimeError):
    """Only fixed public failure codes may cross the CI output boundary."""


def require(value, code):
    if not value:
        raise CandidateError(code)


def load_tool(name):
    spec = importlib.util.spec_from_file_location('ssearch_candidate_' + name,
                                                ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def allowed_delta(api, head):
    return review_delta(api, head, ANCHOR, ALLOWED)


def review_delta(api, head, anchor, allowed):
    require(type(head) is str and re.fullmatch('[0-9a-f]{40}', head), 'candidate_head_invalid')
    comparison = api.get(f'compare/{anchor}...{head}', optional=True)
    if not isinstance(comparison, dict):
        return False
    files = comparison.get('files')
    # GitHub compare exposes at most 300 files. Exactly 300 is ambiguous too.
    return (comparison.get('status') in ('ahead', 'identical')
        and comparison.get('base_commit', {}).get('sha') == anchor
        and comparison.get('merge_base_commit', {}).get('sha') == anchor
        and type(files) is list and len(files) < 300
        and all(type(item) is dict and type(item.get('filename')) is str and item['filename'] in allowed
                and ('previous_filename' not in item or
                     (type(item['previous_filename']) is str and item['previous_filename'] in allowed))
                for item in files))


def candidate_route(api, pr, head, *, merge_sha=None):
    return review_route(api, pr, head, ANCHOR, ALLOWED, merge_sha)


def installer_route(api, pr, head, *, merge_sha=None):
    return review_route(api, pr, head, INSTALLER_ANCHOR, INSTALLER_ALLOWED, merge_sha)


def review_route(api, pr, head, anchor, allowed, merge_sha):
    require(type(pr) is dict and pr.get('head', {}).get('sha') == head
        and pr.get('head', {}).get('repo', {}).get('full_name') == 'empty-complete/ssearch'
        and pr.get('base', {}).get('repo', {}).get('full_name') == 'empty-complete/ssearch'
        and pr.get('base', {}).get('ref') == 'main', 'candidate_pr_identity_mismatch')
    if not review_delta(api, head, anchor, allowed):
        return False
    if merge_sha is not None:
        require(type(merge_sha) is str and re.fullmatch('[0-9a-f]{40}', merge_sha)
            and pr.get('merged') is True and bool(pr.get('merged_at'))
            and pr.get('merge_commit_sha') == merge_sha, 'candidate_merge_identity_mismatch')
        source_tree = api.get(f'git/commits/{head}').get('tree', {}).get('sha')
        merged_tree = api.get(f'git/commits/{merge_sha}').get('tree', {}).get('sha')
        require(type(source_tree) is str and re.fullmatch('[0-9a-f]{40}', source_tree)
            and source_tree == merged_tree, 'candidate_merge_tree_mismatch')
    return True


def review_settings(route):
    require(route in ('candidate-source', 'installer-source'), 'candidate_route_required')
    return dict(mode='source', desktop_stack=False, publish=False,
        authentication='full6a' if route == 'installer-source' else 'candidate',
        test_targets=['tests/test_ci_p4_candidate.py', 'tests/test_ci_publish.py', 'tests/test_ci_inputs.py',
                      'tests/test_installer_qa.py', 'tests/test_resources.py'])


def validate_decision(decision, candidate_bytes, *, pins, proof):
    require(type(decision) is dict and set(decision) == {'status', 'candidateDigest', 'reports', 'openGates'}
        and decision['status'] == 'candidate'
        and decision['candidateDigest'] == hashlib.sha256(candidate_bytes).hexdigest()
        and decision['reports'] == [f"https://github.com/{pins['repository']}/actions/runs/{pins['run_id']}",
                                   f"https://github.com/{pins['repository']}/actions/runs/{proof['run_id']}"]
        and type(decision['openGates']) is list and len(decision['openGates']) == len(OPEN)
        and all(type(item) is str for item in decision['openGates'])
        and set(decision['openGates']) == OPEN, 'candidate_decision_invalid')
    return decision


def verify_jobs(api, run_id):
    jobs = api.pages(f'actions/runs/{run_id}/jobs?filter=latest', 'jobs')
    for name in ('changes', 'build'):
        matches = [job for job in jobs if job.get('name') == name]
        require(len(matches) == 1 and matches[0].get('status') == 'completed'
            and matches[0].get('conclusion') == 'success', 'candidate_required_job_failed')


def read_receipt(api, proof, installer):
    with tempfile.TemporaryDirectory(prefix='ssearch-candidate-') as temporary:
        archive = Path(temporary) / 'receipt.zip'
        with archive.open('xb') as output:
            api.command(['api', '--method', 'GET',
                f"repos/empty-complete/ssearch/actions/artifacts/{proof['artifact_id']}/zip"], output=output)
        require(archive.stat().st_size == proof['archive_size']
            and installer.digest(archive) == proof['archive_sha256'], 'candidate_archive_mismatch')
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            require(len(entries) == 1, 'candidate_receipt_inventory_invalid')
            entry = entries[0]
            require(entry.filename == 'installation-receipt.json' and not entry.is_dir()
                and not (entry.flag_bits & 1) and not (entry.external_attr & 0x400)
                and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG)
                and 0 < entry.file_size <= 65536, 'candidate_receipt_entry_invalid')
            data = source.read(entry)
        require(hashlib.sha256(data).hexdigest() == proof['receipt_sha256'], 'candidate_receipt_hash_mismatch')
        value = json.loads(data.decode('utf-8-sig'))
        require(type(value) is dict, 'candidate_receipt_invalid')
        return value


def verify_candidate_inputs(api, root, head):
    """Pure API/file validation; CLI separately verifies the actual clean checkout."""
    try:
        require(allowed_delta(api, head), 'candidate_packaged_inputs_changed')
        installer = load_tool('ci_installer')
        root = Path(root)
        for relative in (INPUT, CONFIG, CANDIDATE, *LOCKS):
            installer.no_links(root / relative)
        original = api.file(INPUT, ANCHOR).encode('utf-8')
        require((root / INPUT).read_bytes() == original, 'candidate_original_pins_changed')
        pins = json.loads(original)
        policy = installer.read_json(root / CONFIG, limit=65536)
        require(set(policy) == {'ui_compatibility_decision', 'installation_proof'}, 'candidate_policy_invalid')
        proof = policy['installation_proof']
        require(type(proof) is dict and set(proof) == {'source_commit', 'run_id', 'artifact_id', 'artifact_name',
            'archive_size', 'archive_sha256', 'receipt_sha256', 'inventory_sha256'}
            and proof['source_commit'] == ANCHOR
            and proof['artifact_name'] == 'electron-service-installer-proof-' + ANCHOR
            and all(type(proof[key]) is int and proof[key] > 0 for key in ('run_id', 'artifact_id', 'archive_size'))
            and proof['archive_size'] <= 65536
            and all(type(proof[key]) is str and re.fullmatch('[0-9a-f]{64}', proof[key])
                for key in ('archive_sha256', 'receipt_sha256', 'inventory_sha256')), 'candidate_proof_pins_invalid')
        candidate = (root / CANDIDATE).read_bytes()
        require(candidate == api.file(CANDIDATE, pins['source_commit']).encode('utf-8'), 'candidate_bytes_changed')
        for relative in LOCKS:
            require((root / relative).read_bytes() == api.file(relative, pins['source_commit']).encode('utf-8'),
                    'candidate_lock_changed')
        validate_decision(policy['ui_compatibility_decision'], candidate, pins=pins, proof=proof)
        for inputs in (pins, {**pins, **proof}):
            installer.validate_input_metadata(api.get(f"actions/runs/{inputs['run_id']}"),
                api.get(f"actions/artifacts/{inputs['artifact_id']}"), inputs)
            verify_jobs(api, inputs['run_id'])
        receipt = read_receipt(api, proof, installer)
        require(set(receipt) == {'schema_version', 'ok', 'payload_source_commit', 'harness_source_commit',
            'installer_sha256', 'installer_size', 'artifact_run_id', 'artifact_id', 'payload_inventory_sha256',
            'runner_platform', 'windows_validation', 'full_suite_passed', 'checks', 'open_gates'}
            and type(receipt['schema_version']) is int and receipt['schema_version'] == 1 and receipt['ok'] is True
            and receipt['payload_source_commit'] == pins['source_commit'] and receipt['harness_source_commit'] == ANCHOR
            and receipt['installer_sha256'] == pins['installer_sha256'] and receipt['installer_size'] == pins['installer_size']
            and receipt['artifact_run_id'] == pins['run_id'] and receipt['artifact_id'] == pins['artifact_id']
            and receipt['payload_inventory_sha256'] == proof['inventory_sha256']
            and receipt['runner_platform'] == 'Windows Server 2025 x64'
            and receipt['windows_validation'] == [] and receipt['full_suite_passed'] is False
            and type(receipt['checks']) is dict and set(receipt['checks']) == set(installer.CHECKS)
            and all(receipt['checks'][key] is True for key in installer.CHECKS)
            and receipt['open_gates'] == installer.OPEN_GATES, 'candidate_installation_proof_invalid')
        return dict(route='candidate-source', source_commit=head, payload_source_commit=pins['source_commit'],
                    installation_harness_commit=ANCHOR, full_suite_passed=False, windows_validation=[])
    except CandidateError:
        raise
    except Exception:
        raise CandidateError('candidate_evidence_unavailable') from None


def verify_installer_inputs(api, root, head):
    """Authenticate Full input metadata without downloading or executing its ZIP."""
    try:
        require(review_delta(api, head, INSTALLER_ANCHOR, INSTALLER_ALLOWED), 'installer_review_inputs_changed')
        installer = load_tool('ci_installer')
        root = Path(root)
        for relative in (INPUT, CONFIG, CANDIDATE, *LOCKS):
            installer.no_links(root / relative)
            require((root / relative).read_bytes() == api.file(relative, INSTALLER_ANCHOR).encode('utf-8'),
                    'installer_review_historical_input_changed')
        pins = installer.load_input_set('full6a', root=root)
        installer.validate_input_metadata(api.get(f"actions/runs/{pins['run_id']}"),
            api.get(f"actions/artifacts/{pins['artifact_id']}"), pins)
        verify_jobs(api, pins['run_id'])
        return dict(route='installer-source', source_commit=head, payload_source_commit=pins['source_commit'],
                    full_suite_passed=False, windows_validation=[])
    except CandidateError:
        raise
    except Exception:
        raise CandidateError('installer_review_evidence_unavailable') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--head-sha')
    parser.add_argument('--pr-number', type=int)
    parser.add_argument('--settings')
    parser.add_argument('--installer-review', action='store_true')
    args = parser.parse_args()
    try:
        if args.settings is not None:
            print(json.dumps(review_settings(args.settings)))
            return 0
        publisher = load_tool('ci_publish')
        installer = load_tool('ci_installer')
        installer.verify_harness(args.head_sha)
        api = publisher.GitHub()
        route = installer_route if args.installer_review else candidate_route
        require(type(args.pr_number) is int and args.pr_number > 0
            and route(api, api.get(f'pulls/{args.pr_number}'), args.head_sha),
                'candidate_route_required')
        verify = verify_installer_inputs if args.installer_review else verify_candidate_inputs
        result = verify(api, ROOT, args.head_sha)
        installer.verify_harness(args.head_sha)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('candidate_review_failed', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
