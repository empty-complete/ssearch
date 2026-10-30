"""Read-only, fail-closed evaluation of ssearch release evidence v1.

Eligibility concerns the declared candidate and required gates only. It neither
authenticates reports nor promotes a stage, signs binaries, or publishes releases.
"""
import argparse
import json
import math
from pathlib import Path
import re
from typing import Literal, NamedTuple


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / 'schemas/release-evidence.schema.json'
BASE_GATES = ('runtime.source', 'runtime.frozen', 'backend.integrity',
              'security.access', 'offline.first-run', 'upgrade.compatibility',
              'recovery.restore', 'platform.windows-11')
CAPABILITY_GATES = {
    'metadata': ('quality.metadata',),
    'ui': ('ui.browser', 'ux.primary-flow'),
    'deep': ('security.cached-content-policy', 'security.deep-access', 'quality.deep'),
    'repair': ('recovery.repair',),
}
PRIMARY_SCENARIOS = frozenset(('add-source', 'find-duplicate', 'reveal-in-explorer',
    'open-progress', 'restore-from-tray', 'skip-replay-onboarding'))
# Compatibility changes require an explicit reviewed contract update.
COMPATIBLE_APIS = frozenset({('1', '1')})


class GateResult(NamedTuple):
    gate_id: str
    status: Literal['pass', 'fail', 'not_run', 'blocked']
    artifact_sha256: str
    environment_id: str
    report_path: str | None
    reason: str | None


def _matches_type(value, kind):
    return {
        'object': type(value) is dict,
        'array': type(value) is list,
        'string': type(value) is str,
        'integer': type(value) is int,
        'number': type(value) in (int, float) and math.isfinite(value),
        'boolean': type(value) is bool,
        'null': value is None,
    }[kind]


def _validate(value, rule, schema, path='$'):
    """Validate the closed subset used by our bundled schema, without I/O refs.

    This is not a general JSON Schema implementation. Adding another validation
    keyword fails closed until support is implemented here.
    """
    supported = {'$schema', '$defs', '$ref', 'title', 'description', 'type', 'enum',
        'properties', 'required', 'additionalProperties', 'minProperties',
        'minLength', 'pattern', 'minimum', 'items', 'minItems', 'maxItems', 'uniqueItems'}
    if set(rule) - supported:
        raise ValueError('unsupported_schema_keyword')
    if '$ref' in rule:
        reference = rule['$ref']
        if not reference.startswith('#/$defs/'):
            raise ValueError('external_schema_reference')
        return _validate(value, schema['$defs'][reference[8:]], schema, path)
    kinds = rule.get('type', [])
    if isinstance(kinds, str):
        kinds = [kinds]
    if kinds and not any(_matches_type(value, kind) for kind in kinds):
        return [path + ':type']
    if 'enum' in rule and not any(type(value) is type(item) and value == item for item in rule['enum']):
        return [path + ':enum']
    errors = []
    if type(value) is dict:
        properties = rule.get('properties', {})
        errors.extend(path + '.' + key + ':required' for key in rule.get('required', []) if key not in value)
        if len(value) < rule.get('minProperties', 0):
            errors.append(path + ':minProperties')
        for key, item in value.items():
            if type(key) is not str:
                errors.append(path + ':key_type')
                continue
            child = properties.get(key, rule.get('additionalProperties', True))
            if child is False:
                errors.append(path + '.' + key + ':unknown')
            elif child is not True:
                errors.extend(_validate(item, child, schema, path + '.' + key))
    elif type(value) is list:
        if len(value) < rule.get('minItems', 0) or len(value) > rule.get('maxItems', math.inf):
            errors.append(path + ':length')
        if rule.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            errors.append(path + ':uniqueItems')
        for index, item in enumerate(value):
            errors.extend(_validate(item, rule.get('items', {}), schema, f'{path}[{index}]'))
    elif type(value) is str:
        if len(value) < rule.get('minLength', 0) or ('pattern' in rule and re.search(rule['pattern'], value) is None):
            errors.append(path + ':string')
    elif type(value) in (int, float):
        if not math.isfinite(value) or value < rule.get('minimum', -math.inf):
            errors.append(path + ':number')
    return errors


def _pilot_passes(observation):
    if not observation or not observation['independent'] or observation['unresolved_failures']:
        return False
    participants = observation['participants']
    if len({p['participant_id'] for p in participants}) != len(participants):
        return False
    for participant in participants:
        if not participant['first_time']:
            return False
        scenarios = participant['scenarios']
        if {s['scenario_id'] for s in scenarios} != PRIMARY_SCENARIOS:
            return False
        if any(not s['success'] or s['assistance_requested'] or s['errors']
               or s['duration_seconds'] is None or s['recovery'] != 'none' for s in scenarios):
            return False
    return True


def evaluate_release(evidence: dict, *, required_gates: tuple[str, ...]) -> dict:
    """Union caller gates with mandatory policy; return deterministic blockers.

    Input must come from a trusted evidence collector. A nonempty report path is
    a reference, not proof of authenticity or a request to open arbitrary files.
    """
    blockers = set()
    details = []
    mandatory = set(BASE_GATES)
    capabilities = {}
    if type(required_gates) is not tuple or any(type(g) is not str or not g.strip() for g in required_gates):
        blockers.add('required_gates_invalid')
    else:
        mandatory.update(required_gates)
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
        details = _validate(evidence, schema, schema)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
        details = ['schema_unavailable_or_invalid']
    if details:
        return dict(eligible=False, blockers=sorted(blockers | {'schema_invalid'}),
                    required_gates=sorted(mandatory), capabilities={}, validation_errors=details)

    capabilities = dict(evidence['capabilities'])
    for capability, gates in CAPABILITY_GATES.items():
        if capabilities[capability]:
            mandatory.update(gates)
    if any(not capabilities[c] for c in evidence['declared_capabilities']):
        blockers.add('capability_downgrade')
    if not capabilities['metadata']:
        blockers.add('metadata_required')
    candidate = evidence['candidate']
    versions = candidate['versions']
    if (versions['desktop_api'], versions['service_api']) not in COMPATIBLE_APIS:
        blockers.add('api_incompatible')
    for key in ('electron', 'node', 'chromium', 'frontend', 'service', 'postgres', 'vector'):
        if re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?', versions[key]) is None:
            blockers.add('versions_unpinned')

    seen = set()
    for gate in evidence['gates']:
        gate_id = gate['gate_id']
        if gate_id in seen:
            blockers.add('duplicate_gate')
        seen.add(gate_id)
        if gate['binding'] != candidate or gate['artifact_sha256'] != candidate['desktop_sha256']:
            blockers.add('artifact_mismatch')
        environment = evidence['environments'].get(gate['environment_id'])
        if environment is None:
            blockers.add('environment_missing')
        elif any(value is None for value in environment.values()):
            blockers.add('environment_incomplete')
        for capability, gates in CAPABILITY_GATES.items():
            if gate_id in gates and not capabilities[capability] and gate['status'] == 'pass':
                blockers.add('disabled_capability_evidence')
        if gate['status'] == 'fail':
            blockers.add('gate_failed')
        if gate['status'] != 'pass':
            if gate_id in mandatory:
                blockers.add('gate_' + gate['status'])
            if not gate['reason']:
                blockers.add('reason_missing')
            continue
        if not gate['report_path']:
            blockers.add('report_missing')
        expected_kind = ('pilot' if gate_id == 'ux.primary-flow' else
                         'browser' if gate_id == 'ui.browser' else
                         'native' if gate_id.startswith('platform.') else None)
        if expected_kind and gate['evidence_kind'] != expected_kind:
            blockers.add('evidence_kind_mismatch')
        if expected_kind in ('pilot', 'native') and environment is not None:
            expected_os = 'Windows 10' if gate_id == 'platform.windows-10' else 'Windows 11'
            if environment['os'] != expected_os or environment['architecture'] != 'x64':
                blockers.add('environment_mismatch')
        if gate_id == 'ux.primary-flow' and not _pilot_passes(gate.get('observation')):
            blockers.add('ux_observation_incomplete')
    if mandatory - seen:
        blockers.add('gate_missing')
    return dict(eligible=not blockers, blockers=sorted(blockers),
                required_gates=sorted(mandatory), capabilities=capabilities, validation_errors=[])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--require', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
        result = evaluate_release(evidence, required_gates=tuple(args.require))
    except (OSError, ValueError, RecursionError, OverflowError):
        result = dict(eligible=False, blockers=['evidence_unreadable'])
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result['eligible'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
