from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectBinding:
    project_id: str
    source_id: int
    path_prefix: str
    confirmed: bool


@dataclass(frozen=True)
class ProjectDefinition:
    project_id: str
    canonical_name: str
    source_ids: tuple[int, ...]
    bindings: tuple[ProjectBinding, ...]


@dataclass(frozen=True)
class ProjectAlias:
    alias_id: str
    project_id: str
    source_ids: tuple[int, ...]
    variants: tuple[str, ...]
    confirmed: bool
    basis: str


@dataclass(frozen=True)
class AliasUse:
    alias_id: str
    project_id: str
    source_id: int
    matched_text: str
    canonical_name: str


@dataclass(frozen=True)
class AmbiguousProject:
    term: str
    project_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProjectResolution:
    original_query: str
    normalized_query: str
    residual_query: str
    selected_project_id: str | None
    preferred_project_ids: tuple[str, ...]
    expanded_terms: tuple[str, ...]
    applied_aliases: tuple[AliasUse, ...]
    ambiguities: tuple[AmbiguousProject, ...]


@dataclass(frozen=True)
class DocumentContext:
    file_id: int
    source_id: int
    name: str
    relative_path: str
    root: str
    source_label: str
    project_id: str | None
    project_name: str | None
    aliases: tuple[str, ...]
    context_revision: int


@dataclass(frozen=True)
class PreparedEmbeddingInput:
    text: str
    input_hash: str
    token_count: int


@dataclass(frozen=True)
class FileMetadata:
    relative_path: str
    name: str
    size: int
    modified_ns: int


@dataclass(frozen=True)
class DirectorySnapshot:
    relative_path: str
    files: tuple[FileMetadata, ...] = ()
    directories: tuple[str, ...] = ()
    protected_names: tuple[str, ...] = ()
    complete: bool = True
    errors: tuple[dict, ...] = ()
    skipped_links: int = 0


@dataclass(frozen=True)
class SearchScope:
    """Explicit allowed source IDs; an empty tuple always means no sources."""
    source_ids: tuple[int, ...]


@dataclass(frozen=True)
class ProjectFilter:
    """None is unrestricted, including unassigned files; () allows no files."""
    project_ids: tuple[str, ...] | None
    context_revision: int
    input_schema: str


@dataclass(frozen=True)
class CatalogFile:
    file_id: int
    source_id: int
    root: str
    relative_path: str
    name: str
    size: int
    modified_ns: int
    source_status: str
    revision: int


@dataclass(frozen=True)
class ReconcileCounts:
    added: int
    updated: int
    removed: int


@dataclass(frozen=True)
class LexicalHit:
    file: CatalogFile
    score: float
    project_id: str | None = None
    matched_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class VectorHit:
    file: CatalogFile
    similarity: float
    project_id: str | None = None


@dataclass(frozen=True)
class EmbeddingInput:
    file: CatalogFile
    text: str
    input_schema: str
    context_revision: int | None = None
    input_hash: str | None = None


@dataclass(frozen=True)
class EmbeddingWrite:
    item: EmbeddingInput
    vector: tuple[float, ...]
    input_hash: str


@dataclass(frozen=True)
class EmbeddingModel:
    """Verified-model provenance supplied by the owner, not verified by this DTO.

    manifest_digest is SHA-256 of UTF-8 json.dumps(manifest, sort_keys=True,
    separators=(',', ':'), ensure_ascii=False), including both ONNX and tokenizer
    asset hashes. Input schema versions never relabel existing stored vectors.
    """
    model_key: str
    dimension: int
    input_schema: str
    manifest_digest: str


@dataclass(frozen=True)
class SearchResult:
    file_id: int
    root: str
    relative_path: str
    name: str
    size: int
    modified_ns: int
    score: float
    stale: bool = False
    project_id: str | None = None
    matched_fields: tuple[str, ...] = ()
    match_kind: str | None = None

    @property
    def full_path(self) -> str:
        return str(Path(self.root).joinpath(*self.relative_path.split('/')))
