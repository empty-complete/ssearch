"""Fail-closed integrity checks. This module never executes runtime binaries.

The tracked manifest is the trust anchor; hashes detect changed prepared assets,
not a hostile replacement of both the source manifest and its payload.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct


class RuntimeValidationError(RuntimeError):
    """Prepared runtime is absent, malformed, or no longer matches its manifest."""


@dataclass(frozen=True)
class RuntimeAsset:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RuntimeManifest:
    postgres_version: str
    vector_version: str
    architecture: str
    files: tuple[RuntimeAsset, ...]
    licenses: tuple[str, ...]
    directory: Path | None = field(default=None, repr=False, compare=False)


REQUIRED = frozenset((
    'bin/postgres.exe', 'bin/initdb.exe', 'bin/pg_ctl.exe', 'bin/psql.exe',
    'bin/pg_dump.exe', 'bin/pg_restore.exe',
    'bin/libpq.dll', 'lib/vector.dll', 'share/extension/vector.control',
    'share/extension/vector--0.8.7.sql', 'licenses/postgresql.txt',
    'licenses/pgvector.txt', 'licenses/third-party.txt',
    # Exact app-local DLL closure of the PostgreSQL18.6/pgvector0.8.7 roots,
    # established from their import and delay-import tables during preparation.
    'bin/icudt77.dll', 'bin/icuin77.dll', 'bin/icuuc77.dll',
    'bin/libcrypto-3-x64.dll', 'bin/libiconv-2.dll', 'bin/libintl-9.dll',
    'bin/liblz4.dll', 'bin/libssl-3-x64.dll', 'bin/libwinpthread-1.dll',
    'bin/libxml2.dll', 'bin/libzstd.dll', 'bin/msvcp140.dll',
    'bin/vcruntime140.dll', 'bin/vcruntime140_1.dll', 'bin/zlib1.dll',
    'licenses/MSVC-runtime-license.html', 'licenses/MSVC-redistribution.html',
))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _safe_path(directory: Path, value: str) -> Path:
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/'))
            or any(part.endswith((' ', '.')) for part in value.split('/'))
            or any(ord(c) < 32 for c in value)):
        raise RuntimeValidationError(f'Unsafe runtime relative path: {value!r}')
    path = directory.joinpath(*PurePosixPath(value).parts)
    if not path.resolve().is_relative_to(directory):
        raise RuntimeValidationError(f'Runtime path escapes directory: {value}')
    # Reject links/junctions even when they currently target an internal path.
    for part in (path, *path.parents):
        if part == directory:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise RuntimeValidationError(f'Runtime link is forbidden: {value}')
    return path


def load_runtime_manifest(runtime_dir: Path) -> RuntimeManifest:
    """Validate metadata and every listed size/hash before returning any path."""
    directory = Path(runtime_dir).resolve()
    try:
        data = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if (data['schema_version'] != 1 or data['postgresql_version'] != '18.6'
                or data['pgvector_version'] != '0.8.7' or data['architecture'] != 'x64'):
            raise ValueError('Expected PostgreSQL 18.6 / pgvector 0.8.7 x64')
        files = data['files']
        if not isinstance(files, list) or not files:
            raise ValueError('Expected nonempty file list')
        records = []
        seen = set()
        for entry in files:
            name, size, digest = entry['path'], entry['size'], entry['sha256']
            path = _safe_path(directory, name)
            if name.casefold() in seen:
                raise ValueError('Duplicate runtime path')
            seen.add(name.casefold())
            if (type(size) is not int or size < 0 or not isinstance(digest, str)
                    or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('Expected nonnegative size and lowercase SHA-256')
            if not path.is_file() or path.stat().st_size != size:
                raise RuntimeValidationError(f'Runtime file missing or size changed: {name}')
            if sha256(path) != digest:
                raise RuntimeValidationError(f'Runtime SHA-256 mismatch: {name}')
            records.append(RuntimeAsset(name, size, digest))
        licenses = tuple(sorted(record.path for record in records
                                if record.path.startswith('licenses/')))
        if not licenses or any(not record.size for record in records if record.path in licenses):
            raise RuntimeValidationError('Empty or missing runtime licenses')
        return RuntimeManifest(postgres_version=data['postgresql_version'],
                               vector_version=data['pgvector_version'],
                               architecture=data['architecture'], files=tuple(records),
                               licenses=licenses, directory=directory)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeValidationError(f'Invalid runtime manifest: {directory}: {error}') from error


def verify_runtime(runtime_dir: Path) -> RuntimeManifest:
    """Require extension, executables, licenses, exact inventory and x64 PE files."""
    manifest = load_runtime_manifest(runtime_dir)
    listed = {item.path for item in manifest.files}
    missing = REQUIRED - listed
    if missing:
        raise RuntimeValidationError('Required runtime files missing: ' + ', '.join(sorted(missing)))
    if any(item.path in REQUIRED and not item.size for item in manifest.files):
        raise RuntimeValidationError('Empty required runtime file')
    for path in manifest.directory.rglob('*'):
        name = path.relative_to(manifest.directory).as_posix()
        _safe_path(manifest.directory, name)
        if path.is_file() and name != 'manifest.json' and name not in listed:
            raise RuntimeValidationError(f'Unlisted runtime file: {name}')
    for item in manifest.files:
        if Path(item.path).suffix.lower() in ('.exe', '.dll'):
            try:
                with (manifest.directory / item.path).open('rb') as stream:
                    header = stream.read(64)
                    if header[:2] != b'MZ' or len(header) != 64:
                        raise ValueError('Missing DOS header')
                    stream.seek(struct.unpack_from('<I', header, 60)[0])
                    pe = stream.read(6)
                    if pe[:4] != b'PE\0\0' or pe[4:] != b'\x64\x86':
                        raise ValueError('Expected x64 PE machine')
            except (OSError, ValueError, struct.error) as error:
                raise RuntimeValidationError(f'Invalid x64 PE: {item.path}') from error
    return manifest
