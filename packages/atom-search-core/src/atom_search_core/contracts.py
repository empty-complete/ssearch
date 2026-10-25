"""Public integration contracts; all annotations use the standard library."""
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from .models import (CatalogFile, DirectorySnapshot, DocumentContext, EmbeddingInput, EmbeddingWrite, LexicalHit,
                     PreparedEmbeddingInput, ProjectAlias, ProjectDefinition, ProjectFilter, ProjectResolution,
                     ReconcileCounts, SearchScope, VectorHit)


class Cancellation(Protocol):
    """A cancellation flag, for example threading.Event, owned by the client."""

    def is_set(self) -> bool: ...


ProgressCallback = Callable[[dict[str, Any]], None]


class Source(Protocol):
    """Yield directory-scoped metadata snapshots without using a GUI."""

    def snapshots(self, root: str, cancel: Cancellation, *,
                  excluded_roots: tuple[Path, ...] = ()) -> Iterable[DirectorySnapshot]: ...


class Embedder(Protocol):
    """Provide model identity and array-like float vectors in text order.

    An encode batch has shape (len(texts), dimension). Matrix conversion and
    validation belong to the optional semantic layer, so no numpy types need
    to be imported merely to use this public contract.
    """

    model_id: str
    dimension: int

    def encode(self, texts: list[str], *, kind: Literal["query", "passage"] = "passage") -> Any: ...


class EmbeddingPreparer(Protocol):
    """Bound the exact unprefixed payload, counting prefix and model specials."""

    def prepare_input(self, text: str, *, kind: str,
                      max_tokens: int = 512) -> PreparedEmbeddingInput: ...


class ContextProjection(Protocol):
    """Persist current metadata configuration and CAS-publish bounded batches."""

    def sync(self, projects: Sequence[ProjectDefinition], aliases: Sequence[ProjectAlias], *,
             expected_settings_revision: int, source_labels: dict[int, str]) -> int: ...
    def configuration(self) -> tuple[int, tuple[tuple[ProjectDefinition, ...], tuple[ProjectAlias, ...], dict[int, str]]]: ...
    def pending_context(self, limit: int, *, scope: SearchScope | None = None,
                        require_prepared: bool = False) -> list[CatalogFile]: ...
    def publish_batch(self, records: Sequence[tuple[DocumentContext, int, PreparedEmbeddingInput | None]], *,
                      cancel: Cancellation | None = None, publish_if: Callable[[], bool] | None = None) -> int: ...


class CatalogBackend(Protocol):
    """Metadata catalog and embedding publication, independent of storage."""

    def begin_root(self, root: str) -> int: ...
    def finish_root(self, root_id: int, status: str, message: str = '') -> None: ...
    def reconcile(self, root_id: int, snapshot: DirectorySnapshot) -> ReconcileCounts: ...
    def resolve_scope(self, root: str | None) -> SearchScope: ...
    def get_file(self, file_id: int, *, scope: SearchScope) -> CatalogFile | None: ...
    def status(self, *, scope: SearchScope) -> dict[str, Any]: ...
    def pending_embeddings(self, model_key: str, dimension: int, limit: int, *,
                           scope: SearchScope | None = None) -> list[EmbeddingInput]: ...
    def save_embeddings(self, records: Sequence[EmbeddingWrite], model_key: str) -> int: ...
    def close(self) -> None: ...


class RetrievalBackend(Protocol):
    """Scoped hits ordered by score/similarity DESC, then file_id ASC."""

    def lexical_candidates(self, query: str, *, scope: SearchScope, limit: int,
                           project_filter: ProjectFilter | None = None) -> list[LexicalHit]: ...
    def literal_candidates(self, query: str, *, scope: SearchScope, limit: int,
                           project_filter: ProjectFilter | None = None) -> list[LexicalHit]: ...
    def project_candidates(self, resolution: ProjectResolution, *, scope: SearchScope,
                           context_revision: int, limit: int, mode: str = 'lexical') -> list[LexicalHit]: ...
    def vector_candidates(self, vector: Sequence[float], *, model_key: str,
                          scope: SearchScope, limit: int,
                          project_filter: ProjectFilter | None = None) -> list[VectorHit]: ...
