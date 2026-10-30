"""Closed owner source gates. Source success never grants binary acceptance."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = '031c9dd248ceb10af00091b8a9f0f12c2d8cf49e'
REPOSITORY = 'empty-complete/ssearch'
BOOTSTRAP = 'codex/ci-owner-source-admission'
TASK2_BOOTSTRAP = 'codex/ci-task2-source-admission'
BOOTSTRAP_ANCHORS = {BOOTSTRAP: ANCHOR, TASK2_BOOTSTRAP: '9674fc4d311dd0bb3471885624d9391d2aa95488'}
POLICY = ('tools/ci_source_scopes.py', 'tools/ci_publish.py', '.github/workflows/windows-build.yml')
PROFILES = {
    'codex/p9-repair-policy': (
        tuple('src/atom_search/repair/' + name + '.py' for name in ('__init__', 'catalog', 'contracts', 'planner', 'policy'))
        + ('tests/test_repair_policy.py', 'docs/contracts/agent-operations/repair-plan.schema.json', 'docs/contracts/agent-operations/README.md'),
        ('tests/test_repair_policy.py', 'tests/test_settings_schema.py', 'tests/test_settings_atomic.py', 'tests/test_cached_health.py')),
    'codex/p6-onboarding-provider': (
        tuple('src/atom_search/onboarding/' + name + '.py' for name in ('__init__', 'contracts', 'demo_provider', 'demo_memory', 'demo_files', '_windows_files'))
        + ('tests/onboarding/test_demo_provider.py', 'tests/onboarding/test_demo_files.py'),
        ('tests/onboarding/test_demo_provider.py', 'tests/onboarding/test_demo_files.py', 'tests/test_query_resolution.py',
         'tests/test_storage_ports.py', 'tests/test_source_registry.py',
         'tests/test_catalog_scheduler.py::test_owned_subtrees_are_not_descended_and_prefix_neighbor_is_kept',
         'tests/test_catalog_scheduler.py::test_direct_owned_root_is_invalid_before_settings_commit_and_registration_is_host_only')),
    'codex/p8-signed-manifest': (
        tuple('src/atom_search/updates/' + name + '.py' for name in ('__init__', 'contracts', 'manifest', 'cng', 'trust'))
        + ('resources/updates/trusted-keys.json', 'docs/contracts/updates/README.md', 'docs/contracts/updates/manifest.schema.json',
           'tests/test_update_manifest.py', 'tests/fixtures/update_signing.py'),
        ('tests/test_update_manifest.py', 'tests/test_package.py', 'tests/test_diagnostics.py')),
    'codex/p10-content-policy': (
        ('packages/atom-search-core/src/atom_search_core/content_models.py',)
        + tuple('src/atom_search/content_indexing/' + name + '.py' for name in ('__init__', 'access', 'contracts', 'scheduler'))
        + ('tests/test_content_policy.py', 'tests/test_content_schedule.py'),
        ('tests/test_content_policy.py', 'tests/test_content_schedule.py', 'tests/test_content.py')),
    'codex/p7-scoped-read-api': (
        ('src/atom_search/agents/__init__.py', 'src/atom_search/agents/read_api.py', 'tests/agents/test_read_api.py'),
        ('tests/agents/test_read_api.py', 'tests/test_service_client.py', 'tests/test_service_http.py')),
    'codex/p11-authorized-workspaces': (
        tuple('tools/agent_contributions/' + name + '.py' for name in ('__init__', '__main__', 'contracts', 'config', 'workspace', 'git_client'))
        + ('tests/test_contribution_workspace.py', 'docs/contracts/agent-contributions/config.schema.json', 'docs/contracts/agent-contributions/README.md'),
        ('tests/test_contribution_workspace.py', 'tests/test_core_distribution.py')),
    'codex/p12-evidence-matrix': (
        ('tools/qa/release_matrix.py', 'schemas/release-evidence.schema.json', 'tests/qa/test_release_matrix.py', 'docs/qa/pilot-protocol.md'),
        ('tests/qa/test_release_matrix.py',)),
    'codex/p5-search-id-batch': (
        ('src/atom_search/service/application.py', 'packages/atom-search-core/src/atom_search_core/storage/public_ids.py',
         'tests/integration/test_pg_search_materialization.py', 'tests/test_project_api.py'),
        ('tests/integration/test_pg_search_materialization.py', 'tests/integration/test_pg_public_ids.py',
         'tests/integration/test_pg_project_search.py',
         'tests/test_service_http.py::test_http_envelope_and_metadata_only_fetch',
         'tests/test_service_http.py::test_http_auth_origin_scope_and_limits',
         'tests/test_service_owner.py::test_scoped_search_and_metadata_fetch_do_not_probe_document', 'tests/test_project_api.py')),
    BOOTSTRAP: (
        ('tools/ci_source_scopes.py', 'tests/test_ci_source_scopes.py', 'tools/ci_publish.py', 'tests/test_ci_publish.py',
         '.github/workflows/windows-build.yml', 'tests/test_ci_inputs.py'),
        ('tests/test_ci_source_scopes.py', 'tests/test_ci_publish.py', 'tests/test_ci_inputs.py', 'tests/test_ci_p4_candidate.py')),
    'codex/p11-evidence-validation': (
        ('docs/contracts/agent-contributions/README.md', 'tests/test_contribution_evidence.py',
         'tools/agent_contributions/__main__.py', 'tools/agent_contributions/contracts.py',
         'tools/agent_contributions/evidence.py', 'tools/agent_contributions/validation.py',
         'tools/check_agent_contributions.py'),
        ('tests/test_contribution_evidence.py', 'tests/test_contribution_workspace.py', 'tests/test_core_distribution.py')),
    'codex/p12-quality-metrics': (
        ('tools/qa/benchmark_search.py', 'tests/qa/test_benchmark_metrics.py'),
        ('tests/qa/test_benchmark_metrics.py',)),
    TASK2_BOOTSTRAP: (
        ('tools/ci_source_scopes.py', 'tests/test_ci_source_scopes.py'),
        ('tests/test_ci_source_scopes.py', 'tests/test_ci_publish.py', 'tests/test_ci_inputs.py', 'tests/test_ci_p4_candidate.py')),
}


class ScopeError(RuntimeError):
    """Only fixed public refusal codes cross the CI output boundary."""


def require(value, code):
    if not value:
        raise ScopeError(code)


def sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{40}', value) is not None


def inventory(items):
    require(type(items) is list and 0 < len(items) < 300, 'source_scope_inventory_invalid')
    result = []
    for item in items:
        require(type(item) is dict and type(item.get('filename')) is str
            and bool(item['filename'])
            and item.get('status') in ('added', 'removed', 'modified', 'renamed', 'copied', 'changed', 'unchanged')
            and ('previous_filename' not in item or (type(item['previous_filename']) is str and bool(item['previous_filename'])))
            and (item['status'] not in ('renamed', 'copied') or 'previous_filename' in item),
            'source_scope_inventory_invalid')
        result.append((item['filename'], item.get('previous_filename'), item.get('status')))
    require(len({item[0].casefold() for item in result}) == len(result), 'source_scope_inventory_invalid')
    return result


def admit(api, pr, head, files, *, merge_sha=None):
    require(type(pr) is dict and type(pr.get('head')) is dict and type(pr.get('base')) is dict,
            'source_scope_identity_invalid')
    owner, base = pr['head'], pr['base']
    require(type(owner.get('ref')) is str and sha(owner.get('sha')) and sha(base.get('sha'))
        and type(base.get('ref')) is str and type(owner.get('repo')) is dict
        and type(base.get('repo')) is dict and type(owner['repo'].get('full_name')) is str
        and type(base['repo'].get('full_name')) is str, 'source_scope_identity_invalid')
    branch = owner['ref']
    if branch not in PROFILES:
        return None
    require(sha(head) and owner['sha'] == head and owner['repo']['full_name'] == REPOSITORY
        and base['repo']['full_name'] == REPOSITORY and base['ref'] == 'main', 'source_scope_identity_invalid')
    records = inventory(files)
    require(type(pr.get('changed_files')) is int and pr['changed_files'] == len(records), 'source_scope_inventory_invalid')
    allowed, targets = PROFILES[branch]
    require(all(path in allowed and (previous is None or previous in allowed)
                for path, previous, _ in records), 'source_scope_paths_invalid')
    if branch in BOOTSTRAP_ANCHORS:
        require(base['sha'] == BOOTSTRAP_ANCHORS[branch], 'source_scope_bootstrap_invalid')
    current = api.get('commits/main')
    require(type(current) is dict and current.get('sha') == (merge_sha if merge_sha is not None else base['sha']),
            'source_scope_base_stale')
    if base['sha'] != ANCHOR:
        ancestry = api.get(f"compare/{ANCHOR}...{base['sha']}")
        require(ancestry.get('status') in ('ahead', 'identical')
            and ancestry.get('base_commit', {}).get('sha') == ANCHOR
            and ancestry.get('merge_base_commit', {}).get('sha') == ANCHOR, 'source_scope_ancestry_invalid')
    comparison = api.get(f"compare/{base['sha']}...{head}")
    require(comparison.get('status') == 'ahead' and comparison.get('base_commit', {}).get('sha') == base['sha']
        and comparison.get('merge_base_commit', {}).get('sha') == base['sha'], 'source_scope_ancestry_invalid')
    require(sorted(inventory(comparison.get('files'))) == sorted(records), 'source_scope_inventory_invalid')
    # A bootstrap exempts only policy files within its own closed path inventory.
    editable_policy = allowed if branch in BOOTSTRAP_ANCHORS else ()
    require(all(api.file(path, base['sha']) == api.file(path, head)
                for path in POLICY if path not in editable_policy), 'source_scope_policy_changed')
    require(targets and all(type(target) is str and target for target in targets), 'source_scope_target_unavailable')
    tree = api.get(f'git/trees/{head}?recursive=1')
    require(type(tree) is dict and tree.get('truncated') is False and type(tree.get('tree')) is list,
            'source_scope_target_unavailable')
    entries = tree['tree']
    for path in dict.fromkeys(target.split('::')[0] for target in targets):
        matches = [item for item in entries if type(item) is dict and item.get('path') == path]
        require(len(matches) == 1 and matches[0].get('type') == 'blob' and matches[0].get('mode') in ('100644', '100755'),
                'source_scope_target_unavailable')
    if merge_sha is not None:
        require(sha(merge_sha) and pr.get('merged') is True and bool(pr.get('merged_at'))
            and pr.get('merge_commit_sha') == merge_sha, 'source_scope_merge_invalid')
        source_tree = api.get('git/commits/' + head).get('tree', {}).get('sha')
        merged_tree = api.get('git/commits/' + merge_sha).get('tree', {}).get('sha')
        require(sha(source_tree) and source_tree == merged_tree, 'source_scope_merge_tree_invalid')
    return dict(route='owner-source', profile=branch, mode='source', desktop_stack=False,
                publish=False, test_targets=list(targets))


def github():
    spec = importlib.util.spec_from_file_location('ssearch_source_publisher', ROOT / 'tools/ci_publish.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GitHub()


def settings(api, number, head, *, merge_sha=None):
    pr = api.get(f'pulls/{number}')
    result = admit(api, pr, head, api.pages(f'pulls/{number}/files'), merge_sha=merge_sha)
    require(result is not None, 'source_scope_profile_required')
    return result


def verify_checkout(head, targets):
    def git(*arguments):
        result = subprocess.run(['git', '-C', str(ROOT), *arguments], capture_output=True, text=True, check=False)
        require(result.returncode == 0, 'source_scope_checkout_invalid')
        return result.stdout.strip()
    require(git('rev-parse', 'HEAD') == head and not git('status', '--porcelain', '--untracked-files=all'),
            'source_scope_checkout_invalid')
    spec = importlib.util.spec_from_file_location('ssearch_source_build_support', ROOT / 'packaging/build_support.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.validate_test_selection(ROOT, development=True, quick=True, test_targets=targets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('settings', 'authenticate'))
    parser.add_argument('--pr-number', type=int, required=True)
    parser.add_argument('--head-sha', required=True)
    parser.add_argument('--merge-sha')
    args = parser.parse_args()
    try:
        require(args.pr_number > 0 and sha(args.head_sha), 'source_scope_identity_invalid')
        api = github()
        require(args.command != 'authenticate' or args.merge_sha is None, 'source_scope_merge_invalid')
        result = settings(api, args.pr_number, args.head_sha, merge_sha=args.merge_sha)
        if args.command == 'authenticate':
            verify_checkout(args.head_sha, result['test_targets'])
            # Re-read mutable PR/main metadata after checking the actual checkout.
            require(settings(api, args.pr_number, args.head_sha) == result, 'source_scope_base_stale')
            verify_checkout(args.head_sha, result['test_targets'])
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, ScopeError) else 'source_scope_unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
