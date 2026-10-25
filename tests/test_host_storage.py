"""Host domains count owned storage and selected external leaves without ACL edits."""
import json
from pathlib import Path

import pytest

from atom_search.runtime import _windows
from atom_search.runtime.storage import StorageBudget, StorageBudgetError


def test_external_leaves_count_allocation_deduplicate_and_allow_absence(tmp_path):
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    source = tmp_path / 'catalog.sqlite3'
    source.write_bytes(b'x' * 16384)
    (engine / 'same').hardlink_to(source)
    sidecar = Path(str(source) + '-wal')
    before = source.read_bytes()
    budget = StorageBudget((engine,), ledger_dir=ledger, read_only_files=(source, sidecar),
                           quota_bytes=1024 * 1024, min_free_bytes=0)
    one = _windows.allocated_bytes((engine,))
    assert _windows.allocated_bytes((engine,), read_only_files=budget.read_only_files) == one
    sidecar.write_bytes(b'w' * 8192)
    assert _windows.allocated_bytes((engine,), read_only_files=budget.read_only_files) == one + 8192
    sidecar.unlink()
    assert source.read_bytes() == before
    assert budget.ledger == ledger / 'storage-reservations.json'


def test_domain_conflict_preserves_live_ledger(tmp_path):
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    source = tmp_path / 'legacy'
    source.write_bytes(b'original')
    first = StorageBudget((engine,), ledger_dir=ledger, read_only_files=(source,),
                          quota_bytes=1024 * 1024, min_free_bytes=0)
    second = StorageBudget((engine,), ledger_dir=ledger,
                           quota_bytes=1024 * 1024, min_free_bytes=0)
    with first.reserve(100, 'existing'):
        original = first.ledger.read_bytes()
        with pytest.raises(StorageBudgetError, match='reservation_policy_conflict'):
            second.reserve(0, 'peer')
        assert first.ledger.read_bytes() == original


def test_host_bookkeeping_requires_headroom_before_ledger_write(tmp_path):
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    budget = StorageBudget((engine,), ledger_dir=ledger, quota_bytes=1, min_free_bytes=0)
    with pytest.raises(StorageBudgetError, match='quota_exceeded'):
        budget.reserve(0, 'empty')
    assert not budget.ledger.exists()


def test_external_membership_rejects_overlap_and_directory(tmp_path):
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    for files in ((engine / 'inside',), (tmp_path,), (tmp_path / 'same', tmp_path / 'same')):
        with pytest.raises(StorageBudgetError):
            StorageBudget((engine,), ledger_dir=ledger, read_only_files=files,
                          quota_bytes=1000000, min_free_bytes=0)


def test_ledger_junction_cannot_be_hidden_by_resolution(tmp_path):
    import _winapi
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    link = engine / 'alias'
    _winapi.CreateJunction(str(ledger), str(link))
    try:
        with pytest.raises(StorageBudgetError, match='reparse'):
            StorageBudget((engine,), ledger_dir=link, quota_bytes=1000000, min_free_bytes=0)
    finally:
        link.rmdir()


def test_absent_external_leaf_rejects_unsafe_ancestor_and_foreign_owner(tmp_path):
    import _winapi
    import os
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    target = tmp_path / 'ordinary'; target.mkdir()
    link = tmp_path / 'junction'; _winapi.CreateJunction(str(target), str(link))
    try:
        with pytest.raises(StorageBudgetError, match='reparse'):
            StorageBudget((engine,), ledger_dir=ledger, read_only_files=(link / 'absent.sqlite3',),
                          quota_bytes=1024**2, min_free_bytes=0)
    finally:
        link.rmdir()
    foreign = Path(os.environ['SystemRoot']) / 'System32' / 'AtomSearch-task5-absent.sqlite3'
    assert not foreign.exists()
    with pytest.raises(StorageBudgetError, match='source_owner_mismatch'):
        StorageBudget((engine,), ledger_dir=ledger, read_only_files=(foreign,),
                      quota_bytes=1024**2, min_free_bytes=0)


def test_external_leaf_reappearance_is_revalidated_without_source_acl_change(tmp_path):
    import os
    import subprocess
    engine = _windows.private_directory(tmp_path / 'engine')
    ledger = _windows.private_directory(engine / 'postgresql')
    source = tmp_path / 'external.sqlite3'
    source.write_bytes(b'x' * 8192)
    env = dict(os.environ, ATOM_TEST_LEAF=str(source))
    def powershell(command):
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            "$ErrorActionPreference='Stop'; " + command],
            env=env,capture_output=True,timeout=20)
        assert result.returncode==0, result.stderr.decode(errors='replace')
        return result.stdout
    acl = powershell('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl')
    budget = StorageBudget((engine,),ledger_dir=ledger,read_only_files=(source,),quota_bytes=1024**2,min_free_bytes=0)
    with budget.reserve(0,'present'): pass
    source.unlink()
    with budget.reserve(0,'absent'): pass
    source.mkdir()
    try:
        with pytest.raises(StorageBudgetError,match='regular_file_required'):
            budget.reserve(0,'unsafe_reappearance')
    finally: source.rmdir()
    source.write_bytes(b'x'*8192)
    assert powershell('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl') == acl
    with budget.reserve(0,'safe_reappearance'): pass
    assert source.read_bytes()==b'x'*8192
    assert json.loads(budget.ledger.read_text())==[]
    env['ATOM_TEST_ACL'] = acl.decode().strip()
    powershell("$f=[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF); $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; $rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadData','Deny'); $f.AddAccessRule($rule); [System.IO.File]::SetAccessControl($env:ATOM_TEST_LEAF,$f)")
    denied_acl = powershell('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl')
    try:
        with pytest.raises(PermissionError):
            source.read_bytes()
        # NTFS permits metadata accounting even when content reads are denied.
        with budget.reserve(0,'metadata_still_accessible'): pass
        assert powershell('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl')==denied_acl
    finally:
        powershell("$f=[System.Security.AccessControl.FileSecurity]::new(); $f.SetSecurityDescriptorSddlForm($env:ATOM_TEST_ACL,[System.Security.AccessControl.AccessControlSections]::Access); [System.IO.File]::SetAccessControl($env:ATOM_TEST_LEAF,$f)")
    assert source.read_bytes()==b'x'*8192
