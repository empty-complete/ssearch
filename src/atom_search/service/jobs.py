"""One metadata worker and one bounded inference queue owned by ServiceOwner.

Only acknowledged catalogue transactions contribute progress. A restart keeps
the scheduling deadline, never resurrects unacknowledged worker counters.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import threading
import time
import uuid

from atom_search.http.security import error
from atom_search.content import message
from atom_search.runtime import _windows
from atom_search.settings.schema import settings_error
from atom_search_core.source import canonical_root
from .contracts import JobSnapshot

MAX_SCHEDULE_BYTES = 65536


def _root_digest(root):
    return hashlib.sha256(root.encode('utf-8')).hexdigest()


def utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class JobToken:
    source_uuid: str
    settings_revision: int
    catalog_generation: int
    source_generation: int
    root: str
    source_ids: tuple[int, ...]


@dataclass
class _Job:
    token: JobToken
    snapshot: JobSnapshot
    cancel: threading.Event
    report: dict | None = None


class CatalogScheduler:
    def __init__(self, owner, *, automatic=True, clock=utc_now, jitter=random.random, autostart=True):
        self.owner, self.clock, self.jitter = owner, clock, jitter
        self._automatic = automatic
        self._condition = threading.Condition(threading.RLock())
        self._jobs = {}
        self._catalog = deque()
        self._embed = deque()
        self._running_catalog = None
        self._running_embed = None
        self._stopping = False
        self._paused = False
        self._settings_prepared = False
        self._settings_tokens = set()
        self._generations = {}
        self._sources = {}
        self._due = {}
        self._failures = {}
        self._interval = 86400
        self._schedule_version = 0
        self._saved_version = -1
        self.apply_settings(owner.repository.snapshot())
        self._load_schedule()
        self.queue_context()
        self._threads = [threading.Thread(target=self._catalog_loop, name='atom-catalog', daemon=False),
                         threading.Thread(target=self._embed_loop, name='atom-inference', daemon=False)]
        if automatic:
            self._threads.append(threading.Thread(target=self._timer_loop, name='atom-schedule', daemon=False))
        self._started = False
        if autostart:
            self.start()

    def start(self):
        with self._condition:
            if self._stopping:
                raise error('service_stopping', 503)
            if self._started:
                return
            self._started = True
            for thread in self._threads:
                thread.start()

    def _spread(self, identity):
        # Stable per profile/source, independent of restart and wall-clock drift.
        digest = hashlib.sha256((str(self.owner.profile_dir) + identity).encode()).digest()
        return int.from_bytes(digest[:4], 'big') % 60

    def prepare_settings(self):
        with self._condition:
            # Capture intent before repository.pending can invalidate a worker.
            # Pause admission/dequeue until commit or rollback is authoritative;
            # running workers retain their ordinary cancellation/CAS checks.
            self._settings_prepared = True
            self._paused = True
            self._settings_tokens = {job.token for job in self._jobs.values()
                if job.snapshot.state in ('queued', 'running') and not job.cancel.is_set()}

    def apply_settings(self, snapshot):
        now = self.clock()
        incoming = {item['id']: dict(item, path=canonical_root(item['path']))
                    for item in snapshot.settings['sources']}
        with self._condition:
            # A prepared worker may already have observed repository.pending
            # and become terminal before this component sees the new revision.
            # Only that attested intent, or live obsolete work, may be replayed.
            pending = {}
            for job in self._jobs.values():
                identity = job.token.source_uuid
                prepared = job.token in self._settings_tokens
                unfinished = job.snapshot.state in ('queued', 'running')
                if ((unfinished or prepared and job.snapshot.state == 'cancelled')
                        and not job.cancel.is_set()
                        and (prepared or job.token.settings_revision != snapshot.revision)
                        and self._sources.get(identity) == incoming.get(identity)
                        and incoming.get(identity, {}).get('enabled')):
                    if identity not in pending or pending[identity][0] != 'catalog':
                        pending[identity] = (job.snapshot.phase, job.token)
            for identity in self._sources.keys() | incoming.keys():
                if self._sources.get(identity) != incoming.get(identity):
                    self._generations[identity] = self._generations.get(identity, 0) + 1
            self._sources = incoming
            self._revision = snapshot.revision
            interval = snapshot.settings['indexing']['interval_seconds']
            if interval != self._interval:
                self._due = {identity: now + timedelta(seconds=interval) for identity in incoming}
            self._interval = interval
            self._schedule_version += 1
            for identity in incoming:
                self._due.setdefault(identity, now + timedelta(seconds=self._spread(identity)))
            for job in self._jobs.values():
                if job.snapshot.state in ('queued', 'running') and not self._valid(job):
                    job.cancel.set()
                    if job.snapshot.state == 'queued':
                        job.snapshot = replace(job.snapshot, state='cancelled', last_progress_at=now.isoformat())
            for identity, (phase, previous) in pending.items():
                # New job IDs/tokens and zero counters. Already acknowledged
                # metadata remains usable; an embedding-only restart need not
                # enumerate the source again.
                self._generations[identity] += 1
                # Preserve the already admitted catalogue identity. _valid still
                # rechecks mapping/generation/root before any replacement write.
                token = JobToken(identity, self._revision, previous.catalog_generation,
                    self._generations[identity], incoming[identity]['path'], previous.source_ids)
                job = self._new_job(token, phase)
                (self._catalog if phase == 'catalog' else self._embed).append(job.snapshot.job_id)
            self._settings_tokens.clear()
            self._trim()
            self._notify_health()

    def _valid(self, job):
        token = job.token
        source = self._sources.get(token.source_uuid)
        if (source is None or not source['enabled'] or source['path'] != token.root
                or token.source_generation != self._generations.get(token.source_uuid)
                or token.settings_revision != self._revision or job.cancel.is_set()):
            return False
        # The SQL generation grows normally after every snapshot; it must not
        # rewind. Stable internal IDs also reject a replaced active catalogue.
        try:
            current = self.owner.repository.snapshot()
            if current.revision != token.settings_revision:
                return False
            scope = self.owner.registry.resolve((token.source_uuid,), allowed=(token.source_uuid,))
            return (scope.source_ids == token.source_ids
                    and self.owner.index_generation() >= token.catalog_generation)
        except Exception:
            return False

    def _new_job(self, token, phase):
        identity = str(uuid.uuid4())
        snapshot = JobSnapshot(identity, token.source_uuid, 'queued', phase, None, None,
                               0, 0, 0, 0, 0, None, None)
        job = _Job(token, snapshot, threading.Event())
        self._jobs[identity] = job
        return job

    def start_catalog(self, source_ids: tuple[str, ...]) -> tuple[str, ...]:
        now = self.clock()
        with self._condition:
            if self._stopping:
                raise error('service_stopping', 503)
            if self._paused:
                raise settings_error('settings_busy', status_code=409)
            result = []
            for identity in dict.fromkeys(source_ids):
                source = self._sources.get(identity)
                if source is None or not source['enabled']:
                    raise error('source_scope_denied', 403)
                existing = next((job for job in self._jobs.values()
                    if job.snapshot.source_id == identity and job.snapshot.phase == 'catalog'
                    and job.snapshot.state in ('queued', 'running')), None)
                if existing is not None:
                    result.append(existing.snapshot.job_id)
                    continue
                # A new source generation supersedes its previous embedding,
                # without ever cancelling another source's work.
                self._generations[identity] = self._generations.get(identity, 0) + 1
                for old in self._jobs.values():
                    if old.snapshot.source_id == identity and old.snapshot.state in ('queued', 'running'):
                        old.cancel.set()
                scope = self.owner.registry.resolve((identity,), allowed=(identity,))
                token = JobToken(identity, self._revision, self.owner.index_generation(),
                                 self._generations[identity], source['path'], scope.source_ids)
                job = self._new_job(token, 'catalog')
                self._catalog.append(job.snapshot.job_id)
                result.append(job.snapshot.job_id)
                self._due[identity] = now + timedelta(seconds=self._interval)
            self._trim()
            self._schedule_version += 1
            self._notify_health()
        return tuple(result)

    def queue_context(self, *, manual=False):
        """Resume metadata backfill on the existing inference queue, without FS scans."""
        if (not self._automatic and not manual) or getattr(self.owner.core, 'projection', None) is None:
            return
        with self._condition:
            if self._stopping:
                raise error('service_stopping', 503)
            for identity, source in self._sources.items():
                if not source['enabled'] or any(job.snapshot.source_id == identity
                        and job.snapshot.state in ('queued', 'running') and not job.cancel.is_set()
                        for job in self._jobs.values()):
                    continue
                scope = self.owner.registry.resolve((identity,), allowed=(identity,))
                token = JobToken(identity, self._revision, self.owner.index_generation(),
                    self._generations[identity], source['path'], scope.source_ids)
                job = self._new_job(token, 'embed')
                self._embed.append(job.snapshot.job_id)
            self._notify_health()

    def _trim(self):
        # Bound retained terminal history independently of file count.
        terminal = [identity for identity, job in self._jobs.items()
                    if job.snapshot.state not in ('queued', 'running')]
        for identity in terminal[:-256]:
            self._jobs.pop(identity, None)

    def cancel(self, job_id: str) -> JobSnapshot:
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                raise error('job_not_found', 404)
            job.cancel.set()
            # A catalog cancellation also cancels only its matching derived
            # embedding token, never other sources or a replacement generation.
            for item in self._jobs.values():
                if item.token == job.token and item.snapshot.state in ('queued', 'running'):
                    item.cancel.set()
                    if item.snapshot.state == 'queued':
                        item.snapshot = replace(item.snapshot, state='cancelled', last_progress_at=self.clock().isoformat())
            self._notify_health()
            return job.snapshot

    def snapshot(self) -> tuple[JobSnapshot, ...]:
        with self._condition:
            return tuple(job.snapshot for job in self._jobs.values())

    def _notify_health(self):
        health = getattr(self.owner, 'health', None)
        if health is not None:
            health.record('jobs', tuple(job.snapshot for job in self._jobs.values()), checked_at=self.clock())
        self._condition.notify_all()

    def _record_source_health(self, job):
        health = getattr(self.owner, 'health', None)
        if health is not None and job.report is not None and job.snapshot.error_code != 'catalog_failed':
            health.record_source_report(job.token.source_uuid, job.report,
                revision=job.token.settings_revision, checked_at=self.clock())
        probes = getattr(self.owner, 'probes', None)
        if probes is not None:
            probes.request()

    def tick(self, now_utc: datetime) -> None:
        with self._condition:
            if self._stopping or self._paused:
                return
            due = tuple(identity for identity, source in self._sources.items()
                        if source['enabled'] and self._due[identity] <= now_utc)
        if due:
            # A settings pause may reject admission after selection. Keep its
            # existing due/backoff until work has actually been admitted.
            self.start_catalog(due)
            with self._condition:
                # Advance from now, not the missed historical deadline: one
                # wave after sleep and never a catch-up series.
                for identity in due:
                    self._due[identity] = now_utc + timedelta(seconds=self._interval)

    def quiesce(self, timeout=5):
        deadline = time.monotonic() + timeout
        with self._condition:
            self._paused = True
            for job in self._jobs.values():
                if job.snapshot.state in ('queued', 'running'):
                    self.cancel(job.snapshot.job_id)
            while self._running_catalog is not None or self._running_embed is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._paused = False
                    self._notify_health()
                    return False
                self._condition.wait(remaining)
        return True

    def resume(self):
        with self._condition:
            if self._settings_prepared:
                try:
                    snapshot = self.owner.repository.snapshot()
                except Exception:
                    # An unresolved journal is not authority to resume writes.
                    return
                if self._settings_tokens:
                    # A failed publication may never have called apply_settings.
                    # Reissue only pre-publication intent against restored policy.
                    self.apply_settings(snapshot)
                self._settings_prepared = False
            self._paused = False
            self._notify_health()

    def _next(self, queue, *, embedding=False):
        while not self._stopping:
            if not self._paused and (not embedding or not self._catalog and self._running_catalog is None):
                while queue:
                    identity = queue.popleft()
                    job = self._jobs.get(identity)
                    if job is None or job.snapshot.state != 'queued':
                        continue
                    if not self._valid(job):
                        job.snapshot = replace(job.snapshot, state='cancelled')
                        self._notify_health()
                        continue
                    job.snapshot = replace(job.snapshot, state='running',
                        started_at=job.snapshot.started_at or self.clock().isoformat())
                    self._notify_health()
                    return job
            self._condition.wait(.25)
        return None

    def _catalog_progress(self, job, report):
        with self._condition:
            if not self._valid(job):
                return
            succeeded, skipped, errors = report['files'], report['skipped_links'], len(report['errors'])
            job.report = report
            job.snapshot = replace(job.snapshot, discovered=succeeded, succeeded=succeeded, skipped=skipped,
                errors=errors, processed=succeeded + skipped + errors, last_progress_at=self.clock().isoformat())
            self._notify_health()

    def _catalog_loop(self):
        while True:
            with self._condition:
                job = self._next(self._catalog)
                if job is None:
                    return
                self._running_catalog = job.snapshot.job_id
            try:
                with self.owner.operation(job.cancel):
                    # Persist a wave's deadline before its first scan, without
                    # making a caller wait for whole-engine admission I/O.
                    self._save_schedule()
                    report = self.owner.core.scan(job.token.root,
                        progress=lambda value: self._catalog_progress(job, value), cancel=job.cancel,
                        excluded_roots=self.owner.registry.owned_exclusions(), publish_if=lambda: self._valid(job))
                self._catalog_progress(job, report)
                with self._condition:
                    job.report = report
                    state = {'complete': 'completed', 'partial': 'partial', 'cancelled': 'cancelled'}.get(report['status'], 'failed')
                    if not self._valid(job):
                        state = 'cancelled'
                        job.report = dict(report, status='cancelled')
                    job.snapshot = replace(job.snapshot, state=state,
                        error_code='source_unavailable' if report['status'] == 'unavailable' else None,
                        last_progress_at=self.clock().isoformat())
                    if state in ('completed', 'partial') and (self.owner.core._embedder is not None
                            or getattr(self.owner.core, 'projection', None) is not None):
                        embedding = self._new_job(job.token, 'embed')
                        self._embed.append(embedding.snapshot.job_id)
                    if state == 'failed':
                        failures = min(64, self._failures.get(job.token.source_uuid, 0) + 1)
                        self._failures[job.token.source_uuid] = failures
                        self._due[job.token.source_uuid] = self.clock() + timedelta(seconds=min(60, 2 ** min(failures - 1, 6)) + self.jitter())
                        self._schedule_version += 1
                    elif state == 'completed':
                        self._failures.pop(job.token.source_uuid, None)
            except Exception:
                with self._condition:
                    cancelled = job.cancel.is_set()
                    job.snapshot = replace(job.snapshot, state='cancelled' if cancelled else 'failed',
                        errors=job.snapshot.errors + (0 if cancelled else 1),
                        processed=job.snapshot.processed + (0 if cancelled else 1),
                        error_code=None if cancelled else 'catalog_failed', last_progress_at=self.clock().isoformat())
                    status = 'cancelled' if cancelled else 'partial' if job.snapshot.succeeded else 'unavailable'
                    job.report = dict(job.report or dict(files=0, directories=0, added=0, updated=0,
                        removed=0, skipped_links=0, errors=[]), status=status)
                    if not cancelled:
                        job.report['errors'] = [*job.report['errors'],
                            {'path': '', 'operation': 'catalog', 'message': message('worker.failure')}]
            finally:
                try:
                    with self._condition:
                        self._record_source_health(job)
                        if not self._catalog:
                            # Clear durable pending tokens once this metadata wave
                            # ends, keeping ordinary completed intervals intact.
                            self._schedule_version += 1
                        self._notify_health()
                    self._save_schedule()
                finally:
                    # A terminal snapshot may be visible before durable cleanup.
                    # Quiesce and embedding must retain this writer until then.
                    with self._condition:
                        self._running_catalog = None
                        self._notify_health()

    def _embed_loop(self):
        from atom_search_core.models import SearchScope
        while True:
            with self._condition:
                job = self._next(self._embed, embedding=True)
                if job is None:
                    return
                self._running_embed = job.snapshot.job_id
            try:
                with self.owner.operation(job.cancel):
                    report = self.owner.core.index_embedding_batch(scope=SearchScope(job.token.source_ids),
                        cancel=job.cancel, publish_if=lambda: self._valid(job))
                with self._condition:
                    saved, attempted = report['indexed'], report['attempted']
                    context_attempted = report.get('context_attempted', 0)
                    state = ('cancelled' if report['cancelled'] or not self._valid(job)
                             else 'completed' if not attempted and not context_attempted else 'queued')
                    if state == 'cancelled':
                        saved = attempted = 0  # An obsolete token publishes no new counters.
                    # CAS rejections are skipped, never reported as success.
                    job.snapshot = replace(job.snapshot, state=state,
                        discovered=job.snapshot.discovered + attempted,
                        succeeded=job.snapshot.succeeded + saved,
                        skipped=job.snapshot.skipped + (0 if state == 'cancelled' else attempted - saved),
                        processed=job.snapshot.processed + (0 if state == 'cancelled' else attempted),
                        last_progress_at=self.clock().isoformat())
                    if state == 'queued':
                        self._embed.append(job.snapshot.job_id)
            except Exception:
                with self._condition:
                    job.snapshot = replace(job.snapshot, state='failed', errors=job.snapshot.errors + 1,
                        processed=job.snapshot.processed + 1, error_code='embedding_failed',
                        last_progress_at=self.clock().isoformat())
            finally:
                with self._condition:
                    self._running_embed = None
                    self._notify_health()
                probes = getattr(self.owner, 'probes', None)
                if probes is not None:
                    probes.request()

    def wait_catalog(self, job_id, *, progress=None, cancel=None, cancel_job=True):
        with self._condition:
            while True:
                job = self._jobs[job_id]
                if cancel is not None and cancel.is_set():
                    if cancel_job:
                        self.cancel(job_id)
                    else:
                        return dict(job.report or {}, status='cancelled')
                if progress is not None and job.report is not None:
                    progress(job.report)
                if job.snapshot.state not in ('queued', 'running'):
                    return job.report or dict(status='cancelled', files=0, directories=0, added=0,
                        updated=0, removed=0, skipped_links=0, errors=[])
                self._condition.wait(.1)

    def wait_embeddings(self, *, progress=None, cancel=None):
        with self._condition:
            jobs = [job for job in self._jobs.values() if job.snapshot.phase == 'embed'
                    and job.snapshot.state in ('queued', 'running')]
            while any(job.snapshot.state in ('queued', 'running') for job in jobs):
                if cancel is not None and cancel.is_set():
                    for job in jobs:
                        self.cancel(job.snapshot.job_id)
                if progress:
                    progress({'indexed': sum(job.snapshot.succeeded for job in jobs)})
                self._condition.wait(.1)
            return {'indexed': sum(job.snapshot.succeeded for job in jobs),
                    'cancelled': bool(cancel and cancel.is_set())}

    def _timer_loop(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(1)
            try:
                self.tick(self.clock())
            except Exception:
                # Pending settings/recovery is not permission to scan a root.
                pass

    def _load_schedule(self):
        directory = getattr(self.owner.repository, 'runtime_dir', None)
        self._schedule_file = directory / 'catalog-schedule.json' if directory is not None else None
        if self._schedule_file is None or not self._schedule_file.exists():
            return
        _windows.assert_private(self._schedule_file)
        with self._schedule_file.open('rb') as stream:
            data = stream.read(MAX_SCHEDULE_BYTES + 1)
        if len(data) > MAX_SCHEDULE_BYTES:
            raise settings_error('settings_recovery_required', status_code=500)
        value = json.loads(data)
        version = value.get('schema_version')
        if value.get('revision') == self._revision and version in (1, 2):
            now = self.clock()
            for identity, value in value.get('sources', {}).items():
                if identity in self._sources:
                    due = datetime.fromisoformat(value['next_due'])
                    self._due[identity] = max(due, now + timedelta(seconds=self._spread(identity)))
                    failures = value.get('failures', 0)
                    if type(failures) is int and 0 <= failures <= 64:
                        self._failures[identity] = failures
                    pending = value.get('pending')
                    source = self._sources[identity]
                    generation = value.get('source_generation')
                    if (type(pending) is dict and source['enabled']
                            and pending.get('source_uuid') == identity
                            and pending.get('settings_revision') == self._revision
                            and ((version == 1 and pending.get('root') == source['path'])
                                 or (version == 2 and pending.get('root_digest') == _root_digest(source['path'])))
                            and type(generation) is int and generation > 0
                            and pending.get('source_generation') == generation
                            and type(pending.get('catalog_generation')) is int
                            and 0 <= pending['catalog_generation'] <= self.owner.index_generation()
                            and tuple(pending.get('source_ids', ())) == self.owner.registry.resolve(
                                (identity,), allowed=(identity,)).source_ids):
                        self._generations[identity] = pending['source_generation']
                        # A new UUID/token and zero counters will be created at
                        # the spread deadline. No old result/progress is revived.
                        self._due[identity] = now + timedelta(seconds=self._spread(identity))
            self._saved_version = self._schedule_version

    def _save_schedule(self):
        if self._schedule_file is None:
            return
        with self._condition:
            version = self._schedule_version
            if version == self._saved_version:
                return
            pending = {}
            for job in self._jobs.values():
                if job.snapshot.phase == 'catalog' and job.snapshot.state in ('queued', 'running'):
                    token = asdict(job.token)
                    token['root_digest'] = _root_digest(token.pop('root'))
                    pending[job.token.source_uuid] = token
            value = dict(schema_version=2, revision=self._revision, sources={identity: {
                'next_due': self._due[identity].isoformat(), 'failures': self._failures.get(identity, 0),
                'source_generation': self._generations.get(identity, 0),
                'pending': pending.get(identity)} for identity in self._sources})
        # Match atomic_json's exact encoding. Refuse before any scan rather than
        # dropping tokens or writing a cache that this reader cannot reopen.
        if len(json.dumps(value, separators=(',', ':')).encode('utf-8')) > MAX_SCHEDULE_BYTES:
            raise settings_error('settings_recovery_required', status_code=500)
        try:
            with self.owner.repository.mutation(), self.owner.repository.budget.reserve(MAX_SCHEDULE_BYTES * 2, 'catalog_schedule'):
                _windows.atomic_json(self._schedule_file, value)
            with self._condition:
                self._saved_version = version
        except Exception:
            # No counters are restored from this optional deadline cache.
            pass

    def close(self):
        with self._condition:
            self._stopping = True
            for job in self._jobs.values():
                job.cancel.set()
            self._notify_health()
        for thread in self._threads:
            if thread.ident is not None:
                thread.join()
