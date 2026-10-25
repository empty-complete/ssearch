"""Independent catalog and retrieval API, with no GUI or mandatory third-party dependencies."""
from .contracts import (Cancellation, CatalogBackend, ContextProjection, Embedder, EmbeddingPreparer, ProgressCallback,
                        RetrievalBackend, Source)
from .models import (CatalogFile, DirectorySnapshot, DocumentContext, EmbeddingInput, EmbeddingModel,
                     EmbeddingWrite, FileMetadata, LexicalHit, ReconcileCounts,
                     PreparedEmbeddingInput, ProjectFilter, SearchResult, SearchScope, VectorHit)
from .service import SearchService

__version__ = "0.2.0"
__all__ = [
    "Cancellation", "DirectorySnapshot", "Embedder", "FileMetadata",
    "ProgressCallback", "SearchResult", "SearchService", "Source", "__version__",
    "CatalogBackend", "RetrievalBackend", "CatalogFile", "SearchScope", "ProjectFilter",
    "ReconcileCounts", "LexicalHit", "VectorHit", "EmbeddingInput",
    "EmbeddingWrite", "EmbeddingModel", "EmbeddingPreparer", "PreparedEmbeddingInput", "DocumentContext", "ContextProjection",
]
