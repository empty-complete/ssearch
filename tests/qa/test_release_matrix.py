"""Release evidence contracts. All receipts below are synthetic unit fixtures."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_GATES = ('runtime.source', 'runtime.frozen', 'backend.integrity', 'security.access',
              'offline.first-run', 'upgrade.compatibility', 'recovery.restore',
              'quality.metadata', 'platform.windows-11')


@pytest.fixture
def matrix():
    path = ROOT / 'tools/qa/release_matrix.py'
    if not path.is_file():
        pytest.fail('release_evidence_contract_not_implemented', pytrace=False)
    spec = importlib.util.spec_from_file_location('release_matrix', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate(ui=False):
    binding = dict(source_sha='a' * 40, desktop_sha256='b' * 64,
                   service_sha256='c' * 64, payload_inventory_sha256='d' * 64,
                   owner_generation='generation-1', versions=dict(
                       electron='42.0.0', node='24.0.0', chromium='148.0.0',
                       frontend='0.2.0', service='0.2.0', desktop_api='1',
                       service_api='1', schema='3', model='metadata-only',
                       postgres='17.5', vector='0.8.0'))
    environment = dict(os='Windows 11', os_build='26100', architecture='x64',
                       cpu='unit-fixture', ram_bytes=8 * 1024 ** 3)
    gates = list(BASE_GATES) + (['ux.primary-flow', 'ui.browser'] if ui else [])
    return dict(schema_version=1, candidate=binding,
                capabilities=dict(metadata=True, ui=ui, deep=False, repair=False),
                declared_capabilities=['metadata'] + (['ui'] if ui else []),
                environments={'fixture': environment},
                gates=[receipt(gate, binding) for gate in gates])


def receipt(gate, binding):
    return dict(gate_id=gate, status='pass', artifact_sha256=binding['desktop_sha256'],
                environment_id='fixture', report_path='unit-fixture.json', reason=None,
                binding=copy.deepcopy(binding),
                evidence_kind='pilot' if gate == 'ux.primary-flow' else
                    'native' if gate == 'platform.windows-11' else 'automated')


def check(matrix, evidence):
    return matrix.evaluate_release(evidence, required_gates=())


def test_complete_metadata_fixture_is_eligible_without_stage_promotion(matrix):
    result = check(matrix, candidate())
    assert result['eligible'] is True
    assert result['blockers'] == []
    assert 'stable' not in result and 'stage_accepted' not in result


@pytest.mark.parametrize('status', ['not_run', 'blocked', 'fail'])
def test_not_run_is_not_pass(matrix, status):
    data = candidate()
    data['gates'][0].update(status=status, report_path=None, reason='not available')
    assert check(matrix, data)['eligible'] is False


@pytest.mark.parametrize('field,value', [
    ('desktop_sha256', 'e' * 64), ('service_sha256', 'e' * 64),
    ('payload_inventory_sha256', 'e' * 64), ('source_sha', 'e' * 40),
    ('owner_generation', 'generation-2')])
def test_wrong_binary_digest_blocks(matrix, field, value):
    data = candidate()
    data['gates'][0]['binding'][field] = value
    result = check(matrix, data)
    assert result['eligible'] is False
    assert 'artifact_mismatch' in result['blockers']


def test_wrong_legacy_artifact_digest_blocks(matrix):
    data = candidate()
    data['gates'][0]['artifact_sha256'] = 'e' * 64
    assert 'artifact_mismatch' in check(matrix, data)['blockers']


@pytest.mark.parametrize('field', ['electron', 'frontend', 'service', 'model', 'schema'])
def test_runtime_version_binding_mismatch_blocks(matrix, field):
    data = candidate()
    data['gates'][0]['binding']['versions'][field] = 'different'
    assert 'artifact_mismatch' in check(matrix, data)['blockers']


def test_incompatible_api_pair_blocks(matrix):
    data = candidate()
    data['candidate']['versions']['service_api'] = '2'
    for gate in data['gates']:
        gate['binding'] = copy.deepcopy(data['candidate'])
    assert 'api_incompatible' in check(matrix, data)['blockers']


def test_metadata_pilot_does_not_claim_deep(matrix):
    data = candidate()
    result = check(matrix, data)
    assert result['capabilities']['deep'] is False
    data['gates'].append(receipt('quality.deep', data['candidate']))
    assert 'disabled_capability_evidence' in check(matrix, data)['blockers']


@pytest.mark.parametrize('capability,gate', [('deep', 'quality.deep'), ('repair', 'recovery.repair')])
def test_enabled_capability_adds_mandatory_gate(matrix, capability, gate):
    data = candidate()
    data['capabilities'][capability] = True
    data['declared_capabilities'].append(capability)
    result = check(matrix, data)
    assert result['eligible'] is False
    assert gate in result['required_gates']


def test_declared_feature_cannot_be_silently_disabled(matrix):
    data = candidate()
    data['declared_capabilities'].append('deep')
    assert 'capability_downgrade' in check(matrix, data)['blockers']


@pytest.mark.parametrize('section,key', [('environment', 'os'), ('environment', 'os_build'),
    ('environment', 'cpu'), ('environment', 'ram_bytes'), ('version', 'schema'),
    ('version', 'model'), ('version', 'postgres'), ('version', 'vector')])
def test_schema_requires_environment_and_versions(matrix, section, key):
    data = candidate()
    target = data['environments']['fixture'] if section == 'environment' else data['candidate']['versions']
    del target[key]
    assert 'schema_invalid' in check(matrix, data)['blockers']


def test_unmeasured_environment_value_is_null_and_blocks(matrix):
    data = candidate()
    data['environments']['fixture']['ram_bytes'] = None
    assert 'environment_incomplete' in check(matrix, data)['blockers']


def test_unknown_environment_blocks(matrix):
    data = candidate()
    data['gates'][0]['environment_id'] = 'absent'
    assert 'environment_missing' in check(matrix, data)['blockers']


def test_primary_flow_without_ux_evidence_is_not_pass(matrix):
    data = candidate(ui=True)
    data['gates'] = [gate for gate in data['gates'] if gate['gate_id'] != 'ux.primary-flow']
    result = check(matrix, data)
    assert result['eligible'] is False
    assert 'ux.primary-flow' in result['required_gates']


def test_primary_flow_failure_blocks_ui_candidate(matrix):
    data = candidate(ui=True)
    next(g for g in data['gates'] if g['gate_id'] == 'ux.primary-flow')['status'] = 'fail'
    assert check(matrix, data)['eligible'] is False


@pytest.mark.parametrize('gate', ['ux.primary-flow', 'platform.windows-11'])
def test_browser_evidence_cannot_close_pilot_or_native_gate(matrix, gate):
    data = candidate(ui=True)
    next(g for g in data['gates'] if g['gate_id'] == gate)['evidence_kind'] = 'browser'
    assert 'evidence_kind_mismatch' in check(matrix, data)['blockers']


def test_browser_evidence_can_close_browser_gate(matrix):
    data = candidate(ui=True)
    next(g for g in data['gates'] if g['gate_id'] == 'ui.browser')['evidence_kind'] = 'browser'
    assert check(matrix, data)['eligible'] is True


def test_duplicate_gate_cannot_mask_failure(matrix):
    data = candidate()
    duplicate = copy.deepcopy(data['gates'][0])
    duplicate['status'] = 'fail'
    data['gates'].append(duplicate)
    assert 'duplicate_gate' in check(matrix, data)['blockers']


def test_caller_can_only_add_required_gates(matrix):
    result = matrix.evaluate_release(candidate(), required_gates=('custom.audit',))
    assert result['eligible'] is False
    assert set(BASE_GATES).issubset(result['required_gates'])
    assert 'custom.audit' in result['required_gates']


@pytest.mark.parametrize('mutation', ['status', 'bool', 'hash', 'empty_report', 'unknown_capability'])
def test_malformed_evidence_fails_closed(matrix, mutation):
    data = candidate()
    if mutation == 'status':
        data['gates'][0]['status'] = 'PASS'
    elif mutation == 'bool':
        data['capabilities']['deep'] = 'false'
    elif mutation == 'hash':
        data['candidate']['source_sha'] = 'not-a-sha'
    elif mutation == 'empty_report':
        data['gates'][0]['report_path'] = None
    else:
        data['capabilities']['surprise'] = True
    assert check(matrix, data)['eligible'] is False


def test_schema_is_published_and_describes_exact_binding():
    path = ROOT / 'schemas/release-evidence.schema.json'
    assert path.is_file(), 'release evidence schema must be published'
    schema = json.loads(path.read_text(encoding='utf-8'))
    assert schema['$schema'] == 'https://json-schema.org/draft/2020-12/schema'
    assert {'candidate', 'environments', 'gates', 'capabilities'}.issubset(schema['required'])
