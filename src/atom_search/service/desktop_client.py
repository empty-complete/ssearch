"""Private inherited-pipe adapter over P2; never constructs a service owner.

Frames are a four-byte unsigned big-endian byte count followed by UTF-8 JSON:
{session_id: UUID, generation: UUID, envelope: P2 envelope}. Request envelope.data
is {method, arguments}; replies retain the same correlation and P2 DTO/error.
The first valid session binds this broker process. Desktop main owns the pipes,
request cancellation and at most 64 pending requests; renderers never see them.
Requests execute serially. No mutation is retried, including unknown outcomes.
EOF ends the broker, never the service. Frozen CLI wiring belongs to packaging.
"""
from dataclasses import asdict, is_dataclass
from pathlib import Path
import queue
import struct
import threading

from atom_search.http.security import error, public_uuid, scope_tuple
from atom_search.http.serialization import MAX_BODY, decode, encode, envelope, unpack
from .client import ServiceClient
from .contracts import SearchRequest, ServiceError

MAX_RESPONSE = 2 * 1024 * 1024
FRAME_OVERHEAD = 4096
FRAME_TIMEOUT = 10


def _bounded(operation, timeout):
    """Private pipes can stall on a partial frame or an unread reply."""
    result = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, operation()))
        except BaseException as failure:
            result.put((False, failure))

    threading.Thread(target=run, name='ssearch-broker-io', daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError() from None
    if not ok:
        raise value
    return value


def _read_frame(stream, first):
    def exact(length):
        chunks = bytearray()
        while len(chunks) < length:
            chunk = stream.read(length - len(chunks))
            if not chunk:
                raise ValueError('truncated_frame')
            chunks.extend(chunk)
        return bytes(chunks)

    header = first + exact(3)
    length = struct.unpack('!I', header)[0]
    if not 1 <= length <= MAX_BODY + FRAME_OVERHEAD:
        raise ValueError('invalid_frame_length')
    return decode(exact(length))


def _write_frame(stream, value):
    content = encode(value)
    if len(content) > MAX_RESPONSE + FRAME_OVERHEAD:
        raise ValueError('response_too_large')
    # Raw streams may write fewer bytes than requested.
    remaining = memoryview(struct.pack('!I', len(content)) + content)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError('closed_pipe')
        remaining = remaining[written:]
    stream.flush()


def _call(client, method, arguments):
    fields = {
        'search': ({'query'}, {'query', 'source_ids', 'limit', 'mode', 'project_id'}),
        'fetch': ({'result_id'}, {'result_id', 'source_ids'}),
        'status': (set(), set()), 'settings_get': (set(), set()),
        'settings_validate': ({'candidate', 'expected_revision'}, {'candidate', 'expected_revision'}),
        'settings_apply': ({'candidate', 'expected_revision'}, {'candidate', 'expected_revision'}),
        'start_catalog': (set(), {'source_ids'}), 'cancel_job': ({'job_id'}, {'job_id'}),
    }
    if type(method) is not str or method not in fields:
        raise error('route_not_found', 404)
    required, allowed = fields[method]
    if type(arguments) is not dict or not required <= arguments.keys() <= allowed:
        raise error('invalid_request')
    if len(encode(arguments)) > MAX_BODY:
        raise error('request_too_large', 413)
    arguments = dict(arguments)
    if 'source_ids' in arguments:
        arguments['source_ids'] = scope_tuple(arguments['source_ids'])
    for name in ('result_id', 'job_id'):
        if name in arguments:
            public_uuid(arguments[name])
    if method == 'search':
        if (type(arguments['query']) is not str or not 1 <= len(arguments['query'].strip()) <= 512
                or type(arguments.get('limit', 50)) is not int or not 1 <= arguments.get('limit', 50) <= 100
                or arguments.get('mode', 'hybrid') not in ('literal', 'lexical', 'hybrid')):
            raise error('invalid_request')
        if arguments.get('project_id') is not None:
            public_uuid(arguments['project_id'])
        result = client.search(SearchRequest(**arguments))
    else:
        if method in ('settings_validate', 'settings_apply') and (
                type(arguments['candidate']) is not dict or type(arguments['expected_revision']) is not int
                or arguments['expected_revision'] < 1):
            raise error('invalid_request')
        result = getattr(client, method)(**arguments)
    if method == 'start_catalog':
        return {'job_ids': result}  # Keep the P2 HTTP DTO, including an empty list.
    return asdict(result) if is_dataclass(result) else result


def run_desktop_client(input, output, *, profile: Path) -> int:
    """Serve one private desktop-main session. stdout is protocol only.

    Profile chooses existing P2 discovery, ACL/PID/DPAPI and nonce/version checks.
    There is no credential/endpoint/method override and no owner bootstrap here.
    Structural corruption closes the pipe; valid calls get correlated P2 errors.
    """
    client = None
    try:
        client = ServiceClient(Path(profile).absolute() / 'engine' / 'runtime' / 'endpoint.json', None)
        client.status()  # Authenticated nonce proof, even if the first call is private apply.
        generation = client.descriptor['generation']
        session = None
        while True:
            # An idle desktop session has no deadline. Once a frame begins,
            # partial header/body and stalled writes have a bounded budget.
            first = input.read(1)
            if not first:
                return 0
            frame = _bounded(lambda: _read_frame(input, first), FRAME_TIMEOUT)
            if type(frame) is not dict or set(frame) != {'session_id', 'generation', 'envelope'}:
                return 1
            public_uuid(frame['session_id'])
            public_uuid(frame['generation'])
            payload = frame['envelope']
            # unpack validates exact P2 envelope/version/request UUID. Requests
            # cannot smuggle a service error envelope into a privileged call.
            if type(payload) is not dict or payload.get('error') is not None:
                return 1
            data = unpack(payload)
            if type(data) is not dict or set(data) != {'method', 'arguments'}:
                return 1
            if session is None and frame['generation'] == generation:
                session = frame['session_id']
            try:
                if frame['generation'] != generation or frame['session_id'] != session:
                    raise error('service_identity_mismatch', 409)
                value = _call(client, data['method'], data['arguments'])
                response = envelope(value, request_id=payload['request_id'])
            except ServiceError as failure:
                response = envelope(error=failure, request_id=payload['request_id'])
            except (ValueError, TypeError, KeyError, RecursionError):
                response = envelope(error=error('invalid_request'), request_id=payload['request_id'])
            except Exception:
                response = envelope(error=error('service_unavailable', 503, retryable=True),
                                    request_id=payload['request_id'])
            _bounded(lambda: _write_frame(output, dict(session_id=frame['session_id'],
                generation=frame['generation'], envelope=response)), FRAME_TIMEOUT)
    except (Exception, KeyboardInterrupt):
        # Raw exceptions can contain paths, credentials or caller-controlled text.
        return 1
    finally:
        if client is not None:
            client.close()  # P2 clients cannot stop the owner.
