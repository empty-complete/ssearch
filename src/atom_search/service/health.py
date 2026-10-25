"""Memory-only caller health and a fixed number of independent local probes.

Catalogue availability is an observation of a crawl, never a side effect of
reading status. SQL counts have their own clock and do not borrow job progress.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import threading
import time

from .contracts import (ConfigVersion, SourceHealth, ModelHealth, DatabaseHealth,
    StorageHealth, HealthComponents, HealthSnapshot, OutboxHealth)

TTL_SECONDS = 300
READ_CAPABILITIES = frozenset({'search', 'fetch', 'status', 'settings_get'})
PROBE_PENDING = object()


def utc_now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _stale(value, now):
    return value is None or (now - datetime.fromisoformat(value)).total_seconds() > TTL_SECONDS


def storage_health(values, *, checked_at):
    # Physical is measured in P0's complete accounting domain. WAL/temp and
    # logical SQL bytes are breakdowns, not additional physical allocations.
    names = ('physical_owned_bytes', 'pgdata_bytes', 'wal_bytes', 'temp_bytes',
             'cache_bytes', 'outbox_bytes', 'backup_bytes', 'staging_bytes',
             'database_logical_bytes', 'quota_bytes', 'free_bytes')
    return StorageHealth(*(values.get(name) for name in names), values.get('reserved_bytes'),
                         _stamp(checked_at), checked_at is None, values.get('maintenance_required', False))


class HealthCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._config = ConfigVersion(1, 0)
        self._sources = {}
        self._paths = {}
        self._counts = {}
        self._generation = 0
        self._database = DatabaseHealth('postgresql', None, None, 'starting', None, True, None)
        self._model = ModelHealth('unknown', None, None, True, False)
        self._storage = storage_health({}, checked_at=None)
        self._jobs = ()
        self._project_support = (False, None)
        self._stopping = False

    def configure(self, snapshot):
        """Owner's apply hook supplies authoritative policy without read I/O."""
        with self._lock:
            from atom_search_core.source import canonical_root
            enabled = {item['id']: canonical_root(item['path']) if item.get('path') else None
                       for item in snapshot.settings['sources'] if item['enabled']}
            self._sources = {identity: self._sources.get(identity,
                SourceHealth(identity, 'unknown', None, None, True, None, None, None, None, True))
                for identity in enabled}
            self._counts = {identity: counts for identity, counts in self._counts.items() if identity in enabled}
            self._paths = enabled
            version = ConfigVersion(snapshot.schema_version, snapshot.revision)
            if version != self._config:
                self._project_support = (False, None)
            self._config = version

    def policy(self):
        with self._lock:
            return self._config, dict(self._paths)

    def record(self, component: str, snapshot: object, *, checked_at: datetime):
        with self._lock:
            stamp = _stamp(checked_at)
            if component == 'source':
                if snapshot.source_id not in self._sources:
                    return
                previous = self._sources[snapshot.source_id]
                self._sources[snapshot.source_id] = replace(snapshot, checked_at=stamp, stale=False,
                    last_success_at=stamp if snapshot.state == 'available' else previous.last_success_at)
            elif component in ('database', 'model', 'storage'):
                setattr(self, '_' + component, replace(deepcopy(snapshot), checked_at=stamp, stale=False))
            elif component == 'jobs':
                self._jobs = tuple(deepcopy(snapshot))
            else:
                raise ValueError('unknown_health_component')

    def record_counts(self, counts, *, generation, revision, checked_at):
        with self._lock:
            if revision != self._config.revision:
                return
            self._counts = {identity: (count[0], count[1], _stamp(checked_at))
                            for identity, count in counts.items() if identity in self._sources}
            self._generation = generation

    def record_project_support(self, *, available, revision, checked_at):
        """A bounded background observation, never a status-time SQL check."""
        with self._lock:
            if revision == self._config.revision:
                self._project_support = (available is True, _stamp(checked_at))

    def record_source_report(self, identity, report, *, revision, checked_at):
        with self._lock:
            if revision != self._config.revision or identity not in self._sources:
                return
            status = report.get('status')
            if status not in ('complete', 'partial', 'unavailable'):
                return  # Cancellation/busy is not evidence of availability.
            state = {'complete': 'available', 'partial': 'partial', 'unavailable': 'unreachable'}.get(status, 'unknown')
            errors = report.get('errors', ())
            denied = any(item.get('error_code') == 'access_denied' for item in errors)
            if denied and state == 'unreachable':
                state = 'access_denied'
            previous = self._sources[identity]
            self.record('source', replace(previous, state=state,
                error_code='source_access_denied' if state == 'access_denied' else
                           'source_unavailable' if state == 'unreachable' else None), checked_at=checked_at)

    def stopping(self):
        with self._lock:
            self._stopping = True

    def snapshot(self, *, now: datetime, access) -> HealthSnapshot:
        with self._lock:
            # Intersect current policy before counts or readiness are derived.
            permitted = set(self._sources) if 'owner' in access.capabilities else set(access.source_ids) & self._sources.keys()
            sources = []
            for identity, value in self._sources.items():
                if identity not in permitted:
                    continue
                count, embedded, checked = self._counts.get(identity, (None, None, None))
                sources.append(replace(value, stale=_stale(value.checked_at, now), catalogued_files=count,
                    metadata_embedded_files=embedded, catalog_counts_checked_at=checked,
                    catalog_counts_stale=_stale(checked, now)))
            sources = tuple(sources)
            database = replace(self._database, stale=_stale(self._database.checked_at, now))
            catalogue = any(
                (value.catalogued_files or 0) > 0 or value.last_success_at is not None for value in sources)
            lexical = database.runtime_state == 'ready' and catalogue
            model = replace(self._model, stale=_stale(self._model.checked_at, now), lexical_available=lexical)
            storage = replace(self._storage, stale=_stale(self._storage.checked_at, now))
            if database.runtime_state not in ('ready', 'starting'):
                readiness = 'blocked'
            elif not catalogue or database.runtime_state == 'starting':
                readiness = 'warming'
            elif (database.runtime_state != 'ready' or database.stale or model.state != 'ready' or model.stale
                  or any(value.state != 'available' or value.stale or value.catalog_counts_stale for value in sources)):
                readiness = 'degraded'
            else:
                readiness = 'ready'
            capabilities = READ_CAPABILITIES if 'owner' in access.capabilities else READ_CAPABILITIES & access.capabilities
            supported, checked = self._project_support
            if (supported and self._config.schema_version in (2, 3) and not _stale(checked, now) and database.runtime_state == 'ready'
                    and not database.stale and permitted and 'search' in capabilities):
                capabilities |= {'project_search', 'project_aliases'}
            return HealthSnapshot('stopping' if self._stopping else 'alive', readiness, '0.2.0', '0.2.0', '1',
                _stamp(now), self._config, self._generation, HealthComponents(sources, model, database, storage,
                tuple(job for job in self._jobs if job.source_id in permitted), OutboxHealth(0, 0, None, False)),
                tuple(sorted(capabilities)))

    def desktop_snapshot(self, root=None):
        """Legacy shell shape derived from the same cache, with no SQL fallback."""
        from .contracts import AccessContext
        with self._lock:
            selected = tuple(identity for identity, path in self._paths.items() if root is None or path == root)
            health = self.snapshot(now=utc_now(), access=AccessContext('desktop', READ_CAPABILITIES, selected))
            sources = health.components.sources
            counts = [source.catalogued_files for source in sources]
            files = sum(counts) if counts and all(count is not None for count in counts) else None
            roots = [dict(id=source.source_id, path=self._paths[source.source_id], status={
                'available': 'complete', 'unreachable': 'unavailable'}.get(source.state, source.state),
                last_attempt=source.checked_at, last_success=source.last_success_at, error=source.error_code)
                for source in sources]
            return dict(files=files, roots=roots, last_success=max(
                (source.last_success_at for source in sources if source.last_success_at), default=None),
                backend='postgresql', metadata_ready=bool(sources) and all(source.state == 'available' for source in sources),
                semantic_ready=health.components.model.state == 'ready',
                embeddings_ready=bool(sources) and all(source.catalogued_files is not None and
                    source.catalogued_files == source.metadata_embedded_files for source in sources),
                model_status=health.components.model.state, readiness=health.readiness)


class ProbeScheduler:
    """One persistent worker per local lane, never a thread per status/tick.

    Probe functions are deadline/cancellation aware and never enumerate sources.
    A late result cannot freshen a component. Shutdown drains these workers
    before the owner closes its sole database/runtime.
    """
    def __init__(self, health, probes, *, budget_seconds=2, interval_seconds=60):
        self.health, self.probes = health, dict(probes)
        self.budget_seconds, self.interval_seconds = budget_seconds, interval_seconds
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._pending = set()
        self._running = set()
        self._due = {}
        self._last_started = {}
        self._threads = {}
        self._timer = None

    def tick(self, now: datetime):
        with self._condition:
            if self._stop.is_set():
                return
            for name in self.probes:
                if name not in self._running and name not in self._pending and now.timestamp() >= self._due.get(name, 0):
                    self._pending.add(name)
                    self._last_started[name] = now.timestamp()
                    self._due[name] = now.timestamp() + self.interval_seconds
                    if name not in self._threads:
                        thread = threading.Thread(target=self._loop, args=(name,), name='atom-health-' + name, daemon=False)
                        self._threads[name] = thread
                        thread.start()
            self._condition.notify_all()

    def request(self, name='database'):
        with self._condition:
            self._due[name] = self._last_started.get(name, 0) + 1
        self.tick(utc_now())

    def start(self):
        self.tick(utc_now())
        with self._condition:
            if self._timer is None and not self._stop.is_set():
                self._timer = threading.Thread(target=self._timer_loop, name='atom-health-timer', daemon=False)
                self._timer.start()

    def _timer_loop(self):
        while not self._stop.wait(1):
            self.tick(utc_now())

    def _loop(self, name):
        while True:
            with self._condition:
                while name not in self._pending and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    return
                self._pending.remove(name)
                self._running.add(name)
            deadline = time.monotonic() + self.budget_seconds
            try:
                result = self.probes[name](deadline, self._stop)
                if result is PROBE_PENDING:
                    with self._condition:
                        self._due[name] = time.time() + 1
                elif result and not self._stop.is_set() and time.monotonic() <= deadline:
                    checked = utc_now()
                    for component, value in result.items():
                        if component == 'counts':
                            self.health.record_counts(**value, checked_at=checked)
                        elif component == 'project_support':
                            self.health.record_project_support(**value, checked_at=checked)
                        else:
                            self.health.record(component, value, checked_at=checked)
            except Exception:
                # Preserve component timestamps; failed SQL cannot keep a
                # previously advertised projection feature freshly available.
                if name == 'database' and isinstance(self.health, HealthCache):
                    config, _ = self.health.policy()
                    self.health.record_project_support(available=False,
                        revision=config.revision, checked_at=utc_now())
            finally:
                with self._condition:
                    self._running.discard(name)
                    self._condition.notify_all()

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        for thread in (*self._threads.values(), self._timer):
            if thread is not None and thread.ident is not None:
                thread.join()


def database_probe(owner, deadline, stop):
    if stop.is_set():
        return None
    config, paths = owner.health.policy()
    state = owner.runtime.status(deadline=deadline)  # Bounded kernel/lock observation.
    endpoint = state.endpoint
    model = owner.core._embedder
    result = dict(database=DatabaseHealth('postgresql', getattr(endpoint, 'postgres_version', None),
        getattr(endpoint, 'vector_version', None), state.state, None, True, state.error_code),
        model=ModelHealth('ready' if model is not None else 'unavailable',
            getattr(model, 'model_id', None), None, True, False),
        project_support=dict(available=False, revision=config.revision))
    if state.state != 'ready' or stop.is_set():
        return result
    # This API validates the same owned runtime and uses a read-only SQL
    # transaction. Each statement and lock acquisition observes the deadline.
    with owner.runtime.open_health_connection(deadline=deadline) as connection:
        generation = connection.execute("SELECT generation FROM atom_search.catalogs WHERE state='active'").fetchone()[0]
        identity = owner.core.catalog._model
        key = identity.model_key if identity is not None else None
        rows = connection.execute("""SELECT p.source_uuid::text,count(f.id),
            count(e.file_id) FILTER (WHERE e.file_revision=f.revision
                AND m.dimension=%s AND m.input_schema=%s AND m.manifest_digest=%s
                AND ((m.input_schema='filename-path-root-project-v2'
                    AND pc.context_revision=st.context_revision AND pc.file_revision=f.revision
                    AND pc.input_hash=e.input_hash)
                OR (m.input_schema='filename-parent-v1'
                    AND e.input_hash=encode(sha256(convert_to(f.name || CASE
                    WHEN position('/' in f.relative_path)>0 THEN ' | ' ||
                        replace(left(f.relative_path,length(f.relative_path)-length(f.name)-1),'/', ' / ')
                    ELSE '' END,'UTF8')),'hex'))))
            FROM atom_search.sources s JOIN atom_search.catalogs c ON c.id=s.catalog_id
            JOIN atom_search.source_public_ids p ON p.source_id=s.id
            LEFT JOIN atom_search.files f ON f.source_id=s.id
            LEFT JOIN atom_search.file_embeddings e ON e.file_id=f.id AND e.model_key=%s
            LEFT JOIN atom_search.embedding_models m ON m.model_key=e.model_key
            LEFT JOIN atom_search.file_project_context pc ON pc.file_id=f.id
            LEFT JOIN atom_search.project_context_state st ON st.id=1
            WHERE c.state='active' AND p.source_uuid=ANY(%s::uuid[])
            GROUP BY p.source_uuid""", (getattr(identity, 'dimension', None),
                getattr(identity, 'input_schema', None), getattr(identity, 'manifest_digest', None), key, list(paths))).fetchall()
        result['counts'] = dict(counts={identity: (count, embedded if model is not None else 0)
            for identity, count, embedded in rows}, generation=generation, revision=config.revision)
        if getattr(owner.core, 'project_search_supported', False) is True:
            project = connection.execute('''SELECT settings_revision,context_revision,configuration_hash,
                (SELECT max(version) FROM atom_search.schema_migrations)
                FROM atom_search.project_context_state WHERE id=1''').fetchone()
            result['project_support']['available'] = bool(config.schema_version in (2, 3)
                and project and project[0] == config.revision
                and type(project[1]) is int and project[1] > 0
                and isinstance(project[2], str) and len(project[2]) == 64 and project[3] == 5)
        # Logical size is independent of physical allocation and reservations.
        # It shares this observation's SQL deadline, never a status request.
        logical = connection.execute('SELECT pg_database_size(current_database())').fetchone()[0]
    if time.monotonic() < deadline and not stop.is_set():
        with owner.health._lock:
            owner.health._storage = replace(owner.health._storage, database_logical_bytes=logical)
    return result


def storage_probe(owner, deadline, stop):
    if stop.is_set():
        return None
    from atom_search.runtime import _windows
    budget = owner.runtime.storage_budget
    allocation = getattr(owner, '_storage_allocation', None)
    if allocation is None:
        data = owner.runtime.data_dir
        groups = {'pgdata_bytes': (data,), 'wal_bytes': (data / 'pg_wal',),
                  'temp_bytes': lambda path: path.is_relative_to(data) and 'pgsql_tmp' in path.parts,
                  'cache_bytes': (owner.repository.engine / 'cache',),
                  'outbox_bytes': (owner.repository.engine / 'outbox',),
                  'backup_bytes': (owner.repository.backup_dir,),
                  'staging_bytes': (owner.repository.engine / 'update-staging',)}
        owner._storage_allocation = allocation = _windows.AllocationScan(
            budget.roots, read_only_files=budget.read_only_files, groups=groups)
    try:
        values = budget.measure(deadline=deadline, allocation=allocation, stop=stop)
    except Exception:
        owner._storage_allocation = None
        raise
    if values is None:
        return PROBE_PENDING
    owner._storage_allocation = None
    with owner.health._lock:
        values['database_logical_bytes'] = owner.health._storage.database_logical_bytes
    return {'storage': storage_health(values, checked_at=utc_now())}
