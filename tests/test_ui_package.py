"""Explicit build selections cannot silently turn a UI Quick into full pytest."""
import importlib.util
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
    names = ('SSEARCH_PRODUCT_METADATA', 'SSEARCH_ELECTRON_ZIP_DIR', 'SSEARCH_ELECTRON_ZIP_SHA256')
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
        -ElectronZipDirectory $Directory -ElectronZipSha256 ('a' * 64)
    throw 'Expected preflight refusal'
} catch {
    if ($_.Exception.Message -ne 'Prepared build runtime is missing.') { throw }
}
foreach ($name in @('SSEARCH_PRODUCT_METADATA', 'SSEARCH_ELECTRON_ZIP_DIR', 'SSEARCH_ELECTRON_ZIP_SHA256')) {
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
