"""Packaged report guards; synthetic reports never qualify a real application."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contracts():
    spec = importlib.util.spec_from_file_location('ssearch_desktop_proofs', ROOT / 'packaging/desktop_proofs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_report():
    return dict(schema_version=1, source_commit='a' * 40, scenario='load',
                proof_scope='packaged_hidden_offscreen', ok=True, reason_code='passed',
                hashes=dict.fromkeys(('desktop_sha256', 'service_sha256', 'app_asar_sha256',
                                      'product_metadata_sha256'), 'b' * 64),
                versions=dict(app='0.2.0', core_expected='0.2.0', service=None,
                              electron='45.0.0', node='24.22.0', chrome='156.0.8078.48'),
                owner=None,
                measured_counts=dict(scope='authenticated_descriptors_and_retained_children',
                    verified_authority_pids=[], live_retained_owner_children=0, owner_starts=0,
                    broker_starts=0, global_os_owners=None, global_models=None, global_pg_processes=None,
                    read_only_profile_invocations=0,
                    service_method_calls=dict.fromkeys(('search', 'fetch', 'status', 'settings_get',
                        'settings_validate', 'settings_apply', 'start_catalog', 'cancel_job'), 0),
                    broker_stdin_frame_counts=dict.fromkeys(('search', 'fetch', 'status', 'settings_get',
                        'settings_validate', 'settings_apply', 'start_catalog', 'cancel_job'), 0)),
                checks=[dict(name=name, status='pass') for name in ('installed_bootstrap_loaded',
                    'sandboxed_named_bridge', 'unavailable_without_service', 'no_service_start', 'clean_shutdown')],
                open_gates=['native_input', 'windows_10', 'full_p4_task3_acceptance'])


def expected():
    return dict(source_commit='a' * 40, app_version='0.2.0', core_version='0.2.0',
                hashes=fixture_report()['hashes'], electron='45.0.0', node='24.22.0', chrome='156.0.8078.48')


def test_policy_is_read_from_the_single_desktop_contract(contracts):
    policy = contracts.load_ui_policy(ROOT)
    assert set(policy['required_checks']) == {'load', 'security', 'lifecycle', 'empty-profile'}
    assert all('clean_shutdown' in names for names in policy['required_checks'].values())
    assert policy['budgets_ms']['lifecycle'] == 600000


def test_actual_scope_and_all_required_checks_are_needed(contracts):
    policy = contracts.load_ui_policy(ROOT)
    contracts.validate_ui_report(fixture_report(), scenario='load', expected=expected(), policy=policy)


@pytest.mark.parametrize('mutation', ['source', 'hash', 'helper', 'missing', 'duplicate', 'failed',
                                     'not_run', 'version', 'counts', 'live_owner', 'full_claim'])
def test_report_cannot_transfer_or_overstate_packaged_proof(contracts, mutation):
    report = copy.deepcopy(fixture_report())
    if mutation == 'source':
        report['source_commit'] = 'c' * 40
    elif mutation == 'hash':
        report['hashes']['service_sha256'] = 'c' * 64
    elif mutation == 'helper':
        report['proof_scope'] = 'internal_packaged_helper'
    elif mutation == 'missing':
        report['checks'].pop()
    elif mutation == 'duplicate':
        report['checks'].append(report['checks'][0].copy())
    elif mutation == 'failed':
        report['checks'][0]['status'] = 'fail'
    elif mutation == 'not_run':
        report['checks'][0]['status'] = 'not_run'
    elif mutation == 'version':
        report['versions']['electron'] = '0.0.0'
    elif mutation == 'counts':
        report['measured_counts']['global_models'] = 1
    elif mutation == 'live_owner':
        report['measured_counts']['live_retained_owner_children'] = 1
    else:
        report['open_gates'].remove('full_p4_task3_acceptance')
    with pytest.raises(contracts.DesktopProofError):
        contracts.validate_ui_report(report, scenario='load', expected=expected(),
                                     policy=contracts.load_ui_policy(ROOT))


@pytest.mark.parametrize('fault', [None, 'selection', 'owner_count', 'serve_hash', 'serve_missing', 'ui_missing'])
def test_combined_gate_requires_same_payload_and_every_independent_proof(contracts, tmp_path, fault):
    """Synthetic boundary reports check rejection, never qualify native code."""
    qa, stage, package = (tmp_path / name for name in ('qa', 'stage', 'package'))
    for path in (qa, stage, package, tmp_path / 'desktop/src', tmp_path / 'frontend/compatibility'):
        path.mkdir(parents=True)
    shutil.copyfile(ROOT / 'desktop/src/diagnosticsPolicy.ts', tmp_path / 'desktop/src/diagnosticsPolicy.ts')
    shutil.copyfile(ROOT / 'frontend/compatibility/candidate-versions.json', tmp_path / 'frontend/compatibility/candidate-versions.json')
    def write(path, value): path.write_text(json.dumps(value), encoding='utf-8')
    tests = dict(source_commit='a' * 40, scope='targeted', test_targets=['tests/test_owned.py'],
                 frontend_test_targets=[], desktop_test_targets=[], full_suite_passed=False, exit_code=0)
    write(qa / 'test-receipt.json', tests)
    inventory_path = stage / 'payload-inventory.json'
    write(inventory_path, {})
    build_path = stage / 'desktop-build-receipt.json'
    build = dict(source_commit='a' * 40, test_scope='targeted', package_only=True, frozen_acceptance=False,
                 node='v24.22.1', npm='11.19.1', packaged_path=str(package), payload_inventory=str(inventory_path),
                 python_tests_delegated=tests['test_targets'], frontend_test_targets=[], desktop_test_targets=[])
    if fault == 'selection': build['python_tests_delegated'] = []
    write(build_path, build)
    for name in ('frozen-self-test.json', 'postgres-runtime-proof.json'): write(qa / name, {'fixture': name})
    owner = dict(execution_mode='frozen', scenario='two-clients',
                 access_checks=['result_not_found', 'source_scope_denied', 'access_denied'],
                 **dict.fromkeys(('ok', 'clean_stop', 'model_ready', 'two_clients', 'scoped_fetch',
                     'metadata_only', 'auth', 'attach', 'second_launch', 'token_absent'), True),
                 **dict.fromkeys(('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes'), 1))
    if fault == 'owner_count': owner['model_constructions'] = 2
    write(qa / 'service-owner-proof.json', owner)
    flags = dict.fromkeys(('two_clients', 'private_validate_apply', 'cas_conflict', 'unauthenticated_401',
        'empty_sources', 'no_jobs_or_scan', 'metadata_search_empty', 'child_identity_verified',
        'second_serve_same_owner', 'primary_eof_clean_stop', 'restart_preserved', 'model_ready',
        'postgres_ready', 'no_output'), True)
    serve = dict(schema_version=1, ok=True, execution_mode='frozen', scenario='ordinary-serve',
                 source_commit='a' * 40, source_commit_observed=False,
                 source_identity_binding='external-frozen-application-proof', app_version='0.2.0',
                 service_sha256_before='b' * 64, service_sha256_after='b' * 64,
                 executable_path_has_unicode=True, executable_path_has_spaces=True, windows_validation=[],
                 scenarios={'ascii': flags.copy(), 'unicode': flags.copy()})
    if fault == 'serve_hash': serve['service_sha256_after'] = 'c' * 64
    if fault == 'serve_missing': serve['scenarios']['unicode'].pop('primary_eof_clean_stop')
    write(qa / 'frozen-service-proof.json', serve)
    policy = contracts.load_ui_policy(tmp_path)
    for scenario, names in policy['required_checks'].items():
        report = fixture_report()
        report.update(scenario=scenario, checks=[dict(name=name, status='pass') for name in names])
        if scenario in ('lifecycle', 'empty-profile'):
            report['owner'] = dict(pid=123, created='1234', generation='00000000-0000-0000-0000-000000000001',
                                   installation_id='00000000-0000-0000-0000-000000000002')
            report['measured_counts']['verified_authority_pids'] = [123]
            report['versions']['service'] = '0.2.0'
        if fault == 'ui_missing' and scenario == 'security': continue
        write(qa / f'ui-{scenario}.json', report)
    calls = []
    def validate(report, **identity): calls.append((report, identity))
    files = {name: dict(sha256='b' * 64) for name in ('ssearch.exe', 'service/ssearch-service.exe',
                                                   'resources/app.asar', 'resources/product-metadata.json')}
    def invoke():
        return contracts.verify_desktop_proofs(tmp_path, 'a' * 40, package, qa, build_path,
            app_version='0.2.0', core_version='0.2.0', inventory_validator=lambda *a, **kw: files,
            application_validator=validate, postgres_validator=validate)
    if fault:
        with pytest.raises((contracts.DesktopProofError, OSError)): invoke()
    else:
        index = invoke()
        assert len(calls) == 2
        assert all(identity == dict(source_commit='a' * 40, app_version='0.2.0', core_version='0.2.0')
                   for _, identity in calls)
        assert index['required_frozen_proofs_passed'] is True and index['stage_accepted'] is False
        assert set(index['ui']) == {'load', 'security', 'lifecycle', 'empty-profile'}
