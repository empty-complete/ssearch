"""Host-only onboarding contracts using the existing public search DTOs."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from atom_search.service.contracts import ResultMetadata, SearchRequest, SearchResponse


class ReadonlySearchProvider(Protocol):
    def search(self, request: SearchRequest) -> SearchResponse: ...
    def fetch(self, result_id: str, *, source_ids: tuple[str, ...] | None = None) -> ResultMetadata: ...


@dataclass(frozen=True)
class DemoSession:
    session_id: str
    version: int
    root: Path
    provider: ReadonlySearchProvider
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class CleanupResult:
    removed: tuple[Path, ...] = ()
    deferred: tuple[Path, ...] = ()
    preserved: tuple[Path, ...] = ()
