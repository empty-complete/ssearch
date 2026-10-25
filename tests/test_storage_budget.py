"""Remaining-peak accounting: removing checks would permit writes over policy."""
import pytest
import json
import os
import subprocess
import sys
from pathlib import Path
from atom_search.runtime.storage import StorageBudget, StorageBudgetError
from atom_search.runtime import _windows


@pytest.fixture
def root(tmp_path):
    parent = tmp_path / "private"
    _windows.private_directory(parent)
    child = parent / "data"
    _windows.private_directory(child)
    return child


def test_budget_rejects_overlapping_roots(root):
    with pytest.raises(StorageBudgetError, match="overlapping_roots"):
        StorageBudget((root, root / "pg_wal"), quota_bytes=1000, min_free_bytes=10)


def test_health_measurement_keeps_ledger_bytes_and_separates_reservations(root, monkeypatch):
    import time
    monkeypatch.setattr(_windows, 'allocated_bytes', lambda roots, **kwargs: 1140)
    monkeypatch.setattr(_windows, 'available_bytes', lambda path: 9000)
    budget = StorageBudget((root,), quota_bytes=3000, min_free_bytes=0)
    with budget.reserve(500, 'still-running'):
        before = budget.ledger.read_bytes()
        def forbidden(*args, **kwargs):
            raise AssertionError('health mutated ledger')
        with monkeypatch.context() as patch:
            patch.setattr(_windows, 'atomic_json', forbidden)
            measurement = budget.measure(deadline=time.monotonic() + 2)
        assert (measurement['physical_owned_bytes'], measurement['reserved_bytes']) == (1140, 500)
        assert budget.ledger.read_bytes() == before


def test_health_measurement_refuses_unknown_ledger_without_replacing_it(root, monkeypatch):
    import time
    budget = StorageBudget((root,), quota_bytes=3000, min_free_bytes=0)
    _windows.atomic_json(budget.ledger, {'unknown': 'preserve'})
    before = budget.ledger.read_bytes()
    with pytest.raises(StorageBudgetError):
        budget.measure(deadline=time.monotonic() + 2)
    assert budget.ledger.read_bytes() == before


def test_incremental_health_scan_only_publishes_complete_deduplicated_domain(root, monkeypatch):
    assert hasattr(_windows, 'AllocationScan'), 'bounded health needs a resumable allocation observation'
    pgdata = root / 'pgdata'
    wal = pgdata / 'pg_wal'
    wal.mkdir(parents=True)
    cache = root / 'cache'
    cache.mkdir()
    (wal / 'segment').write_bytes(b'w' * 10001)
    (pgdata / 'relation').write_bytes(b'r' * 10001)
    (cache / 'shared').hardlink_to(pgdata / 'relation')
    legacy = root.parent / 'legacy.sqlite'
    legacy.write_bytes(b'l' * 5001)
    expected = _windows.allocated_bytes((root,), read_only_files=(legacy,))
    expected_pg = _windows.allocated_bytes((pgdata,))
    clock = [0]
    original = _windows._file_allocation
    def step(path):
        clock[0] += 1
        return original(path)
    monkeypatch.setattr(_windows, '_file_allocation', step)
    scan = _windows.AllocationScan((root,), read_only_files=(legacy,),
        groups={'pgdata_bytes': (pgdata,), 'wal_bytes': (wal,), 'cache_bytes': (cache,)}, clock=lambda: clock[0])
    assert scan.advance(deadline=1) is None
    for _ in range(10):
        measured = scan.advance(deadline=clock[0] + 1)
        if measured is not None:
            break
    else:
        pytest.fail('bounded chunks never completed')
    assert measured['physical_owned_bytes'] == expected
    assert measured['pgdata_bytes'] == expected_pg
    assert measured['wal_bytes'] < measured['pgdata_bytes']
    assert measured['cache_bytes'] > 0


def test_incremental_health_scan_bounds_disappearance_retries_and_refuses_reparse(root, monkeypatch):
    import time
    assert hasattr(_windows, 'AllocationScan'), 'health must not retry a changing tree forever'
    (root / 'volatile').touch()
    def vanished(path):
        raise _windows._AllocationTreeChanged('allocation_probe_failed')
    scan = _windows.AllocationScan((root,))
    with monkeypatch.context() as patch:
        patch.setattr(_windows, '_file_allocation', vanished)
        assert scan.advance(deadline=time.monotonic() + 2) is None
        assert scan.advance(deadline=time.monotonic() + 2) is None
        with pytest.raises(_windows.WindowsError):
            scan.advance(deadline=time.monotonic() + 2)
    import _winapi
    outside = _windows.private_directory(root.parent / 'outside-health')
    (outside / 'valuable').write_bytes(b'x' * 1000)
    link = root / 'linked'
    _winapi.CreateJunction(str(outside), str(link))
    try:
        with pytest.raises(_windows.WindowsError, match='reparse'):
            _windows.AllocationScan((root,)).advance(deadline=time.monotonic() + 2)
    finally:
        link.rmdir()


def test_incremental_legacy_domain_allows_absent_optional_sidecars(root):
    import time
    legacy = root.parent / 'legacy-migrated.sqlite'
    legacy.write_bytes(b'l' * 10001)
    files = (legacy, Path(str(legacy) + '-wal'), Path(str(legacy) + '-shm'))
    budget = StorageBudget((root,), quota_bytes=1000000, min_free_bytes=0,
                           ledger_dir=root.parent, read_only_files=files)
    expected = _windows.allocated_bytes((root,), read_only_files=files)
    scan = _windows.AllocationScan(budget.roots, read_only_files=budget.read_only_files)
    for _ in range(3):
        observed = budget.measure(deadline=time.monotonic() + 2, allocation=scan)
        if observed is not None:
            break
    assert observed is not None and observed['physical_owned_bytes'] == expected
    assert observed['reserved_bytes'] == 0 and not budget.ledger.exists()


def test_reserve_refuses_peak_before_write(root, monkeypatch):
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: 400)
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 1000)
    a = StorageBudget((root,), quota_bytes=1000, min_free_bytes=100)
    b = StorageBudget((root,), quota_bytes=1000, min_free_bytes=100)
    with a.reserve(400, "import"):
        with pytest.raises(StorageBudgetError, match="quota_exceeded"):
            b.reserve(201, "update")
    with b.reserve(600, "update"):
        pass


def test_reservation_does_not_double_count_written_bytes(root, monkeypatch):
    used = [400]
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: used[0])
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 10000)
    budget = StorageBudget((root,), quota_bytes=1000, min_free_bytes=100)
    with budget.reserve(300, "import") as reservation:
        used[0] = 500
        reservation.ensure_remaining(200)
        with budget.reserve(250, "update"):
            with pytest.raises(StorageBudgetError, match="quota_exceeded"):
                reservation.ensure_remaining(251)
        reservation.ensure_remaining(500)


def test_reservation_releases_after_exception_and_preserves_files(root, monkeypatch):
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: 10)
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 1000)
    budget = StorageBudget((root,), quota_bytes=100, min_free_bytes=10)
    with pytest.raises(ValueError):
        with budget.reserve(90, "write"):
            (root / "keep").write_text("keep")
            raise ValueError("interrupted")
    with budget.reserve(90, "retry"):
        assert (root / "keep").read_text() == "keep"


def test_free_space_and_invalid_values_refuse_reservation(root, monkeypatch):
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: 0)
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 100)
    budget = StorageBudget((root,), quota_bytes=1000, min_free_bytes=30)
    with pytest.raises(StorageBudgetError, match="free_space"):
        budget.reserve(71, "write")
    for value in (-1, True, 1.5):
        with pytest.raises(StorageBudgetError):
            budget.reserve(value, "write")


@pytest.mark.parametrize("probe,code", [
    ("allocated_bytes", "allocation_probe_failed"),
    ("available_bytes", "disk_probe_failed"),
])
def test_admission_probe_failure_keeps_code_and_previous_reservation(root, monkeypatch, probe, code):
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: 0)
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 10000)
    budget = StorageBudget((root,), quota_bytes=1000, min_free_bytes=0)
    with budget.reserve(100, "existing") as reservation:
        original = getattr(_windows, probe)
        def refused(_):
            raise _windows.WindowsError(code)
        with monkeypatch.context() as patch:
            patch.setattr(_windows, probe, refused)
            with pytest.raises(StorageBudgetError, match="^" + code + "$"):
                reservation.ensure_remaining(200)
            assert json.loads(budget.ledger.read_text())[0]["remaining"] == 100
        assert getattr(_windows, probe) is original
        # A refused update must neither publish its larger value nor erase the
        # previous reservation: exact headroom succeeds and one byte over fails.
        with budget.reserve(900, "remaining_headroom"):
            with pytest.raises(StorageBudgetError, match="^quota_exceeded$"):
                budget.reserve(1, "must_not_admit")


def test_ledger_publication_failure_keeps_ledger_code_and_previous_reservation(root, monkeypatch):
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: 0)
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 10000)
    budget = StorageBudget((root,), quota_bytes=1000, min_free_bytes=0)
    with budget.reserve(100, "existing") as reservation:
        def refused(*_):
            raise _windows.WindowsError("acl_unavailable")
        with monkeypatch.context() as patch:
            patch.setattr(_windows, "atomic_json", refused)
            with pytest.raises(StorageBudgetError, match="^reservation_ledger_unavailable$"):
                reservation.ensure_remaining(200)
        assert json.loads(budget.ledger.read_text())[0]["remaining"] == 100
        with budget.reserve(900, "remaining_headroom"):
            with pytest.raises(StorageBudgetError, match="^quota_exceeded$"):
                budget.reserve(1, "must_not_admit")


def test_allocated_bytes_deduplicates_hardlinks_and_rejects_reparse(root):
    original = root / "one"
    original.write_bytes(b"x" * 10000)
    once = _windows.allocated_bytes((root,))
    (root / "two").hardlink_to(original)
    assert _windows.allocated_bytes((root,)) == once
    assert once >= 10000
    (root / "independent-copy").write_bytes(original.read_bytes())
    assert _windows.allocated_bytes((root,)) == 2 * once


def test_reservations_are_shared_with_another_process(root, monkeypatch):
    monkeypatch.setattr(_windows, "allocated_bytes", lambda roots: 0)
    monkeypatch.setattr(_windows, "available_bytes", lambda path: 100000)
    budget = StorageBudget((root,), quota_bytes=1000, min_free_bytes=0)
    script = (
        "from pathlib import Path; import sys; "
        "from atom_search.runtime.storage import StorageBudget,StorageBudgetError; "
        "budget=StorageBudget((Path(sys.argv[1]),),quota_bytes=1000,min_free_bytes=0)\n"
        "try:\n budget.reserve(201,'other')\n"
        "except StorageBudgetError as error:\n print(str(error)); sys.exit(0)\n"
        "sys.exit(9)\n")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    with budget.reserve(800, "parent"):
        result = subprocess.run([sys.executable, "-c", script, str(root)], env=env,
                                capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "quota_exceeded"


def test_allocation_rejects_junction_without_counting_external_data(root):
    import _winapi
    outside = root.parent / "outside"
    _windows.private_directory(outside)
    (outside / "valuable").write_bytes(b"x" * 1000)
    link = root / "junction"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        with pytest.raises(_windows.WindowsError, match="reparse"):
            _windows.allocated_bytes((root,))
    finally:
        link.rmdir()
    assert (outside / "valuable").stat().st_size == 1000


def test_refused_budget_does_not_create_missing_root(root):
    missing = root / "new-pgdata"
    budget = StorageBudget((missing,), quota_bytes=10, min_free_bytes=0)
    with pytest.raises(StorageBudgetError, match="quota_exceeded"):
        budget.reserve(11, "initdb")
    assert not missing.exists()


def test_native_ordinary_allocation_rejects_quota_between_eof_and_allocated(root):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    kernel = c.WinDLL("kernel32", use_last_error=True)
    sectors, per_sector, free, total = (w.DWORD() for _ in range(4))
    kernel.GetDiskFreeSpaceW.argtypes = [w.LPCWSTR, *[c.POINTER(w.DWORD)] * 4]
    assert kernel.GetDiskFreeSpaceW(str(root.anchor), c.byref(sectors), c.byref(per_sector),
                                   c.byref(free), c.byref(total))
    length = 2 * sectors.value * per_sector.value + 1
    path = root / "ordinary"
    with path.open("wb") as stream:
        stream.write(b"x" * length)
        stream.flush()
        os.fsync(stream.fileno())
    assert not path.stat().st_file_attributes & (0x200 | 0x800)
    class Standard(c.Structure):
        _fields_ = [("allocated", c.c_longlong), ("eof", c.c_longlong),
                    ("links", w.DWORD), ("pending", c.c_ubyte), ("directory", c.c_ubyte)]
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    with path.open("rb") as stream:
        native = Standard()
        assert query(msvcrt.get_osfhandle(stream.fileno()), 1, c.byref(native), c.sizeof(native))
    assert native.allocated > length
    budget = StorageBudget((root,), quota_bytes=(length + native.allocated) // 2, min_free_bytes=0)
    with pytest.raises(StorageBudgetError, match="quota_exceeded"):
        budget.reserve(0, "must-refuse-before-next-write")
    assert _windows.allocated_bytes((root,)) == native.allocated


@pytest.mark.parametrize("kind", ["sparse", "compressed"])
def test_native_special_file_counts_physical_storage_not_eof(root, kind):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    kernel = c.WinDLL("kernel32", use_last_error=True)
    ioctl = kernel.DeviceIoControl
    ioctl.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.c_void_p,
                     w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    path = root / kind
    with path.open("w+b") as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        returned = w.DWORD()
        if kind == "sparse":
            assert ioctl(handle, 0x900c4, None, 0, None, 0, c.byref(returned), None)
            stream.seek(5 * 1024 ** 3)
        else:
            method = w.WORD(1)
            assert ioctl(handle, 0x9c040, c.byref(method), c.sizeof(method),
                         None, 0, c.byref(returned), None)
        stream.write(b"x" * 131073)
        stream.flush()
        os.fsync(stream.fileno())
    assert path.stat().st_file_attributes & (0x200 if kind == "sparse" else 0x800)
    size = kernel.GetCompressedFileSizeW
    size.argtypes, size.restype = [w.LPCWSTR, c.POINTER(w.DWORD)], w.DWORD
    high = w.DWORD()
    c.set_last_error(0)
    low = size(str(path), c.byref(high))
    assert low != 0xffffffff or c.get_last_error() == 0
    physical = high.value << 32 | low
    assert 0 < physical < path.stat().st_size
    assert _windows.allocated_bytes((root,)) == physical


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("vanished")])
def test_allocation_enumeration_failure_never_admits_smaller_total(root, monkeypatch, error):
    def denied_walk(path, *, followlinks=False, onerror=None):
        if onerror:
            onerror(error)
        return iter(())
    monkeypatch.setattr(_windows.os, "walk", denied_walk)
    with pytest.raises(_windows.WindowsError, match="allocation_enumeration_failed"):
        _windows.allocated_bytes((root,))


@pytest.mark.parametrize("mode", ["invalid_handle", "query_failure", "negative_allocation",
                                  "zero_allocation", "identity_failure"])
def test_allocation_metadata_failure_or_zero_is_not_replaced_by_eof(root, monkeypatch, mode):
    import ctypes as c
    (root / "file").write_bytes(b"x" * 10001)
    original = _windows._api
    closed = []
    close = _windows._close
    def api(dll, name, args, result=None):
        function = original(dll, name, args) if result is None else original(dll, name, args, result)
        if mode == "invalid_handle" and name == "CreateFileW":
            return lambda *args: c.c_void_p(-1).value
        if name != "GetFileInformationByHandleEx":
            return function
        def query(handle, kind, buffer, size):
            if mode == "query_failure" or mode == "identity_failure" and kind == 18:
                return 0
            answer = function(handle, kind, buffer, size)
            if answer and kind == 1 and mode in ("negative_allocation", "zero_allocation"):
                c.cast(buffer, c.POINTER(c.c_longlong))[0] = -1 if mode == "negative_allocation" else 0
            return answer
        return query
    monkeypatch.setattr(_windows, "_api", api)
    monkeypatch.setattr(_windows, "_close", lambda handle: (closed.append(handle), close(handle)))
    if mode == "zero_allocation":
        assert _windows.allocated_bytes((root,)) == 0
    else:
        with pytest.raises(_windows.WindowsError, match="allocation_probe_failed"):
            _windows.allocated_bytes((root,))
    assert len(closed) == (0 if mode == "invalid_handle" else 1)


@pytest.mark.parametrize("remove_parent", [False, True])
def test_native_disappearance_restarts_all_roots_and_deduplicates(root, monkeypatch, remove_parent):
    other = _windows.private_directory(root.parent / "other")
    keeper = root / "keeper"
    keeper.write_bytes(b"k" * 10001)
    (other / "hardlink").hardlink_to(keeper)
    victim_dir = _windows.private_directory(other / "volatile")
    victim = victim_dir / "file"
    victim.write_bytes(b"v" * 10001)
    added = root / "added-after-first-root"
    original = _windows._file_allocation
    seen = []
    def delete_before_native_open(path):
        seen.append(path)
        if path == victim:
            victim.unlink()
            if remove_parent:
                victim_dir.rmdir()
            # The first root was already scanned. A local retry/skip would
            # miss this real allocation, or retain stale dedup bookkeeping.
            added.write_bytes(b"n" * 20001)
        return original(path)
    monkeypatch.setattr(_windows, "_file_allocation", delete_before_native_open)
    measured = _windows.allocated_bytes((root, other))
    expected = original(keeper)[1] + original(added)[1]
    assert measured == expected and expected > 0
    assert seen.count(keeper) == 2 and seen.count(victim) == 1
    budget = StorageBudget((root, other), quota_bytes=expected - 1, min_free_bytes=0)
    with pytest.raises(StorageBudgetError, match="^quota_exceeded$"):
        budget.reserve(0, "real_remaining_allocation")


def test_native_disappearing_directory_restarts_enumeration(root, monkeypatch):
    keeper = root / "keeper"
    keeper.write_bytes(b"k" * 10001)
    victim = _windows.private_directory(root / "volatile")
    scandir, allocation = os.scandir, _windows._file_allocation
    visits = []
    counted = []
    def remove_before_native_scandir(path):
        visits.append(Path(path))
        if Path(path) == victim:
            victim.rmdir()
        return scandir(path)
    def count(path):
        counted.append(path)
        return allocation(path)
    monkeypatch.setattr(os, "scandir", remove_before_native_scandir)
    monkeypatch.setattr(_windows, "_file_allocation", count)
    assert _windows.allocated_bytes((root,)) == allocation(keeper)[1]
    assert visits.count(root) == 2 and counted.count(keeper) == 2


def test_native_persistent_disappearance_is_bounded_and_never_admitted(root, monkeypatch):
    keeper = root / "keeper"
    keeper.write_bytes(b"k" * 10001)
    victim = root / "volatile"
    walk, allocation = os.walk, _windows._file_allocation
    starts = []
    def changing_walk(path, **kwargs):
        starts.append(path)
        victim.write_bytes(b"v" * 10001)
        return walk(path, **kwargs)
    def disappear(path):
        if path == victim:
            victim.unlink()
        return allocation(path)
    monkeypatch.setattr(os, "walk", changing_walk)
    monkeypatch.setattr(_windows, "_file_allocation", disappear)
    budget = StorageBudget((root,), quota_bytes=1024**2, min_free_bytes=0)
    with pytest.raises(StorageBudgetError, match="^allocation_probe_failed$"):
        budget.reserve(0, "must_not_admit_churn")
    assert len(starts) == 3
    assert not budget.ledger.exists()
    assert keeper.read_bytes() == b"k" * 10001


@pytest.mark.parametrize("native_code", [5, 6, 32, 123, 0])
@pytest.mark.parametrize("boundary", ["CreateFileW", "GetFileInformationByHandleEx"])
def test_allocation_nonmissing_native_errors_never_rescan(root, monkeypatch, native_code, boundary):
    import ctypes as c
    (root / "file").write_bytes(b"x" * 10001)
    api = _windows._api
    failures = []
    def observed(dll, name, args, result=None):
        function = api(dll, name, args) if result is None else api(dll, name, args, result)
        if name != boundary:
            return function
        def fail(*_):
            failures.append(native_code)
            # Zero simulates a failed boundary that did not set last_error;
            # an unrelated stale missing-file code must never enable retry.
            if native_code:
                c.set_last_error(native_code)
            return c.c_void_p(-1).value if boundary == "CreateFileW" else 0
        return fail
    monkeypatch.setattr(_windows, "_api", observed)
    c.set_last_error(2)
    with pytest.raises(_windows.WindowsError, match="^allocation_probe_failed$"):
        _windows.allocated_bytes((root,))
    assert failures == [native_code]


def test_allocation_explicit_delete_pending_restarts_whole_scan(root, monkeypatch):
    import ctypes as c
    keeper = root / "file"
    keeper.write_bytes(b"x" * 10001)
    expected = _windows.allocated_bytes((root,))
    api = _windows._api
    opens = []
    def observed(dll, name, args, result=None):
        function = api(dll, name, args) if result is None else api(dll, name, args, result)
        if name != "CreateFileW":
            return function
        def open_file(*values):
            opens.append(1)
            if len(opens) == 1:
                c.set_last_error(303)
                return c.c_void_p(-1).value
            return function(*values)
        return open_file
    monkeypatch.setattr(_windows, "_api", observed)
    assert _windows.allocated_bytes((root,)) == expected
    assert len(opens) == 2


def test_allocation_reparse_failure_does_not_rescan(root, monkeypatch):
    import _winapi
    outside = _windows.private_directory(root.parent / "external")
    (outside / "keep").write_bytes(b"x" * 10001)
    link = root / "junction"
    _winapi.CreateJunction(str(outside), str(link))
    walk = os.walk
    starts = []
    def observed(path, **kwargs):
        starts.append(path)
        return walk(path, **kwargs)
    monkeypatch.setattr(os, "walk", observed)
    try:
        with pytest.raises(_windows.WindowsError, match="^reparse_path$"):
            _windows.allocated_bytes((root,))
        assert len(starts) == 1
    finally:
        link.rmdir()
