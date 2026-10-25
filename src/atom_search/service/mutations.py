"""One application mutation coordinator. Reads/validation never take its lock."""
from __future__ import annotations

from copy import deepcopy

from .contracts import SettingChange, SettingsValidation
from atom_search.settings.schema import SettingsValidationError, settings_error, validate_settings


def _changes(before, after, path=''):
    if type(before) is dict and type(after) is dict:
        return tuple(change for key in sorted(before.keys() | after.keys()) if key != 'revision'
                     for change in _changes(before.get(key), after.get(key), f'{path}.{key}'.strip('.')))
    return () if before == after else (SettingChange(path, deepcopy(before), deepcopy(after)),)


class MutationCoordinator:
    def __init__(self, repository, *, quiesce=None, apply=None, apply_reserved=None,
                 validate_sources=None, resume=None):
        self.repository = repository
        self._quiesce = quiesce or (lambda changes, timeout: True)
        self._apply = apply or (lambda snapshot: None)
        self._apply_reserved = apply_reserved or (lambda snapshot, reservation: self._apply(snapshot))
        self._lock = repository._mutation_lock
        self._validate_sources = validate_sources or (lambda sources: ())
        self._resume = resume or (lambda: None)

    def validate(self, candidate: dict, expected_revision: int) -> SettingsValidation:
        current = self.repository.snapshot()
        self.repository._check_revision(current.settings, expected_revision)
        try:
            normalized = validate_settings(candidate)
        except SettingsValidationError as error:
            return SettingsValidation(False, expected_revision, None, (), (), error.errors)
        errors = self._validate_sources(normalized['sources'])
        if errors:
            return SettingsValidation(False, expected_revision, None, (), (), errors)
        if normalized['revision'] not in (expected_revision, expected_revision + 1):
            raise settings_error('revision_conflict', status_code=409)
        changes = _changes(current.settings, normalized)
        if any(change.path == 'storage' or change.path.startswith('storage.') or
               change.path == 'database' or change.path.startswith('database.') for change in changes):
            raise settings_error('settings_immutable')
        normalized['revision'] = expected_revision + 1
        normalized = validate_settings(normalized)
        validate_settings(dict(current.settings, revision=expected_revision + 2), allow_legacy=True)
        from .sources import classify_source_roots
        warnings = ('source_roots_overlap',) if classify_source_roots(tuple(normalized['sources'])) else ()
        return SettingsValidation(True, expected_revision, normalized, changes, warnings, ())

    def owner_apply(self, candidate: dict, expected_revision: int):
        validation = self.validate(candidate, expected_revision)
        if not validation.valid:
            raise SettingsValidationError(validation.errors)
        if not self._lock.acquire(timeout=5):
            raise settings_error('settings_busy', status_code=409)
        try:
            # Workers may need the host guard to finish their last transaction.
            # Never retain it while waiting for cooperative cancellation.
            if not self._quiesce(validation.diff, 5):
                raise settings_error('settings_busy', status_code=409)
            with self.repository.mutation():
                self.repository._check_revision(self.repository._current(), expected_revision)
                operation = None
                try:
                    with self.repository._reserve() as reservation:
                        operation = self.repository._commit(validation.normalized, expected_revision)
                        self._apply_reserved(self.repository._publish(operation['candidate']), reservation)
                        result = self.repository._finish(operation)
                    return result
                except Exception:
                    if operation is None:
                        raise  # No confirmed commit; disk failure or recovery gate.
                    try:
                        with self.repository._reserve() as reservation:
                            restored = self.repository._rollback(operation)
                            self._apply_reserved(restored, reservation)
                    except Exception:
                        self.repository._pending = True
                        raise settings_error('settings_recovery_required', status_code=500) from None
                    raise settings_error('settings_apply_failed', status_code=500) from None
        finally:
            try:
                self._resume()
            finally:
                self._lock.release()
