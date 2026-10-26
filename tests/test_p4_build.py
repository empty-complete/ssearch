"""Common Electron build routing at fake external boundaries, never native proof."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'a' * 40


@pytest.fixture
def build_fixture(tmp_path):
    for name in ('tools', 'packaging', 'resources/ci', 'node', 'frontend-cache',
                 'desktop-cache', 'electron-zip'):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'tools/build.ps1', tmp_path / 'tools/build.ps1')
    shutil.copyfile(ROOT / 'resources/ci/build-inputs.json', tmp_path / 'resources/ci/build-inputs.json')
    (tmp_path / 'compiler.exe').write_bytes(b'compiler boundary')
    (tmp_path / 'packaging/build_support.py').write_text(r'''
import json, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(__file__).resolve().parents[1]
command = args[0]
with (root/'helper-calls.jsonl').open('a') as output:
    output.write(json.dumps(args)+'\n')
def values(flag): return [arg.split('=',1)[1] for arg in args if arg.startswith(flag+'=')]
def value(flag): return args[args.index(flag)+1]
if command in ('test-selection', 'desktop-test-selection'):
    python, frontend, desktop = values('--test-target'), values('--frontend-test-target'), values('--desktop-test-target')
    quick = '--quick' in args
    if (quick and ('--development' not in args or not (python or frontend or desktop))) or (not quick and (python or frontend or desktop)):
        raise SystemExit(2)
    selection = dict(scope='targeted' if quick else 'full', test_targets=python)
    if command == 'desktop-test-selection': selection.update(frontend_test_targets=frontend, desktop_test_targets=desktop)
    print(json.dumps(selection))
elif command == 'development-start':
    base=root/'artifacts'/('development-quick' if '--quick' in args else 'development')/('a'*40)
    layout=dict(base=str(base), **{name:str(base/name) for name in ('artifacts','dist','build','wheel','qa')})
    for path in layout.values(): pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    print(json.dumps(layout))
elif command == 'core-wheel':
    path=pathlib.Path(value('--directory'))/'core.whl'
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'wheel boundary'); print(path)
elif command == 'version': print('0.2.0')
elif command == 'ui-proof-policy':
    print(json.dumps(dict(required_checks={}, budgets_ms=dict(load=1700,security=2700,lifecycle=3700,**{'empty-profile':4700}),policy_sha256='f'*64)))
''', encoding='utf-8')
    # Forge owns the sole installation-payload service copy. This stand-in only
    # exposes its documented receipt so common-harness routing can be checked.
    (tmp_path / 'tools/build_desktop.ps1').write_text(r'''
param($Python,$NodeDirectory,$FrontendCachePath,$DesktopCachePath,$ElectronZipDirectory,
      $ElectronZipSha256,$ServiceStageDirectory,$OutputDirectory,[switch]$Development,
      [switch]$Quick,[string[]]$TestTargets=@(),[string[]]$FrontendTestTargets=@(),
      [string[]]$DesktopTestTargets=@())
$root = Split-Path -Parent $PSScriptRoot
$null = New-Item -ItemType Directory -Path $OutputDirectory
$package = Join-Path $OutputDirectory 'ssearch-win32-x64'
$null = New-Item -ItemType Directory -Path $package
[IO.File]::WriteAllText((Join-Path $package 'ssearch.exe'), 'Electron boundary')
Copy-Item -LiteralPath $ServiceStageDirectory -Destination (Join-Path $package 'service') -Recurse
$inventory = Join-Path $OutputDirectory 'payload-inventory.json'
[IO.File]::WriteAllText($inventory, '{}')
$metadata = @{schema_version=1;source_commit=('a'*40);app_version='0.2.0';core_version='0.2.0';
    product=@{product_name='ssearch';product_name_ru='Simple Search';author_name='Fixture Author';
    organization_group='fixture';support_email='fixture@example.invalid';copyright_holder='Fixture Author'}}
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'product-metadata.json'), ($metadata|ConvertTo-Json -Depth 5))
$null = New-Item -ItemType Directory -Path (Join-Path $package 'resources')
Copy-Item -LiteralPath (Join-Path $OutputDirectory 'product-metadata.json') -Destination (Join-Path $package 'resources/product-metadata.json')
$receipt = @{schema_version=1;source_commit=('a'*40);test_scope=$(if($Quick){'targeted'}else{'full'});
    python_tests_delegated=@($TestTargets);frontend_test_targets=@($FrontendTestTargets);
    desktop_test_targets=@($DesktopTestTargets);packaged_path=$package;payload_inventory=$inventory;
    package_only=$true;frozen_acceptance=$false}
if ($env:P4_BUILD_FAULT -eq 'receipt') { $receipt.source_commit=('b'*40) }
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'desktop-build-receipt.json'), ($receipt|ConvertTo-Json -Depth 5))
$PSBoundParameters | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'desktop-call.json') -Encoding UTF8
''', encoding='utf-8-sig')
    driver = tmp_path / 'driver.ps1'
    driver.write_text(r'''
param($Root,$Python,$Mode)
$ErrorActionPreference='Stop'
$build=Join-Path $Root 'tools/build.ps1'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($build,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Invalid harness syntax' }
$boundary=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-Checked'},$true)
$ownedBoundary=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-OwnedProof'},$true)
if (!$ownedBoundary) { throw 'Bounded frozen native proof controller is required' }
. ([scriptblock]::Create($ownedBoundary.Extent.Text.Replace('function Invoke-OwnedProof','function Invoke-OriginalOwnedProof')))
$fake=@'
function Invoke-Checked {
    param([string]$Program,[string[]]$Arguments)
    $native = $Arguments -contains '--postgres-self-test' -or $Arguments -contains '--self-test' -or
        $Arguments -contains '--service-test' -or $Arguments -contains '--ui-self-test'
    $timeoutSeconds = 0
    if ($native) { $timeoutSeconds = $script:proofBudget }
    $profilesPreserved = $true
    foreach ($name in @('LOCALAPPDATA','APPDATA','TEMP','TMP')) {
        if ([Environment]::GetEnvironmentVariable($name,'Process') -cne $expectedProfiles[$name]) { $profilesPreserved=$false }
    }
    @{program=$Program;arguments=@($Arguments);native=$native;profiles_preserved=$profilesPreserved;
      isolated_path=($env:PATH -ceq (Join-Path $env:SystemRoot 'System32'));
      source_bootstrap_absent=(!(Test-Path Env:PYTHONPATH));timeout_seconds=$timeoutSeconds} | ConvertTo-Json -Compress |
        Add-Content -LiteralPath (Join-Path $repository 'calls.jsonl') -Encoding UTF8
    if ($Arguments -contains $helper) {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) { throw 'Helper rejected build' }
    } elseif ($Arguments -contains 'PyInstaller') {
        $stage=Join-Path $Arguments[([array]::IndexOf($Arguments,'--distpath')+1)] 'ssearch-service'
        $null=New-Item -ItemType Directory -Path (Join-Path $stage '_internal/models') -Force
        [IO.File]::WriteAllText((Join-Path $stage 'ssearch-service.exe'),'service image')
    } elseif ($Arguments -contains '--service-executable') {
        if ($env:P4_BUILD_FAULT -eq 'serve') { throw 'Frozen serve failed' }
        [IO.File]::WriteAllText($Arguments[([array]::IndexOf($Arguments,'--report')+1)],'{}')
    } elseif ($Arguments -contains '--ui-self-test') {
        if ($env:P4_BUILD_FAULT -eq 'ui') { throw 'Packaged UI proof failed' }
        [IO.File]::WriteAllText($Arguments[([array]::IndexOf($Arguments,'--ui-self-test')+1)],'{}')
    } elseif ($Arguments -contains '--postgres-self-test' -or $Arguments -contains '--self-test' -or $Arguments -contains '--service-test') {
        [IO.File]::WriteAllText($Arguments[1],'{}')
        if ($env:P4_BUILD_FAULT -eq 'image') { [IO.File]::AppendAllText($Program,'changed') }
    } elseif ($Program -eq (Join-Path $repository 'compiler.exe')) {
        $destination=($Arguments|Where-Object {$_.StartsWith('/DOutputDir=')}).Substring(12)
        [IO.File]::WriteAllText((Join-Path $destination 'ssearch-Setup-0.2.0.exe'),'installer boundary')
    }
}
'@
$text=[IO.File]::ReadAllText($build)
# Replace from the later offset first so the checked-boundary offsets stay valid.
$ownedFake=@'
function Invoke-OwnedProof {
    param([string]$Program,[string[]]$Arguments,[int]$TimeoutSeconds)
    if ($env:P4_BUILD_FAULT -eq 'hung') {
        # A real retained toy child fills both pipes before hanging. Never starts
        # native service/model/PG or delegates cleanup to a discovered PID.
        Invoke-OriginalOwnedProof $Python @('-c', 'import sys,time; sys.stdout.write(''PRIVATE_CAPTURE''*20000); sys.stdout.flush(); sys.stderr.write(''PRIVATE_CAPTURE''*20000); sys.stderr.flush(); time.sleep(60)') 1
        throw 'Hung child was incorrectly accepted'
    }
    $script:proofBudget=$TimeoutSeconds
    try { Invoke-Checked $Program $Arguments }
    finally { $script:proofBudget=0 }
}
'@
$text=$text.Remove($ownedBoundary.Extent.StartOffset,$ownedBoundary.Extent.EndOffset-$ownedBoundary.Extent.StartOffset).Insert($ownedBoundary.Extent.StartOffset,$ownedFake)
$text=$text.Remove($boundary.Extent.StartOffset,$boundary.Extent.EndOffset-$boundary.Extent.StartOffset).Insert($boundary.Extent.StartOffset,$fake)
[IO.File]::WriteAllText($build,$text,[Text.UTF8Encoding]::new($true))
$options=@{Electron=$true;Development=$true;Python=$Python;ISCC=(Join-Path $Root 'compiler.exe');
    NodeDirectory=(Join-Path $Root 'node');FrontendCachePath=(Join-Path $Root 'frontend-cache');
    DesktopCachePath=(Join-Path $Root 'desktop-cache');ElectronZipDirectory=(Join-Path $Root 'electron-zip');
    ElectronZipSha256='ae53887a23bb640369c93dbd38c408a2af5c0deb86f2fa76c6c244cb444b7f47'}
switch($Mode) {
    'ui' { $options.Quick=$true; $options.FrontendTestTargets=@('tests/view.test.tsx') }
    'mixed' { $options.Quick=$true; $options.TestTargets=@('tests/test_owned.py::test_owner');$options.DesktopTestTargets=@('tests/owner.test.ts') }
    'invalid' { $options.Quick=$true }
    'no-development' { $options.Development=$false }
    'bad-pin' { $options.ElectronZipSha256=('0'*64) }
    'qt-target' { $options.Electron=$false; $options.FrontendTestTargets=@('tests/view.test.tsx') }
}
$expectedProfiles=@{}
foreach($name in @('LOCALAPPDATA','APPDATA','TEMP','TMP')) {
    $expectedProfiles[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
}
$before=@{}
foreach($name in @('PATH','LOCALAPPDATA','APPDATA','TEMP','TMP','PYTHONPATH','PYTHONUSERBASE','PYTHONNOUSERSITE','PYINSTALLER_CONFIG_DIR')) {
    $before[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
}
try { & $build @options }
finally {
    foreach($name in $before.Keys) {
        if ([Environment]::GetEnvironmentVariable($name,'Process') -cne $before[$name]) { throw 'Build environment was not restored' }
    }
}
''', encoding='utf-8-sig')
    return tmp_path, driver


def exercise(build_fixture, mode, fault=None):
    root, driver = build_fixture
    environment = dict(os.environ)
    environment['P4_BUILD_FAULT'] = fault or ''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(driver), str(root), sys.executable, mode], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=60, env=environment)
    calls = root / 'calls.jsonl'
    records = [json.loads(line) for line in calls.read_text(encoding='utf-8-sig').splitlines()] if calls.exists() else []
    return root, result, records


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Common Windows build harness')


@pytest.mark.parametrize('mode,targets', [('full', []), ('ui', None),
    ('mixed', ['tests/test_owned.py::test_owner'])])
def test_electron_selection_routes_python_once_and_forge_once(build_fixture, mode, targets):
    root, result, records = exercise(build_fixture, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    python = [row['arguments'] for row in records if any(arg.endswith('ci_pytest.py') for arg in row['arguments'])]
    assert python == ([] if targets is None else [['-u', str(root/'tools/ci_pytest.py'), '--timeout-seconds', '3600', *targets]])
    desktop = json.loads((root / 'desktop-call.json').read_text(encoding='utf-8-sig'))
    assert desktop['Python'] == sys.executable
    assert desktop['NodeDirectory'] == str(root/'node')
    assert desktop['FrontendCachePath'] == str(root/'frontend-cache')
    assert desktop['DesktopCachePath'] == str(root/'desktop-cache')
    assert desktop['ElectronZipDirectory'] == str(root/'electron-zip')
    assert desktop['ElectronZipSha256'] == json.loads((root/'resources/ci/build-inputs.json').read_text())['electron_runtime']['archive']['sha256']
    assert desktop['ServiceStageDirectory'].endswith('ssearch-service')
    assert desktop['FrontendTestTargets'] == (['tests/view.test.tsx'] if mode == 'ui' else [])
    assert desktop['DesktopTestTargets'] == (['tests/owner.test.ts'] if mode == 'mixed' else [])
    assert all('atom.spec' not in arg for row in records for arg in row['arguments'])
    assert len([row for row in records if 'PyInstaller' in row['arguments']]) == 1
    assert any('ssearch-service.spec' in arg for row in records for arg in row['arguments'])
    assert len([row for row in records if '--postgres-self-test' in row['arguments']]) == 1
    assert len([row for row in records if '--self-test' in row['arguments']]) == 1
    assert len([row for row in records if '--service-test' in row['arguments']]) == 1
    native = [row for row in records if row['native']]
    assert len(native) == 7
    assert all(row['isolated_path'] and row['source_bootstrap_absent'] and row['profiles_preserved'] for row in native)
    for row in native:
        args = row['arguments']
        if '--ui-scenario' in args:
            scenario = args[args.index('--ui-scenario')+1]
            assert row['timeout_seconds'] == {'load':122, 'security':123, 'lifecycle':124, 'empty-profile':125}[scenario]
        else:
            assert row['timeout_seconds'] == 600
    serve = next(row for row in records if '--service-executable' in row['arguments'])
    executable = Path(serve['arguments'][serve['arguments'].index('--service-executable')+1])
    assert ' ' in str(executable) and not str(executable).isascii()
    ui = [row for row in records if '--ui-self-test' in row['arguments']]
    assert [row['arguments'][row['arguments'].index('--ui-scenario')+1] for row in ui] == ['load', 'security', 'lifecycle', 'empty-profile']
    compiler = next(row for row in records if row['program'] == str(root/'compiler.exe'))
    assert '/DElectronBundle=1' in compiler['arguments']
    assert '/DSourceDir=' + str(Path(desktop['OutputDirectory'])/'ssearch-win32-x64') in compiler['arguments']
    helpers = [json.loads(line) for line in (root/'helper-calls.jsonl').read_text().splitlines()]
    for command in ('development-start', 'test-receipt', 'development-manifest'):
        invocation = next(args for args in helpers if args[0] == command)
        assert '--desktop-stack' in invocation
    inventories = [args for args in helpers if args[0] == 'verify-desktop-bundle']
    assert len(inventories) == 3
    assert all('--inventory' in args and '--output' not in args for args in inventories)


@pytest.mark.parametrize('fault', ['serve', 'ui', 'receipt', 'image', 'hung'])
def test_failed_frozen_or_forge_identity_stops_before_inno_and_manifest(build_fixture, fault):
    root, result, records = exercise(build_fixture, 'ui', fault)
    assert result.returncode != 0
    assert not any(row['program'] == str(root/'compiler.exe') for row in records)
    helpers = [json.loads(line) for line in (root/'helper-calls.jsonl').read_text().splitlines()]
    assert not any(args[0] == 'development-manifest' for args in helpers)
    if fault == 'hung':
        assert 'FROZEN_NATIVE_TIMEOUT' in result.stdout + result.stderr
        assert 'PRIVATE_CAPTURE' not in result.stdout + result.stderr


@pytest.mark.parametrize('mode', ['invalid', 'no-development', 'bad-pin', 'qt-target'])
def test_invalid_electron_selection_stops_before_wheel_or_outputs(build_fixture, mode):
    root, result, records = exercise(build_fixture, mode)
    assert result.returncode != 0
    assert not records
    assert not (root/'artifacts').exists()
