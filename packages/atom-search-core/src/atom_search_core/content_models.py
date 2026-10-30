"""Stdlib content data; no parser, model session, storage or service authority."""
from dataclasses import dataclass

from .models import PreparedEmbeddingInput


@dataclass(frozen=True)
class ContentWorkToken:
    file_id: int
    source_id: int
    relative_path: str
    file_revision: int
    size: int
    modified_ns: int
    catalog_generation: int
    content_generation: int
    profile_generation: int
    context_revision: int
    extractor_version: str
    chunker_version: str
    profile: str


@dataclass(frozen=True)
class ExtractionSegment:
    locator: str
    text: str


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    segments: tuple[ExtractionSegment, ...]
    text_hash: str | None
    extractor_version: str
    warnings: tuple[str, ...]
    error_code: str | None
    bytes_read: int
    truncated: bool


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    locator: str
    text: str
    text_hash: str
    prepared_input: PreparedEmbeddingInput | None


@dataclass(frozen=True)
class ChunkWrite:
    chunk: ChunkDraft
    model_key: str | None
    vector: tuple[float, ...] | None


@dataclass(frozen=True)
class ContentPublishResult:
    accepted: bool
    chunks_written: int
    generation: int
    reason: str | None
