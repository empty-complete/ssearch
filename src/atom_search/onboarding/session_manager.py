"""Host-only ownership and admission for explicit offline demo sessions.

The trusted host binds the same profile used by its future service owner. The
fixed staging subtree stays inside that profile; registration must complete
before materialization. Actual host startup/owner handoff is a separate seam.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Callable, Literal

from atom_search_core.source import canonical_root
from atom_search.service.contracts import ResultMetadata, SearchRequest, SearchResponse, ServiceError
from .contracts import CleanupResult, DemoSession
from .demo_files import close_demo_session, open_demo_session


@dataclass(frozen=True)
class DemoSessionInfo:
    """Safe session descriptor; raw provider and staging paths stay in the host."""
    session_id: str
    version: int
    capabilities: tuple[str, ...]
    mode: Literal['demo'] = field(default='demo', init=False)


@dataclass
class _ManagedSession:
    session: DemoSession
    closing: bool = False
    admitted: int = 0
    cleanup_running: bool = False
    retired: bool = False
    cleanup_result: CleanupResult | None = None


def _failure(code: str, *, retryable: bool = False) -> ServiceError:
    messages = {
        'access_denied': 'Не удалось зарегистрировать защищённую папку учебных данных.',
        'demo_session_not_found': 'Учебная сессия недоступна. Откройте обучение заново.',
        'demo_session_closed': 'Учебная сессия закрывается. Дождитесь завершения очистки.',
        'demo_session_limit': 'Сначала завершите текущую учебную сессию и её очистку.',
        'too_many_requests': 'Учебный поиск занят. Повторите запрос немного позже.',
        'demo_session_unavailable': 'Не удалось открыть учебные данные. Повторите попытку.',
        'demo_cleanup_pending': 'Очистка учебных данных ещё не завершена. Повторите попытку.',
    }
    statuses = {'access_denied': 403, 'demo_session_not_found': 404, 'demo_session_closed': 409,
                'demo_session_limit': 409, 'too_many_requests': 429,
                'demo_session_unavailable': 503, 'demo_cleanup_pending': 503}
    return ServiceError(code, messages[code], retryable=retryable, status_code=statuses[code])


class DemoSessionManager:
    """One host composition; it has no work client, owner, token or fallback.

    begin_close is the host's nonblocking shutdown cutoff. close invokes that
    cutoff itself, drains every admitted search/fetch, then runs the accepted
    owned-file cleanup. Busy receipts retain ownership and consume session
    capacity until a later close retry. This class exposes no transport route.
    """
    def __init__(self, *, manifest_path: Path, profile_dir: Path,
                 build_capabilities: tuple[str, ...], register_owned_exclusion: Callable[[Path], None],
                 max_sessions: int = 1, max_inflight: int = 8):
        if (type(build_capabilities) is not tuple
                or any(type(capability) is not str for capability in build_capabilities)):
            raise ValueError('invalid_demo_capabilities')
        if (type(max_sessions) is not int or not 1 <= max_sessions <= 4
                or type(max_inflight) is not int or not 1 <= max_inflight <= 64):
            raise ValueError('invalid_demo_limits')
        if not callable(register_owned_exclusion):
            raise ValueError('invalid_demo_registration')
        self._manifest = Path(canonical_root(str(manifest_path)))
        self._profile = Path(canonical_root(str(profile_dir)))
        self._owned_base = self._profile / 'runtime' / 'onboarding-demo'
        self._build_capabilities = tuple(build_capabilities)
        self._register = register_owned_exclusion
        self._max_sessions, self._max_inflight = max_sessions, max_inflight
        self._condition = threading.Condition(threading.RLock())
        self._sessions: dict[str, _ManagedSession] = {}
        self._opening = 0
        self._registered = False

    def _owned(self, session_id: str) -> _ManagedSession:
        # Never use the low-level global session registry as caller authority.
        if type(session_id) is not str or session_id not in self._sessions:
            raise _failure('demo_session_not_found')
        return self._sessions[session_id]

    def open(self) -> DemoSessionInfo:
        with self._condition:
            if len(self._sessions) + self._opening >= self._max_sessions:
                raise _failure('demo_session_limit', retryable=True)
            self._opening += 1
            if not self._registered:
                try:
                    acknowledged = self._register(self._owned_base)
                    if acknowledged is not None:
                        raise ValueError('registration_not_acknowledged')
                except Exception:
                    # A callback may carry private owner paths, tokens or error
                    # details. None of those belong in the broker response.
                    self._opening -= 1
                    self._condition.notify_all()
                    raise _failure('access_denied') from None
                self._registered = True
        try:
            session = open_demo_session(self._manifest, self._owned_base, self._build_capabilities)
        except Exception:
            with self._condition:
                self._opening -= 1
                self._condition.notify_all()
            raise _failure('demo_session_unavailable', retryable=True) from None
        with self._condition:
            self._opening -= 1
            self._sessions[session.session_id] = _ManagedSession(session)
            self._condition.notify_all()
            capabilities = tuple(capability for capability in session.capabilities
                                 if capability in self._build_capabilities)
            return DemoSessionInfo(session.session_id, session.version, capabilities)

    @contextmanager
    def _operation(self, session_id: str):
        with self._condition:
            owned = self._owned(session_id)
            if owned.closing:
                raise _failure('demo_session_closed')
            if owned.admitted >= self._max_inflight:
                raise _failure('too_many_requests', retryable=True)
            owned.admitted += 1
        try:
            yield owned.session.provider
        finally:
            with self._condition:
                owned.admitted -= 1
                self._condition.notify_all()

    def search(self, session_id: str, search_request: SearchRequest) -> SearchResponse:
        with self._operation(session_id) as provider:
            return provider.search(search_request)

    def fetch(self, session_id: str, result_id: str, *,
              source_ids: tuple[str, ...] | None = None) -> ResultMetadata:
        with self._operation(session_id) as provider:
            return provider.fetch(result_id, source_ids=source_ids)

    def begin_close(self, session_id: str) -> None:
        """Trusted host only: stop admission without waiting for active work."""
        with self._condition:
            self._owned(session_id).closing = True
            self._condition.notify_all()

    def close(self, session_id: str) -> CleanupResult:
        with self._condition:
            owned = self._owned(session_id)
            self.begin_close(session_id)
            while owned.admitted or owned.cleanup_running:
                self._condition.wait()
            if owned.retired:
                # A concurrently admitted closer may share the completed result.
                # New calls cannot look up this retired identity afterward.
                assert owned.cleanup_result is not None
                return owned.cleanup_result
            owned.cleanup_running = True
        try:
            result = close_demo_session(owned.session.session_id)
        except Exception:
            with self._condition:
                owned.cleanup_running = False
                self._condition.notify_all()
            raise _failure('demo_cleanup_pending', retryable=True) from None
        with self._condition:
            owned.cleanup_running = False
            owned.cleanup_result = result
            if not result.deferred:
                owned.retired = True
                self._sessions.pop(session_id)
            self._condition.notify_all()
            return result
