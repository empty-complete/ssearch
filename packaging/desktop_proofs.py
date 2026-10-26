"""Packaged proof contracts. Source tests and report shape never qualify binaries."""
import ast
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID


SCENARIOS = frozenset(('load', 'security', 'lifecycle', 'empty-profile'))
METHODS = frozenset(('search', 'fetch', 'status', 'settings_get', 'settings_validate',
                     'settings_apply', 'start_catalog', 'cancel_job'))


class DesktopProofError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise DesktopProofError(code)


def load_ui_policy(repository: Path):
    """Read literal arrays/budgets from the single accepted desktop policy.

    This is data extraction, never execution of TypeScript. A changed format
    fails closed and requires coordinated producer/consumer changes.
    """
    raw = (repository / 'desktop/src/diagnosticsPolicy.ts').read_bytes()
    source = raw.decode('utf-8')
    match = re.findall(r'export const requiredUiChecks = \{(.*?)\} as const;', source, re.S)
    require(len(match) == 1, 'ui_policy_missing')
    arrays = re.findall(r"(?:'([^']+)'|([a-z-]+))\s*:\s*(\[[^\]]*\])", match[0], re.S)
    required = {}
    for quoted, bare, value in arrays:
        name = quoted or bare
        values = ast.literal_eval(value)
        require(name not in required and type(values) is list and bool(values)
                and all(type(item) is str and re.fullmatch('[a-z0-9_]+', item) for item in values)
                and len(values) == len(set(values)) and 'clean_shutdown' in values, 'invalid_ui_policy')
        required[name] = values
    match = re.findall(r'export const uiProofBudgets = \{(.*?)\} as const;', source, re.S)
    require(len(match) == 1, 'ui_budgets_missing')
    values = re.findall(r"(?:'([^']+)'|([a-z-]+))\s*:\s*([0-9_]+)", match[0])
    budgets = {}
    for quoted, bare, value in values:
        name = quoted or bare
        require(name not in budgets, 'duplicate_ui_budget')
        budgets[name] = int(value.replace('_', ''))
    require(set(required) == set(budgets) == SCENARIOS
            and all(0 < value <= 600000 for value in budgets.values()), 'invalid_ui_scenarios')
    return dict(schema_version=1, required_checks=required, budgets_ms=budgets,
                policy_sha256=hashlib.sha256(raw).hexdigest())


def _count(value):
    return type(value) is int and value >= 0


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def validate_ui_report(report, *, scenario, expected, policy):
    require(scenario in SCENARIOS, 'unknown_ui_scenario')
    fields = {'schema_version', 'source_commit', 'scenario', 'proof_scope', 'ok', 'reason_code',
              'hashes', 'versions', 'owner', 'measured_counts', 'checks', 'open_gates'}
    require(type(report) is dict and set(report) == fields
            and type(report['schema_version']) is int and report['schema_version'] == 1
            and report['source_commit'] == expected['source_commit'] and report['scenario'] == scenario
            and report['proof_scope'] == 'packaged_hidden_offscreen' and report['ok'] is True
            and report['reason_code'] == 'passed', 'packaged_ui_identity_mismatch')
    require(report['hashes'] == expected['hashes'], 'packaged_ui_image_mismatch')
    versions = report['versions']
    require(type(versions) is dict and set(versions) == {'app', 'core_expected', 'service', 'electron', 'node', 'chrome'}
            and all(versions[key] == expected[expected_key] for key, expected_key in (
                ('app', 'app_version'), ('core_expected', 'core_version'), ('electron', 'electron'),
                ('node', 'node'), ('chrome', 'chrome')))
            and versions['service'] in (None, expected['app_version']), 'packaged_ui_version_mismatch')
    checks = report['checks']
    require(type(checks) is list and all(type(item) is dict and set(item) == {'name', 'status'}
            and type(item['name']) is str and item['status'] in ('pass', 'not_run') for item in checks),
            'packaged_ui_checks_invalid')
    names = [item['name'] for item in checks]
    require(len(names) == len(set(names)), 'packaged_ui_duplicate_check')
    measured = {item['name']: item['status'] for item in checks}
    require(all(measured.get(name) == 'pass' for name in policy['required_checks'][scenario]),
            'packaged_ui_required_check_missing')
    counts = report['measured_counts']
    require(type(counts) is dict and counts.get('scope') == 'authenticated_descriptors_and_retained_children'
            and all(counts.get(name, 'missing') is None for name in
                    ('global_os_owners', 'global_models', 'global_pg_processes'))
            and all(_count(counts.get(name)) for name in
                    ('live_retained_owner_children', 'owner_starts', 'broker_starts', 'read_only_profile_invocations'))
            and counts['live_retained_owner_children'] == 0, 'packaged_ui_count_scope_mismatch')
    pids = counts.get('verified_authority_pids')
    require(type(pids) is list and all(type(pid) is int and pid > 0 for pid in pids)
            and len(pids) == len(set(pids)), 'packaged_ui_authority_invalid')
    for name in ('service_method_calls', 'broker_stdin_frame_counts'):
        values = counts.get(name)
        require(type(values) is dict and set(values) == METHODS and all(_count(value) for value in values.values()),
                'packaged_ui_wire_counts_invalid')
    if scenario == 'load':
        require(all(counts[name] == 0 for name in ('owner_starts', 'broker_starts', 'read_only_profile_invocations'))
                and all(value == 0 for name in ('service_method_calls', 'broker_stdin_frame_counts')
                        for value in counts[name].values())
                and not pids and report['owner'] is None and versions['service'] is None,
                'bootstrap_started_service')
    if scenario in ('security', 'lifecycle', 'empty-profile'):
        owner = report['owner']
        require(type(owner) is dict and set(owner) == {'pid', 'created', 'generation', 'installation_id'}
                and type(owner['pid']) is int and owner['pid'] > 0 and owner['pid'] in pids
                and type(owner['created']) is str and re.fullmatch('[1-9][0-9]{0,19}', owner['created'])
                and _uuid(owner['generation']) and _uuid(owner['installation_id'])
                and all(counts[name] >= 1 for name in ('owner_starts', 'broker_starts', 'read_only_profile_invocations'))
                and counts['service_method_calls']['status'] >= 1
                and counts['broker_stdin_frame_counts']['status'] >= 1
                and versions['service'] == expected['app_version'], 'packaged_ui_owner_unverified')
    gates = report['open_gates']
    require(type(gates) is list and all(type(gate) is str for gate in gates)
            and {'native_input', 'windows_10', 'full_p4_task3_acceptance'}.issubset(gates),
            'packaged_ui_acceptance_overstated')
    return dict(scenario=scenario, checks=measured, proof_scope=report['proof_scope'],
                global_counts_measured=False, open_gates=gates)


def read_report(path: Path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024 * 1024,
            'proof_file_missing_or_invalid')
    value = json.loads(path.read_text(encoding='utf-8'))
    require(type(value) is dict, 'proof_object_required')
    return value


def file_hash(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_desktop_proofs(repository, commit, directory, qa, desktop_receipt, *,
                          app_version, core_version, inventory_validator,
                          application_validator, postgres_validator):
    """Bind every required real proof to the same compiled payload before Inno."""
    build = read_report(desktop_receipt)
    tests = read_report(qa / 'test-receipt.json')
    require(build.get('source_commit') == tests.get('source_commit') == commit
            and build.get('test_scope') == tests.get('scope') in ('targeted', 'full')
            and tests.get('exit_code') == 0 and type(tests.get('exit_code')) is int
            and tests.get('full_suite_passed') is (tests['scope'] == 'full')
            and build.get('package_only') is True and build.get('frozen_acceptance') is False
            and build.get('node') == 'v24.22.1' and build.get('npm') == '11.19.1'
            and Path(build.get('packaged_path', '')).resolve() == directory.resolve(),
            'desktop_build_receipt_mismatch')
    for destination, source in (('python_tests_delegated', 'test_targets'),
                                ('frontend_test_targets', 'frontend_test_targets'),
                                ('desktop_test_targets', 'desktop_test_targets')):
        require(type(tests.get(source)) is list and build.get(destination) == tests[source],
                'desktop_test_selection_mismatch')
    require(tests['scope'] != 'targeted' or any(tests[name] for name in
            ('test_targets', 'frontend_test_targets', 'desktop_test_targets')), 'empty_quick_selection')
    inventory_path = Path(build.get('payload_inventory', ''))
    require(inventory_path.is_absolute() and inventory_path.name == 'payload-inventory.json'
            and inventory_path.parent.resolve() == desktop_receipt.parent.resolve(), 'inventory_binding_mismatch')
    files = inventory_validator(directory, inventory_path, source_commit=commit,
                                app_version=app_version, core_version=core_version)
    hashes = {name: files[relative]['sha256'] for name, relative in (
        ('desktop_sha256', 'ssearch.exe'), ('service_sha256', 'service/ssearch-service.exe'),
        ('app_asar_sha256', 'resources/app.asar'), ('product_metadata_sha256', 'resources/product-metadata.json'))}
    application = read_report(qa / 'frozen-self-test.json')
    application_validator(application, source_commit=commit, app_version=app_version, core_version=core_version)
    postgres_validator(read_report(qa / 'postgres-runtime-proof.json'),
                       source_commit=commit, app_version=app_version, core_version=core_version)
    owner = read_report(qa / 'service-owner-proof.json')
    require(owner.get('execution_mode') == 'frozen' and owner.get('scenario') == 'two-clients'
            and all(owner.get(name) is True for name in ('ok', 'clean_stop', 'model_ready', 'two_clients',
                'scoped_fetch', 'metadata_only', 'auth', 'attach', 'second_launch', 'token_absent'))
            and owner.get('access_checks') == ['result_not_found', 'source_scope_denied', 'access_denied']
            and all(type(owner.get(name)) is int and owner[name] == 1 for name in
                    ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes')),
            'frozen_owner_model_pg_proof_missing')
    serve = read_report(qa / 'frozen-service-proof.json')
    required_serve = {'two_clients', 'private_validate_apply', 'cas_conflict', 'unauthenticated_401',
        'empty_sources', 'no_jobs_or_scan', 'metadata_search_empty', 'child_identity_verified',
        'second_serve_same_owner', 'primary_eof_clean_stop', 'restart_preserved', 'model_ready',
        'postgres_ready', 'no_output'}
    require(serve.get('schema_version') == 1 and serve.get('ok') is True
            and serve.get('execution_mode') == 'frozen' and serve.get('scenario') == 'ordinary-serve'
            and serve.get('source_commit') == commit and serve.get('source_commit_observed') is False
            and serve.get('source_identity_binding') == 'external-frozen-application-proof'
            and serve.get('app_version') == app_version
            and serve.get('service_sha256_before') == serve.get('service_sha256_after') == hashes['service_sha256']
            and serve.get('executable_path_has_unicode') is True and serve.get('executable_path_has_spaces') is True
            and serve.get('windows_validation') == []
            and type(serve.get('scenarios')) is dict and set(serve['scenarios']) == {'ascii', 'unicode'}
            and all(type(flags) is dict and set(flags) == required_serve
                    and all(value is True for value in flags.values()) for flags in serve['scenarios'].values()),
            'frozen_ordinary_serve_proof_missing')
    candidate = json.loads((repository / 'frontend/compatibility/candidate-versions.json').read_text(encoding='utf-8'))
    expected = dict(source_commit=commit, app_version=app_version, core_version=core_version, hashes=hashes,
                    electron=candidate['electron']['version'], node=candidate['electron']['embedded_node'],
                    chrome=candidate['electron']['chromium'])
    policy = load_ui_policy(repository)
    reports = {name: qa / f'ui-{name}.json' for name in sorted(SCENARIOS)}
    ui = {name: validate_ui_report(read_report(path), scenario=name, expected=expected, policy=policy)
          for name, path in reports.items()}
    mandatory = ['postgres-runtime-proof.json', 'frozen-self-test.json', 'service-owner-proof.json',
                 'frozen-service-proof.json', 'test-receipt.json', *[path.name for path in reports.values()]]
    return dict(schema_version=1, source_commit=commit, app_version=app_version, core_version=core_version,
                payload_kind='electron-service', hashes=hashes, policy_sha256=policy['policy_sha256'],
                test_scope=tests['scope'], full_suite_passed=tests['full_suite_passed'],
                required_frozen_proofs_passed=True, scoped_backend_counts={name: owner[name] for name in
                    ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes')},
                ui=ui, proof_sha256={name: file_hash(qa / name) for name in mandatory},
                inventory_sha256=file_hash(inventory_path), desktop_receipt_sha256=file_hash(desktop_receipt),
                windows_validation=[], stage_accepted=False)
