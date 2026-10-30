"""Content boundary DTOs. A grant is an observation, never a credential."""
from dataclasses import dataclass
from typing import Protocol

from atom_search_core.content_models import ContentWorkToken


@dataclass(frozen=True)
class ContentAccessPolicy:
    mode: str = 'require_current_file_read'
    allow_cached_unreachable: bool = False
    policy_revision: int = 1


@dataclass(frozen=True)
class ContentFileRef:
    source_id: str
    result_id: str
    file_id: int
    file_revision: int
    relative_path: str


@dataclass(frozen=True)
class FileReadResult:
    source_id: str
    result_id: str
    file_revision: int
    status: str


class BoundedFileChecker(Protocol):
    """Trusted host authority, NOT a plain blocking open wrapped in a timer.

    The host must enforce the absolute monotonic deadline in an isolated bounded
    worker, check the concrete current-user file handle and return the matching
    revision. Root readability grants nothing. 'offline'/'unreachable' must mean
    transport unavailability, never a generic I/O error or an access denial.
    This source seam implements neither that process nor native ACL proof.
    """

    def check(self, file: ContentFileRef, *, deadline: float) -> FileReadResult: ...


@dataclass(frozen=True)
class ContentGrant:
    source_id: str
    result_id: str
    file_revision: int
    policy_revision: int
    operation: str
    checked_at: str
    cached_allowed: bool


@dataclass(frozen=True)
class ContentScheduleState:
    """Authoritative durable inputs supplied by the existing service owner.

    Fingerprints concern extraction, not derived cache availability. Success
    must survive eviction/profile/model/parser changes. Attempts and not_before
    belong to the same attempted fingerprint; retry_not_before is expressed in
    the injected monotonic clock domain (the host rebases persisted deadlines
    on restart from persisted UTC backoff). This DTO is not a persistence implementation.
    """
    profile: str
    content_generation: int
    profile_generation: int
    context_revision: int
    extractor_version: str
    chunker_version: str
    successful_fingerprint: tuple[int, int] | None
    attempted_fingerprint: tuple[int, int] | None = None
    attempts: int = 0
    retry_not_before: float = 0.0
