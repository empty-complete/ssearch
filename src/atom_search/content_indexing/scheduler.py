"""Bounded admission/dequeue for the existing owner; creates NO worker.

on_metadata_commit accepts an incremental committed batch, not a full catalogue
snapshot. The host's is_current validates current file/revision/stat/generations
and deletion/cancellation authority. Global metadata_pending includes ALL sources.
No source is opened here. Production host integration and PG CAS remain separate.
"""
from dataclasses import dataclass, replace
import math
import threading
import time

from atom_search_core.models import CatalogFile
from .contracts import ContentScheduleState, ContentWorkToken


@dataclass
class _Work:
    token: ContentWorkToken
    file: CatalogFile
    attempts: int
    not_before: float


class ContentScheduler:
    def __init__(self, *, state_for, metadata_pending, is_current,
                 clock=time.monotonic, max_pending=128, max_attempts=3, retry_delay=1.0):
        if type(max_pending) is not int or not 1 <= max_pending <= 1024:
            raise ValueError('max_pending must be within 1..1024')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 16:
            raise ValueError('max_attempts must be within 1..16')
        if not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError('retry_delay must be finite and nonnegative')
        self._state_for, self._metadata_pending = state_for, metadata_pending
        self._is_current, self._clock = is_current, clock
        self._capacity, self._max_attempts, self._retry_delay = max_pending, max_attempts, retry_delay
        self._work: dict[int, _Work] = {}
        self._running: ContentWorkToken | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _fingerprint(file):
        return file.size, file.modified_ns

    def _fresh(self, work):
        token = work.token
        if not self._is_current(token):
            return False
        state = self._state_for(work.file)
        return (isinstance(state, ContentScheduleState)
                and (state.profile, state.content_generation, state.profile_generation,
                     state.context_revision, state.extractor_version, state.chunker_version)
                == (token.profile, token.content_generation, token.profile_generation,
                    token.context_revision, token.extractor_version, token.chunker_version)
                and state.profile in ('light', 'balanced', 'detailed'))

    def on_metadata_commit(self, files: tuple[CatalogFile, ...], *,
                           catalog_generation: int) -> tuple[ContentWorkToken, ...]:
        admitted = []
        with self._lock:
            for file in files:
                state = self._state_for(file)
                if not isinstance(state, ContentScheduleState):
                    continue
                fingerprint = self._fingerprint(file)
                if (state.profile not in ('light', 'balanced', 'detailed')
                        or state.successful_fingerprint == fingerprint):
                    self._work.pop(file.file_id, None)
                    continue
                attempts = state.attempts if state.attempted_fingerprint == fingerprint else 0
                if attempts >= self._max_attempts:
                    self._work.pop(file.file_id, None)
                    continue
                token = ContentWorkToken(file.file_id, file.source_id, file.relative_path,
                    file.revision, file.size, file.modified_ns, catalog_generation,
                    state.content_generation, state.profile_generation, state.context_revision,
                    state.extractor_version, state.chunker_version, state.profile)
                previous = self._work.get(file.file_id)
                # A later global metadata commit alone does not create new work.
                if previous and self._same_work(previous.token, token):
                    continue
                if previous is None and len(self._work) >= self._capacity:
                    continue  # Not processed; a later committed batch may admit it.
                not_before = (state.retry_not_before
                              if state.attempted_fingerprint == fingerprint else self._clock())
                self._work[file.file_id] = _Work(token, file, attempts, not_before)
                admitted.append(token)
        return tuple(admitted)

    @staticmethod
    def _same_work(a, b):
        return all(getattr(a, name) == getattr(b, name)
                   for name in ContentWorkToken.__dataclass_fields__ if name != 'catalog_generation')

    def next_work(self) -> ContentWorkToken | None:
        with self._lock:
            # Cancellation/replacement does not free a still executing host slot.
            if self._running is not None or self._metadata_pending():
                return None
            for identity, work in tuple(self._work.items()):
                if not self._fresh(work):
                    del self._work[identity]
                    continue
                state = self._state_for(work.file)
                if state.successful_fingerprint == self._fingerprint(work.file):
                    del self._work[identity]
                    continue
                if work.not_before > self._clock():
                    continue
                work.attempts += 1
                self._running = work.token
                return work.token
        return None

    def finish_work(self, token: ContentWorkToken, *, status: str, retryable=False) -> bool:
        """Acknowledge the SAME admitted token after host publish/state commit.

        This does not persist success/attempts or publish content. The host must
        commit durable state before ack, including failed-attempt accounting.
        Tokens are opaque local admissions: reconstructed/equal tokens cannot
        acknowledge another admission or release its execution slot.
        """
        with self._lock:
            if self._running is not token:
                return False
            self._running = None
            work = self._work.get(token.file_id)
            if work is None or work.token is not token:
                return False
            if not self._fresh(work):
                del self._work[token.file_id]
                return False
            if status in ('failed', 'timeout') and retryable and work.attempts < self._max_attempts:
                # A new attempt has distinct local admission identity. A delayed
                # duplicate ack from the previous attempt cannot release it.
                work.token = replace(work.token)
                work.not_before = self._clock() + self._retry_delay * work.attempts
            else:
                del self._work[token.file_id]
            return True

    def cancel_source(self, source_id: int) -> None:
        with self._lock:
            for identity, work in tuple(self._work.items()):
                if work.token.source_id == source_id:
                    del self._work[identity]
