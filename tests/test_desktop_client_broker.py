"""Desktop framing/authority contracts, using the real P2 client and owner."""
from io import BytesIO
import struct
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
    assert ServiceClient(owner_profile.endpoint_file, None).status().schema_version == 1

