"""Attach an agent read view to an existing, authenticated local service.

Credential issuance and owner lifecycle belong to the service host. This module
accepts an explicit protected reference; it never discovers owner credentials.
"""
from pathlib import Path
import re

from atom_search.http.security import error
from atom_search.service.client import ServiceClient
from atom_search.service.contracts import ServiceError

from .read_api import AgentReadAPI


_CONNECT_ERRORS = {
    'authentication_required': 401,
    'access_denied': 403,
    'service_identity_mismatch': 409,
    'service_unavailable': 503,
    'service_stopping': 503,
    'request_timeout': 504,
    'too_many_requests': 429,
}


def _connect_read_client(endpoint_file: Path, credential_ref: str) -> ServiceClient:
    """Verify the endpoint, process identity, nonce proof and explicit credential.

    ServiceClient permits an owner fallback for desktop callers. Reject missing
    and noncanonical references *before* its constructor can select that path.
    A canonical but unresolved reference must fail the authenticated status read.
    """
    if (type(credential_ref) is not str
            or re.fullmatch(r'dpapi-v1-[0-9a-f]{64}', credential_ref) is None):
        raise error('authentication_required', 401) from None

    try:
        client = ServiceClient(endpoint_file, credential_ref)
        # P2 revalidates private endpoint/state/process identity before resolving
        # the credential. This HTTP read also verifies the fresh nonce proof.
        client.status()
        return client
    except ServiceError as failure:
        code = (failure.code if type(failure.code) is str and failure.code in _CONNECT_ERRORS
                else 'service_unavailable')
        raise error(code, _CONNECT_ERRORS[code], retryable=failure.retryable is True) from None
    except Exception:
        raise error('service_unavailable', 503, retryable=True) from None


def connect_read_api(endpoint_file: Path, credential_ref: str) -> AgentReadAPI:
    """Attach a read view using an explicit protected credential reference."""
    return AgentReadAPI(_connect_read_client(endpoint_file, credential_ref))
