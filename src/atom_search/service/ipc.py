"""User-ACL named pipes. Only bounded JSON messages cross this channel."""
import ctypes as c
from ctypes import wintypes as w
from dataclasses import asdict, is_dataclass
import hashlib
import queue
import threading
import time

from atom_search.runtime import _windows
from atom_search.http.security import error
from atom_search.http.serialization import MAX_BODY, decode, encode, envelope, unpack
from .contracts import ServiceError

SETTINGS_APPLY_TIMEOUT = 30


def _assert_private(handle):
    descriptor = c.c_void_p()
    code = _windows._api(_windows._dll('advapi32'), 'GetSecurityInfo',
        [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p,
         c.POINTER(c.c_void_p)], w.DWORD)(handle, 6, 1 | 4, None, None, None, None, c.byref(descriptor))
    if code:
        raise error('service_identity_mismatch', 409)
    try:
        _windows._assert_descriptor_private(descriptor)
    finally:
        _windows._free(descriptor)


def pipe_name(generation):
    sid = hashlib.sha256(_windows.user_sid().encode()).hexdigest()[:24]
    return r'\\.\pipe\AtomSearch.' + sid + '.' + generation


def connect(descriptor):
    import _winapi
    from multiprocessing.connection import PipeConnection
    address = pipe_name(descriptor['generation'])
    _winapi.WaitNamedPipe(address, 5000)
    handle = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
        0, _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
    connection = PipeConnection(handle)
    try:
        _assert_private(handle)
        pid = w.ULONG()
        _windows._check(_windows._api(_windows._dll('kernel32'), 'GetNamedPipeServerProcessId',
            [w.HANDLE, c.POINTER(w.ULONG)])(handle, c.byref(pid)), 'pipe_identity_unavailable')
        if pid.value != descriptor['pid']:
            raise error('service_identity_mismatch', 409)
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        return connection
    except BaseException:
        connection.close()
        raise


def request(descriptor, secret, method, arguments):
    timeout = SETTINGS_APPLY_TIMEOUT if method == 'settings_apply' else 5
    payload = encode(envelope(dict(installation_id=descriptor['installation_id'],
        generation=descriptor['generation'], authorization=secret, method=method, arguments=arguments)))
    if len(payload) > MAX_BODY:
        raise error('request_too_large', 413)
    try:
        with connect(descriptor) as connection:
            timer = threading.Timer(timeout + .1, connection.close)
            timer.daemon = True
            timer.start()
            try:
                connection.send_bytes(payload)
                if not connection.poll(timeout):
                    failure = error('request_timeout', 504, retryable=True)
                    if method == 'settings_apply':
                        failure.details = {'outcome': 'unknown'}
                    raise failure
                response = decode(connection.recv_bytes(2 * 1024 * 1024))
                # Error codes carry the HTTP-equivalent status for this transport.
                code = (response.get('error') or {}).get('code')
                status = {'access_denied': 403, 'authentication_required': 401,
                          'service_identity_mismatch': 409, 'revision_conflict': 409,
                          'source_scope_denied': 403, 'result_not_found': 404, 'route_not_found': 404,
                          'service_unavailable': 503, 'service_stopping': 503,
                          'settings_busy': 409, 'request_timeout': 504,
                          'too_many_requests': 429, 'request_too_large': 413,
                          'settings_apply_failed': 500, 'settings_recovery_required': 500}.get(code, 400 if code else 200)
                return unpack(response, status)
            finally:
                timer.cancel()
    except ServiceError:
        raise
    except (OSError, EOFError, ValueError):
        raise error('service_unavailable', 503, retryable=True) from None


def stream_request(descriptor, secret, method, arguments, progress=None, cancel=None):
    """Legacy manual scan progress, not a public job or scheduler capability."""
    import uuid
    operation_id = str(uuid.uuid4())
    arguments = dict(arguments, operation_id=operation_id)
    payload = encode(envelope(dict(installation_id=descriptor['installation_id'],
        generation=descriptor['generation'], authorization=secret, method=method, arguments=arguments)))
    cancellation_sent = False
    try:
        with connect(descriptor) as connection:
            connection.send_bytes(payload)
            while True:
                if cancel is not None and cancel.is_set() and not cancellation_sent:
                    request(descriptor, secret, 'desktop_cancel', {'operation_id': operation_id})
                    cancellation_sent = True
                if not connection.poll(5):
                    raise error('request_timeout', 504, retryable=True)
                data = unpack(decode(connection.recv_bytes(2 * 1024 * 1024)))
                if data['event'] == 'result':
                    return data['value']
                if data['event'] == 'progress' and progress is not None:
                    progress(data['value'])
    except (OSError, EOFError, ValueError):
        raise error('service_unavailable', 503, retryable=True) from None


class OwnerIPC:
    def __init__(self, owner):
        self.owner = owner
        self.address = pipe_name(owner.generation)
        self._stop = threading.Event()
        self._thread = None
        self._slots = threading.BoundedSemaphore(16)
        self._handle = self._new_handle(first=True)

    def _new_handle(self, *, first=False):
        flags = 0x3 | 0x40000000 | (0x80000 if first else 0)  # duplex, overlapped, first instance
        with _windows._security() as attributes:
            handle = _windows._api(_windows._dll('kernel32'), 'CreateNamedPipeW',
                [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
                 c.POINTER(_windows._SecurityAttributes)], w.HANDLE)(
                self.address, flags, 0x4 | 0x2 | 0x8, 17, MAX_BODY, MAX_BODY, 5000, c.byref(attributes))
        if handle == c.c_void_p(-1).value:
            raise error('service_unavailable', 503)
        try:
            _assert_private(handle)
        except BaseException:
            _windows._close(handle)
            raise
        return handle

    def start(self):
        self._thread = threading.Thread(target=self._listen, name='atom-owner-ipc', daemon=False)
        self._thread.start()

    def _listen(self):
        import _winapi
        from multiprocessing.connection import PipeConnection
        while not self._stop.is_set():
            handle = self._handle
            try:
                pending = _winapi.ConnectNamedPipe(handle, overlapped=True)
                while _winapi.WaitForMultipleObjects([pending.event], False, 100) == 258:
                    if self._stop.is_set():
                        pending.cancel()
                        break
                _, code = pending.GetOverlappedResult(True)
                if code or self._stop.is_set():
                    _winapi.CloseHandle(handle)
                    self._handle = None
                    break
                self._handle = self._new_handle()
                connection = PipeConnection(handle)
                handle = None
                if self._slots.acquire(blocking=False):
                    threading.Thread(target=self._serve, args=(connection,),
                                     name='atom-owner-request', daemon=True).start()
                else:
                    connection.close()
            except OSError:
                if handle is not None:
                    _winapi.CloseHandle(handle)
                    self._handle = None
                if self._stop.is_set():
                    break
                self._handle = self._new_handle()
        if self._handle is not None:
            try:
                _winapi.CloseHandle(self._handle)
            except OSError:
                pass
            self._handle = None

    def _serve(self, connection):
        timer = threading.Timer(5.1, connection.close)
        timer.daemon = True
        timer.start()
        payload = None
        try:
            if not connection.poll(5):
                return
            payload = decode(connection.recv_bytes(MAX_BODY))
            data = unpack(payload)
            if (type(data) is not dict or set(data) != {'installation_id', 'generation', 'authorization', 'method', 'arguments'}
                    or data['installation_id'] != self.owner.installation_id or data['generation'] != self.owner.generation):
                raise error('service_identity_mismatch', 409)
            access = self.owner.authenticate(data['authorization'])
            facade = self.owner.facade(access)
            facade._require('owner')
            method, arguments = data['method'], data['arguments']
            if type(arguments) is not dict:
                raise error('invalid_request')
            if method in {'desktop_scan', 'desktop_embed'}:
                timer.cancel()
                return self._stream(connection, method, arguments, payload['request_id'])
            if method == 'desktop_cancel':
                operation_id = arguments.get('operation_id')
                with self.owner._condition:
                    cancel = self.owner._remote_cancel.get(operation_id)
                    if cancel is not None:
                        cancel.set()
                result = {}
            elif method in {'desktop_search', 'desktop_status', 'desktop_select_source',
                            'desktop_runtime_state', 'desktop_save_runtime_state'}:
                from .desktop import compatible_operation
                result = self.owner.call(lambda: compatible_operation(self.owner, method, arguments))
                if isinstance(result, list):
                    result = [asdict(item) if is_dataclass(item) else item for item in result]
            elif method in {'read_grant_issue', 'read_grant_get', 'read_grant_revoke'}:
                action = method.removeprefix('read_grant_')
                result = self.owner.call(lambda: getattr(self.owner.read_grants, action)(**arguments))
            elif method in {'settings_validate', 'settings_apply'}:
                if set(arguments) != {'candidate', 'expected_revision'}:
                    raise error('invalid_request')
                if method == 'settings_apply':
                    timer.cancel()
                    timer = threading.Timer(SETTINGS_APPLY_TIMEOUT + .1, connection.close)
                    timer.daemon = True
                    timer.start()
                try:
                    result = self.owner.call(lambda: getattr(facade, method)(**arguments),
                        SETTINGS_APPLY_TIMEOUT if method == 'settings_apply' else 5)
                except ServiceError as failure:
                    if method == 'settings_apply' and failure.code == 'request_timeout':
                        failure.details = {'outcome': 'unknown'}
                    raise
            else:
                raise error('route_not_found', 404)
            if method.startswith('desktop_'):
                result = {'value': result}
            response = envelope(asdict(result) if is_dataclass(result) else result, request_id=payload['request_id'])
            connection.send_bytes(encode(response))
        except ServiceError as failure:
            try:
                response = envelope(error=failure, request_id=payload.get('request_id') if isinstance(payload, dict) else None)
                connection.send_bytes(encode(response))
            except (OSError, ValueError):
                pass
        except (OSError, EOFError):
            pass
        except (ValueError, TypeError, KeyError, RecursionError):
            self._failure(connection, payload, error('invalid_request'))
        except Exception:
            self._failure(connection, payload, error('service_unavailable', 503, retryable=True))
        finally:
            timer.cancel()
            connection.close()
            self._slots.release()

    @staticmethod
    def _failure(connection, payload, failure):
        try:
            connection.send_bytes(encode(envelope(error=failure,
                request_id=payload.get('request_id') if isinstance(payload, dict) else None)))
        except (OSError, ValueError):
            pass

    def _stream(self, connection, method, arguments, request_id):
        from .desktop import compatible_operation
        from atom_search.http.security import public_uuid
        operation_id = public_uuid(arguments.pop('operation_id', None))
        cancel = threading.Event()
        updates = queue.Queue(maxsize=1)
        done = threading.Event()
        outcome = []
        if not self.owner._slots.acquire(blocking=False):
            raise error('too_many_requests', 429)
        with self.owner._condition:
            self.owner._remote_cancel[operation_id] = cancel
        def progress(value):
            try:
                updates.put_nowait(value)
            except queue.Full:
                pass
        def run():
            try:
                with self.owner.operation(cancel):
                    outcome.append((True, compatible_operation(self.owner, method, arguments, progress=progress, cancel=cancel)))
            except BaseException as failure:
                outcome.append((False, failure))
            finally:
                with self.owner._condition:
                    self.owner._remote_cancel.pop(operation_id, None)
                self.owner._slots.release()
                done.set()
        threading.Thread(target=run, name='atom-desktop-operation', daemon=False).start()
        try:
            while not done.wait(.25):
                try:
                    update = {'event': 'progress', 'value': updates.get_nowait()}
                except queue.Empty:
                    update = {'event': 'heartbeat', 'value': None}
                self._send_frame(connection, envelope(update, request_id=request_id))
            ok, value = outcome[0]
            if not ok:
                raise value
            self._send_frame(connection, envelope({'event': 'result', 'value': value}, request_id=request_id))
        finally:
            cancel.set()

    @staticmethod
    def _send_frame(connection, payload):
        timer = threading.Timer(5, connection.close)
        timer.daemon = True
        timer.start()
        try:
            connection.send_bytes(encode(payload))
        finally:
            timer.cancel()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(6)
            if self._thread.is_alive():
                raise error('service_unavailable', 503)
        elif self._handle is not None:
            _windows._close(self._handle)
            self._handle = None
