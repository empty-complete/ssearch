"""Windows host composition. Core storage ports never own a process or quota.

Lock order: host lifecycle mutex -> short budget admission -> SQL adapter locks.
The storage mutex is released before SQL. Every updated host writer uses the
same lifecycle guard; uncoordinated maintenance clients are outside admission.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import uuid

from atom_search_core import SearchService
from atom_search_core.models import EmbeddingModel
from atom_search_core.storage.legacy_import import (
    activate_catalog, snapshot_legacy_database, stage_legacy_catalog)
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.public_ids import PostgresPublicIds
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.postgres_retrieval import PostgresRetrieval

from .paths import resource_path
from .runtime import _windows
from .runtime.postgres import PostgresRuntime, PostgresRuntimeError, _storage_policy
from .runtime.storage import StorageBudget

_MIB = 1024 * 1024


def _read(path):
    _windows.assert_private(path)
    return json.loads(path.read_text(encoding='utf-8'))


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


class _Owner:
    def __init__(self, runtime, state):
        self.runtime, self.state = runtime, state
        self.path = runtime.profile_dir / 'lifecycle.json'
        self.blocked = None

    @contextmanager
    def guard(self):
        with _windows.mutex('search-host:' + str(self.runtime.profile_dir.resolve()),
                            timeout_seconds=300):
            self.runtime._assert_owner()
            self.state = _read(self.path)
            yield

    def save(self):
        _windows.atomic_json(self.path, self.state)

    def unused(self):
        # Close this preflight transaction before reserve() or activation.
        with self.runtime.open_connection(role='maintenance') as connection:
            return connection.execute("""SELECT generation=0
                AND NOT EXISTS(SELECT 1 FROM atom_search.sources WHERE catalog_id=c.id)
                AND NOT EXISTS(SELECT 1 FROM atom_search.legacy_imports WHERE state='active')
                FROM atom_search.catalogs c WHERE state='active'""").fetchone() == (True,)

    def import_legacy(self, *, verified_manifest):
        if self.state['used'] or self.state['source'] is None:
            return
        if not self.unused():
            with self.runtime.storage_budget.reserve(_MIB, 'legacy_already_used'):
                self.state['used'] = True
                self.save()
            return
        target = self.state['target_manifest']
        if target is not None and (verified_manifest is None or
                _manifest_digest(target) != _manifest_digest(verified_manifest)):
            # Pending import may only reuse its original verified target. A
            # completed/previously used catalog remains searchable without assets.
            raise PostgresRuntimeError('legacy_target_assets_unverified')
        source = Path(self.state['source'])
        directory = self.runtime.profile_dir / 'legacy-import'
        snapshot = directory / 'snapshot.sqlite3'
        digest = self.state.get('snapshot_digest')
        if digest:
            _windows.assert_private(snapshot)
            if _digest(snapshot) != digest:
                raise PostgresRuntimeError('legacy_snapshot_changed')
            size = snapshot.stat().st_size
        else:
            _windows.assert_read_only_file(source)
            size = sum(p.stat().st_size for p in self.runtime.storage_budget.read_only_files if p.exists())
            if not source.is_file():
                raise PostgresRuntimeError('legacy_source_missing')
        # Snapshot, staging indexes, WAL and retained backup (the same snapshot).
        # Core callbacks subsequently express future peak, never bytes written.
        with self.runtime.storage_budget.reserve(64 * _MIB + size * 24, 'legacy_import') as reservation:
            _windows.private_directory(directory)
            if digest is None:
                if snapshot.exists():
                    # An interrupted publication is retained for diagnosis. Never
                    # overwrite a snapshot whose completed identity is unknown.
                    raise PostgresRuntimeError('legacy_snapshot_uncommitted')
                digest = snapshot_legacy_database(source, snapshot)
                self.state['snapshot_digest'] = digest
                self.save()
            admission_error = None

            def remaining(required):
                nonlocal admission_error
                try:
                    reservation.ensure_remaining(required + 32 * _MIB)
                except Exception as error:
                    admission_error = error
                    raise

            report = stage_legacy_catalog(snapshot,
                lambda: self.runtime.open_connection(role='maintenance'),
                model_manifest=self.state['target_manifest'], legacy_model_manifest=None,
                reserve_bytes=remaining)
            if admission_error is not None:
                raise admission_error
            if report.status not in ('staged', 'active'):
                raise PostgresRuntimeError(report.error_code or 'legacy_import_failed')
            if not self.unused():
                raise PostgresRuntimeError('legacy_activation_conflict')
            reservation.ensure_remaining(32 * _MIB)
            activate_catalog(report.catalog_id, lambda: self.runtime.open_connection(role='maintenance'))
            self.state.update(used=True, imported_catalog_id=report.catalog_id)
            self.save()


class _AdmittedCatalog:
    """Retain admission until each existing adapter transaction has ended."""
    backend = 'postgresql'

    def __init__(self, catalog, owner):
        self._catalog, self._owner = catalog, owner

    def __getattr__(self, name):
        return getattr(self._catalog, name)

    def _existing_bytes(self, root_id=None):
        # Conservatively include every potentially affected descendant and its
        # embeddings, even for a deletion with no incoming rows. Transaction ends
        # before admission. The host guard prevents updated writers racing it.
        with self._owner.runtime.open_connection() as connection:
            row = connection.execute("""SELECT
                COALESCE(sum(pg_column_size(f)),0), count(*)
                FROM atom_search.files f JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND (%s::bigint IS NULL OR s.id=%s)""", (root_id, root_id)).fetchone()
            vectors = connection.execute("""SELECT COALESCE(sum(pg_column_size(e)),0)
                FROM atom_search.file_embeddings e JOIN atom_search.files f ON f.id=e.file_id
                JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND (%s::bigint IS NULL OR s.id=%s)""", (root_id, root_id)).fetchone()[0]
            directories = connection.execute("""SELECT COALESCE(sum(pg_column_size(d)),0), count(*)
                FROM atom_search.directories d JOIN atom_search.sources s ON s.id=d.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND (%s::bigint IS NULL OR s.id=%s)""", (root_id, root_id)).fetchone()
            contexts = 0
            if root_id is not None:  # Reconcile may cascade-delete full raw contexts and their FTS indexes.
                contexts = connection.execute('''SELECT COALESCE(sum(pg_column_size(pc)),0)
                    FROM atom_search.file_project_context pc JOIN atom_search.files f ON f.id=pc.file_id
                    WHERE f.source_id=%s''', (root_id,)).fetchone()[0]
            return (int(row[0]) + int(vectors) + int(directories[0]) + int(contexts)
                    + (int(row[1]) + int(directories[1])) * 8192)

    def _write(self, kind, operation, estimate, *, used=False):
        with self._owner.guard():
            try:
                required = estimate() if callable(estimate) else estimate
                with self._owner.runtime.storage_budget.reserve(required, kind):
                    if used and not self._owner.state['used']:
                        self._owner.state['used'] = True
                        self._owner.save()
                    result = operation()
                self._owner.blocked = None
                return result
            except Exception as error:
                self._owner.blocked = type(error).__name__
                raise

    def begin_root(self, root):
        return self._write('begin_root', lambda: self._catalog.begin_root(root),
                           2 * _MIB + len(root.encode('utf-8')) * 32, used=True)

    def finish_root(self, root_id, status, message=''):
        return self._write('finish_root', lambda: self._catalog.finish_root(root_id, status, message),
                           2 * _MIB + len(message.encode('utf-8')) * 32)

    def reconcile(self, root_id, snapshot):
        incoming = len(repr(snapshot).encode('utf-8')) + len(snapshot.files) * 8192
        return self._write('reconcile', lambda: self._catalog.reconcile(root_id, snapshot),
                           lambda: 32 * _MIB + 32 * (incoming + self._existing_bytes(root_id)))

    def save_embeddings(self, records, model_key):
        records = tuple(records)
        incoming = len(repr(records).encode('utf-8')) + len(records) * 16384
        return self._write('embeddings', lambda: self._catalog.save_embeddings(records, model_key),
                           lambda: 32 * _MIB + 32 * (incoming + self._existing_bytes()))

    def status(self, *, scope):
        result = self._catalog.status(scope=scope)
        result['storage_blocked'] = self._owner.blocked is not None
        return result


class _AdmittedPublicIds:
    """Source bindings use the same whole-engine admission and host guard."""
    def __init__(self, catalog, identities):
        self._catalog, self._identities = catalog, identities

    def __getattr__(self, name):
        return getattr(self._identities, name)

    def bind_many(self, sources):
        sources = tuple(sources)
        return self._catalog._write('source_binding', lambda: self._identities.bind_many(sources),
            2 * _MIB + 8192 * len(sources), used=True)


class _AdmittedProjection:
    """Context writes share the existing host guard and whole-engine budget."""
    def __init__(self, projection, catalog):
        self._projection, self._catalog = projection, catalog

    def __getattr__(self, name):
        return getattr(self._projection, name)

    def _existing_bytes(self, file_ids=None):
        with self._catalog._owner.runtime.open_connection() as connection:
            return connection.execute('''SELECT COALESCE(sum(pg_column_size(pc)),0)
                FROM atom_search.file_project_context pc
                WHERE %s::bigint[] IS NULL OR pc.file_id=ANY(%s::bigint[])''',
                (file_ids, file_ids)).fetchone()[0]

    def sync(self, projects, aliases, *, expected_settings_revision, source_labels=None,
             settings_reservation=None):
        if settings_reservation is not None:
            # The coordinator already reserved the bounded settings/journal peak.
            # Its 3 MiB also covers a single state-row/WAL revision acknowledgement;
            # reusing it avoids adding a second quota demand to an unchanged apply.
            # A changed configuration may never take this path, including a race
            # from an uncoordinated writer after the read-only digest preflight.
            owner = self._catalog._owner
            with owner.guard():
                if settings_reservation.budget is not owner.runtime.storage_budget:
                    raise ValueError('context_reservation_domain_mismatch')
                if self._projection.configuration_matches(projects, aliases, source_labels=source_labels):
                    from .settings.repository import MAX_JOURNAL_BYTES
                    try:
                        settings_reservation.ensure_remaining(3 * MAX_JOURNAL_BYTES)
                        result = self._projection.sync(projects, aliases,
                            expected_settings_revision=expected_settings_revision,
                            source_labels=source_labels, unchanged_only=True)
                        owner.blocked = None
                        return result
                    except Exception as error:
                        owner.blocked = type(error).__name__
                        raise
        incoming = len(repr((projects, aliases, source_labels)).encode('utf-8'))
        def estimate():
            # Settings replace only their configuration/bindings. Existing file
            # contexts are reserved separately when a bounded backfill rewrites them.
            with self._catalog._owner.runtime.open_connection() as connection:
                existing = connection.execute('''SELECT
                    (SELECT COALESCE(sum(pg_column_size(t)),0) FROM atom_search.project_context_state t) +
                    (SELECT COALESCE(sum(pg_column_size(t)),0) FROM atom_search.projects t) +
                    (SELECT COALESCE(sum(pg_column_size(t)),0) FROM atom_search.project_bindings t)''').fetchone()[0]
            return 64 * _MIB + 32 * (incoming + existing)
        return self._catalog._write('project_context_settings', lambda: self._projection.sync(
            projects, aliases, expected_settings_revision=expected_settings_revision,
            source_labels=source_labels), estimate, used=True)

    def publish_context(self, context, *, expected_file_revision, prepared):
        return bool(self.publish_batch(((context, expected_file_revision, prepared),)))

    def publish_batch(self, records, *, cancel=None, publish_if=None):
        records = tuple(records)
        if len(records) > 1000:
            raise ValueError('context_batch_exceeds_1000')
        incoming = len(repr(records).encode('utf-8')) + len(records) * 8192
        identities = [context.file_id for context, _, _ in records]
        return self._catalog._write('project_context_backfill', lambda: self._projection.publish_batch(
            records, cancel=cancel, publish_if=publish_if),
            lambda: 32 * _MIB + 32 * (incoming + self._existing_bytes(identities)))


def _legacy_settings(path):
    _windows.assert_read_only_file(path)
    if path.stat().st_size > 65536:
        raise PostgresRuntimeError('legacy_settings_invalid')
    with path.open('rb') as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise PostgresRuntimeError('legacy_settings_invalid')
    value = json.loads(content)
    if (not isinstance(value, dict)
            or 'schema_version' in value
            or 'root' in value and (not isinstance(value['root'], str) or not value['root'].strip())
            or type(value.get('refresh_days', 1)) is not int
            or not 1 <= value.get('refresh_days', 1) <= 365):
        raise PostgresRuntimeError('legacy_settings_invalid')
    for field in ('last_attempt', 'last_refresh'):
        item = value.get(field, 0)
        if type(item) not in (int, float) or not math.isfinite(item) or item < 0:
            raise PostgresRuntimeError('legacy_settings_invalid')
    # Preserve only established desktop/storage settings; no executable content.
    return {key: value[key] for key in ('root', 'refresh_days', 'last_attempt', 'last_refresh', 'storage') if key in value}


@dataclass(frozen=True)
class ProfilePreparation:
    """The already admitted P1 host domain, also usable before PG starts.

    Four-value unpacking remains compatible with the accepted factory callers.
    Settings writers share this exact budget/ledger, never a second quota.
    """
    engine: Path
    state: dict
    files: tuple
    manifest: dict | None
    budget: StorageBudget

    def __iter__(self):
        yield self.engine
        yield self.state
        yield self.files
        yield self.manifest


@contextmanager
def _prepare_profile(profile_dir: Path, model_dir: Path, *, discover_legacy=False):
    """One host boundary for early settings and factory preparation, without inference.

    Holds the lifecycle guard through the caller's initialization. Verified
    assets establish provenance; inability to load inference does not change it.
    """
    profile = _windows.reject_reparse(Path(profile_dir).absolute())
    profile.mkdir(parents=True, exist_ok=True)
    if (profile / 'postgresql').exists():
        raise PostgresRuntimeError('legacy_postgres_layout_conflict')
    engine = profile / 'engine'
    with _windows.mutex('search-host:' + str(engine.resolve()), timeout_seconds=300):
        engine = _windows.private_directory(engine)
        ledger = _windows.private_directory(engine / 'postgresql')
        state_path = engine / 'lifecycle.json'
        manifest = None
        try:
            from atom_search_core.embeddings import _manifest_and_assets
            manifest = _manifest_and_assets(Path(model_dir).resolve())
        except Exception:
            # Initial migration explicitly retains None, never guessed model
            # provenance. Ordinary publication may configure a model later.
            manifest = None
        if state_path.exists():
            state = _read(state_path)
            if (state.get('version') != 1 or type(state.get('used')) is not bool
                    or state.get('source') is not None and not isinstance(state['source'], str)
                    or 'target_manifest' not in state):
                raise PostgresRuntimeError('host_lifecycle_invalid')
            source = Path(state['source']) if state['source'] else None
            quota, free = _storage_policy(engine)
        else:
            if any(p.name not in {'postgresql'} for p in engine.iterdir()) or any(ledger.iterdir()):
                raise PostgresRuntimeError('unknown_engine_state')
            source = profile / 'catalog.sqlite3'
            settings = profile
            if not source.exists() and discover_legacy:
                candidate = profile.parent / 'KaizenSearch/catalog.sqlite3'
                if candidate.exists():
                    source, settings = candidate, candidate.parent
                elif (candidate.parent / 'settings.json').exists():
                    settings = candidate.parent
            source = source.absolute() if source.exists() else None
            if settings != profile and (settings / 'settings.json').exists():
                _legacy_settings(settings / 'settings.json')
            atom_settings = profile / 'settings.json'
            if atom_settings.exists():
                _windows.assert_read_only_file(atom_settings)
                try:
                    if atom_settings.stat().st_size > 65536:
                        raise ValueError()
                    with atom_settings.open('rb') as stream:
                        content = stream.read(65537)
                    if len(content) > 65536:
                        raise ValueError()
                    value = json.loads(content.decode('utf-8'))
                    if not isinstance(value, dict):
                        raise ValueError()
                except (OSError, ValueError):
                    raise PostgresRuntimeError('storage_policy_invalid') from None
                # An explicit object (even {}) belongs to the current profile.
                # Do not merge it with the selected legacy profile's policy.
                if 'storage' in value:
                    settings = profile
            quota, free = _storage_policy(settings)
            state = {'version': 1, 'source': str(source) if source else None,
                     'target_manifest': manifest, 'used': False, 'snapshot_digest': None}
        files = ((source, Path(str(source) + '-wal'), Path(str(source) + '-shm')) if source else ())
        budget = StorageBudget((engine,), ledger_dir=ledger, read_only_files=files,
                               quota_bytes=quota, min_free_bytes=free)
        with budget.reserve(_MIB, 'host_initialization'):
            if not state_path.exists():
                _windows.atomic_json(engine / 'settings.json', {'storage': {
                    'quota_bytes': quota, 'min_free_bytes': free}})
                _windows.atomic_json(state_path, state)
        destination = profile / 'settings.json'
        legacy_settings = profile.parent / 'KaizenSearch/settings.json'
        if (discover_legacy and not state['used'] and not destination.exists()
                and all(p.name == 'engine' for p in profile.iterdir()) and legacy_settings.exists()
                and (state['source'] is None or Path(state['source']).parent == legacy_settings.parent)):
            value = _legacy_settings(legacy_settings)
            with budget.reserve(_MIB, 'legacy_desktop_settings'):
                temporary = engine / ('settings-migration-' + uuid.uuid4().hex + '.json')
                try:
                    _windows.atomic_json(temporary, value)
                    _windows.reject_reparse(destination)
                    # Windows rename refuses an existing destination, preserving a
                    # setting written by another actor after our absence check.
                    try:
                        temporary.rename(destination)
                    except FileExistsError:
                        pass
                finally:
                    if temporary.exists():
                        _windows.assert_private(temporary)
                        temporary.unlink()
        yield ProfilePreparation(engine, state, files, manifest, budget)


def _load_model(model_dir, manifest, *, preparer=None):
    """Bind a session to verified installed assets; do not retry model loading.

    Installed assets are expected to remain immutable. This bounded check also
    refuses ordinary changes during construction; it is not an asset-lock/ABA
    defence against a hostile concurrent installer.
    """
    embedder = None
    if manifest is not None:
        try:
            from atom_search_core.embeddings import OnnxEmbedder
            embedder = OnnxEmbedder(model_dir, preparer=preparer)
        except Exception:
            pass
        try:
            from atom_search_core.embeddings import _manifest_and_assets
            current = _manifest_and_assets(Path(model_dir).resolve())
        except Exception:
            return None, None
        if (_manifest_digest(current) != _manifest_digest(manifest)
                or embedder is not None and embedder.model_id != current['model_id']):
            embedder = None
        manifest = current
    return embedder, manifest


def _catalog_initialization_bytes(maintenance):
    """Conservative mapping/context DDL heap/index/WAL peak, across every catalog.

    Called under the existing host guard. This is an engineering allowance,
    not a physical-size forecast; a large catalog must not silently cap it.
    """
    with maintenance() as connection:
        if connection.execute("SELECT to_regclass('atom_search.files')").fetchone()[0] is None:
            return 64 * _MIB
        version = connection.execute('SELECT COALESCE(max(version),0) FROM atom_search.schema_migrations').fetchone()[0]
        if version >= 4:
            return 64 * _MIB
        count = connection.execute('SELECT count(*) FROM atom_search.files').fetchone()[0]
        return 64 * _MIB + count * 4096


def open_search_service(profile_dir: Path, model_dir: Path, *, discover_legacy=False) -> SearchService:
    """Open one host-owned PG service; never create a fallback SQLite catalog."""
    runtime = None
    with _prepare_profile(profile_dir, model_dir, discover_legacy=discover_legacy) as prepared:
        engine, state, files, manifest = prepared
        try:
            from atom_search_core.project_context import INPUT_SCHEMA
            from atom_search_core.storage.project_projection import ProjectProjection
            try:
                from atom_search_core.tokenization import PinnedTokenizer
                preparer = PinnedTokenizer(model_dir)
            except (ValueError, OSError, ImportError, RuntimeError):
                preparer = None
            embedder, manifest = _load_model(model_dir, manifest, preparer=preparer)
            runtime = PostgresRuntime(resource_path('postgresql'), engine,
                                      accounting_roots=(engine,), read_only_files=files)
            runtime.start()
            owner = _Owner(runtime, state)
            maintenance = lambda: runtime.open_connection(role='maintenance')
            application = lambda: runtime.open_connection()
            with runtime.storage_budget.reserve(_catalog_initialization_bytes(maintenance), 'catalog_initialization'):
                apply_catalog_migrations(maintenance)
            owner.import_legacy(verified_manifest=manifest)
            model = None
            if manifest is not None:
                model = EmbeddingModel(manifest['model_id'] + '|' + INPUT_SCHEMA, 384,
                    INPUT_SCHEMA, _manifest_digest(manifest))
            with runtime.storage_budget.reserve(2 * _MIB, 'model_registration'):
                catalog = PostgresCatalog(application, model=model)
            admitted = _AdmittedCatalog(catalog, owner)
            projection = _AdmittedProjection(ProjectProjection(application), admitted)
            service = SearchService(catalog=admitted, projection=projection, preparer=preparer,
                input_schema=INPUT_SCHEMA, retrieval=PostgresRetrieval(application),
                embedder=embedder, on_close=runtime.stop)
            service.runtime = runtime
            service.public_ids = _AdmittedPublicIds(service.catalog, PostgresPublicIds(catalog))
            return service
        except BaseException:
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass  # Preserve the failing startup boundary, retain uncertain artifacts.
            raise
