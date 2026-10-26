"""Explicit build selections cannot silently turn a UI Quick into full pytest."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ui_build_support', ROOT / 'packaging/build_support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


@pytest.fixture
def repository(tmp_path):
    for filename in ('tests/test_service.py', 'frontend/tests/candidate.test.tsx',
                     'desktop/tests/lifecycle.test.ts'):
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('// test fixture', encoding='utf-8')
    return tmp_path


def test_ui_only_quick_has_no_python_targets(repository):
    selection = support.validate_desktop_test_selection(
        repository, development=True, quick=True,
        frontend_test_targets=['tests/candidate.test.tsx'])
    assert selection == {'scope': 'targeted', 'test_targets': [],
                         'frontend_test_targets': ['tests/candidate.test.tsx'],
                         'desktop_test_targets': []}


def test_mixed_quick_preserves_explicit_suite_boundaries(repository):
    selection = support.validate_desktop_test_selection(
        repository, development=True, quick=True,
        test_targets=['tests/test_service.py::test_owner'],
        frontend_test_targets=['tests/candidate.test.tsx'],
        desktop_test_targets=['tests/lifecycle.test.ts'])
    assert selection['test_targets'] == ['tests/test_service.py::test_owner']
    assert selection['frontend_test_targets'] == ['tests/candidate.test.tsx']
    assert selection['desktop_test_targets'] == ['tests/lifecycle.test.ts']


@pytest.mark.parametrize('options', [
    {'development': True, 'quick': True},
    {'quick': True, 'desktop_test_targets': ['tests/lifecycle.test.ts']},
    {'development': True, 'desktop_test_targets': ['tests/lifecycle.test.ts']},
])
def test_empty_or_mismatched_mode_is_rejected(repository, options):
    with pytest.raises(support.BuildError):
        support.validate_desktop_test_selection(repository, **options)


@pytest.mark.parametrize('target', [
    '--passWithNoTests', '../tests/candidate.test.tsx', 'tests/../candidate.test.tsx',
    'tests/missing.test.tsx', 'tests/candidate.test.tsx::case', 'tests/candidate.test.tsx\n',
    'C:/external/candidate.test.tsx', 'tests/*.test.tsx',
])
def test_frontend_targets_cannot_be_options_external_paths_or_patterns(repository, target):
    with pytest.raises(support.BuildError):
        support.validate_desktop_test_selection(repository, development=True, quick=True,
                                               frontend_test_targets=[target])


def test_desktop_does_not_accept_frontend_tsx_or_undiscoverable_files(repository):
    for filename in ('candidate.test.tsx', 'helper.ts'):
        (repository / 'desktop/tests' / filename).write_text('// fixture', encoding='utf-8')
        with pytest.raises(support.BuildError):
            support.validate_desktop_test_selection(repository, development=True, quick=True,
                                                   desktop_test_targets=['tests/' + filename])


def test_full_selects_all_suites_without_synthetic_python_target(repository):
    assert support.validate_desktop_test_selection(repository) == {
        'scope': 'full', 'test_targets': [], 'frontend_test_targets': [], 'desktop_test_targets': []}


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell environment restoration')
@pytest.mark.parametrize('prior_value', [None, 'previous-build-value'], ids=['absent', 'present'])
def test_failed_desktop_preflight_restores_parent_environment(tmp_path, prior_value):
    powershell = shutil.which('powershell.exe')
    assert powershell, 'Windows build requires PowerShell'
    names = ('SSEARCH_PRODUCT_METADATA', 'SSEARCH_ELECTRON_ZIP_DIR', 'SSEARCH_ELECTRON_ZIP_SHA256',
             'SSEARCH_SERVICE_STAGE_DIR')
    environment = {key: value for key, value in os.environ.items() if key.upper() not in names}
    if prior_value is not None:
        environment.update(dict.fromkeys(names, prior_value))
    driver = tmp_path / 'environment-proof.ps1'
    driver.write_text(r'''
param([string]$BuildScript, [string]$Directory, [string]$Prior)
$ErrorActionPreference = 'Stop'
try {
    & $BuildScript -Python (Join-Path $Directory 'missing-python.exe') -NodeDirectory $Directory `
        -FrontendCachePath $Directory -DesktopCachePath $Directory -OutputDirectory $Directory `
        -ElectronZipDirectory $Directory -ElectronZipSha256 ('a' * 64) -ServiceStageDirectory $Directory
    throw 'Expected preflight refusal'
} catch {
    if ($_.Exception.Message -ne 'Prepared build runtime is missing.') { throw }
}
foreach ($name in @('SSEARCH_PRODUCT_METADATA', 'SSEARCH_ELECTRON_ZIP_DIR', 'SSEARCH_ELECTRON_ZIP_SHA256',
                    'SSEARCH_SERVICE_STAGE_DIR')) {
    if ($Prior -eq 'absent') {
        if (Test-Path -LiteralPath "Env:\$name") { throw 'Environment entry leaked' }
    } elseif ([Environment]::GetEnvironmentVariable($name, 'Process') -cne 'previous-build-value') {
        throw 'Previous environment value changed'
    }
}
Write-Output 'environment_restored'
''', encoding='utf-8')
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(driver),
                             '-BuildScript', str(ROOT / 'tools/build_desktop.ps1'),
                             '-Directory', str(tmp_path), '-Prior', 'absent' if prior_value is None else 'present'],
                            env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, 'Desktop preflight environment proof failed'
    assert result.stdout.strip() == 'environment_restored'
    assert result.stderr == ''


@pytest.mark.skipif(os.name != 'nt', reason='Inherited PowerShell strict-mode offline contract')
@pytest.mark.parametrize('missing_integrity', [False, True], ids=['valid-minimal-lock', 'missing-integrity'])
def test_offline_frontend_helper_accepts_optional_lock_fields_under_strict_mode(tmp_path, missing_integrity):
    powershell = shutil.which('pwsh.exe')
    assert powershell, 'The common desktop build requires PowerShell 7'
    (tmp_path / 'tools').mkdir()
    shutil.copyfile(ROOT / 'tools/build_frontend.ps1', tmp_path / 'tools/build_frontend.ps1')
    resolved = 'https://registry.npmjs.org/ssearch-fixture/-/ssearch-fixture-1.0.0.tgz'
    for project in ('frontend', 'desktop'):
        (tmp_path / project / '.cache').mkdir(parents=True)
        (tmp_path / f'{project}-cache').mkdir()
        # The root record has no resolved field; ordinary dependencies need no
        # os/cpu restriction. Both are valid package-lock v3 shapes.
        package = {'version': '1.0.0', 'resolved': resolved, 'integrity': 'sha512-fixture'}
        if project == 'frontend' and missing_integrity:
            del package['integrity']
        lock = {'name': 'ssearch-fixture', 'lockfileVersion': 3,
                'packages': {'': {'name': 'ssearch-fixture', 'version': '0.2.0'},
                             'node_modules/ssearch-fixture': package}}
        (tmp_path / project / 'package-lock.json').write_text(json.dumps(lock), encoding='utf-8')
    driver = tmp_path / 'strict-offline.ps1'
    driver.write_text(r'''
param([string]$Root)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function node {
    $global:LASTEXITCODE = 0
    switch ($args -join ' ') {
        '--version' { return 'v24.22.1' }
        '-p process.platform' { return 'win32' }
        '-p process.arch' { return 'x64' }
        default { throw 'Unexpected Node boundary' }
    }
}
function npm {
    $global:LASTEXITCODE = 0
    $arguments = @($args)
    if (($arguments -join ' ') -eq '--version') { return '11.19.1' }
    @{project=(Split-Path -Leaf (Get-Location).Path);arguments=$arguments} |
        ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $Root 'npm-calls.jsonl') -Encoding UTF8
    if ($arguments[0] -eq 'cache' -and $arguments[1] -eq 'ls') {
        return 'make-fetch-happen:request-cache:https://registry.npmjs.org/ssearch-fixture/-/ssearch-fixture-1.0.0.tgz'
    }
    if (($arguments[0] -eq 'cache' -and $arguments[1] -eq 'verify') -or
        $arguments[0] -eq 'ci' -or ($arguments[0] -eq 'run' -and $arguments[1] -eq 'build')) { return }
    throw 'Unexpected npm boundary'
}
function git {
    $global:LASTEXITCODE = 0
    if ($args[-2] -ne 'rev-parse' -or $args[-1] -ne 'HEAD') { throw 'Unexpected Git boundary' }
    return ('a' * 40)
}
& (Join-Path $Root 'tools/build_frontend.ps1') -CachePath (Join-Path $Root 'frontend-cache') `
    -DesktopCachePath (Join-Path $Root 'desktop-cache')
''', encoding='utf-8-sig')
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(driver), str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    calls = [json.loads(line) for line in (tmp_path / 'npm-calls.jsonl').read_text(encoding='utf-8-sig').splitlines()]
    receipt = tmp_path / 'frontend/.cache/offline-dependencies.json'
    if missing_integrity:
        assert result.returncode != 0
        assert 'Offline locked cache incomplete' in result.stderr
        assert not any(row['arguments'][0] == 'ci' for row in calls)
        assert not receipt.exists()
        return
    assert result.returncode == 0, 'Strict offline helper rejected a valid minimal lock'
    installs = [row for row in calls if row['arguments'][0] == 'ci']
    assert len(installs) == 2
    for row in installs:
        assert row['arguments'] == ['ci', '--offline', '--ignore-scripts', '--cache',
            str(tmp_path / f"{row['project']}-cache"), '--no-audit', '--no-fund']
    proof = json.loads(receipt.read_text(encoding='utf-8-sig'))
    assert proof['source_sha'] == 'a' * 40
    assert proof['node'] == 'v24.22.1' and proof['npm'] == '11.19.1'
    assert [project['project'] for project in proof['projects']] == ['frontend', 'desktop']
    for project in proof['projects']:
        assert project['cache_integrity_verified'] is True
        assert project['offline_npm_ci'] is True and project['install_scripts'] is False
        assert project['lock_sha256'] == hashlib.sha256((tmp_path / project['project'] / 'package-lock.json').read_bytes()).hexdigest()
        assert project['packages'] == [{'package': 'node_modules/ssearch-fixture', 'integrity': 'sha512-fixture'}]
