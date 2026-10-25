"""The single owner of the accepted P1 host, settings writer and operation drain."""
from contextlib import contextmanager
from dataclasses import asdict
import hmac
import json
import os
import sys
from pathlib import Path
import threading
import time
import uuid

from atom_search.runtime import _windows
from atom_search.runtime.credentials import CredentialStore
from atom_search.settings.repository import SettingsRepository
from atom_search.http.security import error
from atom_search.http.serialization import MAX_BODY, decode
from .contracts import AccessContext, ServiceError
from .mutations import MutationCoordinator
from .sources import SourceRegistry


class ServiceOwner:
    def __init__(self, profile_dir: Path, model_dir: Path, *, discover_legacy=False, automatic_catalog=True):
        self.profile_dir, self.model_dir = _windows.reject_reparse(Path(profile_dir).absolute()), Path(model_dir)
        self.discover_legacy = discover_legacy
        self.automatic_catalog = automatic_catalog
        self.repository = SettingsRepository(self.profile_dir, model_dir=self.model_dir)
        self._local_repository = self.repository
        self.coordinator = None
        self.core = self.runtime = self.registry = None
        self.scheduler = None
        from .health import HealthCache
        self.health = HealthCache()
        self.probes = None
        self.endpoint_file = self.repository.runtime_dir / 'endpoint.json'
        self.state_file = self.repository.runtime_dir / 'service-state.json'
        self.claim_file = self.repository.runtime_dir / 'service-claim.json'
        self.generation = self.installation_id = self.base_url = self.credential_ref = None
        self.credentials = None
        self.attached = False
        self._server = self._ipc = None
        self._start_lock = threading.RLock()
        self._condition = threading.Condition()
        self._active = 0
        self._closing = self._closed = False
        self._close_error = None
        self._published = False
        self._state_write_attempted = False
        self._startup_unknown = False
        self._slots = threading.BoundedSemaphore(16)
        self._credential_lock = threading.Lock()
        self._access = {}
        self._cancel = set()
        self._remote_cancel = {}
        self._bound_sources = None
        self._context_revision = None
        self._context_queue_pending = False
        self._owns_claim = False
        self._claim = None
        self._attached_client = None
        try:
            self._prepare_settings_owner()
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    @contextmanager
    def _host_guard(self):
        engine = _windows.reject_reparse(self.profile_dir / 'engine')
        with _windows.mutex('search-host:' + str(engine.resolve()), timeout_seconds=5):
            yield

    def _read_claim(self):
        try:
            _windows.assert_private(self.claim_file)
            with self.claim_file.open('rb') as stream:
                content = stream.read(MAX_BODY + 1)
            if len(content) > MAX_BODY:
                raise ValueError()
            value = decode(content)
            if (type(value) is not dict or set(value) != {'schema_version', 'phase', 'profile', 'generation', 'process'}
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or value['phase'] not in ('preparing', 'ready', 'stopped')
                    or value['profile'] != str(self.profile_dir.resolve())
                    or str(uuid.UUID(value['generation'])) != value['generation']):
                raise ValueError()
            process = _windows.ProcessIdentity(**value['process'])
            if (type(process.pid) is not int or process.pid <= 0 or type(process.created) is not int
                    or process.created <= 0 or type(process.executable) is not str):
                raise ValueError()
            return value, process
        except Exception:
            raise error('service_identity_mismatch', 409) from None

    def _assert_claim(self):
        value, process = self._read_claim()
        if (not self._owns_claim or value['generation'] != self.generation
                or value['process'] != self._claim['process'] or _windows.process_identity(process.pid) != process
                or value['phase'] == 'stopped'):
            raise error('service_identity_mismatch', 409)
        return value

    def _attach(self, client):
        from .desktop import RemoteRepository, RemoteCoordinator, RemoteRegistry
        client.status()  # Authenticated identity before exposing any writer.
        self.attached = True
        self._attached_client = client
        self.credential_ref = client.credential_ref
        self.generation = client.descriptor['generation']
        self.installation_id = client.descriptor['installation_id']
        self.base_url = client.descriptor['base_url']
        self.repository = RemoteRepository(self._local_repository, client)
        self.coordinator = RemoteCoordinator(client)
        self.registry = RemoteRegistry(client)

    def _prepare_settings_owner(self):
        """Claim the writer before load/recovery, without PG or inference.

        Only short mutex sections are held by their calling thread. The private
        durable claim bridges constructor, asynchronous start and close threads.
        """
        from .client import ServiceClient, read_owner_state
        from atom_search.search_runtime import _prepare_profile
        with self._host_guard():
            claim = process = None
            if self.claim_file.exists():
                claim, process = self._read_claim()
                actual = _windows.process_identity(process.pid)
                if claim['phase'] != 'stopped' and actual is not None:
                    if actual != process or claim['phase'] != 'ready' or not self.endpoint_file.exists():
                        raise error('service_identity_mismatch', 409)
                    client = ServiceClient(self.endpoint_file, None)
                    if client.descriptor['generation'] != claim['generation']:
                        raise error('service_identity_mismatch', 409)
                    self._attach(client)
                    return
            if self.endpoint_file.exists():
                client = ServiceClient(self.endpoint_file, None)
                if client.owner_alive():
                    if claim is not None:
                        raise error('service_identity_mismatch', 409)
                    self._attach(client)
                    return
            elif self.state_file.exists():
                state, process = read_owner_state(self.endpoint_file)
                if state['state'] == 'ready' and _windows.process_identity(process.pid) is not None:
                    raise error('service_identity_mismatch', 409)
            self.repository._load_candidate()  # Pure preflight before P1 persists policy.
            with _prepare_profile(self.profile_dir, self.model_dir, discover_legacy=self.discover_legacy) as prepared:
                self.repository._preparation = prepared
                _windows.private_directory(self.repository.runtime_dir)
                self.generation = str(uuid.uuid4())
                process = _windows.process_identity(os.getpid())
                if process is None:
                    raise error('service_unavailable', 503)
                self._claim = dict(schema_version=1, phase='preparing', profile=str(self.profile_dir.resolve()),
                                   generation=self.generation, process=asdict(process))
                self._owns_claim = True
                with prepared.budget.reserve(16384, 'service_claim'):
                    _windows.atomic_json(self.claim_file, self._claim)
                self.repository._load_prepared(prepared)
                self.health.configure(self.repository.snapshot())
                # Source policy exists before PG/inference so direct owned roots
                # are rejected by the same early owner writer.
                self._source_policy = SourceRegistry(self.repository, None, owner_id=self.generation)
                from atom_search.paths import resource_path
                roots = (self.profile_dir, self.model_dir, resource_path(),
                    Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1])
                for path in roots:
                    self._source_policy.register_owned_exclusion(path, owner_id=self.generation)
                self.coordinator = MutationCoordinator(self.repository, quiesce=self._quiesce, apply_reserved=self._apply,
                    validate_sources=self._source_policy.validate_sources, resume=self._resume)
                self.repository.upgrade(self.coordinator)

    @property
    def owner_access(self):
        return AccessContext('owner', frozenset({'owner', 'search', 'fetch', 'status', 'settings_get'}), ())

    def facade(self, access: AccessContext):
        from .application import AtomSearchService
        if not isinstance(access, AccessContext):
            raise error('access_denied', 403)
        return AtomSearchService(self, access)

    def _quiesce(self, changes, timeout):
        if not any(change.path == 'sources' for change in changes):
            if self.scheduler is not None:
                self.scheduler.prepare_settings()
            return True
        deadline = time.monotonic() + timeout
        if self.scheduler is not None and not self.scheduler.quiesce(timeout):
            return False
        with self._condition:
            for cancel in self._cancel:
                cancel.set()
            while self._cancel:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def _resume(self):
        if self.scheduler is not None:
            try:
                self.repository.snapshot()  # Finish/rollback must be authoritative before admission.
            except ServiceError:
                return
            self.scheduler.resume()
            if self._context_queue_pending:
                self.scheduler.queue_context()
                self._context_queue_pending = False

    def _apply(self, snapshot, reservation=None):
        sources = snapshot.settings['sources']
        if self.registry is not None and sources != self._bound_sources:
            self.registry.apply(snapshot)
            from copy import deepcopy
            self._bound_sources = deepcopy(sources)
        if self.core is not None:
            from .projects import configure_project_context
            revision = configure_project_context(self.core, snapshot, reservation=reservation)
            if revision != self._context_revision:
                self._context_revision = revision
                self._context_queue_pending = True
        if self.scheduler is not None:
            self.scheduler.apply_settings(snapshot)
        self.health.configure(snapshot)
        if self.probes is not None:
            self.probes.request()

    @contextmanager
    def operation(self, cancel=None):
        with self._condition:
            if self._closing:
                raise error('service_stopping', 503, retryable=True)
            self._active += 1
            if cancel is not None:
                self._cancel.add(cancel)
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                if cancel is not None:
                    self._cancel.discard(cancel)
                self._condition.notify_all()

    def call(self, operation, timeout=5):
        """Deadline bounds the reply; admission/drain retain unfinished work."""
        if not self._slots.acquire(blocking=False):
            raise error('too_many_requests', 429, retryable=True)
        done = threading.Event()
        result = []
        def run():
            try:
                with self.operation():
                    result.append((True, operation()))
            except BaseException as failure:
                result.append((False, failure))
            finally:
                self._slots.release()
                done.set()
        threading.Thread(target=run, name='atom-service-operation', daemon=False).start()
        if not done.wait(max(0, timeout)):
            raise error('request_timeout', 504, retryable=True)
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def issue_credential(self, access):
        if self.credentials is None or self.attached:
            raise error('service_unavailable', 503)
        try:
            # The host guard serializes creators and keeps publication within
            # the owned write. Existing tokens never wait on admission/DPAPI.
            with self.repository.mutation():
                with self._credential_lock:
                    if len(self._access) >= 256:
                        raise error('too_many_requests', 429)
                with self.repository.budget.reserve(16384, 'service_credential'):
                    reference = self.credentials.create(self.generation)
                    secret = self.credentials.resolve(reference)
                    with self._credential_lock:
                        self._access[reference] = (secret, access)
                    return reference
        except _windows.WindowsError as failure:
            if str(failure) != 'mutex_timeout':
                raise
            raise error('service_unavailable', 503, retryable=True) from None

    def authenticate(self, secret):
        if type(secret) is not str or len(secret) > 256:
            raise error('authentication_required', 401)
        with self._credential_lock:
            for value, access in self._access.values():
                if hmac.compare_digest(value, secret):
                    return access
        raise error('authentication_required', 401)

    def credential(self, reference):
        with self._credential_lock:
            value = self._access.get(reference)
        if value is None:
            raise error('authentication_required', 401)
        return value

    def index_generation(self):
        with self.runtime.open_connection() as connection:
            return connection.execute("SELECT generation FROM atom_search.catalogs WHERE state='active'").fetchone()[0]

    def start(self) -> Path:
        from atom_search import search_runtime
        from atom_search.http.server import LocalHTTPServer
        from .ipc import OwnerIPC
        from .client import read_owner_state
        with self._start_lock:
            if self._closing:
                raise error('service_stopping', 503)
            if self.attached:
                self._attached_client.status()
                return self.endpoint_file
            try:
                with self.repository.mutation():
                    claim = self._assert_claim()
                    if self.core is not None:
                        return self.endpoint_file
                    if claim['phase'] != 'preparing':
                        raise error('service_identity_mismatch', 409)
                    if self.state_file.exists():
                        state, process = read_owner_state(self.endpoint_file)
                        if state['state'] == 'ready' and _windows.process_identity(process.pid) is not None:
                            raise error('service_identity_mismatch', 409)
                    self._startup_unknown = True
                    self.core = search_runtime.open_search_service(self.profile_dir, self.model_dir,
                                                                   discover_legacy=self.discover_legacy)
                    self._startup_unknown = False
                    self.runtime = self.core.runtime
                    self.repository.bind_runtime_budget(self.runtime.storage_budget)
                    self.registry = self._source_policy
                    self.registry.identities = self.core.public_ids
                    self.registry.activate()
                    self._bound_sources = self.repository.snapshot().settings['sources']
                    from .projects import configure_project_context
                    self._context_revision = configure_project_context(self.core, self.repository.snapshot())
                    self.installation_id = self.runtime.status().endpoint.installation_id
                    from .jobs import CatalogScheduler
                    self.scheduler = CatalogScheduler(self, automatic=self.automatic_catalog, autostart=False)
                    with self.repository.budget.reserve(65536, 'service_start'):
                        self.credentials = CredentialStore(self.repository.runtime_dir, 'service-v1')
                        self.credential_ref = self.issue_credential(self.owner_access)
                        self._server = LocalHTTPServer(self)
                        self.base_url = 'http://127.0.0.1:' + str(self._server.server_address[1])
                        self._ipc = OwnerIPC(self)
                        self._ipc.start()
                        identity = _windows.process_identity(os.getpid())
                        self._state_write_attempted = True
                        _windows.atomic_json(self.state_file, dict(schema_version=1, state='ready', profile=str(self.profile_dir.resolve()),
                            installation_id=self.installation_id, generation=self.generation,
                            credential_ref=self.credential_ref, process=asdict(identity)))
                        _windows.atomic_json(self.endpoint_file, self.descriptor())
                        self._published = True
                        _windows.atomic_json(self.claim_file, dict(self._assert_claim(), phase='ready'))
                self._server.start()
                self.scheduler.start()
                from .health import ProbeScheduler, database_probe, storage_probe
                self.probes = ProbeScheduler(self.health, {
                    'database': lambda deadline, stop: database_probe(self, deadline, stop),
                    'storage': lambda deadline, stop: storage_probe(self, deadline, stop)})
                self.probes.start()
                return self.endpoint_file
            except BaseException:
                # Startup has released the host guard before any worker drain.
                # RLock still serializes concurrent start/close and sticky errors.
                try:
                    self.close()
                except Exception:
                    pass
                raise

    def descriptor(self):
        return dict(schema_version=1, pid=os.getpid(), installation_id=self.installation_id,
                    generation=self.generation, base_url=self.base_url, api_version='1', credential_ref=self.credential_ref)

    def desktop_view(self):
        from .desktop import DesktopView, RemoteDesktopView
        from .client import ServiceClient
        self.start()
        return (RemoteDesktopView(ServiceClient(self.endpoint_file, self.credential_ref)) if self.attached
                else DesktopView(self))

    def close(self):
        with self._start_lock:
            return self._close()

    def _close(self):
        with self._condition:
            if self._closing:
                while not self._closed:
                    self._condition.wait()
                if self._close_error is not None:
                    raise self._close_error
                return
            self._closing = True
            self.health.stopping()
            for cancel in self._cancel:
                cancel.set()
        failure = None
        try:
            if not self.attached:
                if self._server is not None:
                    self._server.stop()
                if self._ipc is not None:
                    self._ipc.close()
                if self.probes is not None:
                    self.probes.close()
                if self.scheduler is not None:
                    self.scheduler.close()
                with self._condition:
                    while self._active:
                        self._condition.wait()
                if self.core is not None:
                    self.core.close()  # Accepted P1 close already owns Runtime.stop.
                if self._startup_unknown:
                    # P1 may have failed its own cleanup before returning a
                    # handle. Retain the live preparing claim, never free it.
                    raise error('service_unavailable', 503)
                if self._owns_claim:
                    with self.repository.mutation(), self.repository.budget.reserve(65536, 'service_close'):
                        claim = self._assert_claim()
                        if self._published or self._state_write_attempted and self.state_file.exists():
                            from .client import read_owner_state
                            state, process = read_owner_state(self.endpoint_file)
                            if (state['generation'] != self.generation or state['installation_id'] != self.installation_id
                                    or state['credential_ref'] != self.credential_ref
                                    or asdict(process) != claim['process']):
                                raise error('service_identity_mismatch', 409)
                            _windows.atomic_json(self.state_file, dict(state, state='stopped'))
                        if self._published and self.endpoint_file.exists():
                            _windows.assert_private(self.endpoint_file)
                            if json.loads(self.endpoint_file.read_bytes()).get('generation') == self.generation:
                                self.endpoint_file.unlink()
                        if self.credentials is not None:
                            for reference in self._access:
                                self.credentials.remove(reference)
                        _windows.atomic_json(self.claim_file, dict(claim, phase='stopped'))
        except BaseException as caught:
            failure = caught
            raise
        finally:
            with self._condition:
                self._closed, self._close_error = True, failure
                self._condition.notify_all()


def open_service_owner(profile_dir: Path, model_dir: Path, *, discover_legacy=False,
                       automatic_catalog=True) -> ServiceOwner:
    return ServiceOwner(profile_dir, model_dir, discover_legacy=discover_legacy, automatic_catalog=automatic_catalog)
