"""Transport independent DTOs shared by the desktop and future service clients.

Only the standard library is imported here. A settings projection is not an
owner candidate, and a mutable_fields entry grants no authority on its own.
"""
from __future__ import annotations

from dataclasses import dataclass


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 details: dict | None = None, *, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.details, self.status_code = details or {}, status_code


@dataclass(frozen=True)
class SettingsSnapshot:
    schema_version: int
    revision: int
    settings: dict
    mutable_fields: tuple[str, ...]


@dataclass(frozen=True)
class SettingChange:
    path: str
    before: object
    after: object


@dataclass(frozen=True)
class FieldError:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class SettingsValidation:
    valid: bool
    expected_revision: int
    normalized: dict | None
    diff: tuple[SettingChange, ...]
    warnings: tuple[str, ...]
    errors: tuple[FieldError, ...]


@dataclass(frozen=True)
class AccessContext:
    subject_id: str
    capabilities: frozenset[str]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class SearchRequest:
    query: str
    source_ids: tuple[str, ...] | None = None
    limit: int = 50
    mode: str = 'hybrid'
    project_id: str | None = None


@dataclass(frozen=True)
class ResultMetadata:
    result_id: str
    source_id: str
    name: str
    relative_path: str
    full_path: str
    size: int
    modified_ns: int
    stale: bool
    project_id: str | None
    matched_fields: tuple[str, ...]
    score: float | None
    match_kind: str


@dataclass(frozen=True)
class QueryInterpretation:
    original_query: str
    normalized_query: str
    project_id: str | None
    expanded_terms: tuple[str, ...]


@dataclass(frozen=True)
class AppliedAlias:
    alias_id: str
    project_id: str
    source_id: str
    matched_text: str
    canonical_name: str


@dataclass(frozen=True)
class ProjectOption:
    project_id: str
    canonical_name: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class QueryAmbiguity:
    term: str
    options: tuple[ProjectOption, ...]


@dataclass(frozen=True)
class SearchResponse:
    results: tuple[ResultMetadata, ...]
    query_interpretation: QueryInterpretation
    applied_aliases: tuple[AppliedAlias, ...]
    ambiguities: tuple[QueryAmbiguity, ...]
    catalog_generation: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    source_id: str | None
    state: str
    phase: str
    started_at: str | None
    last_progress_at: str | None
    discovered: int
    processed: int
    succeeded: int
    skipped: int
    errors: int
    total: int | None
    error_code: str | None


@dataclass(frozen=True)
class ConfigVersion:
    schema_version: int
    revision: int


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    state: str
    checked_at: str | None
    last_success_at: str | None
    stale: bool
    error_code: str | None
    catalogued_files: int | None
    metadata_embedded_files: int | None
    catalog_counts_checked_at: str | None
    catalog_counts_stale: bool


@dataclass(frozen=True)
class ModelHealth:
    state: str
    model_id: str | None
    checked_at: str | None
    stale: bool
    lexical_available: bool


@dataclass(frozen=True)
class DatabaseHealth:
    engine: str
    version: str | None
    extension_version: str | None
    runtime_state: str
    checked_at: str | None
    stale: bool
    error_code: str | None


@dataclass(frozen=True)
class StorageHealth:
    physical_owned_bytes: int | None
    pgdata_bytes: int | None
    wal_bytes: int | None
    temp_bytes: int | None
    cache_bytes: int | None
    outbox_bytes: int | None
    backup_bytes: int | None
    staging_bytes: int | None
    database_logical_bytes: int | None
    quota_bytes: int | None
    free_bytes: int | None
    reserved_bytes: int | None
    checked_at: str | None
    stale: bool
    maintenance_required: bool


@dataclass(frozen=True)
class OutboxHealth:
    queued: int
    sent: int
    last_error: str | None
    repair_blocked: bool


@dataclass(frozen=True)
class HealthComponents:
    sources: tuple[SourceHealth, ...]
    model: ModelHealth
    database: DatabaseHealth
    storage: StorageHealth
    jobs: tuple[JobSnapshot, ...]
    outbox: OutboxHealth


@dataclass(frozen=True)
class HealthSnapshot:
    liveness: str
    readiness: str
    app_version: str
    service_version: str
    api_version: str
    observed_at: str
    config_version: ConfigVersion
    index_generation: int
    components: HealthComponents
    capabilities: tuple[str, ...]
