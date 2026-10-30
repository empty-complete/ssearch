"""Per-request policy gate; host supplies bounded concrete-file authority."""
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import math
import threading
import time

from atom_search.service.contracts import AccessContext, ServiceError
from .contracts import (BoundedFileChecker, ContentAccessPolicy, ContentFileRef,
                        ContentGrant, FileReadResult)


def _deny(code):
    return ServiceError(code, 'Content access is unavailable.', status_code=403)


class ContentCheckBudget:
    """Shared by all gates/checks in ONE request; never reused as authority.

    This limits admission and rejects late replies. Actual blocking I/O must be
    bounded by BoundedFileChecker's host implementation, not by this timer.
    """
    def __init__(self, *, clock=time.monotonic, max_checks=16, timeout=2.0):
        if type(max_checks) is not int or not 1 <= max_checks <= 16:
            raise ValueError('max_checks must be within 1..16')
        if not math.isfinite(timeout) or not 0 < timeout <= 2.0:
            raise ValueError('timeout must be within (0, 2]')
        self._clock = clock
        self._last = clock()
        if not math.isfinite(self._last):
            raise ValueError('clock must return a finite monotonic timestamp')
        self.deadline = self._last + timeout
        self._limit, self._used = max_checks, 0
        self._lock = threading.RLock()

    def ensure_open(self):
        with self._lock:
            now = self._clock()
            self._last = max(self._last, now)
            if not math.isfinite(now) or self._last >= self.deadline:
                raise _deny('content_check_budget_exhausted')

    def consume(self):
        with self._lock:
            self.ensure_open()
            if self._used >= self._limit:
                raise _deny('content_check_budget_exhausted')
            self._used += 1


def _policy(value):
    # Missing/unknown persisted data is NOT the constructor's documented default.
    if isinstance(value, Mapping):
        if set(value) != {'mode', 'allow_cached_unreachable', 'policy_revision'}:
            raise _deny('content_policy_invalid')
        value = ContentAccessPolicy(**value)
    if (not isinstance(value, ContentAccessPolicy)
            or value.mode not in ('deny', 'require_current_file_read', 'cached_owner_grant')
            or type(value.allow_cached_unreachable) is not bool
            or type(value.policy_revision) is not int or value.policy_revision < 1):
        raise _deny('content_policy_invalid')
    return value


class ContentAccessGate:
    """Create per request with an owner-resolved explicit source scope.

    New content capabilities are explicit seam names, not capabilities issued
    by existing owner/token code. Empty source scope grants no access, including
    for an owner: the host must resolve its authority before using this gate.
    The caller cannot supply/upgrade persisted policy through authorize().
    """
    _CAPABILITIES = {'extract': 'content_extract_v1',
                     'search_content': 'content_search_v1',
                     'fetch_content': 'content_read_v1'}

    def __init__(self, *, policy_for: Callable, resolve_file: Callable,
                 checker: BoundedFileChecker, budget: ContentCheckBudget):
        self._policy_for, self._resolve_file = policy_for, resolve_file
        self._checker, self._budget = checker, budget

    def authorize(self, source_id: str, *, operation: str, access: AccessContext,
                  result_id: str) -> ContentGrant:
        capability = self._CAPABILITIES.get(operation)
        if capability is None:
            raise _deny('content_operation_invalid')
        if capability not in access.capabilities or source_id not in access.source_ids:
            raise _deny('content_scope_denied')
        self._budget.ensure_open()
        policy = _policy(self._policy_for(source_id))
        if policy.mode == 'deny':
            raise _deny('content_access_denied')
        ref = self._resolve_file(source_id, result_id)
        if (not isinstance(ref, ContentFileRef) or ref.source_id != source_id
                or ref.result_id != result_id or type(ref.file_revision) is not int
                or ref.file_revision < 1 or type(ref.file_id) is not int or ref.file_id < 1):
            raise _deny('content_access_denied')
        self._budget.consume()
        try:
            outcome = self._checker.check(ref, deadline=self._budget.deadline)
        except (OSError, TimeoutError):
            # Exceptions never infer an offline exception from an ambiguous error.
            raise _deny('content_access_denied') from None
        self._budget.ensure_open()
        if (not isinstance(outcome, FileReadResult) or outcome.source_id != ref.source_id
                or outcome.result_id != ref.result_id or type(outcome.file_revision) is not int
                or outcome.file_revision != ref.file_revision):
            raise _deny('content_access_denied')
        cached = (policy.mode == 'cached_owner_grant' and policy.allow_cached_unreachable
                  and operation != 'extract' and outcome.status in ('offline', 'unreachable'))
        if outcome.status != 'readable' and not cached:
            raise _deny('content_access_denied')
        return ContentGrant(source_id, result_id, ref.file_revision, policy.policy_revision,
                            operation, datetime.now(timezone.utc).isoformat(), cached)
