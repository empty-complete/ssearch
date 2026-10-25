"""One guarded owner settings file with bounded backups and recovery journal.

The journal stores one operation only. A crash before component acknowledgement
rolls back at next load, with a new revision if the candidate reached disk.
Unknown/corrupt files are retained and rejected instead of silently defaulting.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import uuid

from atom_search.paths import model_path
from atom_search.runtime import _windows
from atom_search.service.contracts import SettingsSnapshot
from .atomic import atomic_create_bytes, atomic_write_json
from .migration import default_settings, legacy_runtime_state, migrate_legacy_settings, migrate_settings_v1, migrate_settings_v2
from .schema import MAX_SETTINGS_BYTES, settings_error, validate_settings

MAX_JOURNAL_BYTES = 1048576
MUTABLE_FIELDS = ('sources', 'indexing.interval_seconds', 'ui', 'updates', 'onboarding',
                  'search.default_mode', 'projects', 'search.project_aliases',
                  'agent.repair_policy', 'diagnostics')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _read_json(path, limit):
    _windows.assert_read_only_file(path)
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise settings_error('settings_too_large' if limit == MAX_SETTINGS_BYTES else 'settings_journal_invalid')
    try:
        value = json.loads(data, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise settings_error('settings_invalid') from None
    if type(value) is not dict:
        raise settings_error('settings_invalid')
    return value, data


class SettingsRepository:
    def __init__(self, profile_dir: Path, *, model_dir: Path | None = None):
        self.profile_dir = Path(profile_dir).absolute()
        self.model_dir = Path(model_dir) if model_dir is not None else model_path()
        self.path = self.profile_dir / 'settings.json'
        self.engine = self.profile_dir / 'engine'
        self.runtime_dir = self.engine / 'runtime'
        self.backup_dir = self.engine / 'settings-backups'
        self.journal_path = self.runtime_dir / 'settings-operation.json'
        self._snapshot = None
        self._preparation = None
        self._runtime_budget = None
        self._pending = False
        self._mutation_lock = threading.Lock()

    def bind_runtime_budget(self, budget):
        """Composition may replace the early handle with the active P1 handle.

        Both must describe the same whole-engine roots, ledger and policy.
        This never constructs a budget or changes a storage domain.
        """
        expected = self._preparation.budget
        if (budget.roots != expected.roots or budget.ledger != expected.ledger
                or budget.read_only_files != expected.read_only_files
                or (budget.quota_bytes, budget.min_free_bytes) !=
                   (expected.quota_bytes, expected.min_free_bytes)):
            raise settings_error('settings_immutable')
        self._runtime_budget = budget

    @property
    def budget(self):
        return self._runtime_budget or self._preparation.budget

    def snapshot(self):
        if self._snapshot is None or self._pending:
            raise settings_error('settings_recovery_required')
        return deepcopy(self._snapshot)

    def _publish(self, value):
        self._snapshot = SettingsSnapshot(value['schema_version'], value['revision'], deepcopy(value), MUTABLE_FIELDS)
        return deepcopy(self._snapshot)

    @contextmanager
    def mutation(self):
        if self._preparation is None:
            raise settings_error('settings_recovery_required')
        with _windows.mutex('search-host:' + str(self.engine.resolve()), timeout_seconds=5):
            _windows.assert_private(self.engine)
            yield

    def _reserve(self):
        # Peak includes old/new settings, journal replacement, two retained
        # bounded backups and runtime state, plus a current-context revision
        # acknowledgement (unchanged JSON only). All share this P1 reservation.
        return self.budget.reserve(3 * MAX_JOURNAL_BYTES, 'owner_settings')

    def _load_candidate(self):
        value, original = _read_json(self.path, MAX_SETTINGS_BYTES) if self.path.exists() else (None, None)
        legacy = value is not None and 'schema_version' not in value
        if value is not None:
            if legacy:
                candidate = migrate_legacy_settings(value, source_id=str(uuid.uuid4()))
            elif type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2, 3):
                raise settings_error('settings_schema_unsupported')
            else:
                candidate = validate_settings(value, allow_legacy=True)
        else:
            candidate = default_settings()
        return value, original, candidate, legacy

    def load(self) -> SettingsSnapshot:
        # Reject known-invalid input before P1 can persist a domain policy.
        # This preflight cannot authorize a later overwrite: another startup
        # may migrate/create the settings while we are waiting for its guard.
        self._load_candidate()
        from atom_search.search_runtime import _prepare_profile
        # Use P1's existing early preparation, before creating any new subtree.
        # It verifies provenance only; this does not construct PG or inference.
        with _prepare_profile(self.profile_dir, self.model_dir) as prepared:
            self._load_prepared(prepared)
            from atom_search.service.mutations import MutationCoordinator
            return self.upgrade(MutationCoordinator(self))

    def upgrade(self, coordinator):
        """Called under startup preparation, after original-schema recovery."""
        current = self.snapshot()
        if current.schema_version == 1:
            return coordinator.owner_apply(migrate_settings_v1(current.settings), current.revision)
        if current.schema_version == 2:
            return coordinator.owner_apply(migrate_settings_v2(current.settings), current.revision)
        return current

    def _load_prepared(self, prepared):
        """Composition hook: caller owns the existing P1 host guard/preparation."""
        self._preparation = prepared
        # The existing host guard makes this the authoritative decision,
        # for both legacy migration and an initially absent settings file.
        value, original, candidate, legacy = self._load_candidate()
        _windows.private_directory(self.runtime_dir)
        _windows.private_directory(self.backup_dir)
        policy = {'quota_bytes': prepared.budget.quota_bytes,
                  'min_free_bytes': prepared.budget.min_free_bytes}
        if not legacy and value is not None and candidate['storage'] != policy:
            raise settings_error('settings_immutable')
        candidate['storage'] = policy
        if self.journal_path.exists():
            self._recover()
            value, _ = _read_json(self.path, MAX_SETTINGS_BYTES)
            return self._publish(validate_settings(value, allow_legacy=True))
        if original is None or legacy:
            with self._reserve():
                if legacy:
                    backup = self.backup_dir / 'legacy.json'
                    if backup.exists():
                        _windows.assert_private(backup)
                        if backup.read_bytes() != original:
                            raise settings_error('settings_recovery_required')
                    else:
                        with backup.open('xb') as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                    atomic_write_json(self.runtime_dir / 'desktop-state.json',
                                      dict(legacy_runtime_state(value), source_path=candidate['sources'][0]['path'] if candidate['sources'] else ''))
                atomic_write_json(self.path, validate_settings(candidate))
        return self._publish(candidate)

    def _current(self):
        value, _ = _read_json(self.path, MAX_SETTINGS_BYTES)
        return validate_settings(value, allow_legacy=True)

    def _preserve_original_schema(self, before):
        # Keep the exact physical original, independently of normalized journal
        # values. An interrupted attempt may already have this bounded backup.
        version = before['schema_version']
        destination = self.backup_dir / f'schema{version}.json'
        if destination.exists():
            saved, _ = _read_json(destination, MAX_SETTINGS_BYTES)
            if validate_settings(saved, allow_legacy=True)['schema_version'] != version:
                raise settings_error('settings_recovery_required')
            return
        value, original = _read_json(self.path, MAX_SETTINGS_BYTES)
        if value != before:
            raise settings_error('settings_recovery_required')
        atomic_create_bytes(destination, original)

    def _check_revision(self, current, expected_revision):
        if type(expected_revision) is not int or current['revision'] != expected_revision:
            raise settings_error('revision_conflict', details={'revision': current['revision']}, status_code=409)

    def _journal(self, operation):
        if len(json.dumps(operation, ensure_ascii=False, allow_nan=False).encode()) > MAX_JOURNAL_BYTES:
            raise settings_error('settings_journal_invalid')
        atomic_write_json(self.journal_path, operation)

    def _commit(self, candidate, expected_revision):
        if self._pending:
            raise settings_error('settings_recovery_required')
        before = self._current()
        self._check_revision(before, expected_revision)
        candidate = validate_settings(candidate)
        if candidate['revision'] not in (expected_revision, expected_revision + 1):
            raise settings_error('revision_conflict', status_code=409)
        if candidate['storage'] != before['storage'] or candidate['database'] != before['database']:
            raise settings_error('settings_immutable')
        candidate['revision'] = expected_revision + 1
        candidate = validate_settings(candidate)
        # A committed component failure must still be able to persist the
        # original values at revision+2, including a decimal digit rollover.
        validate_settings(dict(before, revision=expected_revision + 2), allow_legacy=True)
        operation = {'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'state': 'prepared',
                     'before': before, 'candidate': candidate}
        if before['schema_version'] in (1, 2):
            self._preserve_original_schema(before)
        atomic_write_json(self.backup_dir / 'last-good.json', before)
        self._journal(operation)
        self._pending = True
        # A failed replace leaves the old revision and live snapshot untouched.
        try:
            atomic_write_json(self.path, candidate)
        except Exception:
            # It did not commit. Preserve the existing live revision and close
            # the prepared record; if that also fails, recovery stays required.
            self._rollback(operation)
            raise
        operation['state'] = 'committed'
        self._journal(operation)
        self._publish(candidate)
        return operation

    def _finish(self, operation):
        operation['state'] = 'applied'
        self._journal(operation)
        self._pending = False
        return self.snapshot()

    def _rollback(self, operation):
        current = self._current()
        before, candidate = operation['before'], operation['candidate']
        if current == before:
            restored = before
        elif current == candidate:
            restored = validate_settings(dict(before, revision=candidate['revision'] + 1), allow_legacy=True)
            atomic_write_json(self.path, restored)
        elif current == dict(before, revision=candidate['revision'] + 1):
            # The rollback replace reached disk before its journal ACK.
            restored = current
        else:
            raise settings_error('settings_recovery_required')
        operation['state'] = 'rolled_back'
        self._journal(operation)
        self._pending = False
        return self._publish(restored)

    def _recover(self):
        try:
            operation, _ = _read_json(self.journal_path, MAX_JOURNAL_BYTES)
            if (set(operation) != {'schema_version', 'operation_id', 'state', 'before', 'candidate'}
                    or type(operation['schema_version']) is not int or operation['schema_version'] != 1
                    or str(uuid.UUID(operation['operation_id'])) != operation['operation_id']
                    or operation['state'] not in {'prepared', 'committed', 'applied', 'rolled_back'}):
                raise ValueError()
            before = validate_settings(operation['before'], allow_legacy=True)
            candidate = validate_settings(operation['candidate'], allow_legacy=True)
            if (candidate['revision'] != before['revision'] + 1 or
                    candidate['schema_version'] < before['schema_version'] or
                    candidate['storage'] != before['storage'] or candidate['database'] != before['database']):
                raise ValueError()
        except Exception:
            raise settings_error('settings_journal_invalid') from None
        if operation['state'] in {'prepared', 'committed'}:
            with self._reserve():
                self._rollback(operation)
        else:
            self._pending = False

    def commit(self, candidate: dict, expected_revision: int) -> SettingsSnapshot:
        # A direct owner writer has the same completion/rollback semantics,
        # with no application component hooks to run.
        from atom_search.service.mutations import MutationCoordinator
        return MutationCoordinator(self).owner_apply(candidate, expected_revision)

    def runtime_state(self):
        path = self.runtime_dir / 'desktop-state.json'
        if not path.exists():
            return legacy_runtime_state({})
        value, _ = _read_json(path, MAX_SETTINGS_BYTES)
        if set(value) - {'schema_version', 'last_attempt', 'last_refresh', 'source_path'} or value.get('schema_version') != 1:
            raise settings_error('settings_invalid')
        result = legacy_runtime_state(value)
        if 'source_path' in value:
            if type(value['source_path']) is not str:
                raise settings_error('settings_invalid')
            result['source_path'] = value['source_path']
        return result

    def save_runtime_state(self, *, last_attempt, last_refresh, source_path):
        if type(source_path) is not str:
            raise settings_error('settings_invalid')
        value = dict(legacy_runtime_state({'last_attempt': last_attempt, 'last_refresh': last_refresh}),
                     source_path=source_path)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_SETTINGS_BYTES:
            raise settings_error('settings_too_large')
        with self.mutation(), self.budget.reserve(MAX_SETTINGS_BYTES * 2, 'desktop_runtime_state'):
            atomic_write_json(self.runtime_dir / 'desktop-state.json', value)
