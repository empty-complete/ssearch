"""Identity-checked local client. Read calls use HTTP, settings use private IPC."""
import hmac
import http.client
from pathlib import Path
import re
import secrets
from urllib.parse import urlencode, urlsplit

from atom_search.runtime import _windows
from atom_search.runtime.credentials import CredentialStore, CredentialError
from atom_search.http.security import error, proof, public_uuid
from atom_search.http.serialization import MAX_BODY, decode, encode, unpack, restore
from .contracts import SearchResponse, SettingsSnapshot, SettingsValidation, ResultMetadata, HealthSnapshot, JobSnapshot


def read_owner_state(endpoint_file):
    path = endpoint_file.parent / 'service-state.json'
    _windows.assert_private(path)
    with path.open('rb') as stream:
        content = stream.read(MAX_BODY + 1)
    if len(content) > MAX_BODY:
        raise ValueError('invalid_owner_state')
    state = decode(content)
    if (type(state) is not dict or set(state) != {'schema_version', 'state', 'profile', 'installation_id',
            'generation', 'credential_ref', 'process'} or type(state['schema_version']) is not int
            or state['schema_version'] != 1 or state['state'] not in ('ready', 'stopped')
            or state['profile'] != str(endpoint_file.parents[2].resolve())):
        raise ValueError('invalid_owner_state')
    public_uuid(state['installation_id']); public_uuid(state['generation'])
    identity = _windows.ProcessIdentity(**state['process'])
    if (type(identity.pid) is not int or identity.pid <= 0 or type(identity.created) is not int
            or identity.created <= 0 or type(identity.executable) is not str):
        raise ValueError('invalid_owner_state')
    return state, identity


class ServiceClient:
    def __init__(self, endpoint_file: Path, credential_ref: str | None):
        self.endpoint_file = Path(endpoint_file).absolute()
        self.credential_ref = credential_ref
        self.descriptor = self._read_identity(allow_dead=True)
        self.credential_ref = credential_ref or self.descriptor['credential_ref']
        self._verified = False

    def _read_identity(self, *, allow_dead=False):
        try:
            path = self.endpoint_file
            if path.name != 'endpoint.json' or path.parent.name != 'runtime' or path.parent.parent.name != 'engine':
                raise ValueError()
            _windows.assert_private(path)
            with path.open('rb') as stream:
                content = stream.read(MAX_BODY + 1)
            if len(content) > MAX_BODY:
                raise ValueError()
            descriptor = decode(content)
            if (type(descriptor) is not dict or set(descriptor) != {'schema_version', 'pid', 'installation_id',
                    'generation', 'base_url', 'api_version', 'credential_ref'} or type(descriptor['schema_version']) is not int
                    or descriptor['schema_version'] != 1
                    or type(descriptor['pid']) is not int or descriptor['pid'] <= 0 or descriptor['api_version'] != '1'
                    or not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', descriptor['base_url'])
                    or urlsplit(descriptor['base_url']).port > 65535):
                raise ValueError()
            public_uuid(descriptor['installation_id']); public_uuid(descriptor['generation'])
            state, expected = read_owner_state(path)
            if (state['installation_id'] != descriptor['installation_id']
                    or state['credential_ref'] != descriptor['credential_ref']
                    or state['generation'] != descriptor['generation']):
                raise ValueError()
            if expected.pid != descriptor['pid']:
                raise ValueError()
            actual = _windows.process_identity(expected.pid)
            if actual != expected and (not allow_dead or actual is not None):
                raise ValueError()
            self._alive = actual == expected and state['state'] == 'ready'
            if not allow_dead and not self._alive:
                raise ValueError()
            return descriptor
        except Exception:
            raise error('service_identity_mismatch', 409) from None

    def owner_alive(self):
        self._read_identity(allow_dead=True)
        return self._alive

    def _identity(self):
        current = self._read_identity()
        if current != self.descriptor:
            raise error('service_identity_mismatch', 409)

    def _secret(self):
        try:
            return CredentialStore(self.endpoint_file.parent, 'service-v1').resolve(self.credential_ref)
        except CredentialError:
            raise error('authentication_required', 401) from None

    def _http(self, method, route, body=None, *, handshake=False):
        self._identity()
        descriptor = self.descriptor
        parsed = urlsplit(descriptor['base_url'])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        headers = {'X-Atom-Installation': descriptor['installation_id'], 'X-Atom-Generation': descriptor['generation']}
        secret = self._secret()
        nonce = secrets.token_hex(32)
        if handshake:
            headers.update({'X-Atom-Credential': self.credential_ref, 'X-Atom-Nonce': nonce})
        else:
            if not self._verified:
                self._http('GET', '/v1/handshake', handshake=True)
                self._verified = True
            headers['Authorization'] = 'Bearer ' + secret
        data = encode(body) if body is not None else None
        if data is not None and len(data) > MAX_BODY:
            raise error('request_too_large', 413)
        try:
            connection.request(method, route, data, headers)
            response = connection.getresponse()
            if (response.getheader('X-Atom-Installation') != descriptor['installation_id']
                    or response.getheader('X-Atom-Generation') != descriptor['generation']):
                raise error('service_identity_mismatch', 409)
            content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError()
            result = unpack(decode(content), response.status)
            if handshake and not hmac.compare_digest(result['proof'], proof(secret, nonce, descriptor)):
                raise error('service_identity_mismatch', 409)
            return result
        except (OSError, http.client.HTTPException, ValueError, KeyError):
            raise error('service_unavailable', 503, retryable=True) from None
        finally:
            connection.close()

    def search(self, request):
        from dataclasses import asdict
        return restore(SearchResponse, self._http('POST', '/v1/search', asdict(request)))

    def fetch(self, result_id, *, source_ids=None):
        route = '/v1/results/' + str(result_id)
        if source_ids == ():
            route += '?scope=none'
        elif source_ids is not None:
            route += '?' + urlencode([('source_id', item) for item in source_ids])
        return restore(ResultMetadata, self._http('GET', route))

    def status(self):
        return restore(HealthSnapshot, self._http('GET', '/v1/status'))

    def settings_get(self):
        return restore(SettingsSnapshot, self._http('GET', '/v1/settings'))

    def _ipc(self, method, arguments):
        from .ipc import request
        self._identity()
        data = request(self.descriptor, self._secret(), method, arguments)
        return data['value'] if method.startswith('desktop_') else data

    def settings_validate(self, candidate, expected_revision):
        return restore(SettingsValidation, self._ipc('settings_validate', dict(candidate=candidate, expected_revision=expected_revision)))

    def settings_apply(self, candidate, expected_revision):
        return restore(SettingsSnapshot, self._ipc('settings_apply', dict(candidate=candidate, expected_revision=expected_revision)))

    def start_catalog(self, source_ids=None):
        return tuple(self._http('POST', '/v1/jobs/catalog', {'source_ids': source_ids})['job_ids'])

    def cancel_job(self, job_id):
        return restore(JobSnapshot, self._http('POST', '/v1/jobs/' + public_uuid(job_id) + '/cancel', {}))

    def close(self):
        pass  # Caller views never stop the owner.
