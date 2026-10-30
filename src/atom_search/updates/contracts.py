"""Immutable values admitted by the signed update boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


class UpdateError(RuntimeError):
    """Fixed public error codes; untrusted bytes and paths never enter messages."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UpdatePolicy:
    manifest_bytes: int = 262144
    artifact_count: int = 16
    artifact_bytes: int = 4294967296
    release_bytes: int = 8589934592
    clock_skew_seconds: int = 300
    inventory_bytes: int = 4194304
    inventory_entries: int = 20000
    path_codepoints: int = 512
    unpacked_release_bytes: int = 34359738368


@dataclass(frozen=True)
class UpdateVersions:
    app: str
    schema: int
    postgres: str
    extension: str
    desktop: str
    service: str
    service_api: str
    settings_schema: int


@dataclass(frozen=True)
class ArchiveInventoryRef:
    relative_path: str
    size: int
    sha256: str
    entry_count: int
    unpacked_bytes: int


@dataclass(frozen=True)
class ReleaseArtifact:
    id: str
    relative_path: str
    size: int
    sha256: str
    kind: Literal['app', 'schema', 'postgres_minor', 'extension', 'postgres_major']
    format: Literal['file', 'zip']
    inventory: ArchiveInventoryRef | None


@dataclass(frozen=True)
class ReleaseCompatibility:
    desktop: str
    service: str
    service_api: str
    schema_min: int
    schema_max: int
    settings_schema_min: int
    settings_schema_max: int


@dataclass(frozen=True)
class ExecutableDigest:
    path: str
    sha256: str


@dataclass(frozen=True)
class ReleaseExecutables:
    desktop: ExecutableDigest
    service: ExecutableDigest


@dataclass(frozen=True)
class VerifiedManifest:
    release_id: str
    sequence: int
    channel: Literal['stable', 'nightly']
    issued_at: datetime
    expires_at: datetime
    versions: UpdateVersions
    artifacts: tuple[ReleaseArtifact, ...]
    payload_sha256: str
    source_commit: str
    compatibility: ReleaseCompatibility
    executables: ReleaseExecutables


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class VerifiedArchiveInventory:
    entries: tuple[ArchiveEntry, ...]
    unpacked_bytes: int
    sha256: str


@dataclass(frozen=True)
class StagedRelease:
    manifest: VerifiedManifest
    directory: Path
    files: tuple[Path, ...]
