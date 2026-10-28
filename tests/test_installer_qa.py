"""Execute the real installed-report validator with controlled JSON, no install."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


def test_installed_report_rejects_sqlite_or_wrong_build(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'tools/check_installer.ps1'
    harness = tmp_path / 'validate.ps1'
    harness.write_text(r'''
param($Source, $Work)
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-InstalledSelfTest'},$true)
Invoke-Expression $fn.Extent.Text
function Assert-OwnedInstallation {}
function Assert-Marker {}
function Start-HiddenAndWait {
 param($Program,$Arguments,$Label)
 $script:fixture | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Arguments[1] -Encoding UTF8
}
$testPath=$Work; $dataPath=$Work; $appExecutable='unused'
$sourceCommit='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$buildManifest=@{app_version='0.2.0';core_version='0.2.0';catalog=@{backend='postgresql';schema_version=5}}
$fixture=@{ok=$true;model_ready=$true;scan_status='complete';files=3;vector_count=3;dimension=384;
 literal_result='КМ-101 узлы фасада.dwg';semantic_result='Контракт монтажных работ.docx'}
$runtimeCleanupConfirmed=$true
$caught=$false
try { $null=Invoke-InstalledSelfTest 'old.json' } catch { $caught=$true }
if (-not $caught) { throw 'Historical SQLite report was accepted' }
if ($runtimeCleanupConfirmed) { throw 'Invalid application report authorized uninstall' }
$fixture+=@{backend='postgresql';factory='open_search_service';schema_version=5;clean_stop=$true;
 prior_schema_version=5;public_identity_preserved=$true;
 source_clean=$true;source_commit=$sourceCommit;app_version='0.2.0';core_version='0.2.0';execution_mode='frozen';
 server_version='18.6';extension_version='0.8.7';catalog_id=1;
 installation_id='11111111-1111-1111-1111-111111111111';model_manifest_digest=('a'*64);
 index_fingerprint=('b'*64);
 model_key='fixture|filename-path-root-project-v2'}
$null=Invoke-InstalledSelfTest 'valid.json'
if (-not $runtimeCleanupConfirmed) { throw 'Valid cleanup was not confirmed' }
$fixture.source_commit='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
$caught=$false
try { $null=Invoke-InstalledSelfTest 'wrong.json' } catch { $caught=$true }
if (-not $caught) { throw 'Wrong build accepted' }
if ($runtimeCleanupConfirmed) { throw 'Wrong application identity authorized uninstall' }
function Start-HiddenAndWait { throw 'injected_missing_report' }
$runtimeCleanupConfirmed=$true
try { $null=Invoke-InstalledSelfTest 'missing.json' } catch {}
if ($runtimeCleanupConfirmed) { throw 'Missing application report authorized uninstall' }
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-InstalledDesktopEvidence'},$true)
Invoke-Expression $fn.Extent.Text
$runtimeCleanupConfirmed=$true; $DesktopEvidenceDirectory='enabled'
try { Invoke-InstalledDesktopEvidence } catch {}
if ($runtimeCleanupConfirmed) { throw 'Failed desktop launch authorized uninstall' }
'3 report cases passed'
''', encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness),
                             str(source), str(tmp_path)], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def test_preserved_snapshot_observes_actual_application_profile(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'tools/check_installer.ps1'
    harness = tmp_path / 'snapshot.ps1'
    harness.write_text(r'''
param($Source,$Work)
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -Force
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-PreservedProfileSnapshot'},$true)
Invoke-Expression $fn.Extent.Text
function Assert-ArtifactPath { param($Path,[switch]$Qa) return $Path }
$postgresPath=Join-Path $Work 'synthetic'; $appData=Join-Path $Work 'application'
$null=New-Item -ItemType Directory -Path $postgresPath,$appData
[IO.File]::WriteAllText((Join-Path $postgresPath 'data'),'unchanged')
[IO.File]::WriteAllText((Join-Path $appData 'data'),'original')
$before=Get-PreservedProfileSnapshot -ProfilePath $appData
[IO.File]::WriteAllText((Join-Path $appData 'data'),'corrupted')
if ((Get-PreservedProfileSnapshot -ProfilePath $appData) -ceq $before) {
 throw 'Actual application corruption was hidden by synthetic profile'
}
''', encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness),str(source),str(tmp_path)],
                            capture_output=True,timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = '99c53a141c4864072e8a357d2a0cc5423c1c810b'
HARNESS = 'b' * 40


@pytest.mark.parametrize('reference', [False, True], ids=['installed', 'post-uninstall-reference'])
def test_existing_installer_self_test_uses_service_and_same_application_profile(tmp_path, reference):
    """Actual existing controller body, valid frozen report; no application runs."""
    script = tmp_path / 'route.ps1'
    script.write_text(r'''
param([string]$Controller, [string]$Root, [string]$Reference)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Controller,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'controller_syntax_invalid' }
$node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Invoke-InstalledSelfTest'},$true)
. ([scriptblock]::Create($node.Extent.Text))
$sourceCommit='99c53a141c4864072e8a357d2a0cc5423c1c810b'
$buildManifest=@{payload_kind='electron-service';app_version='0.2.0';core_version='0.2.0';
    catalog=@{backend='postgresql';schema_version=5}}
$isElectron=$true
$testPath=$Root
$appExecutable=Join-Path $Root 'installed/ssearch.exe'
$serviceExecutable=Join-Path $Root 'installed/service/ssearch-service.exe'
$referenceExecutable=Join-Path $Root 'reference/ssearch.exe'
$referenceServiceExecutable=Join-Path $Root 'reference/service/ssearch-service.exe'
$dataPath=Join-Path $Root 'actual application profile Кириллица with spaces'
$null=New-Item -ItemType Directory -Path $dataPath
$settings=Join-Path $dataPath 'settings.json'
[IO.File]::WriteAllText($settings,'{"retained":true}')
$script:ownedChecks=0; $script:markerChecks=0; $script:called=$null
function Assert-OwnedInstallation { $script:ownedChecks++ }
function Assert-Marker { $script:markerChecks++ }
function Start-HiddenAndWait {
    param([string]$Program,[string[]]$Arguments,[string]$Label,[int]$TimeoutMilliseconds)
    $script:called=@{program=$Program;arguments=$Arguments}
    $report=@{ok=$true;model_ready=$true;scan_status='complete';files=3;vector_count=3;dimension=384;
        literal_result='КМ-101 узлы фасада.dwg';semantic_result='Контракт монтажных работ.docx';
        backend='postgresql';factory='open_search_service';schema_version=5;prior_schema_version=5;
        clean_stop=$true;public_identity_preserved=$true;source_clean=$true;execution_mode='frozen';
        source_commit=$sourceCommit;app_version='0.2.0';core_version='0.2.0';server_version='18.6';
        extension_version='0.8.7';catalog_id=1;installation_id='11111111-1111-4111-8111-111111111111';
        model_manifest_digest=('c'*64);index_fingerprint=('d'*64);model_key='model|filename-path-root-project-v2';
        public_source_id='22222222-2222-4222-8222-222222222222';public_file_ids=@{drawing='33333333-3333-4333-8333-333333333333'}}
    $path=$Arguments[[Array]::IndexOf($Arguments,'--self-test')+1]
    [IO.File]::WriteAllText($path,($report|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    $script:processCompletionConfirmed=$true
}
$fromReference=$Reference -eq 'true'
if($fromReference) { $result=Invoke-InstalledSelfTest 'proof.json' -FromBundle }
else { $result=Invoke-InstalledSelfTest 'proof.json' }
$expected=if($fromReference){$referenceServiceExecutable}else{$serviceExecutable}
$argsSeen=$script:called.arguments
@{service_selected=($script:called.program -ceq $expected);
    same_profile=($argsSeen[[Array]::IndexOf($argsSeen,'--data-dir')+1] -ceq $dataPath);
    registration_boundary=($script:ownedChecks -eq $(if($fromReference){0}else{1}));
    real_settings_unchanged=([IO.File]::ReadAllText($settings) -ceq '{"retained":true}');
    marker_checked=($script:markerChecks -eq 1);cleanup_confirmed=($script:runtimeCleanupConfirmed -eq $true);
    public_identity_retained=($result.public_source_id -ceq '22222222-2222-4222-8222-222222222222')}|
    ConvertTo-Json -Compress
''', encoding='utf-8-sig')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(script), str(ROOT / 'tools/check_installer.ps1'), str(tmp_path), str(reference).lower()],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.fail('installer_controller_probe_unavailable', pytrace=False)
    clean_exit = result.returncode == 0
    assert clean_exit, 'actual_installer_controller_probe_failed'
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        pytest.fail('installer_controller_probe_result_invalid', pytrace=False)
    assert observed['service_selected'] is True
    assert all(value is True for value in observed.values())


@pytest.fixture
def controller():
    path = ROOT / 'tools/ci_installer.py'
    assert path.is_file(), 'Installer artifact/receipt controller contract is required'
    spec = importlib.util.spec_from_file_location('ssearch_ci_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory_fixture(tmp_path):
    payload = tmp_path / 'installed Кириллица with spaces'
    payload.mkdir()
    files = {'ssearch.exe': b'desktop fixture', 'service/ssearch-service.exe': b'service fixture',
             'service/_internal/resources/help.html': b'offline help'}
    inventory = {}
    for name, content in files.items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        inventory[name] = {'size': len(content), 'sha256': sha(path)}
    canonical = tmp_path / 'published-inventory.json'
    # This is the original Windows text writer, including its CRLF translation.
    with canonical.open('x', encoding='utf-8') as stream:
        json.dump({'schema_version': 1, 'source_commit': PAYLOAD, 'app_version': '0.2.0',
            'core_version': '0.2.0', 'files': dict(sorted(inventory.items())), 'inventory_only': True}, stream, indent=2)
        stream.write('\n')
    manifest = {'source_commit': PAYLOAD, 'payload_kind': 'electron-service', 'app_version': '0.2.0',
        'core_version': '0.2.0', 'desktop': {'inventory_sha256': sha(canonical)},
        'executable': {'sha256': inventory['ssearch.exe']['sha256'], 'size': inventory['ssearch.exe']['size']},
        'service_executable': {'sha256': inventory['service/ssearch-service.exe']['sha256'],
                               'size': inventory['service/ssearch-service.exe']['size']}}
    owned = {}
    for name in ('unins000.exe', 'unins000.dat'):
        path = payload / name
        path.write_bytes(b'owned Inno fixture ' + name.encode())
        owned[name] = {'size': path.stat().st_size, 'sha256': sha(path)}
    return payload, manifest, owned, inventory, canonical


def test_owned_installed_inventory_matches_published_windows_writer_without_dist(controller, tmp_path):
    payload, manifest, owned, inventory, canonical = inventory_fixture(tmp_path)
    output = tmp_path / 'observed-inventory.json'
    result = controller.verify_installed_payload(payload, manifest, owned_inno_files=owned, inventory_output=output)
    assert result == inventory
    assert output.read_bytes() == canonical.read_bytes()
    assert b'\r\n' in output.read_bytes(), 'Original Windows inventory serialization must be preserved'


@pytest.mark.parametrize('fault', ['desktop', 'service', 'extra', 'inno-replaced', 'inno-unowned', 'published-digest', 'reused-output'])
def test_installed_inventory_never_self_accepts_changed_or_unknown_files(controller, tmp_path, fault):
    payload, manifest, owned, _, _ = inventory_fixture(tmp_path)
    output = tmp_path / 'observed-inventory.json'
    if fault == 'desktop':
        (payload / 'ssearch.exe').write_bytes(b'changed')
    elif fault == 'service':
        (payload / 'service/ssearch-service.exe').write_bytes(b'changed')
    elif fault == 'extra':
        (payload / 'unins001.exe').write_bytes(b'unknown owned status')
    elif fault == 'inno-replaced':
        (payload / 'unins000.exe').write_bytes(b'foreign replacement')
    elif fault == 'inno-unowned':
        owned.pop('unins000.dat')
    elif fault == 'published-digest':
        manifest['desktop']['inventory_sha256'] = '0' * 64
    else:
        output.write_bytes(b'preserve existing output')
    with pytest.raises(ValueError):
        controller.verify_installed_payload(payload, manifest, owned_inno_files=owned, inventory_output=output)
    if fault == 'reused-output':
        assert output.read_bytes() == b'preserve existing output'
    assert (payload / 'unins000.exe').exists(), 'Validation must never delete installed files'


@pytest.mark.parametrize('field', ['public_source_id', 'public_file_ids', 'catalog_id', 'installation_id',
                                  'model_manifest_digest', 'index_fingerprint'])
def test_reinstall_and_post_uninstall_application_proof_preserve_real_public_identity(controller, field):
    first = {'public_source_id': 'source', 'public_file_ids': {'drawing': 'file'}, 'catalog_id': 1,
             'installation_id': 'installation', 'model_manifest_digest': 'c' * 64, 'index_fingerprint': 'd' * 64}
    restored = {**first, 'prior_index_verified': True, 'prior_files': 3,
                'prior_index_fingerprint': first['index_fingerprint']}
    controller.validate_application_preservation(first, restored)
    changed = {**restored, field: 'changed'}
    with pytest.raises(ValueError):
        controller.validate_application_preservation(first, changed)


def input_metadata():
    pins = {'repository': 'empty-complete/ssearch', 'source_commit': PAYLOAD, 'run_id': 40409747788,
            'artifact_id': 12693783261, 'workflow_id': 395933832,
            'workflow_path': '.github/workflows/windows-build.yml', 'head_branch': 'codex/p4-shared-package',
            'artifact_name': f'windows-{PAYLOAD}-quick', 'archive_size': 340593598,
            'archive_sha256': '18776ed172a240791304ad9b3e9e26ee6da87696d4cf3d9ae3dd26f849108ac7',
            'installer_name': 'ssearch-Setup-0.2.0.exe', 'installer_size': 340587240,
            'installer_sha256': 'f9d485776ab3e667d3e4365fdae7c91660a0b8dacb4d17afa2a065c50d49380c'}
    run = {'id': pins['run_id'], 'workflow_id': pins['workflow_id'], 'path': pins['workflow_path'],
           'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
           'head_sha': PAYLOAD, 'head_branch': pins['head_branch'],
           'head_repository': {'full_name': pins['repository']}}
    artifact = {'id': pins['artifact_id'], 'name': pins['artifact_name'], 'expired': False,
                'size_in_bytes': pins['archive_size'], 'digest': 'sha256:' + pins['archive_sha256'],
                'workflow_run': {'id': pins['run_id'], 'head_sha': PAYLOAD}}
    return pins, run, artifact


def test_exact_original_quick_artifact_metadata_is_not_the_new_harness_identity(controller):
    pins, run, artifact = input_metadata()
    assert PAYLOAD != HARNESS
    controller.validate_input_metadata(run, artifact, pins)


@pytest.mark.parametrize('fault', ['run', 'workflow', 'path', 'event', 'status', 'conclusion',
                                  'source', 'branch', 'repository', 'artifact', 'name', 'expired',
                                  'archive-size', 'archive-digest', 'artifact-run', 'artifact-source'])
def test_immutable_artifact_metadata_rejects_other_or_unfinished_inputs(controller, fault):
    pins, run, artifact = input_metadata()
    if fault in ('run', 'workflow'):
        run['id' if fault == 'run' else 'workflow_id'] += 1
    elif fault in ('path', 'event', 'status', 'conclusion', 'source', 'branch'):
        key = {'path': 'path', 'event': 'event', 'status': 'status', 'conclusion': 'conclusion',
               'source': 'head_sha', 'branch': 'head_branch'}[fault]
        run[key] = 'other'
    elif fault == 'repository':
        run['head_repository'] = {'full_name': 'foreign/repository'}
    elif fault == 'artifact':
        artifact['id'] += 1
    elif fault == 'name':
        artifact['name'] = 'other'
    elif fault == 'expired':
        artifact['expired'] = True
    elif fault == 'archive-size':
        artifact['size_in_bytes'] += 1
    elif fault == 'archive-digest':
        artifact['digest'] = 'sha256:' + '0' * 64
    else:
        artifact['workflow_run']['id' if fault == 'artifact-run' else 'head_sha'] = 0
    with pytest.raises(ValueError):
        controller.validate_input_metadata(run, artifact, pins)


INSTALL_CHECKS = ('fresh_install_verified', 'same_version_reinstall_verified', 'owned_uninstall_verified',
    'payload_inventory_verified', 'shortcut_ownership_verified', 'application_bytes_preserved',
    'application_index_preserved', 'postgres_bytes_preserved', 'postgres_restart_verified',
    'installed_proofs_passed', 'runtime_cleanup_confirmed', 'process_completion_confirmed',
    'registered_installation_removed', 'owned_shortcuts_removed', 'standard_user_profile_verified', 'parent_job_drained')


def test_public_installation_receipt_contains_no_private_qa_or_platform_claims(controller):
    pins, _, _ = input_metadata()
    raw = {'ok': True, 'source_commit': PAYLOAD, 'installer_sha256': pins['installer_sha256'],
           'installer_bytes': pins['installer_size'], 'inventory_sha256': 'c' * 64,
           'os_product_type': 3, 'os_build': 26100, 'checks': dict.fromkeys(INSTALL_CHECKS, True),
           'os_family': 'Windows 11', 'win10_verified': True, 'path': 'PRIVATE_SENTINEL',
           'run_id': 'PRIVATE_SENTINEL', 'error': 'PRIVATE_SENTINEL', 'pid': 12345,
           'first_self_test': {'credential': 'PRIVATE_SENTINEL'}, 'raw_stderr': 'PRIVATE_SENTINEL'}
    receipt = controller.sanitize_installation_receipt(raw, pins=pins, harness_commit=HARNESS, exit_code=0)
    assert set(receipt) == {'schema_version', 'ok', 'payload_source_commit', 'harness_source_commit',
        'installer_sha256', 'installer_size', 'artifact_run_id', 'artifact_id', 'payload_inventory_sha256',
        'runner_platform', 'windows_validation', 'full_suite_passed', 'checks', 'open_gates'}
    assert receipt['ok'] is True and receipt['payload_source_commit'] == PAYLOAD
    assert receipt['harness_source_commit'] == HARNESS and receipt['full_suite_passed'] is False
    assert receipt['runner_platform'] == 'Windows Server 2025 x64' and receipt['windows_validation'] == []
    assert receipt['checks'] == dict.fromkeys(INSTALL_CHECKS, True)
    assert {'qt_upgrade', 'native_windows_11', 'windows_10', 'full_p4_acceptance'} <= set(receipt['open_gates'])
    assert 'PRIVATE_SENTINEL' not in json.dumps(receipt) and '12345' not in json.dumps(receipt)


@pytest.mark.parametrize('fault', ['nonzero', 'runtime', 'descendants', 'job', 'ownership', 'wrong-installer', 'wrong-source'])
def test_uncertain_cleanup_or_identity_never_produces_success_receipt(controller, fault):
    pins, _, _ = input_metadata()
    raw = {'ok': True, 'source_commit': PAYLOAD, 'installer_sha256': pins['installer_sha256'],
           'installer_bytes': pins['installer_size'], 'inventory_sha256': 'c' * 64,
           'os_product_type': 3, 'os_build': 26100, 'checks': dict.fromkeys(INSTALL_CHECKS, True)}
    if fault in ('runtime', 'descendants', 'job', 'ownership'):
        raw['checks'][{'runtime': 'runtime_cleanup_confirmed', 'descendants': 'process_completion_confirmed',
                       'job': 'parent_job_drained', 'ownership': 'shortcut_ownership_verified'}[fault]] = False
    elif fault == 'wrong-installer':
        raw['installer_sha256'] = '0' * 64
    elif fault == 'wrong-source':
        raw['source_commit'] = HARNESS
    receipt = controller.sanitize_installation_receipt(raw, pins=pins, harness_commit=HARNESS,
                                                      exit_code=1 if fault == 'nonzero' else 0)
    assert receipt['ok'] is False


@pytest.mark.parametrize('fault', ['desktop', 'service', 'extra', 'malformed'])
def test_inventory_cli_failure_exposes_only_fixed_predicates(tmp_path, fault):
    payload, manifest, owned, _, _ = inventory_fixture(tmp_path)
    if fault in ('desktop', 'service'):
        relative = 'ssearch.exe' if fault == 'desktop' else 'service/ssearch-service.exe'
        (payload / relative).write_bytes(b'PRIVATE_SENTINEL')
    elif fault == 'extra':
        (payload / 'PRIVATE_SENTINEL').write_bytes(b'PRIVATE_SENTINEL')
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    owned_path = tmp_path / 'owned.json'
    owned_path.write_text('{PRIVATE_SENTINEL' if fault == 'malformed' else json.dumps(owned), encoding='utf-8')
    result = subprocess.run([sys.executable, str(ROOT / 'tools/ci_installer.py'), 'inventory',
        '--directory', str(payload), '--manifest', str(manifest_path), '--owned', str(owned_path),
        '--output', str(tmp_path / 'observed.json')], capture_output=True, timeout=30)
    assert result.returncode == 1, 'Inventory diagnostic must retain the original failure'
    output = result.stdout.decode('ascii')
    private_absent = all(value not in output for value in ('PRIVATE_SENTINEL', str(tmp_path), 'Traceback'))
    assert private_absent, 'Inventory diagnostic disclosed private data'
    lines = output.splitlines()
    code = ('installer_payload_image_mismatch' if fault in ('desktop', 'service') else
            'installer_inventory_mismatch' if fault == 'extra' else 'unavailable')
    valid_code = bool(lines) and lines[-1] == 'CI_INSTALLER_HELPER code=' + code
    assert valid_code, 'Inventory failure did not produce its closed machine code'
    if fault == 'malformed':
        assert len(lines) == 1, 'Malformed input must not export partial private evidence'
    else:
        desktop = 'false' if fault == 'desktop' else 'true'
        service = 'false' if fault == 'service' else 'true'
        inventory = 'false' if fault == 'extra' else 'unavailable'
        valid_observation = len(lines) == 2 and re.fullmatch(
            f'CI_INSTALLER_INVENTORY files={4 if fault == "extra" else 3} desktop_match={desktop} '
            f'service_match={service} inventory_match={inventory}', lines[0]) is not None
        assert valid_observation, 'Inventory observation must contain only bounded count and match flags'
    clean_stderr = result.stderr in (b'INSTALLER_PROOF_FAILED\n', b'INSTALLER_PROOF_FAILED\r\n')
    assert clean_stderr, 'Inventory stderr must contain only the fixed failure code'


def test_installer_parent_output_filter_rejects_private_and_unknown_lines(tmp_path):
    inputs = [
        ('CI_INSTALLER_PHASE fresh_install fail', True),
        ('CI_INSTALLER_FRESH inventory started', True),
        ('CI_INSTALLER_FRESH reference_inventory pass', True),
        ('CI_INSTALLER_FAILURE substep=inventory hresult=-2146233087 wait_reason=complete exit_integer=true exit_zero=true', True),
        ('CI_INSTALLER_INVENTORY files=5001 desktop_match=true service_match=true inventory_match=false', True),
        ('CI_INSTALLER_HELPER code=installer_inventory_mismatch', True),
        ('CI_INSTALLER_HELPER code=unavailable', True),
        ('CI_INSTALLER_SHORTCUT_GROUP exists=true available=true count=3 known=3 unknown=0', True),
        ('CI_INSTALLER_SHORTCUT role=help exists=true target_match=false', True),
        ('CI_INSTALLER_COM stage=read status=fail role=help hresult=-2146233087', True),
        ('PRIVATE_SENTINEL', False),
        ('CI_INSTALLER_FRESH PRIVATE_SENTINEL fail', False),
        ('CI_INSTALLER_HELPER code=PRIVATE_SENTINEL', False),
        ('CI_INSTALLER_HELPER code=installer_inventory_mismatch PRIVATE_SENTINEL', False),
        ('CI_INSTALLER_FAILURE substep=inventory hresult=PRIVATE_SENTINEL wait_reason=complete exit_integer=true exit_zero=true', False),
        ('CI_INSTALLER_FAILURE substep=inventory hresult=1 wait_reason=PRIVATE_SENTINEL exit_integer=true exit_zero=true', False),
        ('CI_INSTALLER_INVENTORY files=1234567 desktop_match=true service_match=true inventory_match=false', False),
        ('CI_INSTALLER_INVENTORY files=5 desktop_match=PRIVATE_SENTINEL service_match=true inventory_match=false', False),
        ('CI_INSTALLER_PHASE fresh_install pass\nPRIVATE_SENTINEL', False),
        ('CI_INSTALLER_SHORTCUT role=PRIVATE_SENTINEL exists=true target_match=true', False),
        ('CI_INSTALLER_SHORTCUT_GROUP exists=true available=true count=3 known=3 unknown=PRIVATE_SENTINEL', False),
        ('CI_INSTALLER_COM stage=read status=fail role=help hresult=PRIVATE_SENTINEL', False),
    ]
    data = tmp_path / 'inputs.json'
    data.write_text(json.dumps([line for line, _ in inputs]), encoding='utf-8')
    script = tmp_path / 'filter.ps1'
    script.write_text(r'''
param($Source,$InputFile)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'filter_syntax_invalid'}
$fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -ceq 'Test-InstallerPublicLine'},$true)
. ([scriptblock]::Create($fn.Extent.Text))
$values=Get-Content -LiteralPath $InputFile -Raw -Encoding UTF8 | ConvertFrom-Json
@(foreach($value in $values) {[bool](Test-InstallerPublicLine $value)}) | ConvertTo-Json -Compress
''', encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script),
        str(ROOT / 'tools/run_ci_build.ps1'), str(data)], capture_output=True, timeout=30)
    assert result.returncode == 0, 'Actual parent output policy probe failed'
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        pytest.fail('parent_output_policy_result_invalid', pytrace=False)
    assert observed == [expected for _, expected in inputs]


def test_installer_helper_forwards_safe_host_lines_before_failure_and_keeps_policy_json(tmp_path):
    """Real PS helper and parent filter; only the external producer is fake."""
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'ci_installer.py').write_text(
        'import sys\n'
        'if sys.argv[1] == "policy":\n'
        '    print(\'{"budgets_ms":{"load":5000}}\')\n'
        'else:\n'
        '    print("CI_INSTALLER_INVENTORY files=3 desktop_match=true service_match=true inventory_match=false")\n'
        '    print("CI_INSTALLER_HELPER code=installer_inventory_mismatch")\n'
        '    print("PRIVATE_SENTINEL")\n'
        'sys.exit(1 if sys.argv[1] == "fail" else 0)\n', encoding='utf-8')
    script = tmp_path / 'pipeline.ps1'
    script.write_text(r'''
param($Controller,$Parent,$Fixture,$Interpreter)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
foreach($entry in @(@{path=$Controller;name='Invoke-InstallerHelper'},
                   @{path=$Parent;name='Test-InstallerPublicLine'})) {
    $ast=[Management.Automation.Language.Parser]::ParseFile($entry.path,[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'helper_pipeline_syntax_invalid'}
    $fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq $entry.name},$true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
$repository=$Fixture;$Python=$Interpreter
$outcomes=@()
foreach($mode in @('fail','pass')) {
    $script:caught=$false
    # The actual caller discards success-pipeline output. Host diagnostics must
    # survive that discard and must already exist when the helper throws.
    $raw=@(& {try {$null=Invoke-InstallerHelper @($mode)} catch {$script:caught=$true}} 6>&1)
    $lines=@($raw | ForEach-Object {[string]$_})
    $public=@($lines | Where-Object {Test-InstallerPublicLine $_})
    $outcomes+=($public.Count -eq 2 -and
        $public[0] -ceq 'CI_INSTALLER_INVENTORY files=3 desktop_match=true service_match=true inventory_match=false' -and
        $public[1] -ceq 'CI_INSTALLER_HELPER code=installer_inventory_mismatch' -and
        -not ($lines -match 'PRIVATE_SENTINEL') -and $script:caught -eq ($mode -ceq 'fail'))
}
$policy=Invoke-InstallerHelper @('policy')
$outcomes+=((($policy | ConvertFrom-Json).budgets_ms.load) -eq 5000)
$outcomes | ConvertTo-Json -Compress
''', encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(script), str(ROOT / 'tools/check_installer.ps1'), str(ROOT / 'tools/run_ci_build.ps1'),
        str(tmp_path), sys.executable], capture_output=True, timeout=30)
    assert result.returncode == 0, 'Actual helper/parent output pipeline probe failed'
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        pytest.fail('helper_pipeline_result_invalid', pytrace=False)
    assert observed == [True, True, True]


@pytest.mark.parametrize('fault', ['valid', 'missing', 'wrong-target', 'ascii'])
def test_actual_shortcut_guard_with_owned_unicode_links_under_pwsh(tmp_path, fault):
    """Actual Windows COM + actual guard, matching the installed QA host.

    Tiny owned files are shortcut destinations only; no application, installer,
    Qt or executable fixture runs. COM unavailability is a failure, never skip.
    """
    script = tmp_path / 'shortcuts.ps1'
    script.write_text(r'''
param($Controller,$Parent,$Root,$Fault)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$fixtureStage='compile'
try {
# IShellLinkW vtable/IID: Microsoft WinSDK ShObjIdl_core.h; persistence uses
# Unicode IPersistFile. Never create the fixture through WScript.TargetPath.
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
public class SsearchFixtureLinkClass {}
[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ISsearchFixtureLinkW {
    [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr data, uint flags);
    [PreserveSig] int GetIDList(out IntPtr value);
    [PreserveSig] int SetIDList(IntPtr value);
    [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
    [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
    [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    [PreserveSig] int SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
    [PreserveSig] int GetHotkey(out ushort value);
    [PreserveSig] int SetHotkey(ushort value);
    [PreserveSig] int GetShowCmd(out int value);
    [PreserveSig] int SetShowCmd(int value);
    [PreserveSig] int GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size, out int index);
    [PreserveSig] int SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string value, int index);
    [PreserveSig] int SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string value, uint reserved);
    [PreserveSig] int Resolve(IntPtr window, uint flags);
    [PreserveSig] int SetPath([MarshalAs(UnmanagedType.LPWStr)] string value);
}
[ComImport, Guid("0000010b-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ISsearchFixturePersistFile {
    [PreserveSig] int GetClassID(out Guid value);
    [PreserveSig] int IsDirty();
    [PreserveSig] int Load([MarshalAs(UnmanagedType.LPWStr)] string path, uint mode);
    [PreserveSig] int Save([MarshalAs(UnmanagedType.LPWStr)] string path, [MarshalAs(UnmanagedType.Bool)] bool remember);
    [PreserveSig] int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string path);
    [PreserveSig] int GetCurFile(out IntPtr value);
}
public static class SsearchLinkFixture {
    public static string Stage = "create";
    static void Check(int value) { if(value != 0) throw new COMException("fixture_hresult", value); }
    public static void Create(string path, string target) {
        if(!Path.IsPathRooted(path) || !Path.IsPathRooted(target) || File.Exists(path))
            throw new InvalidOperationException("fixture_boundary");
        Stage="create";
        object writer=new SsearchFixtureLinkClass();
        try {
            Stage="set_path"; Check(((ISsearchFixtureLinkW)writer).SetPath(target));
            Stage="save"; Check(((ISsearchFixturePersistFile)writer).Save(path, true));
        } finally { Marshal.FinalReleaseComObject(writer); }
        Stage="create";
        object reader=new SsearchFixtureLinkClass();
        try {
            Stage="load"; Check(((ISsearchFixturePersistFile)reader).Load(path, 0));
            var buffer=new StringBuilder(260);
            Stage="get_path"; Check(((ISsearchFixtureLinkW)reader).GetPath(buffer, buffer.Capacity, IntPtr.Zero, 0));
            Stage="verify";
            if(buffer.Length == 0 || buffer.Length >= 259 || !String.Equals(buffer.ToString(), target, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("fixture_unicode_roundtrip");
        } finally { Marshal.FinalReleaseComObject(reader); }
    }
}
'@
$fixtureStage='guard_setup'
$tokens=$null;$errors=$null
foreach($entry in @(@{path=$Controller;name='Assert-ShortcutTargets'},
                   @{path=$Parent;name='Test-InstallerPublicLine'})) {
    $ast=[Management.Automation.Language.Parser]::ParseFile($entry.path,[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'shortcut_guard_syntax_invalid'}
    $fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq $entry.name},$true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
$isElectron=$true;$comparison=[StringComparison]::OrdinalIgnoreCase
$fixtureStage='filesystem'
$appPath=Join-Path $Root $(if($Fault -ceq 'ascii') {'PRIVATE_SENTINEL app with spaces'} else {'PRIVATE_SENTINEL приложение with spaces'})
$groupPath=Join-Path $Root $(if($Fault -ceq 'ascii') {'PRIVATE_SENTINEL menu'} else {'PRIVATE_SENTINEL menu Кириллица'})
$appExecutable=Join-Path $appPath 'ssearch.exe'
$uninstallerPath=Join-Path $appPath 'unins000.exe'
$help=Join-Path $appPath 'service/_internal/resources/help.html'
$null=New-Item -ItemType Directory -Path (Split-Path -Parent $help),$groupPath -Force
foreach($path in @($appExecutable,$uninstallerPath,$help)) {[IO.File]::WriteAllText($path,'owned destination')}
$other=Join-Path $Root 'PRIVATE_SENTINEL other destination'
[IO.File]::WriteAllText($other,'preserved private data')
$fixtureStage='create'
foreach($entry in @(@{name='ssearch.lnk';target=$appExecutable},
                   @{name='Справка ssearch.lnk';target=$help},
                   @{name='Удалить ssearch.lnk';target=$uninstallerPath})) {
    [string]$selectedTarget=if($Fault -ceq 'wrong-target' -and $entry.name -ceq 'Справка ssearch.lnk') {$other} else {$entry.target}
    [SsearchLinkFixture]::Create((Join-Path $groupPath $entry.name),$selectedTarget)
}
if($Fault -ceq 'missing'){Remove-Item -LiteralPath (Join-Path $groupPath 'Справка ssearch.lnk')}
$script:caught=$false
$captured=@(& {try {Assert-ShortcutTargets} catch {$script:caught=$true}} 6>&1)
$lines=@($captured | ForEach-Object {[string]$_})
$allowed=@($lines | Where-Object {-not (Test-InstallerPublicLine $_)}).Count -eq 0
$expected=if($Fault -ceq 'missing') {'CI_INSTALLER_SHORTCUT role=help exists=false target_match=unavailable'}
    elseif($Fault -ceq 'wrong-target') {'CI_INSTALLER_SHORTCUT role=help exists=true target_match=false'}
    else {'CI_INSTALLER_SHORTCUT role=uninstall exists=true target_match=true'}
@{fixture_ok=$true;contract=($script:caught -eq ($Fault -cnotin @('valid','ascii')));
    classified=($lines.Count -gt 0 -and $allowed);
    expected_predicate=(@($lines | Where-Object {$_ -ceq $expected}).Count -ge 1);
    private_absent=(@($lines | Where-Object {$_.Contains('PRIVATE_SENTINEL')}).Count -eq 0);
    unrelated_data_preserved=([IO.File]::ReadAllText($other) -ceq 'preserved private data')} | ConvertTo-Json -Compress
} catch {
    if($fixtureStage -ceq 'create' -and ('SsearchLinkFixture' -as [type])) {$fixtureStage=[SsearchLinkFixture]::Stage}
    $hresult=[int]$_.Exception.HResult
    if($_.Exception.InnerException -is [Runtime.InteropServices.COMException]) {$hresult=[int]$_.Exception.InnerException.HResult}
    @{fixture_ok=$false;stage=$fixtureStage;hresult=$hresult} | ConvertTo-Json -Compress
}
''', encoding='utf-8-sig')
    result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(script),
        str(ROOT / 'tools/check_installer.ps1'), str(ROOT / 'tools/run_ci_build.ps1'), str(tmp_path), fault],
        capture_output=True, timeout=30)
    clean_exit = result.returncode == 0
    assert clean_exit, 'Real Windows COM shortcut component probe failed'
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        pytest.fail('shortcut_component_result_invalid', pytrace=False)
    fixture_ok = observed.get('fixture_ok') is True
    stage = observed.get('stage')
    stage = stage if stage in ('compile', 'guard_setup', 'filesystem', 'create', 'set_path', 'save', 'load', 'get_path', 'verify') else 'unavailable'
    hresult = observed.get('hresult')
    hresult = hresult if type(hresult) is int and -(2**31) <= hresult < 2**31 else None
    assert fixture_ok, {'fixture_stage': stage, 'hresult': hresult}
    assert observed == dict.fromkeys(('fixture_ok', 'contract', 'classified', 'expected_predicate',
                                      'private_absent', 'unrelated_data_preserved'), True)
