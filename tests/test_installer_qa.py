"""Execute the real installed-report validator with controlled JSON, no install."""
import json
from pathlib import Path
import subprocess


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
