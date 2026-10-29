"""Public signed-manifest and persistent replay contracts; required Windows CNG proof."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('update_test_signing', ROOT / 'tests/fixtures/update_signing.py')
_signing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_signing)


def api(name):
    # Keep the first RED an assertion about missing product behavior, not a collection error.
    assert importlib.util.find_spec('atom_search.updates') is not None, 'signed update verification is not implemented'
    return importlib.import_module(f'atom_search.updates.{name}')


@pytest.fixture(scope='module')
def key():
    value = _signing.TestSigningKey()
    yield value
    value.close()


def versions():
    return dict(app='0.2.0', desktop='0.2.0', service='0.2.0', service_api='1',
                schema=3, settings_schema=3, postgres='18.6', extension='0.8.7')


def payload():
    target = versions() | dict(app='0.3.0', desktop='0.3.0', service='0.3.0')
    return dict(format_version=1, release_id='fixture-release', source_commit='a' * 40,
                sequence=10, channel='stable', issued_at='2026-10-29T11:00:00Z',
                expires_at='2026-10-30T12:00:00Z', versions=target,
                compatibility=dict(desktop='0.3.0', service='0.3.0', service_api='1',
                                   schema_min=3, schema_max=3, settings_schema_min=3, settings_schema_max=3),
                executables=dict(desktop=dict(path='desktop/ssearch.exe', sha256='b' * 64),
                                 service=dict(path='service/ssearch-service.exe', sha256='c' * 64)),
                artifacts=[dict(id='application', relative_path='payload/application.zip', size=100,
                                sha256='d' * 64, kind='app', format='zip',
                                inventory=dict(relative_path='payload/application.inventory.json', size=200,
                                               sha256='e' * 64, entry_count=2, unpacked_bytes=1000))])


def encode(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def verifier(tmp_path, key, *, trust_changes=None):
    path = tmp_path / 'trusted-keys.json'
    path.write_text(json.dumps(key.trust_document(**(trust_changes or {}))), encoding='utf-8')
    trust = api('trust').TrustStore.from_file(path)
    ledger = api('trust').ReplayLedger(tmp_path / 'runtime' / 'update-replay.json')
    return api('manifest').ManifestVerifier(trust, ledger)


def verify(checker, envelope, **changes):
    args = dict(channel='stable', installed=api('contracts').UpdateVersions(**versions()), now=NOW)
    args.update(changes)
    return checker.verify(envelope, **args)


def rejected(checker, key, data, code=None):
    with pytest.raises(api('contracts').UpdateError) as caught:
        verify(checker, key.envelope(encode(data)))
    if code:
        assert caught.value.code == code


def test_valid_cng_signature_verifies_exact_payload_bytes_and_retry(tmp_path, key):
    checker = verifier(tmp_path, key)
    raw = encode(payload()) + b'\n'
    result = verify(checker, key.envelope(raw))
    assert result.release_id == 'fixture-release'
    assert result.payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.versions.desktop == '0.3.0'
    assert len(result.artifacts) == 1
    assert verify(verifier(tmp_path, key), key.envelope(raw)) == result


def test_bad_signature_blocks_artifact_fetch(tmp_path, key):
    checker = verifier(tmp_path, key)
    events = ['manifest_read']
    raw = encode(payload())
    envelope = key.envelope(raw, signature=b'\0' * 64)
    with pytest.raises(api('contracts').UpdateError) as caught:
        try:
            verify(checker, envelope)
        except api('contracts').UpdateError:
            events.append('signature_rejected')
            raise
        events.append('artifact_read')
    assert caught.value.code == 'update_signature'
    assert events == ['manifest_read', 'signature_rejected']
    assert not (tmp_path / 'runtime/update-replay.json').exists()


def test_real_cng_rejects_changed_bytes_and_other_public_key(tmp_path, key):
    checker = verifier(tmp_path, key)
    raw = encode(payload())
    changed = raw.replace(b'fixture-release', b'altered-release')
    for envelope in (key.envelope(changed, signature=key.sign(raw)),):
        with pytest.raises(api('contracts').UpdateError, match='update_signature'):
            verify(checker, envelope)
    other = _signing.TestSigningKey()
    try:
        with pytest.raises(api('contracts').UpdateError, match='update_signature'):
            verify(checker, key.envelope(raw, signature=other.sign(raw)))
    finally:
        other.close()


@pytest.mark.parametrize('mutation,code', [
    ({'expires_at': '2026-10-28T12:00:00Z'}, 'update_expired'),
    ({'channel': 'nightly'}, 'update_channel'),
    ({'issued_at': '2026-10-29T12:05:01Z'}, 'update_time'),
    ({'source_commit': 'z' * 40}, 'update_manifest'),
])
def test_manifest_rejects_expired_wrong_channel_and_invalid_policy(tmp_path, key, mutation, code):
    checker = verifier(tmp_path, key)
    rejected(checker, key, payload() | mutation, code)
    assert not (tmp_path / 'runtime/update-replay.json').exists()


@pytest.mark.parametrize('field,value', [('app', '0.1.9'), ('desktop', '0.1.9'), ('service', '0.1.9'),
                                         ('postgres', '18.5'), ('extension', '0.8.6'),
                                         ('schema', 2), ('settings_schema', 2)])
def test_each_component_downgrade_is_rejected(tmp_path, key, field, value):
    data = payload()
    data['versions'][field] = value
    if field in ('desktop', 'service'):
        data['compatibility'][field] = value
    rejected(verifier(tmp_path, key), key, data, 'update_downgrade')


def test_unknown_expired_and_future_keys_never_authorize_manifest(tmp_path, key):
    raw = encode(payload())
    checker = verifier(tmp_path, key)
    with pytest.raises(api('contracts').UpdateError, match='update_key'):
        verify(checker, key.envelope(raw, key_id='manifest-supplied-key'))
    for changes in ({'not_after': '2020-01-01T00:00:00Z', 'not_before': '2019-01-01T00:00:00Z'},
                    {'not_before': '2090-01-01T00:00:00Z'}):
        with pytest.raises(api('contracts').UpdateError, match='update_key'):
            verify(verifier(tmp_path, key, trust_changes=changes), key.envelope(raw))


def test_signature_is_checked_before_payload_parsing(tmp_path, key):
    checker = verifier(tmp_path, key)
    with pytest.raises(api('contracts').UpdateError, match='update_signature'):
        verify(checker, key.envelope(b'{not valid JSON', signature=b'\0' * 64))


def test_replay_survives_restart_and_equal_sequence_requires_equal_hash(tmp_path, key):
    first = payload()
    verify(verifier(tmp_path, key), key.envelope(encode(first)))
    verify(verifier(tmp_path, key), key.envelope(encode(first)))
    for data in (first | {'sequence': 9}, first | {'release_id': 'different-release'}):
        rejected(verifier(tmp_path, key), key, data, 'update_replay')
    next_value = first | {'sequence': 11}
    verify(verifier(tmp_path, key), key.envelope(encode(next_value)))
    rejected(verifier(tmp_path, key), key, first, 'update_replay')


def test_replay_high_water_is_separate_for_explicit_channels(tmp_path, key):
    checker = verifier(tmp_path, key)
    verify(checker, key.envelope(encode(payload())))
    nightly = payload() | {'sequence': 1, 'channel': 'nightly'}
    assert verify(checker, key.envelope(encode(nightly)), channel='nightly').sequence == 1
    rejected(checker, key, payload() | {'sequence': 9}, 'update_replay')


@pytest.mark.parametrize('corruption', [b'{', b'{}', b'{"format_version":1,"channels":{"stable":{"sequence":true,"payload_sha256":"a"}}}'])
def test_corrupt_replay_state_fails_closed(tmp_path, key, corruption):
    checker = verifier(tmp_path, key)
    ledger = tmp_path / 'runtime/update-replay.json'
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(corruption)
    rejected(checker, key, payload(), 'update_replay')
    assert ledger.read_bytes() == corruption


def test_concurrent_accept_never_loses_high_water(tmp_path, key):
    checker = verifier(tmp_path, key)
    def accept(sequence):
        try:
            verify(checker, key.envelope(encode(payload() | {'sequence': sequence})))
            return sequence
        except api('contracts').UpdateError as error:
            assert error.code == 'update_replay'
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(accept, range(10, 30)))
    assert 29 in values
    rejected(verifier(tmp_path, key), key, payload() | {'sequence': 28}, 'update_replay')


@pytest.mark.parametrize('field', ['desktop', 'service', 'service_api'])
def test_signed_compatible_pair_must_equal_target_versions(tmp_path, key, field):
    data = payload()
    data['compatibility'][field] = '999'
    rejected(verifier(tmp_path, key), key, data, 'update_compatibility')


def test_installed_schema_and_api_must_be_supported(tmp_path, key):
    for section, field, value in [('compatibility', 'schema_min', 4),
                                  ('compatibility', 'settings_schema_min', 4),
                                  ('versions', 'service_api', '2')]:
        data = payload()
        data[section][field] = value
        if field == 'service_api':
            data['compatibility'][field] = value
        rejected(verifier(tmp_path, key), key, data, 'update_compatibility')


@pytest.mark.parametrize('path', ['/absolute.zip', '../escape.zip', 'a/../escape.zip',
                                 'C:/escape.zip', 'a:b.zip', 'a\\b.zip', '//server/file',
                                 'a//b.zip', 'CON.zip', 'x/NUL', 'x/trailing.', 'x/space ',
                                 'x/\x00file', 'x/COM1.txt', 'x/LPT9.txt'])
def test_manifest_rejects_unsafe_windows_paths(tmp_path, key, path):
    data = payload()
    data['artifacts'][0]['relative_path'] = path
    rejected(verifier(tmp_path, key), key, data)


@pytest.mark.parametrize('value', [-1, True, 4294967297, 1.5])
def test_artifact_sizes_are_bounded_integers_without_bool(tmp_path, key, value):
    data = payload()
    data['artifacts'][0]['size'] = value
    rejected(verifier(tmp_path, key), key, data)


def test_manifest_envelope_exact_byte_limit_and_one_extra(tmp_path, key):
    checker = verifier(tmp_path, key)
    raw = key.envelope(encode(payload()))
    boundary = raw + b' ' * (262144 - len(raw))
    assert verify(checker, boundary).sequence == 10
    with pytest.raises(api('contracts').UpdateError, match='update_limit'):
        verify(checker, boundary + b' ')


def test_artifact_count_total_size_and_case_collision(tmp_path, key):
    data = payload()
    template = dict(id='component', relative_path='component.bin', size=0,
                    sha256='f' * 64, kind='extension', format='file', inventory=None)
    data['artifacts'].extend(template | dict(id=f'part{i}', relative_path=f'part{i}.bin') for i in range(15))
    assert len(verify(verifier(tmp_path, key), key.envelope(encode(data))).artifacts) == 16
    too_many = deepcopy(data)
    too_many['sequence'] = 11
    too_many['artifacts'].append(template)
    rejected(verifier(tmp_path, key), key, too_many, 'update_limit')
    collision = payload()
    collision['artifacts'].append(template | dict(relative_path='PAYLOAD/APPLICATION.ZIP'))
    rejected(verifier(tmp_path, key), key, collision)
    too_large = payload()
    too_large['artifacts'][0]['size'] = 4294967296
    too_large['artifacts'].extend([template | dict(id='a', relative_path='a', size=4294967296),
                                   template | dict(id='b', relative_path='b', size=1)])
    rejected(verifier(tmp_path, key), key, too_large, 'update_limit')


@pytest.mark.parametrize('raw', [b'{"sequence":10,"sequence":11}', b'{"sequence":NaN}',
                               b'{"sequence":Infinity}', b'{"sequence":1.0}', b'\xff'])
def test_signed_json_rejects_duplicates_constants_floats_and_invalid_utf8(tmp_path, key, raw):
    with pytest.raises(api('contracts').UpdateError, match='update_manifest'):
        verify(verifier(tmp_path, key), key.envelope(raw))


def test_envelope_is_exact_and_strict_base64(tmp_path, key):
    checker = verifier(tmp_path, key)
    original = json.loads(key.envelope(encode(payload())))
    variants = [original | {'public_key': 'not a trust source'}, original | {'payload_b64': '***'},
                original | {'signature_b64': base64.b64encode(b'bad').decode()},
                original | {'payload_b64': original['payload_b64'] + '\n'}]
    for envelope in variants:
        with pytest.raises(api('contracts').UpdateError):
            verify(checker, encode(envelope))


@pytest.mark.parametrize('field,value', [('size', 4194305), ('size', -1), ('size', True),
                                       ('entry_count', 20001), ('entry_count', True),
                                       ('unpacked_bytes', 34359738369), ('unpacked_bytes', -1),
                                       ('relative_path', '../inventory.json')])
def test_inventory_references_enforce_all_declared_bounds(tmp_path, key, field, value):
    data = payload()
    data['artifacts'][0]['inventory'][field] = value
    rejected(verifier(tmp_path, key), key, data)


def test_zip_inventory_and_both_executable_hashes_are_required(tmp_path, key):
    for mutate in (lambda data: data['artifacts'][0].update(inventory=None),
                   lambda data: data['artifacts'][0].update(format='file', inventory=None),
                   lambda data: data['executables'].pop('service'),
                   lambda data: data['executables']['desktop'].update(sha256='bad'),
                   lambda data: data['versions'].update(settings_schema=True)):
        data = payload()
        mutate(data)
        rejected(verifier(tmp_path, key), key, data)


def test_unknown_payload_nested_fields_are_rejected(tmp_path, key):
    for section in (None, 'versions', 'compatibility', 'executables'):
        data = payload()
        (data if section is None else data[section])['unrecognized'] = 'value'
        rejected(verifier(tmp_path, key), key, data)


def test_empty_production_trust_store_never_uses_test_key(tmp_path, key):
    production = ROOT / 'resources/updates/trusted-keys.json'
    assert production.is_file(), 'production must ship an explicitly empty trust store'
    assert json.loads(production.read_bytes()) == {'format_version': 1, 'keys': []}
    trust = api('trust').TrustStore.from_file(production)
    checker = api('manifest').ManifestVerifier(trust, api('trust').ReplayLedger(tmp_path / 'replay.json'))
    with pytest.raises(api('contracts').UpdateError, match='update_key'):
        verify(checker, key.envelope(encode(payload())))


@pytest.mark.parametrize('field,limit', [('size', 4194304), ('entry_count', 20000),
                                       ('unpacked_bytes', 34359738368)])
def test_inventory_reference_exact_limit_and_one_extra(tmp_path, key, field, limit):
    data = payload()
    data['artifacts'][0]['inventory'][field] = limit
    checker = verifier(tmp_path, key)
    assert verify(checker, key.envelope(encode(data))).sequence == 10
    data['sequence'] = 11
    data['artifacts'][0]['inventory'][field] = limit + 1
    rejected(checker, key, data, 'update_limit')


def test_single_and_total_artifact_sizes_exact_limits_and_one_extra(tmp_path, key):
    data = payload()
    data['artifacts'][0]['size'] = 4294967296
    checker = verifier(tmp_path, key)
    assert verify(checker, key.envelope(encode(data))).artifacts[0].size == 4294967296
    second = dict(id='extension', relative_path='payload/extension.bin', size=4294967296,
                  sha256='f' * 64, kind='extension', format='file', inventory=None)
    data['sequence'] = 11
    data['artifacts'].append(second)
    assert sum(item.size for item in verify(checker, key.envelope(encode(data))).artifacts) == 8589934592
    data['sequence'] = 12
    data['artifacts'].append(second | dict(id='extra', relative_path='extra.bin', size=1))
    rejected(checker, key, data, 'update_limit')
    data['artifacts'] = [data['artifacts'][0] | dict(size=4294967297)]
    rejected(checker, key, data, 'update_limit')


def test_path_and_clock_skew_exact_limits_and_one_extra(tmp_path, key):
    data = payload()
    data['artifacts'][0]['relative_path'] = '/'.join(['a' * 200, 'b' * 200, 'c' * 106 + '.zip'])
    assert len(data['artifacts'][0]['relative_path']) == 512
    data['issued_at'] = '2026-10-29T12:05:00Z'
    checker = verifier(tmp_path, key)
    assert verify(checker, key.envelope(encode(data))).sequence == 10
    too_long = deepcopy(data)
    too_long['sequence'] = 11
    too_long['artifacts'][0]['relative_path'] += 'x'
    rejected(checker, key, too_long, 'update_limit')
    too_late = data | dict(sequence=11, issued_at='2026-10-29T12:05:01Z')
    rejected(checker, key, too_late, 'update_time')


def test_total_unpacked_size_exact_limit_and_one_extra(tmp_path, key):
    data = payload()
    data['artifacts'][0]['inventory']['unpacked_bytes'] = 34359738367
    data['artifacts'].append(dict(id='extension', relative_path='extension.bin', size=1,
                                  sha256='f' * 64, kind='extension', format='file', inventory=None))
    checker = verifier(tmp_path, key)
    assert verify(checker, key.envelope(encode(data))).sequence == 10
    data['sequence'] = 11
    data['artifacts'][1]['size'] = 2
    rejected(checker, key, data, 'update_limit')


@pytest.mark.parametrize('path', ['x/COM¹.txt', 'x/COM².txt', 'x/COM³.txt', 'x/LPT¹.txt',
                                 'x/LPT².txt', 'x/LPT³.txt', './a.zip', 'a/./b.zip', 'x/..'])
def test_windows_aliases_and_dot_segments_are_rejected(tmp_path, key, path):
    data = payload()
    data['artifacts'][0]['relative_path'] = path
    rejected(verifier(tmp_path, key), key, data, 'update_manifest')


def test_artifact_inventory_names_share_collision_namespace(tmp_path, key):
    data = payload()
    data['artifacts'][0]['inventory']['relative_path'] = 'PAYLOAD/APPLICATION.ZIP'
    rejected(verifier(tmp_path, key), key, data, 'update_manifest')


def test_failed_replay_commit_never_issues_verified_manifest(tmp_path, key, monkeypatch):
    import os
    checker = verifier(tmp_path, key)
    raw = key.envelope(encode(payload()))
    verify(checker, raw)
    ledger = tmp_path / 'runtime/update-replay.json'
    committed = ledger.read_bytes()
    def fail(_):
        raise OSError('test disk flush failure')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail)
        rejected(checker, key, payload() | {'sequence': 11}, 'update_replay')
    assert ledger.read_bytes() == committed
    assert verify(verifier(tmp_path, key), raw).sequence == 10
