"""Bounded signed admission, with replay committed before a manifest is returned."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import re

from .contracts import (ArchiveInventoryRef, ExecutableDigest, ReleaseArtifact,
                        ReleaseCompatibility, ReleaseExecutables, UpdateError,
                        UpdatePolicy, UpdateVersions, VerifiedManifest)
from .trust import ReplayLedger, TrustStore, decode_base64, exact_object, integer, strict_json, timestamp

POLICY = UpdatePolicy()
KINDS = {'app', 'schema', 'postgres_minor', 'extension', 'postgres_major'}
VERSION_FIELDS = {'app', 'desktop', 'service', 'service_api', 'schema', 'settings_schema', 'postgres', 'extension'}
PAIR_FIELDS = {'desktop', 'service', 'service_api', 'schema_min', 'schema_max', 'settings_schema_min', 'settings_schema_max'}


def _text(value, pattern, *, maximum=128):
    if type(value) is not str or len(value) > maximum or not re.fullmatch(pattern, value):
        raise UpdateError('update_manifest')
    return value


def _digest(value):
    return _text(value, '[0-9a-f]{64}', maximum=64)


def _version(value):
    _text(value, r'(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){1,3}', maximum=40)
    parts = tuple(int(part) for part in value.split('.'))
    return parts + (0,) * (4 - len(parts))


def _path(value):
    if type(value) is not str or not value or len(value) > POLICY.path_codepoints:
        raise UpdateError('update_limit' if type(value) is str and len(value) > POLICY.path_codepoints else 'update_manifest')
    if any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF or character in '\\<>:"|?*' for character in value):
        raise UpdateError('update_manifest')
    parts = value.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith(('.', ' '))
                or re.fullmatch(r'(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])',
                                part.split('.')[0], flags=re.IGNORECASE)):
            raise UpdateError('update_manifest')
    return value


def _claim_path(value, occupied):
    path = _path(value)
    folded = path.casefold()
    if any(folded == other or folded.startswith(other + '/') or other.startswith(folded + '/') for other in occupied):
        raise UpdateError('update_manifest')
    occupied.add(folded)
    return path


def _versions(value):
    exact_object(value, VERSION_FIELDS)
    for field in ('app', 'desktop', 'service', 'postgres', 'extension'):
        _version(value[field])
    _text(value['service_api'], '[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}', maximum=64)
    integer(value['schema'])
    integer(value['settings_schema'])
    return UpdateVersions(**value)


def _artifacts(items):
    if type(items) is not list or not items:
        raise UpdateError('update_manifest')
    if len(items) > POLICY.artifact_count:
        raise UpdateError('update_limit')
    artifacts, identities, paths = [], set(), set()
    total, unpacked = 0, 0
    app_count = 0
    for item in items:
        exact_object(item, ('id', 'relative_path', 'size', 'sha256', 'kind', 'format', 'inventory'))
        identity = _text(item['id'], '[a-z0-9][a-z0-9._-]{0,127}')
        if identity in identities or type(item['kind']) is not str or item['kind'] not in KINDS:
            raise UpdateError('update_manifest')
        identities.add(identity)
        path = _claim_path(item['relative_path'], paths)
        size = integer(item['size'], maximum=POLICY.artifact_bytes)
        digest = _digest(item['sha256'])
        total += size
        if total > POLICY.release_bytes:
            raise UpdateError('update_limit')
        inventory = None
        if item['format'] == 'zip':
            reference = exact_object(item['inventory'], ('relative_path', 'size', 'sha256', 'entry_count', 'unpacked_bytes'))
            inventory = ArchiveInventoryRef(
                relative_path=_claim_path(reference['relative_path'], paths),
                size=integer(reference['size'], maximum=POLICY.inventory_bytes),
                sha256=_digest(reference['sha256']),
                entry_count=integer(reference['entry_count'], maximum=POLICY.inventory_entries),
                unpacked_bytes=integer(reference['unpacked_bytes'], maximum=POLICY.unpacked_release_bytes))
            unpacked += inventory.unpacked_bytes
        elif item['format'] == 'file' and item['inventory'] is None:
            unpacked += size
        else:
            raise UpdateError('update_manifest')
        if item['kind'] == 'app':
            app_count += 1
            if item['format'] != 'zip' or app_count > 1:
                raise UpdateError('update_manifest')
        if unpacked > POLICY.unpacked_release_bytes:
            raise UpdateError('update_limit')
        artifacts.append(ReleaseArtifact(identity, path, size, digest, item['kind'], item['format'], inventory))
    return tuple(artifacts)


class ManifestVerifier:
    def __init__(self, trust: TrustStore, ledger: ReplayLedger):
        if not isinstance(trust, TrustStore) or not isinstance(ledger, ReplayLedger):
            raise TypeError('trusted store and persistent replay ledger required')
        self._trust, self._ledger = trust, ledger

    def verify(self, envelope: bytes, *, channel: str, installed: UpdateVersions, now: datetime) -> VerifiedManifest:
        if type(envelope) is not bytes:
            raise UpdateError('update_manifest')
        if len(envelope) > POLICY.manifest_bytes:
            raise UpdateError('update_limit')
        if type(channel) is not str or channel not in ('stable', 'nightly'):
            raise UpdateError('update_channel')
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise UpdateError('update_time')
        if not isinstance(installed, UpdateVersions):
            raise UpdateError('update_compatibility')
        current = _versions(vars(installed))
        wrapper = exact_object(strict_json(envelope), ('key_id', 'payload_b64', 'signature_b64'))
        payload = decode_base64(wrapper['payload_b64'])
        signature = decode_base64(wrapper['signature_b64'])
        # Only the envelope has been parsed. Payload policy never runs before CNG.
        self._trust.verify(wrapper['key_id'], payload, signature, now=now)
        data = exact_object(strict_json(payload), ('format_version', 'release_id', 'source_commit', 'sequence',
                                                  'channel', 'issued_at', 'expires_at', 'versions', 'artifacts',
                                                  'compatibility', 'executables'))
        if type(data['format_version']) is not int or data['format_version'] != 1:
            raise UpdateError('update_manifest')
        identity = _text(data['release_id'], '[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}')
        source = _text(data['source_commit'], '[0-9a-f]{40}', maximum=40)
        sequence = integer(data['sequence'], minimum=1)
        if data['channel'] != channel:
            raise UpdateError('update_channel')
        issued, expires = timestamp(data['issued_at']), timestamp(data['expires_at'])
        if expires <= now:
            raise UpdateError('update_expired')
        if issued >= expires or issued > now + timedelta(seconds=POLICY.clock_skew_seconds):
            raise UpdateError('update_time')
        target = _versions(data['versions'])
        artifacts = _artifacts(data['artifacts'])
        pair = exact_object(data['compatibility'], PAIR_FIELDS)
        for field in ('schema_min', 'schema_max', 'settings_schema_min', 'settings_schema_max'):
            integer(pair[field])
        if (any(pair[field] != getattr(target, field) for field in ('desktop', 'service', 'service_api'))
                or target.service_api != current.service_api
                or not pair['schema_min'] <= current.schema <= pair['schema_max']
                or not pair['settings_schema_min'] <= current.settings_schema <= pair['settings_schema_max']):
            raise UpdateError('update_compatibility')
        for field in ('app', 'desktop', 'service', 'postgres', 'extension'):
            if _version(getattr(target, field)) < _version(getattr(current, field)):
                raise UpdateError('update_downgrade')
        if target.schema < current.schema or target.settings_schema < current.settings_schema:
            raise UpdateError('update_downgrade')
        executables = exact_object(data['executables'], ('desktop', 'service'))
        executable_paths, executable_values = set(), {}
        for component in ('desktop', 'service'):
            value = exact_object(executables[component], ('path', 'sha256'))
            path = _claim_path(value['path'], executable_paths)
            if not path.casefold().endswith('.exe'):
                raise UpdateError('update_manifest')
            executable_values[component] = ExecutableDigest(path, _digest(value['sha256']))
        result = VerifiedManifest(identity, sequence, channel, issued, expires, target, artifacts,
                                  hashlib.sha256(payload).hexdigest(), source,
                                  ReleaseCompatibility(**pair), ReleaseExecutables(**executable_values))
        # This is part of admission, not an optional future caller responsibility.
        self._ledger.accept(result)
        return result
