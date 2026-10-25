"""Bounded stdlib HTTP on the exact owner loopback endpoint."""
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from atom_search.service.contracts import SearchRequest, ServiceError
from .security import authorize_headers, bearer, error, proof
from .serialization import MAX_BODY, decode, encode, envelope

MAX_REJECT_DISCARD = 8 * 1024 * 1024


def route(owner, access, method, path, body):
    facade = owner.facade(access)
    target = urlsplit(path)
    query = parse_qs(target.query, keep_blank_values=True)
    if target.fragment or target.scheme or target.netloc:
        raise error('invalid_request')
    if method == 'POST' and target.path == '/v1/search':
        if query or type(body) is not dict or set(body) - {'query', 'source_ids', 'limit', 'mode', 'project_id'}:
            raise error('invalid_request')
        try:
            return facade.search(SearchRequest(**body))
        except TypeError:
            raise error('invalid_request') from None
    if method == 'GET' and target.path.startswith('/v1/results/'):
        if set(query) - {'source_id', 'scope'} or ('scope' in query and query != {'scope': ['none']}):
            raise error('invalid_request')
        scope = () if 'scope' in query else tuple(query['source_id']) if 'source_id' in query else None
        return facade.fetch(target.path[len('/v1/results/'):], source_ids=scope)
    if method == 'GET' and target.path == '/v1/status' and not query:
        return facade.status()
    if method == 'GET' and target.path == '/v1/settings' and not query:
        return facade.settings_get()
    if method == 'POST' and target.path == '/v1/jobs/catalog':
        facade._require('owner')
        if query or type(body) is not dict or set(body) != {'source_ids'}:
            raise error('invalid_request')
        return {'job_ids': facade.start_catalog(body['source_ids'])}
    if method == 'POST' and target.path.startswith('/v1/jobs/') and target.path.endswith('/cancel'):
        facade._require('owner')
        if query or body != {}:
            raise error('invalid_request')
        return facade.cancel_job(target.path[len('/v1/jobs/'):-len('/cancel')])
    raise error('route_not_found', 404)


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, owner):
        self.owner = owner
        self._connections = threading.BoundedSemaphore(16)
        self._thread = None
        super().__init__(('127.0.0.1', 0), Handler)

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, kwargs={'poll_interval': .05},
                                        name='atom-loopback', daemon=False)
        self._thread.start()

    def stop(self):
        if self._thread is not None:
            self.shutdown()
            self._thread.join()
        self.server_close()

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            body = encode(envelope(error=error('too_many_requests', 429, retryable=True)))
            try:
                # Consume a small ordinary request before closing. Closing a
                # Windows socket with unread request bytes resets the peer and
                # can hide the promised 429 response from HTTP clients.
                request.settimeout(.2)
                incoming = b''
                deadline = time.monotonic() + .2
                while b'\r\n\r\n' not in incoming and len(incoming) <= MAX_BODY and time.monotonic() < deadline:
                    chunk = request.recv(8192)
                    if not chunk:
                        break
                    incoming += chunk
                if b'\r\n\r\n' in incoming:
                    header, received = incoming.split(b'\r\n\r\n', 1)
                    length = re.search(br'(?im)^Content-Length:\s*([0-9]+)\s*$', header)
                    remaining = min(MAX_BODY, int(length[1])) - len(received) if length else 0
                    while remaining > 0 and time.monotonic() < deadline:
                        chunk = request.recv(min(8192, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                request.sendall(b'HTTP/1.0 429 Too Many Requests\r\nContent-Type: application/json\r\nContent-Length: '
                                + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request, client_address):
        pass  # Raw request/OS exceptions can contain private input or tokens.


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        self.deadline = time.monotonic() + 5
        self.request.settimeout(5)
        super().setup()
        def expired():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._watchdog = threading.Timer(5, expired)
        self._watchdog.daemon = True
        self._watchdog.start()

    def finish(self):
        self._watchdog.cancel()
        super().finish()

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(None, error('invalid_request', code))

    def reply(self, data, failure=None):
        try:
            if is_dataclass(data):
                data = asdict(data)
            payload = encode(envelope(data, failure))
            self.send_response(200 if failure is None else failure.status_code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.send_header('X-Atom-Installation', self.server.owner.installation_id)
            self.send_header('X-Atom-Generation', self.server.owner.generation)
            if getattr(self, 'headers', {}).get('Origin') == self.server.owner.base_url:
                self.send_header('Access-Control-Allow-Origin', self.server.owner.base_url)
                self.send_header('Vary', 'Origin')
            self.end_headers()
            self.wfile.write(payload)
        except (OSError, ValueError):
            pass
        self.close_connection = True

    def handle_api(self):
        owner = self.server.owner
        try:
            if self.path != '/v1/handshake' and self.headers.get('Authorization') is None:
                raise error('authentication_required', 401)
            authorize_headers(owner, self.headers, self.client_address[0])
            if self.headers.get('Transfer-Encoding') is not None:
                raise error('invalid_request')
            length = self.headers.get('Content-Length', '0')
            if not re.fullmatch(r'[0-9]{1,10}', length):
                raise error('invalid_request')
            length = int(length)
            if length > MAX_BODY:
                return self.reject_oversized(length)
            if self.command == 'GET' and length:
                raise error('invalid_request')
            data = self.rfile.read(length) if length else b''
            if len(data) != length:
                raise error('invalid_request')
            self._watchdog.cancel()
            if self.path == '/v1/handshake' and self.command == 'GET':
                nonce = self.headers.get('X-Atom-Nonce', '')
                if not re.fullmatch('[0-9a-f]{64}', nonce):
                    raise error('invalid_request')
                secret, _ = owner.credential(self.headers.get('X-Atom-Credential'))
                return self.reply({'proof': proof(secret, nonce, owner.descriptor())})
            access = bearer(owner, self.headers)
            body = decode(data) if data else None
            value = owner.call(lambda: route(owner, access, self.command, self.path, body),
                               self.deadline - time.monotonic())
            self.reply(value)
        except ServiceError as failure:
            self.reply(None, failure)
        except (ValueError, UnicodeError, RecursionError, TypeError):
            self.reply(None, error('invalid_request'))
        except Exception:
            self.reply(None, error('service_unavailable', 503, retryable=True))

    def reject_oversized(self, length):
        # Reply before waiting for any body. A slow sender must see rejection
        # immediately. Half-close the reply, then discard bounded chunks so a
        # normal large Windows upload doesn't replace that 413 with a reset.
        self.reply(None, error('request_too_large', 413))
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            remaining = min(length, MAX_REJECT_DISCARD)
            while remaining and time.monotonic() < self.deadline:
                self.request.settimeout(max(.001, self.deadline - time.monotonic()))
                chunk = self.rfile.read1(min(remaining, MAX_BODY))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass  # Response already sent; never replace it with another error.

    do_GET = do_POST = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = handle_api
