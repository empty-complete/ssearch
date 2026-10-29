"""Pinned public keys and an atomic, per-channel runtime high-water ledger."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from atom_search.runtime import _windows

from .cng import CngVerifier, validate_public_blob
from .contracts import UpdateError, VerifiedManifest


def strict_json(raw: bytes, *, code='update_manifest'):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value

    def invalid(_):
        raise ValueError()

    try:
        # decode explicitly: json.loads(bytes) also accepts UTF-16/32 and BOMs.
        return json.loads(raw.decode('utf-8'), object_pairs_hook=object_pairs,
                          parse_float=invalid, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise UpdateError(code) from None


def exact_object(value, fields, *, code='update_manifest'):
    if type(value) is not dict or set(value) != set(fields):
        raise UpdateError(code)
    return value


def integer(value, minimum=0, maximum=2**63 - 1, *, code='update_manifest'):
    if type(value) is not int or value < minimum:
        raise UpdateError(code)
    if value > maximum:
        raise UpdateError('update_limit' if code == 'update_manifest' else code)
    return value


def timestamp(value, *, code='update_manifest'):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise UpdateError(code)
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise UpdateError(code) from None


def decode_base64(value, *, code='update_manifest'):
    if type(value) is not str:
        raise UpdateError(code)
    try:
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode('ascii') != value:
            raise ValueError()
        return raw
    except (ValueError, binascii.Error):
        raise UpdateError(code) from None


@dataclass(frozen=True)
class _PinnedKey:
    public_key: bytes
    not_before: datetime
    not_after: datetime


class TrustStore:
    def __init__(self, keys: dict[str, _PinnedKey]):
        self._keys = dict(keys)
        self._verifier = CngVerifier()

    @classmethod
    def from_file(cls, path: Path) -> TrustStore:
        try:
            path = _windows.reject_reparse(Path(path))
            with path.open('rb') as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise UpdateError('update_key')
            document = exact_object(strict_json(raw, code='update_key'),
                                    ('format_version', 'keys'), code='update_key')
            if type(document['format_version']) is not int or document['format_version'] != 1:
                raise UpdateError('update_key')
            if type(document['keys']) is not list or len(document['keys']) > 64:
                raise UpdateError('update_key')
            keys = {}
            for item in document['keys']:
                exact_object(item, ('key_id', 'algorithm', 'public_key_b64', 'not_before', 'not_after'), code='update_key')
                identity = item['key_id']
                if (type(identity) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', identity)
                        or identity in keys or item['algorithm'] != 'ECDSA_P256_SHA256'):
                    raise UpdateError('update_key')
                public_key = decode_base64(item['public_key_b64'], code='update_key')
                validate_public_blob(public_key)
                before = timestamp(item['not_before'], code='update_key')
                after = timestamp(item['not_after'], code='update_key')
                if before >= after:
                    raise UpdateError('update_key')
                keys[identity] = _PinnedKey(public_key, before, after)
            return cls(keys)
        except (OSError, _windows.WindowsError):
            raise UpdateError('update_key') from None

    def verify(self, key_id: str, payload: bytes, signature: bytes, *, now: datetime | None = None) -> None:
        current = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(current, datetime) or current.utcoffset() is None:
            raise UpdateError('update_time')
        if type(key_id) is not str:
            raise UpdateError('update_key')
        key = self._keys.get(key_id)
        if key is None or not key.not_before <= current < key.not_after:
            raise UpdateError('update_key')
        self._verifier.verify(key.public_key, payload, signature)


class ReplayLedger:
    """Owner-private runtime state; missing initially is distinct from malformed.

    The same OS mutex serializes read/compare/replace across processes and ledger
    instances. A retry of exactly the same signed bytes never lowers high-water.
    """

    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    def accept(self, manifest: VerifiedManifest) -> None:
        if (not isinstance(manifest, VerifiedManifest) or manifest.channel not in ('stable', 'nightly')
                or type(manifest.sequence) is not int or not 1 <= manifest.sequence <= 2**63 - 1
                or type(manifest.payload_sha256) is not str
                or not re.fullmatch('[0-9a-f]{64}', manifest.payload_sha256)):
            raise UpdateError('update_replay')
        try:
            path = _windows.reject_reparse(self.path)
            # Guard the lexical input before resolving it: resolving first would
            # hide a junction. Expand existing directory/file aliases before
            # choosing the shared lock, including 8.3 names when available.
            parent = _windows.private_directory(path.parent)
            path = _windows.reject_reparse(parent / path.name).resolve()
            canonical_identity = str(path)
            # Python preserves an explicitly supplied Win32 extended prefix.
            # Fixed local storage was checked above; normalize the lock identity
            # while retaining the extended IO path for long-path support.
            if canonical_identity.startswith('\\\\?\\'):
                canonical_identity = canonical_identity[4:]
            identity = 'ssearch:update-replay:' + canonical_identity
            with _windows.mutex(identity):
                _windows.private_directory(path.parent)
                _windows.reject_reparse(path)
                try:
                    with path.open('rb') as stream:
                        raw = stream.read(8193)
                except FileNotFoundError:
                    state = {'format_version': 1, 'channels': {}}
                else:
                    _windows.assert_private(path)
                    if len(raw) > 8192:
                        raise UpdateError('update_replay')
                    state = self._validate_state(strict_json(raw, code='update_replay'))
                previous = state['channels'].get(manifest.channel)
                if previous is not None:
                    if (manifest.sequence < previous['sequence'] or
                            (manifest.sequence == previous['sequence'] and
                             manifest.payload_sha256 != previous['payload_sha256'])):
                        raise UpdateError('update_replay')
                    if manifest.sequence == previous['sequence']:
                        return
                state['channels'][manifest.channel] = {
                    'sequence': manifest.sequence, 'payload_sha256': manifest.payload_sha256}
                _windows.atomic_json(path, state)
        except (OSError, ValueError, TypeError, _windows.WindowsError):
            raise UpdateError('update_replay') from None

    @staticmethod
    def _validate_state(state):
        exact_object(state, ('format_version', 'channels'), code='update_replay')
        if type(state['format_version']) is not int or state['format_version'] != 1:
            raise UpdateError('update_replay')
        channels = state['channels']
        if type(channels) is not dict or set(channels) - {'stable', 'nightly'}:
            raise UpdateError('update_replay')
        for previous in channels.values():
            exact_object(previous, ('sequence', 'payload_sha256'), code='update_replay')
            integer(previous['sequence'], minimum=1, code='update_replay')
            digest = previous['payload_sha256']
            if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest):
                raise UpdateError('update_replay')
        return state
