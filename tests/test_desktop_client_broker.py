"""Desktop framing/authority contracts, using the real P2 client and owner."""
from datetime import datetime, timezone
from io import BytesIO
import os
import struct
import subprocess
import uuid

import pytest

from atom_search.http.serialization import decode, encode, envelope


def frame(value):
    content = encode(value)
    return struct.pack('!I', len(content)) + content


def request(owner, method, arguments=None, *, session=None, generation=None):
    return dict(session_id=session or str(uuid.uuid4()), generation=generation or owner.generation,
                envelope=envelope(dict(method=method, arguments=arguments or {})))


def replies(stream):
    stream.seek(0)
    result = []
    while header := stream.read(4):
        result.append(decode(stream.read(struct.unpack('!I', header)[0])))
    return result


def run(owner, messages):
    from atom_search.service.desktop_client import run_desktop_client
    output = BytesIO()
    code = run_desktop_client(BytesIO(b''.join(frame(item) for item in messages)), output,
                              profile=owner.profile_dir)
    return code, replies(output)


def test_broker_preserves_scope_and_p2_metadata(owner_profile):
    owner = owner_profile
    session = str(uuid.uuid4())
    messages = [request(owner, 'search', dict(query='drawing', mode='literal', source_ids=scope), session=session)
                for scope in (None, [])]
    code, result = run(owner, messages)
    assert code == 0
    assert len(result[0]['envelope']['data']['results']) == 2
    assert result[1]['envelope']['data']['results'] == []
    for sent, received in zip(messages, result):
        assert received['session_id'] == session and received['generation'] == owner.generation
        assert received['envelope']['request_id'] == sent['envelope']['request_id']
        assert received['envelope']['error'] is None
    assert owner.credentials.resolve(owner.credential_ref) not in str(result)


def test_broker_private_apply_and_revision_conflict(owner_profile):
    snapshot = owner_profile.repository.snapshot()
    candidate = snapshot.settings
    candidate['ui']['theme'] = 'dark' if candidate['ui']['theme'] != 'dark' else 'light'
    session = str(uuid.uuid4())
    messages = [request(owner_profile, method, dict(candidate=candidate, expected_revision=snapshot.revision),
                        session=session) for method in ('settings_validate', 'settings_apply', 'settings_apply')]
    code, result = run(owner_profile, messages)
    assert code == 0
    assert result[0]['envelope']['data']['valid'] is True
    assert result[1]['envelope']['data']['revision'] == snapshot.revision + 1
    assert result[2]['envelope']['error']['code'] == 'revision_conflict'
    assert result[2]['envelope']['data'] is None


@pytest.mark.parametrize('method,arguments,code', [
    ('_ipc', {}, 'route_not_found'),
    ('desktop_select_source', {'path': 'C:/unselected'}, 'route_not_found'),
    ('settings_apply', {'candidate': {}, 'expected_revision': True}, 'invalid_request'),
    ('status', {'authorization': 'must-not-be-echoed'}, 'invalid_request'),
    ('search', {'query': 'drawing', 'source_ids': [123]}, 'invalid_request'),
])
def test_broker_allowlist_rejects_invalid_calls(owner_profile, method, arguments, code):
    _, result = run(owner_profile, [request(owner_profile, method, arguments)])
    assert result[0]['envelope']['error']['code'] == code
    assert 'must-not-be-echoed' not in str(result)


def test_broker_rejects_wrong_generation_and_changed_session(owner_profile):
    first = request(owner_profile, 'status')
    _, result = run(owner_profile, [first, request(owner_profile, 'status')])
    assert result[0]['envelope']['data'] is not None
    assert result[1]['envelope']['error']['code'] == 'service_identity_mismatch'
    _, result = run(owner_profile, [request(owner_profile, 'status', generation=str(uuid.uuid4()))])
    assert result[0]['envelope']['error']['code'] == 'service_identity_mismatch'


@pytest.mark.parametrize('data', [b'\x00', struct.pack('!I', 70000), struct.pack('!I', 10) + b'{}',
                                  struct.pack('!I', 1) + b'{', struct.pack('!I', 0)])
def test_invalid_frames_close_without_echo(owner_profile, data):
    from atom_search.service.desktop_client import run_desktop_client
    output = BytesIO()
    assert run_desktop_client(BytesIO(data), output, profile=owner_profile.profile_dir) == 1
    assert output.getvalue() == b''


def test_empty_eof_does_not_stop_owner(owner_profile):
    code, result = run(owner_profile, [])
    assert (code, result) == (0, [])
    from atom_search.service.client import ServiceClient
    assert ServiceClient(owner_profile.endpoint_file, None).status().api_version == '1'


def test_two_brokers_share_one_generation_and_leave_owner_running(owner_profile):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, owner_profile, [request(owner_profile, 'settings_get')]) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert all(code == 0 for code, _ in results)
    assert {reply[0]['generation'] for _, reply in results} == {owner_profile.generation}
    assert len({reply[0]['envelope']['data']['revision'] for _, reply in results}) == 1


def test_partial_frame_has_deadline_without_leaking_reader_errors(owner_profile, monkeypatch):
    import threading
    from atom_search.service import desktop_client
    release = threading.Event()

    class Stalled:
        first = True

        def read(self, length):
            if self.first:
                self.first = False
                return b'\x00'
            release.wait(5)
            raise OSError('private input must never be echoed')

    monkeypatch.setattr(desktop_client, 'FRAME_TIMEOUT', .05)
    output = BytesIO()
    try:
        assert desktop_client.run_desktop_client(Stalled(), output, profile=owner_profile.profile_dir) == 1
        assert output.getvalue() == b''
    finally:
        release.set()


def test_idle_session_is_not_a_partial_frame_timeout(owner_profile, monkeypatch):
    import time
    from atom_search.service import desktop_client

    class Idle(BytesIO):
        waiting = True

        def read(self, length):
            if self.waiting:
                self.waiting = False
                time.sleep(.05)
            return super().read(length)

    monkeypatch.setattr(desktop_client, 'FRAME_TIMEOUT', .01)
    output = BytesIO()
    sent = request(owner_profile, 'status')
    assert desktop_client.run_desktop_client(Idle(frame(sent)), output, profile=owner_profile.profile_dir) == 0
    assert replies(output)[0]['envelope']['data']['api_version'] == '1'


@pytest.mark.parametrize('arguments', [
    {},
    {'selected_path': 'C:/fictional/access', 'source_id': 'a0000000-0000-4000-8000-000000000001'},
    {'selected_path': None},
    {'selected_path': True},
    {'selected_path': 'relative/folder'},
    {'source_id': 'not-a-uuid'},
    {'path': 'C:/fictional/not-host-retained'},
])
def test_folder_access_requires_exactly_one_private_identity(owner_profile, arguments):
    code, result = run(owner_profile, [request(owner_profile, 'check_folder_access', arguments)])
    assert code == 0 and len(result) == 1
    reply = result[0]['envelope']
    assert reply['data'] is None
    assert reply['error']['code'] == 'invalid_request'
    assert reply['error']['details'] == {}
    assert 'fictional' not in str(reply)


def test_folder_access_owner_checks_selected_directory_without_saving_it(owner_profile, tmp_path):
    selected = tmp_path / 'Explicit selection Папка'
    selected.mkdir()
    before = owner_profile.repository.snapshot()
    session = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    code, result = run(owner_profile, [
        request(owner_profile, 'check_folder_access', {'selected_path': str(selected)}, session=session),
        request(owner_profile, 'settings_get', session=session),
        request(owner_profile, 'status', session=session),
    ])
    finished = datetime.now(timezone.utc)
    assert code == 0 and len(result) == 3
    reply = result[0]['envelope']
    assert reply['error'] is None
    access = reply['data']
    assert set(access) == {'state', 'checked_at', 'message'}
    assert access['state'] == 'accessible'
    checked = datetime.fromisoformat(access['checked_at'])
    assert checked.utcoffset() == timezone.utc.utcoffset(checked) and started <= checked <= finished
    assert type(access['message']) is str and access['message']
    assert str(selected) not in str(reply) and selected.name not in str(reply)
    saved = result[1]['envelope']['data']
    assert saved['settings']['sources'] == before.settings['sources']
    assert saved['revision'] == before.revision
    assert result[2]['envelope']['data']['config_version']['revision'] == before.revision
    assert {item['generation'] for item in result} == {owner_profile.generation}


def test_folder_access_reader_is_denied_before_native_io(owner_profile, read_client, tmp_path, monkeypatch):
    from atom_search.service import desktop_client

    selected = tmp_path / 'Never probe reader supplied folder'
    touched = []
    original_stat = os.stat

    def guarded_stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == str(selected):
            touched.append('stat')
            raise AssertionError('unauthorized native path access')
        return original_stat(path, *args, **kwargs)

    def forbidden_child(*args, **kwargs):
        touched.append('process')
        raise AssertionError('unauthorized probe worker launch')

    # Substitute only credential selection; discovery, DPAPI, nonce, HTTP and
    # owner IPC authorization still use the actual live P2 implementation.
    def reader(endpoint, credential_ref):
        assert endpoint == owner_profile.endpoint_file and credential_ref is None
        read_client._verified = False
        return read_client

    monkeypatch.setattr(desktop_client, 'ServiceClient', reader)
    monkeypatch.setattr(os, 'stat', guarded_stat)
    monkeypatch.setattr(subprocess, 'Popen', forbidden_child)
    session = str(uuid.uuid4())
    source_id = owner_profile.repository.snapshot().settings['sources'][0]['id']
    code, result = run(owner_profile, [
        request(owner_profile, 'check_folder_access', {'selected_path': str(selected)}, session=session),
        request(owner_profile, 'check_folder_access', {'source_id': source_id}, session=session),
        request(owner_profile, 'status', session=session),
    ])
    assert code == 0 and len(result) == 3 and touched == []
    for received in result[:2]:
        reply = received['envelope']
        assert reply['data'] is None
        assert reply['error']['code'] == 'access_denied'
        assert reply['error']['details'] == {}
        assert str(selected) not in str(reply)
    assert result[2]['envelope']['error'] is None
    assert result[2]['envelope']['data']['api_version'] == '1'


@pytest.mark.parametrize('arguments', [
    {}, {'selected_path': None}, {'selected_path': 'relative/folder'},
    {'selected_path': 'C:/fictional/selection', 'source_id': 'a0000000-0000-4000-8000-000000000001'},
    {'path': 'C:/fictional/selection'},
])
def test_source_identity_accepts_only_one_host_retained_path(owner_profile, arguments):
    code, result = run(owner_profile, [request(owner_profile, 'select_source_identity', arguments)])
    assert code == 0 and len(result) == 1
    reply = result[0]['envelope']
    assert reply['data'] is None and reply['error']['code'] == 'invalid_request'
    assert 'fictional' not in str(reply)


def test_source_identity_reuses_removed_root_and_repaths_without_binding_on_lookup(owner_profile, tmp_path):
    from copy import deepcopy

    owner = owner_profile
    original = owner.repository.snapshot()
    source = original.settings['sources'][0]
    new_root = str(tmp_path / 'Uncommitted selection Папка')

    def lookup(path):
        before = owner.repository.snapshot()
        code, result = run(owner, [request(owner, 'select_source_identity', {'selected_path': path})])
        assert code == 0 and len(result) == 1
        reply = result[0]['envelope']
        assert reply['error'] is None
        assert set(reply['data']) == {'source_id'}
        identity = reply['data']['source_id']
        assert str(uuid.UUID(identity)) == identity
        after = owner.repository.snapshot()
        assert (after.revision, after.settings) == (before.revision, before.settings)
        assert path not in str(reply)
        return identity

    try:
        # This UUID already identifies catalogued metadata in actual PostgreSQL.
        assert lookup(source['path']) == source['id']
        assert lookup(source['path'].replace('\\', '/') + '/.') == source['id']
        removed = deepcopy(original.settings)
        removed['sources'] = removed['sources'][1:]
        owner.coordinator.owner_apply(removed, original.revision)
        assert lookup(source['path']) == source['id']
        latest = owner.repository.snapshot()
        readded = deepcopy(latest.settings)
        readded['sources'].insert(0, dict(source, id=lookup(source['path'])))
        owner.coordinator.owner_apply(readded, latest.revision)
        assert owner.repository.snapshot().settings['sources'][0] == source

        assert owner.core.public_ids.source_uuid(new_root) is None
        replacement = lookup(new_root)
        assert replacement != source['id']
        assert owner.core.public_ids.source_uuid(new_root) is None  # Lookup never binds.
        latest = owner.repository.snapshot()
        repathed = deepcopy(latest.settings)
        repathed['sources'][0] = dict(source, id=replacement, path=new_root)
        owner.coordinator.owner_apply(repathed, latest.revision)
        assert lookup(new_root) == replacement
        assert lookup(source['path']) == source['id']
    finally:
        latest = owner.repository.snapshot()
        if latest.settings['sources'] != original.settings['sources']:
            restored = deepcopy(latest.settings)
            restored['sources'] = original.settings['sources']
            owner.coordinator.owner_apply(restored, latest.revision)


def test_source_identity_reader_is_denied_before_owner_lookup(owner_profile, read_client, monkeypatch):
    from atom_search.service import desktop_client

    lookups = []

    def forbidden_lookup(path):
        lookups.append(path)
        raise AssertionError('reader reached owner source identity lookup')

    def reader(endpoint, credential_ref):
        assert endpoint == owner_profile.endpoint_file and credential_ref is None
        read_client._verified = False
        return read_client

    monkeypatch.setattr(desktop_client, 'ServiceClient', reader)
    monkeypatch.setattr(owner_profile.registry, 'select_source_id', forbidden_lookup)
    selected = owner_profile.repository.snapshot().settings['sources'][0]['path']
    code, result = run(owner_profile, [request(owner_profile, 'select_source_identity', {'selected_path': selected})])
    assert code == 0 and len(result) == 1 and lookups == []
    reply = result[0]['envelope']
    assert reply['data'] is None and reply['error']['code'] == 'access_denied'
    assert selected not in str(reply)


def test_source_identity_lookup_failure_never_returns_fallback_uuid(owner_profile, monkeypatch):
    before = owner_profile.repository.snapshot()
    lookups = []

    def unavailable(root):
        lookups.append(root)
        raise OSError('PRIVATE_LOOKUP_FAILURE must not cross the broker')

    monkeypatch.setattr(owner_profile.core.public_ids, 'source_uuid', unavailable)
    selected = before.settings['sources'][0]['path']
    code, result = run(owner_profile, [request(owner_profile, 'select_source_identity', {'selected_path': selected})])
    assert code == 0 and len(result) == 1
    reply = result[0]['envelope']
    assert reply['data'] is None and reply['error']['code'] == 'service_unavailable'
    assert len(lookups) == 1
    assert 'PRIVATE_LOOKUP_FAILURE' not in str(reply)
    after = owner_profile.repository.snapshot()
    assert (after.revision, after.settings) == (before.revision, before.settings)
