"""Narrow manifest/inventory/artifact read interface. No activation authority."""
from __future__ import annotations

from datetime import datetime
from typing import BinaryIO, Protocol

from ..contracts import UpdateVersions, VerifiedManifest
from ..manifest import ManifestVerifier, POLICY


class ReleaseTransport(Protocol):
    def read_manifest(self, *, max_bytes: int) -> bytes: ...
    def read_inventory(self, manifest: VerifiedManifest, artifact_id: str, *, max_bytes: int) -> bytes: ...
    def open_artifact(self, manifest: VerifiedManifest, artifact_id: str) -> BinaryIO: ...


def read_verified_manifest(transport: ReleaseTransport, verifier: ManifestVerifier, *, channel: str,
                           installed: UpdateVersions, now: datetime) -> VerifiedManifest:
    envelope = transport.read_manifest(max_bytes=POLICY.manifest_bytes)
    return verifier.verify(envelope, channel=channel, installed=installed, now=now)
