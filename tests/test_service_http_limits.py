"""Actual Windows loopback transport, independent of slow PG/bootstrap work."""
from contextlib import contextmanager
import http.client
import socket
import time
from types import SimpleNamespace

from atom_search.http.server import LocalHTTPServer


@contextmanager
def endpoint():
    owner = SimpleNamespace(installation_id='qa-installation', generation='qa-generation', base_url=None)
    server = LocalHTTPServer(owner)
    owner.base_url = 'http://127.0.0.1:' + str(server.server_address[1])
    server.start()
    try:
        yield server.server_address, {'Authorization': 'Bearer qa-only-token',
            'X-Atom-Installation': owner.installation_id, 'X-Atom-Generation': owner.generation}
    finally:
        server.stop()


def assert_large_body_rejected(address, headers):
    connection = http.client.HTTPConnection(*address, timeout=7)
    try:
        connection.request('POST', '/v1/search', b'x' * (8 * 1024 * 1024), headers)
        response = connection.getresponse()
        assert response.status == 413
        response.read()
    finally:
        connection.close()


def assert_slow_oversized_sender_gets_early_reply(address, headers):
    with socket.create_connection(address, timeout=.7) as connection:
        incoming = ('POST /v1/search HTTP/1.0\r\nHost: ' + address[0] + ':' + str(address[1]) + '\r\n'
            + ''.join(name + ': ' + value + '\r\n' for name, value in headers.items())
            + 'Content-Length: 2097152\r\n\r\nx').encode()
        began = time.monotonic()
        connection.sendall(incoming)
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 413 and time.monotonic() - began < .5
        response.read()


def test_large_oversized_body_keeps_413_instead_of_windows_reset():
    with endpoint() as (address, headers):
        assert_large_body_rejected(address, headers)


def test_slow_oversized_sender_gets_413_before_sending_body():
    with endpoint() as (address, headers):
        assert_slow_oversized_sender_gets_early_reply(address, headers)
