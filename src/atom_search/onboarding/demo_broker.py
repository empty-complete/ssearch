"""Named in-process demo calls for a trusted broker composition.

This adapter implements no wire dispatch and accepts no renderer-selected
profile, path, build capability, work client, owner identity or credentials.
A transport/main owner must explicitly bind these methods in a later slice.
"""
from dataclasses import dataclass

from atom_search.service.contracts import ResultMetadata, SearchRequest, SearchResponse
from .session_manager import DemoSessionInfo, DemoSessionManager


@dataclass(frozen=True)
class DemoCloseInfo:
    """Cleanup counts; private staging paths remain inside the trusted host."""
    session_id: str
    cleanup_pending: bool
    removed_count: int
    deferred_count: int
    preserved_count: int


class DemoBrokerAdapter:
    def __init__(self, manager: DemoSessionManager):
        if not isinstance(manager, DemoSessionManager):
            raise TypeError('host_demo_manager_required')
        self._manager = manager

    def open_demo(self) -> DemoSessionInfo:
        return self._manager.open()

    def search_demo(self, session_id: str, search_request: SearchRequest) -> SearchResponse:
        return self._manager.search(session_id, search_request)

    def fetch_demo(self, session_id: str, result_id: str, *,
                   source_ids: tuple[str, ...] | None = None) -> ResultMetadata:
        return self._manager.fetch(session_id, result_id, source_ids=source_ids)

    def close_demo(self, session_id: str) -> DemoCloseInfo:
        result = self._manager.close(session_id)
        return DemoCloseInfo(session_id, bool(result.deferred), len(result.removed),
                             len(result.deferred), len(result.preserved))
