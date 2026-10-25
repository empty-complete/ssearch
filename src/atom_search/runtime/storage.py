"""Shared remaining-peak reservations. These are accounting, not OS disk locks."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
import time
from pathlib import Path
import uuid
from . import _windows


class StorageBudgetError(RuntimeError):
    pass


def _number(value):
    if type(value) is not int or value < 0:
        raise StorageBudgetError("invalid_storage_bytes")
    return value


class StorageBudget:
    def __init__(self, roots: tuple[Path, ...], *, quota_bytes: int, min_free_bytes: int,
                 ledger_dir: Path | None = None, read_only_files: tuple[Path, ...] = ()):
        self.quota_bytes, self.min_free_bytes = _number(quota_bytes), _number(min_free_bytes)
        if not roots:
            raise StorageBudgetError("missing_storage_roots")
        try:
            self.roots = tuple(sorted((_windows.reject_reparse(p).resolve() for p in roots), key=str))
            for index, root in enumerate(self.roots):
                for other in self.roots[index + 1:]:
                    if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                        raise StorageBudgetError("overlapping_roots")
            if len({_windows.volume_identity(p) for p in self.roots}) != 1:
                raise StorageBudgetError("multiple_volumes")
            for root in self.roots:
                _windows.assert_private(root if root.exists() else root.parent)
            self.read_only_files = tuple(sorted(
                (_windows.assert_read_only_file(p).resolve() for p in read_only_files), key=str))
            if len(set(self.read_only_files)) != len(self.read_only_files):
                raise StorageBudgetError("overlapping_roots")
            for path in self.read_only_files:
                if any(path == root or path.is_relative_to(root) or root.is_relative_to(path)
                       for root in self.roots):
                    raise StorageBudgetError("overlapping_roots")
                if _windows.volume_identity(path) != _windows.volume_identity(self.roots[0]):
                    raise StorageBudgetError("multiple_volumes")
            # All peers using these accounted roots share durable outstanding
            # reservations; policy/root changes cannot ignore active entries.
            self.parent = (_windows.reject_reparse(ledger_dir).resolve() if ledger_dir is not None else
                           Path(os.path.commonpath([str(p.parent) for p in self.roots])))
            # Host-domain bookkeeping can allocate ledger replacement files even
            # for a zero-byte operation. Legacy generic callers keep their API.
            self.bookkeeping = 64 * 1024 if ledger_dir is not None else 0
            _windows.assert_private(self.parent)
            self.ledger = self.parent / "storage-reservations.json"
            self.owner = _windows.process_identity(os.getpid())
            if self.owner is None:
                raise StorageBudgetError("reservation_owner_unknown")
        except _windows.WindowsError as error:
            raise StorageBudgetError(str(error)) from None

    @contextmanager
    def _locked(self):
        try:
            with _windows.mutex("storage:" + str(self.parent)):
                _windows.assert_private(self.parent)
                if self.ledger.exists():
                    _windows.assert_private(self.ledger)
                    records = json.loads(self.ledger.read_text(encoding="utf-8"))
                    if not isinstance(records, list):
                        raise StorageBudgetError("reservation_ledger_invalid")
                else:
                    records = []
                alive = []
                for record in records:
                    owner = _windows.ProcessIdentity(**record["owner"])
                    actual = _windows.process_identity(owner.pid)
                    if actual == owner:
                        if (record["roots"] != [str(p) for p in self.roots]
                                or record.get("read_only_files", []) != [str(p) for p in self.read_only_files]
                                or record["policy"] != [self.quota_bytes, self.min_free_bytes]):
                            raise StorageBudgetError("reservation_policy_conflict")
                        _number(record["remaining"])
                        alive.append(record)
                yield alive
                _windows.atomic_json(self.ledger, alive)
        except (OSError, ValueError, KeyError, TypeError, _windows.WindowsError) as error:
            raise StorageBudgetError("reservation_ledger_unavailable") from None

    def _check(self, records):
        bookkeeping = (max(self.bookkeeping, len(json.dumps(records).encode('utf-8')) * 2 + 8192)
                       if self.bookkeeping else 0)
        remaining = sum(item["remaining"] for item in records) + bookkeeping
        try:
            for path in self.read_only_files:
                _windows.assert_read_only_file(path)
                if _windows.volume_identity(path) != _windows.volume_identity(self.roots[0]):
                    raise StorageBudgetError("multiple_volumes")
            allocated = (_windows.allocated_bytes(self.roots, read_only_files=self.read_only_files)
                         if self.read_only_files else _windows.allocated_bytes(self.roots))
            if allocated + remaining > self.quota_bytes:
                raise StorageBudgetError("quota_exceeded")
            probe = self.roots[0] if self.roots[0].exists() else self.roots[0].parent
            if _windows.available_bytes(probe) - remaining < self.min_free_bytes:
                raise StorageBudgetError("free_space_reserve_exceeded")
        except _windows.WindowsError as error:
            # Preserve the Win32 boundary's fixed machine code before _locked
            # can misclassify an admission probe failure as ledger I/O failure.
            raise StorageBudgetError(str(error)) from None

    def reserve(self, extra_bytes: int, operation: str):
        extra_bytes = _number(extra_bytes)
        if not isinstance(operation, str) or not operation or len(operation) > 128:
            raise StorageBudgetError("invalid_storage_operation")
        identity = uuid.uuid4().hex
        with self._locked() as records:
            records.append({"id": identity, "owner": asdict(self.owner), "remaining": extra_bytes,
                            "operation": operation, "roots": [str(p) for p in self.roots],
                            "read_only_files": [str(p) for p in self.read_only_files],
                            "policy": [self.quota_bytes, self.min_free_bytes]})
            self._check(records)
        return StorageReservation(self, identity)

    def measure(self, *, deadline, allocation=None, stop=None):
        """Observe this exact domain/ledger without admission or ledger cleanup.

        An unidentifiable owner or an invalid/conflicting ledger is unknown,
        never an empty ledger. Dead entries are ignored but left untouched.
        """
        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise StorageBudgetError('storage_probe_timeout')
            return seconds
        try:
            with _windows.mutex('storage:' + str(self.parent), timeout_seconds=remaining()):
                _windows.assert_private(self.parent)
                if self.ledger.exists():
                    _windows.assert_private(self.ledger)
                    with self.ledger.open('rb') as stream:
                        data = stream.read(1048577)
                    if len(data) > 1048576:
                        raise StorageBudgetError('reservation_ledger_invalid')
                    records = json.loads(data)
                    if type(records) is not list:
                        raise StorageBudgetError('reservation_ledger_invalid')
                else:
                    records = []
                reserved = 0
                for record in records:
                    remaining()
                    owner = _windows.ProcessIdentity(**record['owner'])
                    # Validate every record, including dead owners: observing
                    # health must not reinterpret an unknown ledger format.
                    if (record['roots'] != [str(path) for path in self.roots]
                            or record.get('read_only_files', []) != [str(path) for path in self.read_only_files]
                            or record['policy'] != [self.quota_bytes, self.min_free_bytes]):
                        raise StorageBudgetError('reservation_policy_conflict')
                    amount = _number(record['remaining'])
                    if _windows.process_identity(owner.pid) == owner:
                        reserved += amount
            remaining()
            volume = _windows.volume_identity(self.roots[0])
            for root in self.roots:
                remaining()
                _windows.assert_private(root if root.exists() else root.parent)
                if _windows.volume_identity(root) != volume:
                    raise StorageBudgetError('multiple_volumes')
            for path in self.read_only_files:
                remaining()
                _windows.assert_read_only_file(path)
                if _windows.volume_identity(path) != volume:
                    raise StorageBudgetError('multiple_volumes')
            if allocation is None:
                values = {'physical_owned_bytes': _windows.allocated_bytes(
                    self.roots, read_only_files=self.read_only_files, deadline=deadline)}
            else:
                if allocation.roots != self.roots or allocation.read_only_files != self.read_only_files:
                    raise StorageBudgetError('storage_domain_conflict')
                values = allocation.advance(deadline=deadline - .05, stop=stop)
                if values is None:
                    return None
            physical = values['physical_owned_bytes']
            free = _windows.available_bytes(self.roots[0])
            remaining()
            return dict(values, reserved_bytes=reserved,
                        quota_bytes=self.quota_bytes, free_bytes=free,
                        maintenance_required=physical + reserved > self.quota_bytes or
                            free - reserved < self.min_free_bytes)
        except (_windows.WindowsError, OSError, ValueError, KeyError, TypeError):
            raise StorageBudgetError('storage_probe_unavailable') from None


class StorageReservation:
    def __init__(self, budget, identity):
        self.budget, self.identity, self.closed = budget, identity, False

    def __enter__(self):
        if self.closed:
            raise StorageBudgetError("reservation_closed")
        return self

    def ensure_remaining(self, required_bytes: int) -> None:
        required_bytes = _number(required_bytes)
        if self.closed:
            raise StorageBudgetError("reservation_closed")
        with self.budget._locked() as records:
            record = next((item for item in records if item["id"] == self.identity), None)
            if record is None:
                raise StorageBudgetError("reservation_missing")
            record["remaining"] = required_bytes
            self.budget._check(records)

    def __exit__(self, *_):
        if not self.closed:
            with self.budget._locked() as records:
                records[:] = [item for item in records if item["id"] != self.identity]
            self.closed = True
