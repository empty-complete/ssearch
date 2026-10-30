"""Owner-supplied authority and workspace values. These are not issue payloads."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class ContributionError(ValueError):
    """A bounded failure code; never includes credentials or Git output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ContributionPolicy:
    max_bundle_bytes: int
    max_queued_proposals: int
    max_queued_bytes: int
    sent_retention_days: int
    allowed_target_branches: tuple[str, ...]


@dataclass(frozen=True)
class ContributionConfig:
    repository_id: str
    remote: str | None
    base_sha: str | None
    developer_credential_ref: str | None
    proposal_destination: str | None
    policy: ContributionPolicy | None
    workspace_root: Path | None


@dataclass(frozen=True)
class ContributionAuthorization:
    task_id: str
    incident_id: str
    repository_id: str
    granted_by: str
    expires_at: datetime
    remote: str
    base_sha: str
    workspace_root: Path
    branch: str
    developer_credential_ref: str


@dataclass(frozen=True)
class ContributionWorkspace:
    path: Path
    incident_id: str
    branch: str
    base_sha: str


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    command_id: str
    source_sha: str
    exit_code: int | None
    report_sha256: str | None
    status: str
    limitation: str | None


@dataclass(frozen=True)
class ReviewEvidence:
    """Independent review supplied by the trusted developer host, not an issue."""

    reviewer_id: str
    reviewed_tree_sha256: str
    blocking_findings: int


@dataclass(frozen=True)
class BuildEvidence:
    source_sha: str
    clean: bool
    manifest_path: Path
    manifest_sha256: str
    frozen_checks: tuple[CheckEvidence, ...]
