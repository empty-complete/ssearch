"""Exact-head source admission contracts; API fixtures never run product code."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASE = '031c9dd248ceb10af00091b8a9f0f12c2d8cf49e'
HEAD, MERGE, TREE = 'a' * 40, 'b' * 40, 'c' * 40
REPO = 'empty-complete/ssearch'
WORKFLOW = '.github/workflows/windows-build.yml'
PROFILES = {
    'codex/p9-repair-policy': (
        ['src/atom_search/repair/' + name + '.py' for name in ('__init__', 'catalog', 'contracts', 'planner', 'policy')]
        + ['tests/test_repair_policy.py', 'docs/contracts/agent-operations/repair-plan.schema.json',
           'docs/contracts/agent-operations/README.md'],
        ['tests/test_repair_policy.py', 'tests/test_settings_schema.py', 'tests/test_settings_atomic.py', 'tests/test_cached_health.py']),
    'codex/p6-onboarding-provider': (
        ['src/atom_search/onboarding/' + name + '.py' for name in ('__init__', 'contracts', 'demo_provider', 'demo_memory', 'demo_files', '_windows_files')]
        + ['tests/onboarding/test_demo_provider.py', 'tests/onboarding/test_demo_files.py'],
        ['tests/onboarding/test_demo_provider.py', 'tests/onboarding/test_demo_files.py', 'tests/test_query_resolution.py',
         'tests/test_storage_ports.py', 'tests/test_source_registry.py',
         'tests/test_catalog_scheduler.py::test_owned_subtrees_are_not_descended_and_prefix_neighbor_is_kept',
         'tests/test_catalog_scheduler.py::test_direct_owned_root_is_invalid_before_settings_commit_and_registration_is_host_only']),
    'codex/p8-signed-manifest': (
        ['src/atom_search/updates/' + name + '.py' for name in ('__init__', 'contracts', 'manifest', 'cng', 'trust')]
        + ['resources/updates/trusted-keys.json', 'docs/contracts/updates/README.md',
           'docs/contracts/updates/manifest.schema.json', 'tests/test_update_manifest.py', 'tests/fixtures/update_signing.py'],
        ['tests/test_update_manifest.py', 'tests/test_package.py', 'tests/test_diagnostics.py']),
    'codex/p10-content-policy': (
        ['packages/atom-search-core/src/atom_search_core/content_models.py']
        + ['src/atom_search/content_indexing/' + name + '.py' for name in ('__init__', 'access', 'contracts', 'scheduler')]
        + ['tests/test_content_policy.py', 'tests/test_content_schedule.py'],
        ['tests/test_content_policy.py', 'tests/test_content_schedule.py', 'tests/test_content.py']),
    'codex/p7-scoped-read-api': (
        ['src/atom_search/agents/__init__.py', 'src/atom_search/agents/read_api.py', 'tests/agents/test_read_api.py'],
        ['tests/agents/test_read_api.py', 'tests/test_service_http.py', 'tests/test_service_client.py']),
    'codex/p11-authorized-workspaces': (
        ['tools/agent_contributions/' + name + '.py' for name in ('__init__', '__main__', 'contracts', 'config', 'workspace', 'git_client')]
        + ['tests/test_contribution_workspace.py', 'docs/contracts/agent-contributions/config.schema.json',
           'docs/contracts/agent-contributions/README.md'],
        ['tests/test_contribution_workspace.py', 'tests/test_core_distribution.py']),
    'codex/p12-evidence-matrix': (
        ['tools/qa/release_matrix.py', 'schemas/release-evidence.schema.json', 'tests/qa/test_release_matrix.py', 'docs/qa/pilot-protocol.md'],
        ['tests/qa/test_release_matrix.py']),
    'codex/p5-search-id-batch': (
        ['src/atom_search/service/application.py', 'packages/atom-search-core/src/atom_search_core/storage/public_ids.py',
         'tests/integration/test_pg_search_materialization.py'],
        ['tests/integration/test_pg_search_materialization.py', 'tests/integration/test_pg_public_ids.py',
         'tests/integration/test_pg_project_search.py',
         'tests/test_service_http.py::test_http_envelope_and_metadata_only_fetch',
         'tests/test_service_http.py::test_http_auth_origin_scope_and_limits',
         'tests/test_service_owner.py::test_scoped_search_and_metadata_fetch_do_not_probe_document']),
    'codex/ci-owner-source-admission': (
        ['tools/ci_source_scopes.py', 'tests/test_ci_source_scopes.py', 'tools/ci_publish.py',
         'tests/test_ci_publish.py', WORKFLOW, 'tests/test_ci_inputs.py'],
        ['tests/test_ci_source_scopes.py', 'tests/test_ci_publish.py', 'tests/test_ci_inputs.py', 'tests/test_ci_p4_candidate.py']),
}


def load_tool(name):
    path = ROOT / 'tools' / (name + '.py')
    assert path.is_file(), 'source_admission_contract_missing'
    spec = importlib.util.spec_from_file_location('source_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicApi:
    def __init__(self, branch):
        self.branch = branch
        self.pr = dict(number=17, changed_files=1,
            base=dict(sha=BASE, ref='main', repo=dict(full_name=REPO)),
            head=dict(sha=HEAD, ref=branch, repo=dict(full_name=REPO)))
        self.files = [dict(filename=PROFILES[branch][0][0], status='modified')]
        self.main = BASE
        self.comparison = dict(status='ahead', base_commit=dict(sha=BASE),
            merge_base_commit=dict(sha=BASE), files=copy.deepcopy(self.files))
        self.tree = TREE
        self.missing = set()
        self.changed_policy = None
        self.file_reads = []
        self.tree_reads = []
        self.target_tree = dict(truncated=False, tree=[dict(path=target.split('::')[0],
            type='blob', mode='100644') for target in dict.fromkeys(
                item.split('::')[0] for item in PROFILES[branch][1])])

    def get(self, endpoint, **kwargs):
        if endpoint == 'pulls/17':
            return copy.deepcopy(self.pr)
        if endpoint == 'commits/main':
            return dict(sha=self.main)
        if endpoint == f"compare/{self.pr['base']['sha']}...{HEAD}":
            return copy.deepcopy(self.comparison)
        if endpoint == f"compare/{BASE}...{self.pr['base']['sha']}":
            return dict(status='ahead', base_commit=dict(sha=BASE), merge_base_commit=dict(sha=BASE))
        if endpoint.startswith('compare/'):
            return dict(status='diverged', base_commit=dict(sha=BASE), merge_base_commit=dict(sha=BASE), files=[])
        if endpoint == 'git/commits/' + HEAD:
            return dict(tree=dict(sha=TREE))
        if endpoint == 'git/commits/' + MERGE:
            return dict(tree=dict(sha=self.tree))
        if endpoint == f'git/trees/{HEAD}?recursive=1':
            self.tree_reads.append(HEAD)
            return copy.deepcopy(self.target_tree)
        raise AssertionError('unexpected_public_metadata_request')

    def pages(self, endpoint, key=None):
        assert endpoint == 'pulls/17/files', 'unexpected_public_inventory_request'
        return copy.deepcopy(self.files)

    def file(self, path, ref):
        self.file_reads.append((path, ref))
        if path in self.missing:
            raise FileNotFoundError('PRIVATE_SENTINEL')
        if path == self.changed_policy and ref == HEAD:
            return 'untrusted changed policy'
        if path == WORKFLOW:
            return 'name: Windows\non: push\njobs:\n  changes:\n    value: true\n  build:\n    value: true\n  publish:\n    value: true\n'
        return 'trusted source\n'


@pytest.mark.parametrize('branch', PROFILES)
def test_existing_classifier_and_cli_admit_real_source_instead_of_binary_full(branch, monkeypatch, capsys):
    api, publisher = PublicApi(branch), load_tool('ci_publish')
    assert publisher.classify(api, 17, HEAD) == 'owner-source'
    monkeypatch.setattr(publisher, 'GitHub', lambda: api)
    monkeypatch.setattr('sys.argv', ['ci_publish.py', 'classify', '--pr-number', '17', '--head-sha', HEAD])
    assert publisher.main() == 0
    assert capsys.readouterr().out.strip() == 'owner-source'


@pytest.mark.parametrize('branch', PROFILES)
def test_each_owner_gets_only_its_fixed_nonempty_source_targets(branch):
    api, source = PublicApi(branch), load_tool('ci_source_scopes')
    api.files = [dict(filename=path, status='modified') for path in PROFILES[branch][0]]
    api.pr['changed_files'] = len(api.files)
    api.comparison['files'] = copy.deepcopy(api.files)
    settings = source.admit(api, api.pr, HEAD, api.files)
    assert settings == dict(route='owner-source', profile=branch, mode='source', desktop_stack=False,
                            publish=False, test_targets=PROFILES[branch][1])
    assert api.tree_reads == [HEAD], 'targets_must_exist_as_ordinary_files_on_exact_head'


@pytest.mark.parametrize('mutation,code', [
    ('extra', 'source_scope_paths_invalid'), ('rename_from', 'source_scope_paths_invalid'),
    ('rename_to', 'source_scope_paths_invalid'), ('policy', 'source_scope_paths_invalid'),
    ('workflow', 'source_scope_paths_invalid'), ('mixed_owner', 'source_scope_paths_invalid'),
    ('zero', 'source_scope_inventory_invalid'), ('huge', 'source_scope_inventory_invalid'),
    ('incomplete', 'source_scope_inventory_invalid'), ('duplicate', 'source_scope_inventory_invalid'),
    ('foreign_head', 'source_scope_identity_invalid'), ('foreign_base', 'source_scope_identity_invalid'),
    ('base_ref', 'source_scope_identity_invalid'), ('head', 'source_scope_identity_invalid'),
    ('missing_base', 'source_scope_identity_invalid'), ('stale', 'source_scope_base_stale'),
    ('diverged', 'source_scope_ancestry_invalid'), ('merge_base', 'source_scope_ancestry_invalid'),
    ('missing_target', 'source_scope_target_unavailable'), ('linked_target', 'source_scope_target_unavailable'),
    ('truncated_tree', 'source_scope_target_unavailable'), ('changed_policy', 'source_scope_policy_changed'),
    ('comparison_files', 'source_scope_inventory_invalid'),
])
def test_recognized_scope_violations_fail_instead_of_falling_back_to_full(mutation, code):
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    if mutation in ('extra', 'policy', 'workflow', 'mixed_owner', 'rename_to'):
        path = {'extra': 'src/atom_search/runtime/postgres.py', 'policy': 'tools/ci_source_scopes.py',
                'workflow': WORKFLOW, 'mixed_owner': 'src/atom_search/updates/trust.py',
                'rename_to': 'src/atom_search/repair/outside.py'}[mutation]
        api.files.append(dict(filename=path, status='modified'))
        api.pr['changed_files'] = 2
        if mutation == 'rename_to':
            api.files[-1]['previous_filename'] = api.files[0]['filename']
    elif mutation == 'rename_from':
        api.files[0]['previous_filename'] = 'src/atom_search/runtime/postgres.py'
    elif mutation in ('zero', 'huge', 'incomplete'):
        api.pr['changed_files'] = {'zero': 0, 'huge': 3001, 'incomplete': 2}[mutation]
    elif mutation == 'duplicate':
        api.files *= 2
        api.pr['changed_files'] = 2
    elif mutation in ('foreign_head', 'foreign_base'):
        api.pr['head' if mutation == 'foreign_head' else 'base']['repo']['full_name'] = 'foreign/repo'
    elif mutation == 'base_ref':
        api.pr['base']['ref'] = 'other'
    elif mutation == 'head':
        api.pr['head']['sha'] = 'd' * 40
    elif mutation == 'missing_base':
        api.pr.pop('base')
    elif mutation == 'stale':
        api.main = 'd' * 40
    elif mutation == 'diverged':
        api.comparison['status'] = 'diverged'
    elif mutation == 'merge_base':
        api.comparison['merge_base_commit']['sha'] = 'd' * 40
    elif mutation == 'missing_target':
        api.target_tree['tree'].pop()
    elif mutation == 'linked_target':
        api.target_tree['tree'][0]['mode'] = '120000'
    elif mutation == 'truncated_tree':
        api.target_tree['truncated'] = True
    elif mutation == 'comparison_files':
        api.comparison['files'] = []
    else:
        api.changed_policy = 'tools/ci_source_scopes.py'
    with pytest.raises(publisher.PublishError, match='^' + code + '$'):
        publisher.classify(api, 17, HEAD)


def test_unknown_risky_branch_keeps_ordinary_full():
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    api.pr['head']['ref'] = 'codex/unregistered-risk'
    assert publisher.classify(api, 17, HEAD) is True


@pytest.mark.parametrize('branch', ['codex/p9-repair-policy-extra', 'codex/p9-repair-policy/child'])
def test_branch_prefixes_do_not_grant_an_owner_profile(branch):
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    api.pr['head']['ref'] = branch
    assert publisher.classify(api, 17, HEAD) is True


def test_owner_admission_uses_fresh_advertised_main_not_the_bootstrap_anchor():
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    fresh = 'd' * 40
    api.main = api.pr['base']['sha'] = fresh
    api.comparison['base_commit']['sha'] = api.comparison['merge_base_commit']['sha'] = fresh
    assert publisher.classify(api, 17, HEAD) == 'owner-source'


@pytest.mark.parametrize('path', ['README.md', 'frontend/src/SearchApp.tsx'])
def test_recognized_branch_cannot_escape_through_documentation_or_ui_exemptions(path):
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    api.files = [dict(filename=path, status='modified')]
    api.comparison['files'] = copy.deepcopy(api.files)
    with pytest.raises(publisher.PublishError, match='^source_scope_paths_invalid$'):
        publisher.classify(api, 17, HEAD)


@pytest.mark.parametrize('path', ['tools/ci_source_scopes.py', 'tools/ci_publish.py', WORKFLOW])
def test_each_trusted_policy_must_match_the_advertised_base(path):
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    api.changed_policy = path
    with pytest.raises(publisher.PublishError, match='^source_scope_policy_changed$'):
        publisher.classify(api, 17, HEAD)


def test_ci_bootstrap_cannot_silently_move_its_trusted_anchor():
    api, publisher = PublicApi('codex/ci-owner-source-admission'), load_tool('ci_publish')
    api.main = api.pr['base']['sha'] = 'd' * 40
    with pytest.raises(publisher.PublishError, match='^source_scope_bootstrap_invalid$'):
        publisher.classify(api, 17, HEAD)


@pytest.mark.parametrize('mutation,code', [
    ('valid', None), ('unmerged', 'source_scope_merge_invalid'),
    ('wrong_merge', 'source_scope_merge_invalid'), ('tree', 'source_scope_merge_tree_invalid'),
    ('advanced_main', 'source_scope_base_stale'),
])
def test_merged_source_route_requires_exact_merge_identity_and_equal_trees(mutation, code):
    api, publisher = PublicApi('codex/p9-repair-policy'), load_tool('ci_publish')
    api.main = MERGE
    api.pr.update(merged=True, merged_at='2026-10-30T00:00:00Z', merge_commit_sha=MERGE)
    if mutation == 'unmerged':
        api.pr['merged'] = False
    elif mutation == 'wrong_merge':
        api.pr['merge_commit_sha'] = 'd' * 40
    elif mutation == 'tree':
        api.tree = 'd' * 40
    elif mutation == 'advanced_main':
        api.main = 'd' * 40
    if code:
        with pytest.raises(publisher.PublishError, match='^' + code + '$'):
            publisher.classify(api, 17, HEAD, merge_sha=MERGE)
    else:
        assert publisher.classify(api, 17, HEAD, merge_sha=MERGE) == 'owner-source'


def test_settings_cli_reauthenticates_and_never_accepts_caller_targets(monkeypatch, capsys):
    source, api = load_tool('ci_source_scopes'), PublicApi('codex/p9-repair-policy')
    monkeypatch.setattr(source, 'github', lambda: api)
    monkeypatch.setattr('sys.argv', ['ci_source_scopes.py', 'settings', '--pr-number', '17', '--head-sha', HEAD])
    assert source.main() == 0
    value = json.loads(capsys.readouterr().out)
    assert value['mode'] == 'source' and value['desktop_stack'] is False and value['publish'] is False
    assert value['test_targets'] == PROFILES[api.branch][1]
    api.files.append(dict(filename='PRIVATE_SENTINEL.py'))
    api.pr['changed_files'] = 2
    assert source.main() == 1
    output = capsys.readouterr()
    assert output.out == '' and output.err.strip() == 'source_scope_paths_invalid'
