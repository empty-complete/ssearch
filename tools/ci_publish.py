"""Publish verified existing Full bytes; never rebuild or create an update feed."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile


REPOSITORY = 'empty-complete/ssearch'
WORKFLOW = '.github/workflows/windows-build.yml'
SHA = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'[0-9a-f]{64}')
PUBLISHER_PATHS = frozenset((WORKFLOW, 'tools/ci_publish.py', 'tests/test_ci_publish.py',
    '.github/workflows/publish-existing.yml', '.github/workflows/publisher-checks.yml'))
DOCUMENTATION = re.compile(r'(README\.md|CONTRIBUTING\.md|AGENTS\.md|docs/.*\.md|'
    r'\.github/(pull_request_template\.md|PULL_REQUEST_TEMPLATE/.*\.md|ISSUE_TEMPLATE/[^/]+\.(md|yml|yaml)))')
UI_CANDIDATE = re.compile(r'(frontend/.*|desktop/.*|tools/build_frontend\.ps1|'
                          r'\.github/workflows/frontend-checks\.yml)')
PROOF_CHECKS = {
    'application': ('model_ready', 'clean_stop', 'source_clean', 'public_identity_preserved'),
    'postgresql': ('scram', 'wrong_password_rejected', 'clean_stop', 'profile_removed',
                  'restart_verified', 'dpapi_verified', 'private_acl_verified',
                  'application_privileges_verified'),
}


class PublishError(RuntimeError):
    """A fixed public failure code; never include credentials or process output."""


def require(condition, code):
    if not condition:
        raise PublishError(code)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def workflow_boundary(text):
    # Preserve exact text including line endings; parse only the fixed job boundaries.
    boundaries = [list(re.finditer(pattern, text)) for pattern in
                  (r'\r?\njobs:\r?\n', r'\r?\n  build:\r?\n', r'\r?\n  publish:\r?\n')]
    require(all(len(matches) == 1 for matches in boundaries), 'workflow_boundary_ambiguous')
    jobs, start, end = [matches[0].start() for matches in boundaries]
    require(jobs < start < end, 'workflow_boundary_order')
    return text[:jobs], text[start:end]


def requires_build(paths, base_workflow, head_workflow):
    """Both rename paths are supplied; unknown or changed build inputs fail closed."""
    if not paths or any(not isinstance(path, str) for path in paths):
        return True
    if all(DOCUMENTATION.fullmatch(path) or UI_CANDIDATE.fullmatch(path) for path in paths):
        return False
    if not set(paths).issubset(PUBLISHER_PATHS):
        return True
    try:
        return workflow_boundary(base_workflow) != workflow_boundary(head_workflow)
    except (ValueError, PublishError):
        return True


def validate_pr(pr, repository, number):
    require(repository == REPOSITORY and type(number) is int and number > 0,
            'invalid_repository_or_pr')
    require(pr.get('number') == number and pr.get('merged') is True and bool(pr.get('merged_at'))
            and pr.get('base', {}).get('ref') == 'main'
            and pr.get('base', {}).get('repo', {}).get('full_name') == repository
            and pr.get('head', {}).get('repo', {}).get('full_name') == repository
            and SHA.fullmatch(pr.get('head', {}).get('sha', ''))
            and SHA.fullmatch(pr.get('merge_commit_sha', ''))
            and bool(pr.get('head', {}).get('ref')), 'merged_pr_identity_mismatch')


def validate_run(run, pr, repository, workflow_id, associations):
    validate_pr(pr, repository, pr['number'])
    head = pr['head']
    matches = [item for item in associations if item.get('number') == pr['number']
        and item.get('merged_at') and item.get('merge_commit_sha') == pr['merge_commit_sha']
        and item.get('head', {}).get('sha') == head['sha']
        and item.get('head', {}).get('ref') == head['ref']
        and item.get('head', {}).get('repo', {}).get('full_name') == repository
        and item.get('base', {}).get('ref') == 'main'
        and item.get('base', {}).get('repo', {}).get('full_name') == repository]
    require(len(matches) == 1, 'commit_pr_association_mismatch')
    require(run.get('workflow_id') == workflow_id and run.get('path') == WORKFLOW
        and run.get('event') == 'pull_request' and run.get('status') == 'completed'
        and run.get('conclusion') == 'success' and run.get('head_sha') == head['sha']
        and run.get('head_branch') == head['ref']
        and run.get('head_repository', {}).get('full_name') == repository,
        'successful_pr_run_identity_mismatch')
    linked = run.get('pull_requests')
    require(isinstance(linked, list) and (not linked or any(
        item.get('number') == pr['number'] for item in linked)), 'run_pr_association_mismatch')


def validate_manual_run(run, jobs, event, env, workflow_id):
    """Only this trusted main dispatch may publish its still-running overall run."""
    inputs = event.get('inputs', {})
    source = env.get('GITHUB_SHA', '')
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and env.get('GITHUB_REF') == 'refs/heads/main'
        and env.get('GITHUB_REPOSITORY') == REPOSITORY
        and event.get('repository', {}).get('full_name') == REPOSITORY
        and inputs.get('mode') == 'full' and inputs.get('publish') in (True, 'true')
        and SHA.fullmatch(source) and str(run.get('id')) == env.get('GITHUB_RUN_ID')
        and run.get('workflow_id') == workflow_id and run.get('path') == WORKFLOW
        and run.get('event') == 'workflow_dispatch' and run.get('head_sha') == source
        and run.get('head_branch') == 'main'
        and run.get('head_repository', {}).get('full_name') == REPOSITORY
        and ((run.get('status') == 'in_progress' and run.get('conclusion') is None)
             or (run.get('status') == 'completed' and run.get('conclusion') == 'success')),
        'manual_full_run_identity_mismatch')
    for name in ('changes', 'build'):
        matching = [job for job in jobs if job.get('name') == name]
        require(len(matching) == 1 and matching[0].get('status') == 'completed'
                and matching[0].get('conclusion') == 'success', 'manual_full_jobs_not_successful')


def read_json(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024 * 1024,
            'invalid_public_receipt')
    value = json.loads(path.read_text(encoding='utf-8'))
    require(isinstance(value, dict), 'invalid_public_receipt')
    return value


def public_names(names):
    installers = [name for name in names if re.fullmatch(r'ssearch-Setup-\d+\.\d+\.\d+\.exe', name)]
    expected = {'build-manifest.json', 'test-receipt.json', 'ci-evidence.json', 'SHA256SUMS.txt'}
    require(len(installers) == 1 and set(names) == expected | set(installers)
            and len(names) == 5, 'public_file_inventory_mismatch')
    return installers[0]


def extract_artifact(archive, metadata, destination):
    require(metadata.get('digest') == 'sha256:' + digest(archive)
            and type(metadata.get('size_in_bytes')) is int
            and 0 < archive.stat().st_size == metadata['size_in_bytes'] <= 1024 ** 3,
            'artifact_archive_integrity_mismatch')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = [entry.filename for entry in entries]
        public_names(names)
        require(len({name.casefold() for name in names}) == 5, 'archive_duplicate_entry')
        for entry in entries:
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require('/' not in entry.filename and '\\' not in entry.filename
                    and not entry.is_dir() and mode in (0, stat.S_IFREG)
                    and not (entry.external_attr & 0x400)
                    and not (entry.flag_bits & 1)
                    and 0 < entry.file_size <= (1024 ** 3 if entry.filename.endswith('.exe') else 2 * 1024 * 1024),
                    'unsafe_artifact_entry')
        destination.mkdir(exist_ok=False)
        for entry in entries:
            with source.open(entry) as stream, (destination / entry.filename).open('xb') as output:
                shutil.copyfileobj(stream, output)


def validate_package(directory, source):
    require(bool(SHA.fullmatch(source)), 'invalid_tested_source')
    paths = list(directory.iterdir())
    installer_name = public_names([path.name for path in paths])
    require(all(path.is_file() and not path.is_symlink() for path in paths), 'invalid_public_file')
    sums_path = directory / 'SHA256SUMS.txt'
    require(sums_path.stat().st_size <= 4096, 'invalid_checksums')
    expected = {path.name: digest(path) for path in paths if path != sums_path}
    actual = {}
    for line in sums_path.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in actual, 'invalid_checksums')
        actual[match[2]] = match[1]
    require(actual == expected, 'public_file_checksum_mismatch')
    manifest = read_json(directory / 'build-manifest.json')
    version = manifest.get('app_version', '')
    require(bool(re.fullmatch(r'\d+\.\d+\.\d+', version))
        and installer_name == f'ssearch-Setup-{version}.exe'
        and manifest.get('source_commit') == source and manifest.get('core_version') == '0.2.0'
        and manifest.get('test_scope') == 'full' and manifest.get('test_targets') == []
        and manifest.get('full_suite_passed') is True
        and manifest.get('catalog') == {'backend': 'postgresql', 'schema_version': 5}
        and manifest.get('capabilities') == {'postgresql': 'application_verified'}
        and manifest.get('windows_validation') == [], 'full_manifest_identity_mismatch')
    installer = directory / installer_name
    require(manifest.get('installer') == {'path': 'artifacts/' + installer_name,
        'size': installer.stat().st_size, 'sha256': digest(installer)}, 'installer_identity_mismatch')
    receipt = read_json(directory / 'test-receipt.json')
    require(receipt == {'source_commit': source, 'scope': 'full', 'test_targets': [],
        'full_suite_passed': True, 'exit_code': 0}
        and type(receipt.get('exit_code')) is int
        and receipt.get('full_suite_passed') is True, 'full_test_receipt_mismatch')
    evidence = read_json(directory / 'ci-evidence.json')
    require(evidence.get('schema_version') == 1 and evidence.get('source_commit') == source
        and evidence.get('test_scope') == 'full' and evidence.get('full_suite_passed') is True
        and evidence.get('runner_platform') == 'Windows Server 2025 x64'
        and evidence.get('windows_validation') == []
        and evidence.get('release_channel') == 'development-prerelease', 'full_evidence_mismatch')
    proofs = evidence.get('frozen_proofs', {})
    require(set(proofs) == set(PROOF_CHECKS), 'frozen_proof_inventory_mismatch')
    for name, checks in PROOF_CHECKS.items():
        proof = proofs[name]
        require(proof.get('passed') is True and proof.get('execution_mode') == 'frozen'
            and bool(DIGEST.fullmatch(proof.get('original_receipt_sha256', '')))
            and proof.get('checks') == dict.fromkeys(checks, True)
            and all(value is True for value in proof['checks'].values()), 'frozen_proof_mismatch')
    return manifest


def validate_existing_release(release, target, directory):
    require(release.get('tag_name') == 'dev-' + target and release.get('target_commitish') == target
            and release.get('prerelease') is True and release.get('draft') is False,
            'existing_release_identity_mismatch')
    expected = {path.name: path for path in directory.iterdir()}
    assets = release.get('assets', [])
    require(len(assets) == 5 and {item.get('name') for item in assets} == set(expected),
            'existing_release_assets_mismatch')
    for item in assets:
        path = expected[item['name']]
        require(item.get('size') == path.stat().st_size
                and item.get('digest') == 'sha256:' + digest(path), 'existing_release_asset_hash_mismatch')


class GitHub:
    def command(self, arguments, *, output=None, optional=False):
        result = subprocess.run(['gh', *arguments], stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=300, check=False)
        if result.returncode:
            if optional and b'HTTP 404' in result.stderr:
                return None
            raise PublishError('github_request_failed')
        return result.stdout

    def get(self, endpoint, *, optional=False):
        value = self.command(['api', '--method', 'GET', 'repos/' + REPOSITORY + '/' + endpoint], optional=optional)
        return None if value is None else json.loads(value)

    def pages(self, endpoint, key=None):
        items = []
        for page in range(1, 101):
            value = self.get(endpoint + ('&' if '?' in endpoint else '?') + f'per_page=100&page={page}')
            batch = value[key] if key else value
            require(isinstance(batch, list), 'invalid_github_inventory')
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise PublishError('github_inventory_limit')

    def file(self, path, ref):
        value = self.get(f'contents/{path}?ref={ref}')
        require(value.get('encoding') == 'base64', 'workflow_content_unavailable')
        return base64.b64decode(value['content'], validate=False).decode('utf-8')


def ensure_ancestor(api, target, current):
    require(bool(SHA.fullmatch(target)) and bool(SHA.fullmatch(current)), 'invalid_target_identity')
    comparison = api.get(f'compare/{target}...{current}')
    require(comparison.get('status') in ('ahead', 'identical')
            and comparison.get('merge_base_commit', {}).get('sha') == target,
            'target_not_on_trusted_main')


def select_pr_artifact(api, pr, workflow_id):
    associations = api.pages(f"commits/{pr['head']['sha']}/pulls")
    runs = api.pages(f"actions/workflows/{workflow_id}/runs?event=pull_request&head_sha={pr['head']['sha']}&status=success", 'workflow_runs')
    for run in sorted(runs, key=lambda item: item['id'], reverse=True):
        try:
            validate_run(run, pr, REPOSITORY, workflow_id, associations)
        except PublishError:
            continue
        artifacts = api.pages(f"actions/runs/{run['id']}/artifacts", 'artifacts')
        matches = [item for item in artifacts if item.get('name') == f"windows-{pr['head']['sha']}-full"
                   and item.get('expired') is False]
        require(len(matches) <= 1, 'ambiguous_full_artifact')
        if matches:
            return run, matches[0]
    raise PublishError('no_successful_exact_full_artifact')


def publication_plan(api, env, *, recovery_pr=None):
    require(env.get('GITHUB_REPOSITORY') == REPOSITORY and env.get('GITHUB_REF') == 'refs/heads/main'
            and bool(SHA.fullmatch(env.get('GITHUB_SHA', ''))), 'trusted_main_required')
    execution = env['GITHUB_SHA']
    current = api.get('commits/main')['sha']
    ensure_ancestor(api, execution, current)
    workflow = api.get('actions/workflows/windows-build.yml')
    require(workflow.get('path') == WORKFLOW and workflow.get('state') == 'active', 'workflow_identity_mismatch')
    manual = recovery_pr is None and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
    if manual:
        event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        run = api.get(f"actions/runs/{int(env['GITHUB_RUN_ID'])}")
        jobs = api.pages(f"actions/runs/{run['id']}/jobs?filter=latest", 'jobs')
        validate_manual_run(run, jobs, event, env, workflow['id'])
        target = source = execution
        artifacts = api.pages(f"actions/runs/{run['id']}/artifacts", 'artifacts')
        matches = [item for item in artifacts if item.get('name') == f'windows-{source}-full'
                   and item.get('expired') is False]
        require(len(matches) == 1, 'missing_or_ambiguous_full_artifact')
        artifact = matches[0]
        number = 0
    else:
        if recovery_pr is not None:
            require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'recovery_dispatch_required')
            number = recovery_pr
        else:
            require(env.get('GITHUB_EVENT_NAME') == 'push', 'main_push_required')
            associated = api.pages(f'commits/{execution}/pulls')
            matches = [item for item in associated if item.get('merged_at')
                       and item.get('merge_commit_sha') == execution
                       and item.get('base', {}).get('ref') == 'main'
                       and item.get('base', {}).get('repo', {}).get('full_name') == REPOSITORY]
            require(len(matches) == 1, 'exact_merged_pr_required')
            number = matches[0]['number']
        pr = api.get(f'pulls/{number}')
        validate_pr(pr, REPOSITORY, number)
        target, source = pr['merge_commit_sha'], pr['head']['sha']
        require(recovery_pr is not None or target == execution, 'push_merge_identity_mismatch')
        ensure_ancestor(api, target, execution)
        run, artifact = select_pr_artifact(api, pr, workflow['id'])
    tested_tree = api.get(f'git/commits/{source}')['tree']['sha']
    target_tree = api.get(f'git/commits/{target}')['tree']['sha']
    require(bool(SHA.fullmatch(tested_tree)) and tested_tree == target_tree, 'tested_target_tree_mismatch')
    return {'target': target, 'source': source, 'tree': tested_tree, 'pr': number,
            'run': run, 'artifact': artifact, 'publisher_execution_sha': execution}


def classify(api, number, head_sha, *, merge_sha=None):
    pr = api.get(f'pulls/{number}')
    require(pr['head']['sha'] == head_sha and bool(SHA.fullmatch(head_sha)),
            'classification_head_changed')
    if not 0 < pr['changed_files'] <= 3000:
        return True
    paths = []
    inventory = api.pages(f'pulls/{number}/files')
    require(len(inventory) == pr['changed_files'] and 0 < len(inventory) <= 3000,
            'incomplete_changed_file_inventory')
    for item in inventory:
        paths.append(item['filename'])
        if item.get('previous_filename'):
            paths.append(item['previous_filename'])
    require(bool(SHA.fullmatch(pr['base']['sha'])) and bool(SHA.fullmatch(pr['head']['sha'])),
            'invalid_classification_identity')
    candidate_path = Path(__file__).with_name('ci_p4_candidate.py')
    if (candidate_path.is_file() and pr['head'].get('repo', {}).get('full_name') == REPOSITORY
            and pr['base'].get('repo', {}).get('full_name') == REPOSITORY and pr['base'].get('ref') == 'main'):
        import importlib.util
        spec = importlib.util.spec_from_file_location('ssearch_p4_review_route', candidate_path)
        candidate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(candidate)
        try:
            if candidate.candidate_route(api, pr, head_sha, merge_sha=merge_sha):
                return 'candidate-source'
        except candidate.CandidateError:
            # Do not turn an invalid candidate identity into a packaging bypass.
            raise PublishError('candidate_review_identity_invalid') from None
    return requires_build(paths, api.file(WORKFLOW, pr['base']['sha']), api.file(WORKFLOW, pr['head']['sha']))


def verify_pr_selection(api, number):
    """Read-only CI check; no caller ref is promoted to publication authority."""
    current = api.get('commits/main')['sha']
    pr = api.get(f'pulls/{number}')
    validate_pr(pr, REPOSITORY, number)
    ensure_ancestor(api, pr['merge_commit_sha'], current)
    workflow = api.get('actions/workflows/windows-build.yml')
    require(workflow.get('path') == WORKFLOW and workflow.get('state') == 'active', 'workflow_identity_mismatch')
    run, artifact = select_pr_artifact(api, pr, workflow['id'])
    tested_tree = api.get(f"git/commits/{pr['head']['sha']}")['tree']['sha']
    merged_tree = api.get(f"git/commits/{pr['merge_commit_sha']}")['tree']['sha']
    require(bool(SHA.fullmatch(tested_tree)) and tested_tree == merged_tree, 'tested_target_tree_mismatch')
    return {'result': 'verified_selection_only', 'source_commit': pr['head']['sha'],
            'target_commit': pr['merge_commit_sha'], 'build_run_id': run['id'], 'artifact_id': artifact['id']}


def verified_tag(api, target):
    reference = api.get('git/ref/tags/dev-' + target, optional=True)
    if reference is None:
        return False
    value = reference['object']
    for _ in range(4):
        if value['type'] != 'tag':
            break
        value = api.get('git/tags/' + value['sha'])['object']
    require(value.get('type') == 'commit' and value.get('sha') == target, 'existing_tag_target_mismatch')
    return True


def publish(api, plan):
    with tempfile.TemporaryDirectory(prefix='ssearch-publish-') as temporary:
        directory = Path(temporary)
        archive = directory / 'artifact.zip'
        artifact = plan['artifact']
        with archive.open('xb') as output:
            api.command(['api', '--method', 'GET', f"repos/{REPOSITORY}/actions/artifacts/{int(artifact['id'])}/zip"], output=output)
        package = directory / 'package'
        extract_artifact(archive, artifact, package)
        validate_package(package, plan['source'])
        tag = 'dev-' + plan['target']
        release = api.get('releases/tags/' + tag, optional=True)
        tag_exists = verified_tag(api, plan['target'])
        if release is not None:
            require(tag_exists, 'existing_release_tag_missing')
            validate_existing_release(release, plan['target'], package)
            return 'preserved_verified_release'
        require(not tag_exists, 'existing_tag_without_release')
        notes = directory / 'notes.md'
        notes.write_text(
            f"Development build tested at {plan['source']} and integrated at {plan['target']}.\n"
            f"The exact Git tree hashes match: {plan['tree']}. Receipts retain the tested commit.\n\n"
            'Full tests and frozen PostgreSQL/application proofs passed on Windows Server 2025 x64.\n'
            'Windows 10/11 native and visual acceptance remains open. This is not Stable or a signed update feed.\n\n'
            f"Build workflow: https://github.com/{REPOSITORY}/actions/runs/{plan['run']['id']}\n"
            f"Publisher execution commit: {plan['publisher_execution_sha']}\n", encoding='utf-8')
        api.command(['release', 'create', tag, '--repo', REPOSITORY, '--target', plan['target'],
            '--prerelease', '--title', 'ssearch development ' + plan['target'][:8], '--notes-file', str(notes),
            *[str(path) for path in sorted(package.iterdir())]])
        require(verified_tag(api, plan['target']), 'published_tag_missing')
        validate_existing_release(api.get('releases/tags/' + tag), plan['target'], package)
        return 'published_verified_release'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    scope = commands.add_parser('classify')
    scope.add_argument('--pr-number', type=int, required=True)
    scope.add_argument('--head-sha', required=True)
    scope.add_argument('--merge-sha')
    selection = commands.add_parser('verify-pr')
    selection.add_argument('--pr-number', type=int, required=True)
    publication = commands.add_parser('publish')
    publication.add_argument('--recovery-pr', type=int)
    publication.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    try:
        api = GitHub()
        if args.command == 'classify':
            route = classify(api, args.pr_number, args.head_sha, merge_sha=args.merge_sha)
            print('candidate-source' if route == 'candidate-source' else ('true' if route else 'false'))
        elif args.command == 'verify-pr':
            print(json.dumps(verify_pr_selection(api, args.pr_number)))
        else:
            plan = publication_plan(api, os.environ, recovery_pr=args.recovery_pr)
            result = 'verified_selection_only' if args.verify_only else publish(api, plan)
            print(json.dumps({'result': result, 'source_commit': plan['source'],
                'target_commit': plan['target'], 'build_run_id': plan['run']['id'],
                'artifact_id': plan['artifact']['id'], 'publisher_execution_sha': plan['publisher_execution_sha']}))
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, PublishError) else 'publisher_failed', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
