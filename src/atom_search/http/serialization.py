"""Bounded, unambiguous JSON envelopes and dataclass reconstruction."""
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
import json
import types
from typing import get_args, get_origin, get_type_hints
import uuid

from atom_search.service.contracts import ServiceError

MAX_BODY = 65536


def encode(value):
    def default(item):
        if isinstance(item, datetime):
            return item.astimezone(timezone.utc).isoformat()
        raise TypeError('not_json_serializable')
    return json.dumps(asdict(value) if is_dataclass(value) else value,
                      ensure_ascii=False, allow_nan=False, separators=(',', ':'), default=default).encode('utf-8')


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def envelope(data=None, error=None, *, request_id=None):
    try:
        if type(request_id) is not str or str(uuid.UUID(request_id)) != request_id:
            request_id = None
    except ValueError:
        request_id = None
    return dict(schema_version=1, request_id=request_id or str(uuid.uuid4()), data=data,
                error=None if error is None else dict(code=error.code, message=error.message,
                    retryable=bool(error.retryable), details=error.details))


def unpack(payload, status=200):
    if (type(payload) is not dict or set(payload) != {'schema_version', 'request_id', 'data', 'error'}
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or type(payload['request_id']) is not str):
        raise ValueError('invalid_service_envelope')
    try:
        if str(uuid.UUID(payload['request_id'])) != payload['request_id']:
            raise ValueError()
    except ValueError:
        raise ValueError('invalid_service_envelope') from None
    error = payload['error']
    if error is not None:
        if (type(error) is not dict or set(error) != {'code', 'message', 'retryable', 'details'}
                or type(error['retryable']) is not bool or type(error['details']) is not dict):
            raise ValueError('invalid_service_error')
        raise ServiceError(error['code'], error['message'], error['retryable'], error['details'], status_code=status)
    if not 200 <= status < 300:
        raise ValueError('invalid_service_status')
    return payload['data']


def restore(annotation, value):
    origin = get_origin(annotation)
    if origin is tuple:
        return tuple(restore(get_args(annotation)[0], item) for item in value)
    if origin in (types.UnionType,):
        if value is None:
            return None
        return restore(next(item for item in get_args(annotation) if item is not type(None)), value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return annotation(**{field.name: restore(hints[field.name], value[field.name]) for field in fields(annotation)})
    return value
