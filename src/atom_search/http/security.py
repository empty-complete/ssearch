"""Server-side authority and endpoint identity. No caller JSON grants access."""
import hashlib
import hmac
import re
import uuid

from atom_search.content import message
from atom_search.service.contracts import ServiceError


def error(code, status=400, *, retryable=False):
    return ServiceError(code, message('service.' + code), retryable, status_code=status)


def public_uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, AttributeError, TypeError):
        raise error('invalid_request') from None


def scope_tuple(value):
    if value is None:
        return None
    if type(value) not in (tuple, list) or len(value) > 64:
        raise error('invalid_request')
    return tuple(public_uuid(item) for item in value)


def proof(secret, nonce, descriptor):
    material = '|'.join((nonce, descriptor['installation_id'], descriptor['generation'], descriptor['base_url']))
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()


def authorize_headers(owner, headers, peer):
    # Reject duplicate security headers instead of letting parsers choose one.
    for name in ('Host', 'Origin', 'Authorization', 'Content-Length', 'Transfer-Encoding',
                 'X-Atom-Installation', 'X-Atom-Generation', 'X-Atom-Credential', 'X-Atom-Nonce'):
        if len(headers.get_all(name, [])) > 1:
            raise error('invalid_request')
    if peer != '127.0.0.1' or headers.get('Host') != owner.base_url.removeprefix('http://'):
        raise error('access_denied', 403)
    origin = headers.get('Origin')
    if origin is not None and origin != owner.base_url:
        raise error('access_denied', 403)
    if (headers.get('X-Atom-Installation') != owner.installation_id
            or headers.get('X-Atom-Generation') != owner.generation):
        raise error('service_identity_mismatch', 409)


def bearer(owner, headers):
    value = headers.get('Authorization', '')
    if not value.startswith('Bearer '):
        raise error('authentication_required', 401)
    return owner.authenticate(value[7:])
