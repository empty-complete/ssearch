"""Behavioral tests for the unbundled VMware QA boundary (no real VM writes)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(not PS, reason="Windows PowerShell 5.1 required")


def ps(code):
    prefix = f"$ErrorActionPreference='Stop'; . '{ROOT / 'tools/check_windows_guest.ps1'}'; . '{ROOT / 'tools/check_vmware.ps1'}'; "
    return subprocess.run([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', prefix + code + '; exit 0'], capture_output=True, text=True, timeout=40, env={k:v for k,v in os.environ.items() if k.lower() != 'psmodulepath'})


def fixture_build(tmp_path):
    import hashlib
    sha = 'a' * 40
    build = tmp_path / 'artifacts' / 'development' / sha
    records = {}
    for key, name in [('installer', 'artifacts/setup.exe'), ('executable', 'dist/ssearch/ssearch.exe')]:
        file = build / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(key.encode())
        records[key] = dict(path=name, size=file.stat().st_size, sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    proof = build / 'qa/proof.json'
    proof.parent.mkdir()
    proof.write_text(json.dumps(dict(ok=True, execution_mode='frozen', source_commit=sha)))
    (build / 'build-manifest.json').write_text(json.dumps(dict(source_commit=sha, qa={'postgresql': 'qa/proof.json'}, **records)))
    return build


def test_payload_rejects_modified_installer_before_any_output(tmp_path):
    build = fixture_build(tmp_path)
    (build / 'artifacts/setup.exe').write_bytes(b'changed')
    out = tmp_path / 'payload'
    r = ps(f"try {{ New-QAPayload '{build}' '{out}' ('b'*40); exit 2 }} catch {{ if ($_.Exception.Message -ne 'ARTIFACT_MISMATCH') {{ throw }} }}")
    assert r.returncode == 0, r.stderr
    assert not out.exists()


UNSAFE_RELATIVE_PATHS = ['../escape', 'C:/escape', 'a/../b', 'a\\b', 'a:b', 'a//b', 'a./b', 'CON/file', 'a /b']


@pytest.fixture(scope='module')
def unsafe_relative_path_results():
    # These guards are pure: keep per-input pytest failures without repeated shell startup.
    names = ','.join("'" + name.replace("'", "''") + "'" for name in UNSAFE_RELATIVE_PATHS)
    r = ps(f"""
    $results = @{{}}
    foreach ($name in @({names})) {{
      try {{ $null = Assert-QARelative $name; $results[$name] = 'ACCEPTED' }}
      catch {{ $results[$name] = $_.Exception.Message }}
    }}
    $results | ConvertTo-Json
    """)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize('name', UNSAFE_RELATIVE_PATHS)
def test_payload_path_refuses_windows_aliases_and_traversal(name, unsafe_relative_path_results):
    assert unsafe_relative_path_results[name] == 'UNSAFE_RELATIVE_PATH'


def test_payload_includes_reference_bundle_and_separate_checker_identity(tmp_path):
    build = fixture_build(tmp_path)
    out = tmp_path / 'payload'
    r = ps(f"New-QAPayload '{build}' '{out}' ('b'*40) | ConvertTo-Json -Depth 8")
    assert r.returncode == 0, r.stderr
    manifest = json.loads((out / 'payload-manifest.json').read_text(encoding='utf-8-sig'))
    assert manifest['source_commit'] == 'a' * 40
    assert manifest['checker_revision'] == 'b' * 40
    paths = {item['path'] for item in manifest['files']}
    assert 'tools/check_installer.ps1' in paths
    assert f"artifacts/development/{'a'*40}/dist/ssearch/ssearch.exe" in paths
    import zipfile
    with zipfile.ZipFile(out / 'payload.zip') as z:
        assert set(z.namelist()) == paths
    # Original build remains free of generated evidence.
    assert not (build / 'payload.zip').exists()


def test_archive_rejects_surprise_path_without_extracting(tmp_path):
    import zipfile
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('../escape.txt', 'not allowed')
    out = tmp_path / 'extract'
    r = ps(f"try {{ Expand-QAPayload '{archive}' @() '{out}'; exit 2 }} catch {{ if ($_.Exception.Message -notmatch 'UNSAFE|ARCHIVE') {{ throw }} }}")
    assert r.returncode == 0, r.stderr
    assert not out.exists()


def test_transport_redacts_process_errors_and_times_out_without_kill(tmp_path):
    r = ps(f"try {{ Invoke-QAProcess '{PS}' @('-NoProfile','-Command','[Console]::Error.WriteLine(\"PRIVATE_SENTINEL\"); exit 3') 2000; exit 2 }} catch {{ $_.Exception.Message }}")
    assert r.returncode == 0, r.stderr
    assert 'PROCESS_EXIT' in r.stdout
    assert 'PRIVATE_SENTINEL' not in r.stdout + r.stderr
    r = ps(f"try {{ Invoke-QAProcess '{PS}' @('-NoProfile','-Command','Start-Sleep -Seconds 2') 100; exit 2 }} catch {{ $_.Exception.Message }}")
    assert r.returncode == 0, r.stderr
    assert 'PROCESS_TIMEOUT_STATE_UNKNOWN' in r.stdout


def test_vm_command_allowlist_refuses_power_or_process_mutation():
    commands = ['stop', 'reset', 'snapshot', 'killProcessInGuest', 'addSharedFolder']
    names = ','.join("'" + command + "'" for command in commands)
    r = ps(f"""
    $results = @{{}}
    foreach ($command in @({names})) {{
      try {{ $null = Invoke-VmCommand $command @(); $results[$command] = 'ACCEPTED' }}
      catch {{ $results[$command] = $_.Exception.Message }}
    }}
    $results | ConvertTo-Json
    """)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == dict.fromkeys(commands, 'VM_COMMAND_FORBIDDEN')


def test_failure_retrieval_uses_fixed_whitelist_and_hashes(tmp_path):
    out = tmp_path / 'returned'
    out.mkdir()
    code = f"""
    $script:seen = @()
    function Invoke-VmCommand {{ param($Command,$Arguments)
      if ($Command -ne 'CopyFileFromGuestToHost') {{ throw 'unexpected command' }}
      $script:seen += $Arguments[0]
      if ($Arguments[0].EndsWith('guest-result.json')) {{ [IO.File]::WriteAllText($Arguments[1], '{{"status":"FAILED"}}'); return '' }}
      throw 'PROCESS_EXIT'
    }}
    $result = Receive-QAEvidence 'C:\\QA\\run' '{out}'
    @{{ files=$result.files; seen=$script:seen }} | ConvertTo-Json -Depth 6
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert len(result['files']) == 1
    assert result['files'][0]['sha256']
    assert all('postgres-profile' not in p and '*' not in p for p in result['seen'])
    assert any(p.endswith('guest-result.json') for p in result['seen'])

def test_gui_identity_guard_rejects_reused_pid_and_other_executable():
    code = f"""
    $ast=[Management.Automation.Language.Parser]::ParseFile('{ROOT / 'tools/check_installer.ps1'}',[ref]$null,[ref]$null)
    $node=$ast.Find({{param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-DesktopProcessIdentity'}},$true)
    if (!$node) {{ throw 'Missing GUI identity guard' }}
    Invoke-Expression $node.Extent.Text
    $start=[datetime]'2026-10-20T12:00:00Z'
    $p=[pscustomobject]@{{Id=123; StartTime=$start; Path='C:\\QA\\app\\AtomSearch.exe'; HasExited=$false}}
    Assert-DesktopProcessIdentity $p 123 $start.Ticks 'C:\\QA\\app\\AtomSearch.exe'
    foreach($case in @(@{{id=124; ticks=$start.Ticks; exe=$p.Path}},@{{id=123; ticks=1; exe=$p.Path}},@{{id=123; ticks=$start.Ticks; exe='C:\\Other\\AtomSearch.exe'}})) {{
      $caught=$false
      try {{ Assert-DesktopProcessIdentity $p $case.id $case.ticks $case.exe }} catch {{ $caught=$true }}
      if (!$caught) {{ throw 'Unowned process accepted' }}
    }}
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr

def test_full_orchestration_failure_still_retrieves_evidence(tmp_path):
    build = fixture_build(tmp_path)
    out = tmp_path / 'run'
    code = f"""
    $script:vmxPath='C:\\VM\\test.vmx'
    $script:vmCredential=New-Object PSCredential('qa',(ConvertTo-SecureString 'LOCAL_ONLY_SENTINEL' -AsPlainText -Force))
    $script:calls=@()
    function Invoke-VmCommand {{ param($Command,$Arguments,[switch]$Unauthenticated,$TimeoutMilliseconds)
      $script:calls += $Command
      switch($Command) {{
        'list' {{ return "Total running VMs: 1`nC:\\VM\\test.vmx" }}
        'checkToolsState' {{ return 'running' }}
        'runProgramInGuest' {{ throw 'PROCESS_EXIT' }}
        'CopyFileFromGuestToHost' {{
          if($Arguments[0].EndsWith('guest-result.json')) {{ [IO.File]::WriteAllText($Arguments[1], '{{"status":"FAILED"}}'); return '' }}
          throw 'PROCESS_EXIT'
        }}
        default {{ throw 'FORBIDDEN_TEST_SIDE_EFFECT' }}
      }}
    }}
    try {{ Invoke-VMwareQA Run '{build}' '{out}' ('b'*40) 'C:\\Users\\qa\\AppData\\Local\\AtomSearch-VM-QA' 10; throw 'UNEXPECTED_PASS' }} catch {{ if($_.Exception.Message -ne 'PROCESS_EXIT') {{ throw }} }}
    $script:calls | ConvertTo-Json
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr
    report = json.loads((out / 'host-result.json').read_text(encoding='utf-8-sig'))
    assert report['status'] == 'FAILED'
    assert report['process_completion'] == 'unknown'
    assert report['evidence'][0]['path'] == 'guest-result.json'
    assert 'LOCAL_ONLY_SENTINEL' not in (out / 'host-result.json').read_text() + r.stdout + r.stderr
    calls = json.loads(r.stdout)
    assert calls[:3] == ['list', 'checkToolsState', 'runProgramInGuest']
    assert calls.count('runProgramInGuest') == 1


def test_archive_hash_failure_prevents_any_extraction(tmp_path):
    import zipfile
    archive = tmp_path / 'changed.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('tools/runner.ps1', b'changed')
    out = tmp_path / 'extracted'
    r = ps(f"try {{ Expand-QAPayload '{archive}' @(@{{path='tools/runner.ps1';size=7;sha256=('0'*64)}}) '{out}'; exit 2 }} catch {{ if($_.Exception.Message -ne 'ARCHIVE_HASH_MISMATCH') {{ throw }} }}")
    assert r.returncode == 0, r.stderr
    assert not out.exists()


def test_valid_payload_extracts_and_existing_destination_is_refused(tmp_path):
    build = fixture_build(tmp_path)
    out = tmp_path / 'payload'
    extracted = tmp_path / 'extracted'
    r = ps(f"$p=New-QAPayload '{build}' '{out}' ('b'*40); Expand-QAPayload '{out / 'payload.zip'}' $p.manifest.files '{extracted}'; try {{ Expand-QAPayload '{out / 'payload.zip'}' $p.manifest.files '{extracted}'; exit 2 }} catch {{ if($_.Exception.Message -ne 'EXTRACTION_DESTINATION_EXISTS') {{ throw }} }}")
    assert r.returncode == 0, r.stderr
    assert (extracted / 'artifacts/development' / ('a'*40) / 'dist/ssearch/ssearch.exe').read_bytes() == b'executable'


def test_guest_root_rejects_powershell_interpolation_before_transport(tmp_path):
    build = fixture_build(tmp_path)
    out = tmp_path / 'payload'
    code = f"""
    $script:vmCredential=New-Object PSCredential('qa',(ConvertTo-SecureString 'test' -AsPlainText -Force))
    function Invoke-QAPreflight {{ @{{status='PREFLIGHT_PASS'}} }}
    function Invoke-VmCommand {{ throw 'TRANSPORT_SHOULD_NOT_RUN' }}
    try {{ Invoke-VMwareQA Run '{build}' '{out}' ('b'*40) 'C:\\Users\\$(bad)\\AppData\\Local\\AtomSearch-VM-QA'; exit 2 }} catch {{ if($_.Exception.Message -ne 'GUEST_ROOT_INVALID') {{ throw }} }}
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr

def test_failed_desktop_capture_is_not_retried_or_acknowledged(tmp_path):
    build = fixture_build(tmp_path)
    out = tmp_path / 'run'
    code = f"""
    $script:vmCredential=New-Object PSCredential('qa',(ConvertTo-SecureString 'test' -AsPlainText -Force))
    $script:captureCount=0; $script:ackCount=0
    function Invoke-QAPreflight {{ @{{status='PREFLIGHT_PASS'}} }}
    function Invoke-VmCommand {{ param($Command,$Arguments,$TimeoutMilliseconds)
      switch($Command) {{
        'runProgramInGuest' {{ return '' }}
        'CopyFileFromHostToGuest' {{ if($Arguments[0].EndsWith('capture-ack.json')){{$script:ackCount++}}; return '' }}
        'captureScreen' {{ $script:captureCount++; if($script:captureCount -eq 1){{[IO.File]::WriteAllText($Arguments[0],'fake desktop');return ''}}; throw 'PROCESS_EXIT' }}
        'CopyFileFromGuestToHost' {{
          if($Arguments[0].EndsWith('bootstrap-ready.json')) {{
            $m=Get-Content -Raw '{out / 'payload-manifest.json'}' | ConvertFrom-Json
            Write-QAJson $Arguments[1] @{{nonce=$m.nonce;run=(Split-Path $Arguments[0] -Parent);created=$true}}
            return ''
          }}
          if($Arguments[0].EndsWith('gui-ready.json')) {{
            $m=Get-Content -Raw '{out / 'payload-manifest.json'}' | ConvertFrom-Json
            Write-QAJson $Arguments[1] @{{nonce=$m.nonce;session_id=1;window_handle=123}}
            return ''
          }}
          throw 'PROCESS_EXIT'
        }}
        default {{ throw 'FORBIDDEN_TEST_SIDE_EFFECT' }}
      }}
    }}
    try {{ Invoke-VMwareQA Run '{build}' '{out}' ('b'*40) 'C:\\Users\\qa\\AppData\\Local\\AtomSearch-VM-QA' 10; exit 2 }} catch {{ if($_.Exception.Message -ne 'DESKTOP_CAPTURE_FAILED_STATE_UNKNOWN'){{throw}} }}
    @{{captures=$script:captureCount;acknowledgements=$script:ackCount}} | ConvertTo-Json
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {'captures': 2, 'acknowledgements': 0}
    result = json.loads((out / 'host-result.json').read_text(encoding='utf-8-sig'))
    assert result['status'] == 'FAILED'
    assert result['native_ui_verified'] is False
    assert result['screenshots_captured'] is False

def test_bootstrap_requires_guest_receipt_before_payload_write(tmp_path):
    build = fixture_build(tmp_path)
    out = tmp_path / 'run'
    code = f"""
    $script:vmCredential=New-Object PSCredential('qa',(ConvertTo-SecureString 'test' -AsPlainText -Force))
    $script:payloadWrites=0
    function Invoke-QAPreflight {{ @{{status='PREFLIGHT_PASS'}} }}
    function Invoke-VmCommand {{ param($Command,$Arguments,$TimeoutMilliseconds)
      switch($Command) {{
        'runProgramInGuest' {{ return '' }}
        'CopyFileFromHostToGuest' {{ $script:payloadWrites++; throw 'UNVERIFIED_GUEST_WRITE' }}
        'CopyFileFromGuestToHost' {{ throw 'PROCESS_EXIT' }}
        default {{ throw 'UNEXPECTED_COMMAND' }}
      }}
    }}
    try {{ Invoke-VMwareQA Run '{build}' '{out}' ('b'*40) 'C:\\Users\\qa\\AppData\\Local\\AtomSearch-VM-QA' 10; exit 2 }} catch {{ if($_.Exception.Message -ne 'PROCESS_EXIT'){{throw}} }}
    $script:payloadWrites
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == '0'

# A guest PASS is observation, not proof that required reports reached the host.
def run_completed_guest_fixture(tmp_path, retrieval, guest_status='PASS'):
    build = fixture_build(tmp_path)
    out = tmp_path / 'run'
    code = f"""
    $script:vmCredential=New-Object PSCredential('qa',(ConvertTo-SecureString 'test' -AsPlainText -Force))
    function Invoke-QAPreflight {{ @{{status='PREFLIGHT_PASS'}} }}
    function Get-QAHash {{ param($Path)
      if('{retrieval}' -eq 'hash' -and $Path.EndsWith('returned\\installer-qa.json')) {{ throw 'PRIVATE_HASH_DETAIL' }}
      (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }}
    function Invoke-VmCommand {{ param($Command,$Arguments,$TimeoutMilliseconds)
      switch($Command) {{
        'runProgramInGuest' {{ return '' }}
        'CopyFileFromHostToGuest' {{ return '' }}
        'captureScreen' {{ [IO.File]::WriteAllText($Arguments[0],'synthetic desktop'); return '' }}
        'CopyFileFromGuestToHost' {{
          $name=Split-Path $Arguments[0] -Leaf
          if((Split-Path $Arguments[1] -Parent) -eq '{out / 'returned'}') {{
            if('{retrieval}' -eq 'none' -or $name -eq 'uninstall-after-failure.log' -or
               ('{retrieval}' -eq 'missing' -and $name -eq 'installer-qa.json') -or
               ('{retrieval}' -eq 'partial' -and $name -ne 'environment.json')) {{ throw 'PRIVATE_COPY_DETAIL' }}
            if('{retrieval}' -eq 'oversize' -and $name -eq 'installer-qa.json') {{
              $stream=[IO.File]::Open($Arguments[1],[IO.FileMode]::CreateNew)
              try {{$stream.SetLength(16MB+1)}} finally {{$stream.Dispose()}}
              return ''
            }}
            [IO.File]::WriteAllText($Arguments[1],'{{"synthetic":true}}'); return ''
          }}
          $m=Get-Content -Raw '{out / 'payload-manifest.json'}' | ConvertFrom-Json
          switch($name) {{
            'bootstrap-ready.json' {{ Write-QAJson $Arguments[1] @{{nonce=$m.nonce;run=(Split-Path $Arguments[0] -Parent);created=$true}} }}
            'gui-ready.json' {{ Write-QAJson $Arguments[1] @{{nonce=$m.nonce;session_id=1;window_handle=123}} }}
            'guest-result.json' {{ Write-QAJson $Arguments[1] @{{nonce=$m.nonce;source_commit=$m.source_commit;manifest_sha256=(Get-QAHash '{out / 'payload-manifest.json'}');archive_sha256=(Get-QAHash '{out / 'payload.zip'}');status='{guest_status}'}} }}
            default {{ throw 'UNEXPECTED_PROBE' }}
          }}
          return ''
        }}
        default {{ throw 'UNEXPECTED_COMMAND' }}
      }}
    }}
    $caught=$null
    try {{ $null=Invoke-VMwareQA Run '{build}' '{out}' ('b'*40) 'C:\\Users\\qa\\AppData\\Local\\AtomSearch-VM-QA' 10 }} catch {{ $caught=$_.Exception.Message }}
    @{{caught=$caught;report=(Get-Content -Raw '{out / 'host-result.json'}' | ConvertFrom-Json)}} | ConvertTo-Json -Depth 12
    """
    r = ps(code)
    assert r.returncode == 0, r.stderr
    assert 'PRIVATE_COPY_DETAIL' not in r.stdout + r.stderr
    assert 'PRIVATE_HASH_DETAIL' not in r.stdout + r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize('retrieval', ['none', 'missing', 'oversize', 'hash'])
def test_guest_pass_requires_all_mandatory_returned_evidence(tmp_path, retrieval):
    observed = run_completed_guest_fixture(tmp_path, retrieval)
    report = observed['report']
    assert report['guest_qa'] == 'PASS'
    assert report['status'] == 'FAILED'
    assert observed['caught'] == report['error_code'] == 'EVIDENCE_INCOMPLETE'
    assert 'installer-qa.json' in report['missing_required_evidence']
    outcome = next(item for item in report['evidence_outcomes'] if item['path'] == 'installer-qa.json')
    expected = {'none': 'COPY_FAILED_OR_MISSING', 'missing': 'COPY_FAILED_OR_MISSING', 'oversize': 'OVERSIZE', 'hash': 'HASH_FAILED'}
    assert outcome['status'] == expected[retrieval]
    assert not any(item['path'] == 'installer-qa.json' for item in report['evidence'])


def test_complete_success_allows_absent_failure_only_log(tmp_path):
    observed = run_completed_guest_fixture(tmp_path, 'optional')
    report = observed['report']
    assert observed['caught'] is None
    assert report['guest_qa'] == report['status'] == 'PASS'
    assert report['missing_required_evidence'] == []
    assert len(report['evidence']) == 14
    assert all(len(item['sha256']) == 64 for item in report['evidence'])
    optional = next(item for item in report['evidence_outcomes'] if item['path'] == 'uninstall-after-failure.log')
    assert optional['required_on_success'] is False
    assert optional['status'] == 'COPY_FAILED_OR_MISSING'


def test_failed_guest_keeps_original_failure_with_partial_evidence(tmp_path):
    observed = run_completed_guest_fixture(tmp_path, 'partial', guest_status='FAILED')
    report = observed['report']
    assert report['guest_qa'] == report['status'] == 'FAILED'
    assert observed['caught'] == report['error_code'] == 'GUEST_QA_FAILED'
    assert [item['path'] for item in report['evidence']] == ['environment.json']
    assert 'installer-qa.json' in report['missing_required_evidence']
    assert len(report['evidence_outcomes']) == 15
