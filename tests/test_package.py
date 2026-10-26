"""Exercise build inputs and onedir layouts without building or installing."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_postgres_runtime_manifest import make_runtime


ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "packaging" / "build_support.py"


class PackageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "PowerShell process-tree wait is Windows-specific")
    def test_installer_waits_for_descendants_and_timeout_keeps_completion_unknown(self):
        self._exercise_installer_tree_wait(15000, False)

    @unittest.skipUnless(os.name == "nt", "PowerShell process-tree wait is Windows-specific")
    def test_installer_timeout_does_not_confirm_an_active_descendant(self):
        self._exercise_installer_tree_wait(1500, True)

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh.exe"), "PowerShell 7 unavailable")
    def test_installer_descendant_completion_under_powershell7(self):
        self._exercise_installer_tree_wait(15000, False, shutil.which("pwsh.exe"))

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh.exe"), "PowerShell 7 unavailable")
    def test_installer_timeout_under_powershell7(self):
        self._exercise_installer_tree_wait(1500, True, shutil.which("pwsh.exe"))

    def _exercise_installer_tree_wait(self, limit, expect_timeout, shell="powershell.exe"):
        import time
        work_parent = ROOT / ".superpowers/sdd/2026-10-20-atom-first-increment"
        work_parent.mkdir(parents=True, exist_ok=True)
        with self.subTest(timeout=expect_timeout), tempfile.TemporaryDirectory(
                prefix="wait-tree-", dir=work_parent) as temporary:
            work = Path(temporary)
            marker = work / "child-finished"
            toy = work / "toy.py"
            toy.write_text(
                "import subprocess, sys, time\nfrom pathlib import Path\n"
                "if sys.argv[1] == 'parent':\n"
                " subprocess.Popen([sys.executable, __file__, 'child', sys.argv[2]], "
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "else:\n time.sleep(3)\n Path(sys.argv[2]).write_text('finished')\n",
                encoding="utf-8")
            harness = work / "exercise.ps1"
            harness.write_text(r'''
param($Source, $Python, $Toy, $Marker, [int]$Limit, [int]$ExpectTimeout)
$ErrorActionPreference = 'Stop'
$testPath = Split-Path -Parent $Toy
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
$function = $ast.Find({param($node)
$node -is [Management.Automation.Language.FunctionDefinitionAst] -and
$node.Name -eq 'Start-HiddenAndWait'
}, $true)
Invoke-Expression $function.Extent.Text
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    $script:ownedWaiter = Microsoft.PowerShell.Management\Start-Process @PSBoundParameters
    return $script:ownedWaiter
}
$script:processCompletionConfirmed = $false
$caught = $false
try {
    try {
        Start-HiddenAndWait -Program $Python -Arguments @($Toy, 'parent', $Marker) `
            -Label 'owned toy tree' -TimeoutMilliseconds $Limit
    } catch { $caught = $true }
    if ($ExpectTimeout) {
        if (-not $caught -or $script:processCompletionConfirmed -or
            (Test-Path -LiteralPath $Marker)) {
            throw 'Timed-out descendant tree was marked complete.'
        }
    } else {
        if ($caught -or -not $script:processCompletionConfirmed -or
            -not (Test-Path -LiteralPath $Marker)) {
            throw 'Initial process exit was mistaken for descendant completion.'
        }
}
} finally {
    # Let the separately retained toy waiter finish naturally before test-file cleanup.
    if ($script:ownedWaiter -and -not $script:ownedWaiter.WaitForExit(15000)) {
        throw 'Owned toy waiter did not finish.'
}
}
@{shell=$PSVersionTable.PSVersion.ToString(); timeout=[bool]$ExpectTimeout;
  completion=$script:processCompletionConfirmed; descendant_finished=(Test-Path -LiteralPath $Marker)} |
    ConvertTo-Json -Compress
''', encoding="utf-8-sig")
            try:
                result = subprocess.run(
                    [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(harness), str(ROOT / "tools/check_installer.ps1"), sys.executable,
                     str(toy), str(marker), str(limit), str(int(expect_timeout))],
                    capture_output=True, timeout=25)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                print("Actual process-tree evidence:", result.stdout.decode().strip())
            finally:
                # The owned toy naturally exits; never kill a guessed PID.
                deadline = time.monotonic() + 15
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(marker.exists(), "Owned toy child did not finish")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell unavailable")
    def test_installer_completion_numeric_evidence_under_powershell51(self):
        self._exercise_installer_completion_matrix("powershell.exe")

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh.exe"), "PowerShell 7 unavailable")
    def test_installer_completion_numeric_evidence_under_powershell7(self):
        self._exercise_installer_completion_matrix(shutil.which("pwsh.exe"))

    def _exercise_installer_completion_matrix(self, shell):
        # Execute the real helper's completion-validation suffix, not a copied
        # numeric predicate. This matrix is pure: no child process is launched.
        harness = self.root / "completion-matrix.ps1"
        harness.write_text(r'''
param($Source)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
$function = $ast.Find({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Start-HiddenAndWait'
}, $true)
$statements = @($function.Body.EndBlock.Statements)
$readIndex = -1
for ($index = 0; $index -lt $statements.Count; $index++) {
    if ($statements[$index] -is [Management.Automation.Language.AssignmentStatementAst] -and
        $statements[$index].Left.Extent.Text -eq '$completion') { $readIndex = $index }
}
if ($readIndex -lt 0) { throw 'Actual completion-validation boundary missing.' }
$validation = [scriptblock]::Create(($statements[($readIndex + 1)..($statements.Count - 1)] |
    ForEach-Object { $_.Extent.Text }) -join "`n")
$cases = @(
    @{json='0'; valid=$true; zero=$true}, @{json='1'; valid=$true; zero=$false},
    @{json='-2147483648'; valid=$true; zero=$false},
    @{json='2147483647'; valid=$true; zero=$false},
    @{json='2147483648'; valid=$false; zero=$false},
    @{json='-2147483649'; valid=$false; zero=$false},
    @{json='0.0'; valid=$false; zero=$false}, @{json='true'; valid=$false; zero=$false},
    @{json='"0"'; valid=$false; zero=$false}, @{json='null'; valid=$false; zero=$false})
$waitId = 'expected'
$Label = 'pure completion matrix'
$testPath = $PSScriptRoot
$passed = 0
foreach ($case in $cases) {
    $completion = ('{"id":"expected","completed":true,"exit_code":' + $case.json + '}') |
        ConvertFrom-Json
    $script:processCompletionConfirmed = $false
    $caught = $false
    try { . $validation } catch { $caught = $true }
    $expectThrow = -not ($case.valid -and $case.zero)
    if ($script:processCompletionConfirmed -ne $case.valid -or $caught -ne $expectThrow) {
        throw ('Actual completion guard mishandled JSON value: ' + $case.json)
    }
    $passed++
}
@{shell=$PSVersionTable.PSVersion.ToString(); numeric_cases=$passed} | ConvertTo-Json -Compress
''', encoding="utf-8-sig")
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
             str(ROOT / "tools/check_installer.ps1")], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(result.stdout)["numeric_cases"], 10)
        print("Actual completion-guard evidence:", result.stdout.decode().strip())

    def setUp(self):
        self.assertTrue(SUPPORT.is_file(), "Build verification helpers must be implemented")
        spec = importlib.util.spec_from_file_location("atom_build_support", SUPPORT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / 'tests/fixtures/project-search-v2',
                        self.root / 'tests/fixtures/project-search-v2')
        (self.root / "pyproject.toml").write_text(
            '[project]\nversion = "0.1.0"\n', encoding="utf-8")
        for name in ("help.html",
                     "branding/atom-search.ico", "licenses/model.txt"):
            path = self.root / "resources" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"local release resource")
        (self.root / "THIRD_PARTY_NOTICES.txt").write_text("Dependency notices", encoding="utf-8")
        (self.root / "LICENSE.txt").write_text("MIT License", encoding="utf-8")
        core_license = self.root / "packages/atom-search-core/LICENSE.txt"
        core_license.parent.mkdir(parents=True, exist_ok=True)
        core_license.write_text("MIT License", encoding="utf-8")
        models = self.root / "models"
        models.mkdir()
        model = b"verified quantized model fixture"
        tokenizer = b'{"version":"1.0"}'
        files = []
        for name, data in (("model_quantized.onnx", model), ("tokenizer.json", tokenizer)):
            (models / name).write_bytes(data)
            files.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "url": "https://example.invalid/" + name})
        self.manifest = {"model_id": "intfloat/multilingual-e5-small", "revision": "a" * 40,
                         "dimension": 384, "onnx_file": "model_quantized.onnx",
                         "tokenizer_file": "tokenizer.json", "files": files}
        self.write_manifest()
        make_runtime(self.root / 'resources/postgresql')

    def test_frozen_self_test_requires_postgres_and_allows_only_owned_loopback(self):
        from atom_search import diagnostics
        self.assertTrue(callable(getattr(diagnostics, 'owned_loopback_only', None)))
        import socket
        endpoint = type('Endpoint', (), {'host': '127.0.0.1', 'port': 12345})()
        with diagnostics.owned_loopback_only(lambda: endpoint):
            for address in [('example.com', 443), ('127.0.0.1', 5432), ('192.0.2.1', 12345)]:
                with self.assertRaises(RuntimeError):
                    socket.create_connection(address, timeout=.01)
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo('example.com', 443)
            self.assertEqual(socket.getaddrinfo('127.0.0.1', 12345)[0][4][0], '127.0.0.1')
        self.assertTrue(callable(getattr(self.module, 'validate_application_proof', None)))
        with self.assertRaises(self.module.BuildError):
            self.module.validate_application_proof({'ok': True, 'model_ready': True},
                source_commit='a' * 40, app_version='0.2.0', core_version='0.2.0')

    def write_manifest(self):
        (self.root / "models" / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8")

    def test_development_layout_is_confined_to_commit_directory(self):
        self.assertTrue(callable(getattr(self.module, "development_layout", None)),
                        "Commit-isolated development layout is missing")
        layout = self.module.development_layout(self.root, "a" * 40)
        base = self.root / "artifacts/development" / ("a" * 40)
        self.assertEqual(layout, {"base": base, "build": base / "build", "dist": base / "dist",
                                  "artifacts": base / "artifacts", "wheel": base / "wheel",
                                  "qa": base / "qa"})
        self.assertFalse(base.exists(), "Computing paths must not write build outputs")

    def test_development_layout_rejects_invalid_commit(self):
        self.assertTrue(callable(getattr(self.module, "development_layout", None)),
                        "Commit-isolated development layout is missing")
        for commit in ("../outside", "a" * 39, "g" * 40, "a" * 40 + "/extra", ""):
            with self.subTest(commit=commit), self.assertRaises(self.module.BuildError):
                self.module.development_layout(self.root, commit)

    def test_development_layout_does_not_reuse_release_paths(self):
        self.assertTrue(callable(getattr(self.module, "development_layout", None)),
                        "Commit-isolated development layout is missing")
        first = self.module.development_layout(self.root, "a" * 40)
        second = self.module.development_layout(self.root, "b" * 40)
        old = self.root / "artifacts/ssearch-Setup-0.1.1.exe"
        for name in ("base", "build", "dist", "artifacts", "wheel", "qa"):
            self.assertNotEqual(first[name], second[name])
            self.assertFalse(old.is_relative_to(first[name]))
            self.assertFalse(first[name].is_relative_to(self.root / "dist"))
            self.assertFalse(first[name].is_relative_to(self.root / "build"))

    def make_git_source(self):
        for arguments in (("init", "-q"), ("add", "pyproject.toml"),
                          ("-c", "user.name=Build QA", "-c", "user.email=qa@example.invalid",
                           "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                           capture_output=True)
        # Fixture inputs are not product source; only the project file is tracked.
        (self.root / ".git/info/exclude").write_text("*\n", encoding="utf-8")
        return subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                                       text=True).strip()

    def test_development_build_refuses_dirty_source_and_reused_commit(self):
        self.assertTrue(callable(getattr(self.module, "prepare_development_build", None)),
                        "Clean source development-build reservation is missing")
        commit = self.make_git_source()
        project = self.root / "pyproject.toml"
        original = project.read_text(encoding="utf-8")
        project.write_text(original + "# dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(self.module.BuildError, "[Cc]lean|чист"):
            self.module.prepare_development_build(self.root)
        project.write_text(original, encoding="utf-8")
        layout = self.module.prepare_development_build(self.root)
        self.assertEqual(layout["base"].name, commit)
        with self.assertRaisesRegex(self.module.BuildError, "существует|exists"):
            self.module.prepare_development_build(self.root)

    def test_quick_and_full_reserve_separate_attempts_for_same_clean_sha(self):
        commit = self.make_git_source()
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        full = self.module.prepare_development_build(self.root)
        quick = self.module.prepare_development_build(self.root, quick=True, test_targets=["tests/test_sample.py"])
        self.assertEqual(full["base"], self.root / "artifacts/development" / commit)
        self.assertEqual(quick["base"], self.root / "artifacts/development-quick" / commit)
        with self.assertRaises(self.module.BuildError):
            self.module.prepare_development_build(self.root, quick=True, test_targets=["tests/test_sample.py"])

    def test_quick_target_validation_rejects_options_external_and_missing_files(self):
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        (tests / "notes.txt").write_text("not a test", encoding="utf-8")
        invalid = [[], [""], ["-k"], ["--ignore=tests/test_sample.py"], ["../test.py"],
                   [str(tests / "test_sample.py")], ["tests/../test_sample.py"],
                   ["tests/missing.py"], ["tests/notes.txt"], ["tests/test_sample.py::"]]
        for targets in invalid:
            with self.subTest(targets=targets), self.assertRaises(self.module.BuildError):
                self.module.validate_test_selection(self.root, development=True, quick=True, test_targets=targets)
        self.assertEqual(self.module.validate_test_selection(
            self.root, development=True, quick=True, test_targets=["tests/test_sample.py::test_ok"]),
            {"scope": "targeted", "test_targets": ["tests/test_sample.py::test_ok"]})

    def test_ui_only_quick_reserves_and_records_combined_selection(self):
        commit = self.make_git_source()
        target = self.root / 'frontend/tests/candidate.test.tsx'
        target.parent.mkdir(parents=True)
        target.write_text('// selected UI contract', encoding='utf-8')
        options = dict(quick=True, desktop_stack=True,
                       frontend_test_targets=['tests/candidate.test.tsx'])
        layout = self.module.prepare_development_build(self.root, **options)
        # Electron creates its shell directories before the service performs
        # its own strict profile check. Every such child must inherit the
        # actual private Windows DACL, not Node's unsupported mode0700 hint.
        from atom_search.runtime import _windows
        self.assertTrue(layout['qa'].is_dir())
        _windows.assert_private(layout['qa'])
        profile = layout['qa'] / 'UI profile Кириллица with spaces'
        profile.mkdir()
        capture = layout['qa'] / 'private-native.stdout'
        capture.write_bytes(b'private QA fixture')
        _windows.assert_private(profile)
        _windows.assert_private(capture)
        receipt = self.module.write_test_receipt(self.root, commit, exit_code=0, **options)
        data = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(data['test_targets'], [])
        self.assertEqual(data['frontend_test_targets'], ['tests/candidate.test.tsx'])
        self.assertEqual(data['desktop_test_targets'], [])
        self.assertEqual(data['scope'], 'targeted')
        self.assertFalse(data['full_suite_passed'])
        with self.assertRaises(self.module.BuildError):
            self.module.write_test_receipt(self.root, commit, exit_code=1, **options)

    def test_embedded_identity_is_generated_from_clean_source_and_rejects_dirty_source(self):
        commit = self.make_git_source()
        core = self.root / "packages/atom-search-core"
        core.mkdir(parents=True, exist_ok=True)
        (core / "pyproject.toml").write_text('[project]\nversion = "0.1.1"\n')
        generate = getattr(self.module, "generate_build_identity", None)
        self.assertTrue(callable(generate), "No trustworthy embedded build identity")
        path = generate(self.root, self.root / "work")
        namespace = {}
        exec(path.read_text(encoding="utf-8"), namespace)
        self.assertEqual(namespace["IDENTITY"], {
            "source_commit": commit, "source_clean": True,
            "app_version": "0.1.0", "core_version": "0.1.1"})
        (self.root / "pyproject.toml").write_text('[project]\nversion = "0.9.0"\n')
        with self.assertRaises(self.module.BuildError):
            generate(self.root, self.root / "dirty-work")
        self.assertFalse((self.root / "dirty-work").exists())

    def test_development_manifest_records_verified_outputs_and_rechecks_source(self):
        self._exercise_manifest_scope(quick=False)

    def test_quick_manifest_requires_matching_successful_targeted_receipt(self):
        self._exercise_manifest_scope(quick=True)

    def _exercise_manifest_scope(self, quick):
        self.assertTrue(callable(getattr(self.module, "write_development_manifest", None)),
                        "Verified development manifest is missing")
        commit = self.make_git_source()
        core = self.root / "packages/atom-search-core"
        core.mkdir(parents=True, exist_ok=True)
        (core / "pyproject.toml").write_text('[project]\nversion = "0.1.1"\n', encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        targets = ["tests/test_sample.py::test_ok"] if quick else []
        layout = self.module.prepare_development_build(self.root, quick=quick, test_targets=targets)
        def finalize():
            return self.module.write_development_manifest(self.root, commit, quick=quick, test_targets=targets)
        layout["qa"].mkdir()
        with self.assertRaisesRegex(self.module.BuildError, "receipt"):
            finalize()
        with self.assertRaises(self.module.BuildError):
            self.module.write_test_receipt(self.root, commit, quick=quick, test_targets=targets, exit_code=5)
        self.assertFalse((layout["qa"] / "test-receipt.json").exists())
        receipt = self.module.write_test_receipt(self.root, commit, quick=quick, test_targets=targets, exit_code=0)
        layout["artifacts"].mkdir()
        installer = layout["artifacts"] / "ssearch-Setup-0.1.0.exe"
        installer.write_bytes(b"installer fixture")
        executable = layout["dist"] / "ssearch/ssearch.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"frozen fixture")
        app_proof = {'ok': True, 'model_ready': True, 'clean_stop': True, 'source_clean': True,
            'source_commit': commit, 'app_version': '0.1.0', 'core_version': '0.1.1',
            'execution_mode': 'frozen', 'backend': 'postgresql', 'factory': 'open_search_service',
            'schema_version': 5, 'server_version': '18.6', 'extension_version': '0.8.7',
            'prior_schema_version': 5, 'public_identity_preserved': True,
            'public_source_id': '11111111-1111-4111-8111-111111111111',
            'public_file_ids': {'one': '22222222-2222-4222-8222-222222222222',
                                'two': '33333333-3333-4333-8333-333333333333',
                                'three': '44444444-4444-4444-8444-444444444444'},
            'scan_status': 'complete', 'files': 3, 'vector_count': 3, 'dimension': 384,
            'catalog_id': 1, 'installation_id': '11111111-1111-1111-1111-111111111111',
            'model_key': 'fixture|filename-path-root-project-v2', 'model_manifest_digest': 'a' * 64,
            'literal_result': 'КМ-101 узлы фасада.dwg', 'semantic_result': 'Контракт монтажных работ.docx'}
        (layout["qa"] / "frozen-self-test.json").write_text(json.dumps(app_proof), encoding="utf-8")
        (layout["qa"] / "build-smoke.png").write_bytes(b"screenshot fixture")
        (layout["qa"] / "build.log").write_text("commands passed", encoding="utf-8")
        (layout['qa'] / 'postgres-runtime-proof.json').write_text(json.dumps({
            'ok': True, 'server_version': '18.6', 'extension_version': '0.8.7',
            'scram': True, 'wrong_password_rejected': True, 'clean_stop': True,
            'profile_removed': True, 'dimension': 384,
            'cosine_rows': [[1, 0.0], [2, 1.0], [3, 2.0]],
            'execution_mode': 'frozen', 'runtime_api': 'PostgresRuntime',
            'restart_verified': True, 'dpapi_verified': True, 'private_acl_verified': True,
            'application_privileges_verified': True,
            'source_commit': commit, 'app_version': '0.1.0', 'core_version': '0.1.1',
            'backup': {'method': 'logical-schema', 'scope': 'synthetic-fixture-schema',
                       'bytes': 1024, 'sha256': 'a' * 64, 'same_major': True,
                       'runtime_marker_excluded': True, 'dump_exit_code': 0},
            'restore': {'rows_equal': True, 'restart_verified': True,
                        'target_identity_preserved': True, 'restore_exit_code': 0,
                        'source_installation_id': 'source', 'target_installation_id': 'target',
                        'dimension': 384, 'cosine_rows': [[1, 0.0], [2, 1.0], [3, 2.0]]},
            'preservation': {'source_verified': True},
        }), encoding='utf-8')
        project = self.root / "pyproject.toml"
        original = project.read_text(encoding="utf-8")
        project.write_text(original + "# changed during build\n", encoding="utf-8")
        with self.assertRaises(self.module.BuildError):
            finalize()
        self.assertFalse((layout["base"] / "build-manifest.json").exists())
        project.write_text(original, encoding="utf-8")
        postgres_path = layout['qa'] / 'postgres-runtime-proof.json'
        successful = postgres_path.read_text(encoding='utf-8')
        for missing in ('execution_mode', 'runtime_api', 'restart_verified', 'dpapi_verified',
                        'private_acl_verified', 'application_privileges_verified',
                        'source_commit', 'backup', 'restore', 'preservation'):
            incomplete = json.loads(successful)
            del incomplete[missing]
            postgres_path.write_text(json.dumps(incomplete), encoding='utf-8')
            with self.assertRaises(self.module.BuildError):
                finalize()
        postgres_path.write_text(successful, encoding='utf-8')
        proof_path = layout['qa'] / 'frozen-self-test.json'
        for missing in ('backend', 'source_commit', 'catalog_id', 'installation_id', 'schema_version',
                        'model_manifest_digest', 'factory', 'clean_stop', 'prior_schema_version',
                        'public_identity_preserved', 'public_source_id', 'public_file_ids'):
            incomplete = dict(app_proof)
            del incomplete[missing]
            proof_path.write_text(json.dumps(incomplete), encoding='utf-8')
            with self.assertRaises(self.module.BuildError):
                finalize()
        for fields in ({'schema_version': 6}, {'public_source_id': '41'},
                       {'public_file_ids': {'a': '51', 'b': '52', 'c': '53'}},
                       {'public_identity_preserved': False}):
            proof_path.write_text(json.dumps(dict(app_proof, **fields)), encoding='utf-8')
            with self.assertRaises(self.module.BuildError):
                finalize()
        proof_path.write_text(json.dumps(app_proof), encoding='utf-8')
        good_receipt = receipt.read_text(encoding="utf-8")
        for field, value in (("source_commit", "b" * 40), ("scope", "full" if quick else "targeted"),
                             ("test_targets", ["tests/other.py"]), ("full_suite_passed", quick), ("exit_code", 1)):
            changed = json.loads(good_receipt)
            changed[field] = value
            receipt.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(self.module.BuildError, "receipt"):
                finalize()
        receipt.write_text(good_receipt, encoding="utf-8")
        manifest = finalize()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["source_commit"], commit)
        self.assertEqual(data["app_version"], "0.1.0")
        self.assertEqual(data["core_version"], "0.1.1")
        self.assertEqual(data["catalog"]["backend"], "postgresql")
        self.assertEqual(data["capabilities"]["postgresql"], "application_verified")
        self.assertEqual(data["windows_validation"], [])
        self.assertEqual(data["test_scope"], "targeted" if quick else "full")
        self.assertEqual(data["test_targets"], targets)
        self.assertIs(data["full_suite_passed"], not quick)
        self.assertEqual(data["installer"], {
            "path": "artifacts/ssearch-Setup-0.1.0.exe", "size": 17,
            "sha256": hashlib.sha256(b"installer fixture").hexdigest()})
        self.assertEqual(data.get("executable"), {
            "path": "dist/ssearch/ssearch.exe", "size": 14,
            "sha256": hashlib.sha256(b"frozen fixture").hexdigest()})
        self.assertEqual(data["qa"], {"self_test": "qa/frozen-self-test.json",
                                      "screenshot": "qa/build-smoke.png", "log": "qa/build.log",
                                      'postgresql': 'qa/postgres-runtime-proof.json',
                                      'tests': 'qa/test-receipt.json'})
        with self.assertRaises(self.module.BuildError):
            finalize()

    def make_bundle(self):
        bundle = self.root / "dist" / "ssearch"
        internal = bundle / "_internal"
        shutil.copytree(self.root / "resources", internal / "resources")
        shutil.copytree(self.root / 'tests/fixtures/project-search-v2',
                        internal / 'resources/project-search-v2')
        shutil.copytree(self.root / "models", internal / "models")
        shutil.copy(self.root / "THIRD_PARTY_NOTICES.txt", internal)
        shutil.copy(self.root / "LICENSE.txt", internal)
        core_notices = internal / "resources/licenses/ssearch-core"
        core_notices.mkdir(parents=True)
        shutil.copy(self.root / "packages/atom-search-core/LICENSE.txt", core_notices)
        for name in ("ssearch.exe", "_internal/python312.dll",
                     "_internal/PySide6/Qt6Core.dll", "_internal/PySide6/Qt6Gui.dll",
                     "_internal/PySide6/Qt6Widgets.dll",
                     "_internal/PySide6/plugins/platforms/qwindows.dll",
                     "_internal/numpy/_core/_multiarray_umath.cp312-win_amd64.pyd",
                     "_internal/numpy.libs/libscipy_openblas64_fixture.dll",
                     "_internal/onnxruntime/capi/onnxruntime_pybind11_state.pyd",
                     "_internal/onnxruntime/capi/onnxruntime_providers_shared.dll",
                     "_internal/tokenizers/tokenizers.pyd",
                     '_internal/psycopg_binary/pq.cp312-win_amd64.pyd',
                     '_internal/psycopg_binary/_psycopg.cp312-win_amd64.pyd',
                     '_internal/psycopg_binary.libs/libpq-fixture.dll',
                     '_internal/psycopg_binary.libs/libssl-fixture.dll',
                     '_internal/psycopg_binary.libs/libcrypto-fixture.dll',
                     '_internal/vcruntime140.dll', '_internal/tzdata/zoneinfo/UTC'):
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"MZ fixture")
        return bundle

    @unittest.skipUnless(os.name == "nt", "PowerShell build routing is Windows-specific")
    def test_powershell_checked_command_logs_stderr_and_stops_on_native_failure(self):
        harness = self.root / "checked-command.ps1"
        harness.write_text(r'''
param($Build, $Python, $Log)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Build, [ref]$tokens, [ref]$errors)
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Invoke-Checked'
}, $true)
. ([scriptblock]::Create($function.Extent.Text))
$buildLog = $Log
Invoke-Checked $Python @('-c', 'import sys; print(1729, file=sys.stderr)')
$failed = $false
try { Invoke-Checked $Python @('-c', 'raise SystemExit(7)') } catch { $failed = $true }
if (-not $failed) { throw 'Nonzero native exit was accepted' }
$global:LASTEXITCODE = 0
$failed = $false
try { Invoke-Checked ($Python + '.missing') @() } catch { $failed = $true }
if (-not $failed) { throw 'Missing executable was accepted using a stale exit code' }
$buildLog = Split-Path -Parent $Log
$failed = $false
try { Invoke-Checked $Python @('-c', 'print(2718)') } catch { $failed = $true }
if (-not $failed) { throw 'Build continued after its log could not be written' }
''', encoding="utf-8-sig")
        log = self.root / "commands.log"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(harness), str(ROOT / "tools/build.ps1"),
                                 sys.executable, str(log)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1729", log.read_text(encoding="utf-8-sig"))

    @unittest.skipUnless(os.name == "nt", "Windows installer path boundary")
    def test_installer_qa_paths_require_exact_development_commit_and_qa_subtree(self):
        harness = self.root / "installer-paths.ps1"
        harness.write_text(r'''
param($Script, $Root)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -Force
$repository = $Root
$artifacts = Join-Path $Root 'artifacts'
$comparison = [StringComparison]::OrdinalIgnoreCase
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$tokens, [ref]$errors)
$node = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Assert-ArtifactPath'}, $true)
. ([scriptblock]::Create($node.Extent.Text))
foreach ($relative in @('old.exe', 'development\invalid\qa\run', 'development-quick\invalid\qa\run', ('development-quick\' + ('a'*40) + '\dist\run'), ('development\' + ('a'*40) + '\dist\run'))) {
    $rejected = $false
    try { $null = Assert-ArtifactPath (Join-Path $artifacts $relative) -Qa } catch { $rejected = $true }
    if (-not $rejected) { throw ('Accepted unsafe QA path: ' + $relative) }
}
$valid = Join-Path $artifacts ('development\' + ('a'*40) + '\qa\Кириллица с пробелами')
if ((Assert-ArtifactPath $valid -Qa) -ne $valid) { throw 'Valid QA path rejected' }
$quick = Join-Path $artifacts ('development-quick\' + ('a'*40) + '\qa\new-run')
if ((Assert-ArtifactPath $quick -Qa) -ne $quick) { throw 'Valid quick QA path rejected' }
# Exercise the real read-only entry gate, including manifest selection, before any install.
$text = Get-Content -LiteralPath $Script -Raw
$start = $text.IndexOf('$installerPath = Assert-ArtifactPath $Installer')
$end = $text.IndexOf('$principal = New-Object')
$entry = [scriptblock]::Create($text.Substring($start, $end - $start))
foreach ($mode in @('development', 'development-quick')) {
    $base = Join-Path $artifacts ($mode + '\' + ('a'*40))
    $null = New-Item -ItemType Directory -Path (Join-Path $base 'artifacts'), (Join-Path $base 'dist\ssearch'), (Join-Path $base 'qa') -Force
    $Installer = Join-Path $base 'artifacts\setup.exe'
    [IO.File]::WriteAllText($Installer, 'installer')
    $exe = Join-Path $base 'dist\ssearch\ssearch.exe'
    [IO.File]::WriteAllText($exe, 'executable')
    $manifest = @{source_commit=('a'*40); installer=@{path='artifacts/setup.exe';size=9;sha256=(Get-FileHash $Installer).Hash};
        executable=@{path='dist/ssearch/ssearch.exe';size=10;sha256=(Get-FileHash $exe).Hash};
        qa=@{postgresql='qa/postgres.json'}}
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $base 'build-manifest.json') -Encoding UTF8
    @{ok=$true;source_commit=('a'*40);execution_mode='frozen'} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $base 'qa\postgres.json') -Encoding UTF8
    $TestDir = Join-Path $base 'qa\new-run'
    . $entry
    if ($buildRoot -ne $base) { throw 'Installer read another mode manifest' }
    $other = if ($mode -eq 'development') { 'development-quick' } else { 'development' }
    foreach ($wrong in @(($other + '\' + ('a'*40)), ($mode + '\' + ('b'*40)))) {
        $TestDir = Join-Path $artifacts ($wrong + '\qa\new-run')
        $rejected = $false
        try { . $entry } catch { $rejected = $true }
        if (-not $rejected) { throw 'Cross-mode or cross-SHA installer QA accepted' }
    }
}

''', encoding="utf-8-sig")
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
                                    str(ROOT / "tools/check_installer.ps1"), str(self.root)],
                                   capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))

    @unittest.skipUnless(os.name == "nt", "PowerShell build routing is Windows-specific")
    def test_powershell_development_routes_external_tools_to_commit_outputs(self):
        self._exercise_development_build_boundary()

    @unittest.skipUnless(os.name == "nt", "PowerShell build routing is Windows-specific")
    def test_build_fails_when_postgres_integration_is_unavailable(self):
        self._exercise_development_build_boundary(fail_postgres=True)

    @unittest.skipUnless(os.name == "nt", "PowerShell build routing is Windows-specific")
    def test_quick_build_runs_only_explicit_targets_and_preserves_frozen_gates(self):
        self._exercise_development_build_boundary(quick=True)

    @unittest.skipUnless(os.name == "nt", "PowerShell build routing is Windows-specific")
    def test_quick_failed_or_empty_selection_stops_before_packaging(self):
        self._exercise_development_build_boundary(quick=True, pytest_exit=5)

    @unittest.skipUnless(os.name == "nt", "PowerShell build routing is Windows-specific")
    def test_quick_invalid_cli_stops_before_build_work(self):
        self._exercise_development_build_boundary(invalid=True)

    def _exercise_development_build_boundary(self, fail_postgres=False, quick=False, pytest_exit=0, invalid=False):
        # Replace only the expensive process boundary in a temporary script.
        # The real parameter handling, layout reservation, routing, environment
        # restoration, core wheel builder and final manifest still execute.
        tools = self.root / "tools"
        tools.mkdir()
        shutil.copy(ROOT / "tools/build.ps1", tools / "build.ps1")
        shutil.copy(ROOT / "tools/ci_pytest.py", tools / "ci_pytest.py")
        pytest_runner = str(tools / "ci_pytest.py")
        packaging = self.root / "packaging"
        packaging.mkdir()
        shutil.copy(SUPPORT, packaging / "build_support.py")
        shutil.copytree(ROOT / 'src/atom_search/runtime', self.root / 'src/atom_search/runtime')
        shutil.copy(ROOT / 'src/atom_search/__init__.py', self.root / 'src/atom_search/__init__.py')
        core = self.root / "packages/atom-search-core"
        core.mkdir(parents=True, exist_ok=True)
        (core / "pyproject.toml").write_text(
            '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
            '[project]\nname="ssearch-core"\nversion="0.1.1"\n', encoding="utf-8")
        (core / "fixture.py").write_text('VALUE = 1\n', encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        (tests / "test_extra.py").write_text("def test_selected(): pass\n", encoding="utf-8")
        (self.root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
        targets = ["tests/test_sample.py", "tests/test_extra.py::test_selected"] if quick else []
        compiler = self.root / "compiler.exe"
        compiler.write_bytes(b"external compiler boundary")
        old = self.root / "artifacts/ssearch-Setup-0.1.1.exe"
        old.parent.mkdir()
        old.write_bytes(b"old release must survive")
        commit = self.make_git_source()
        harness = self.root / "routing.ps1"
        harness.write_text(r'''
param($Build, $Python, $Compiler, $Calls, $FailPostgres, $QuickMode, [int]$PytestExit, $Invalid)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Build, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw $errors[0] }
$boundary = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Invoke-Checked'
}, $true)
$originalChecked = $boundary.Extent.Text.Replace('function Invoke-Checked', 'function Invoke-OriginalChecked')
. ([scriptblock]::Create($originalChecked))
$fake = @'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    @{program=$Program; arguments=@($Arguments)} | ConvertTo-Json -Compress |
        Add-Content -LiteralPath $Calls -Encoding UTF8
    Add-Content -LiteralPath $buildLog -Value ('checked: ' + $Program) -Encoding UTF8
    $isPytest = $Arguments -contains (Join-Path $repository 'tools/ci_pytest.py')
    if ($isPytest -and $FailPostgres -eq 'yes') {
        Invoke-OriginalChecked $Python @('-c', 'import sys; print(''PG integration failed: owned runtime unavailable'', flush=True); sys.exit(1)')
    }
    if ($isPytest) {
        if ($PytestExit -ne 0) { Invoke-OriginalChecked $Python @('-c', ('raise SystemExit(' + $PytestExit + ')')) }
        Invoke-OriginalChecked $Program $Arguments
    } elseif ($Arguments -contains 'development-manifest' -or $Arguments -contains 'verify-source' -or
              $Arguments -contains 'test-receipt') {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) { throw 'Source/manifest gate failed' }
    } elseif ($Arguments -contains 'PyInstaller') {
        $target = Join-Path $Arguments[([array]::IndexOf($Arguments, '--distpath') + 1)] 'ssearch'
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $target 'ssearch.exe') -Value 'frozen fixture'
    } elseif ($Arguments -contains '--self-test') {
        $sourceCommit = (& git rev-parse HEAD).Trim()
        $applicationProof = @{ok=$true; model_ready=$true; clean_stop=$true; source_clean=$true;
          source_commit=$sourceCommit; app_version='0.1.0'; core_version='0.1.1'; execution_mode='frozen';
          backend='postgresql'; factory='open_search_service'; schema_version=5; server_version='18.6';
          prior_schema_version=5; public_identity_preserved=$true;
          public_source_id='11111111-1111-4111-8111-111111111111';
          public_file_ids=@{one='22222222-2222-4222-8222-222222222222';two='33333333-3333-4333-8333-333333333333';three='44444444-4444-4444-8444-444444444444'};
          extension_version='0.8.7'; scan_status='complete'; files=3; vector_count=3; dimension=384;
          catalog_id=1; installation_id='11111111-1111-1111-1111-111111111111';
          model_key='fixture|filename-path-root-project-v2'; model_manifest_digest=('a'*64);
          literal_result='КМ-101 узлы фасада.dwg'; semantic_result='Контракт монтажных работ.docx'} |
          ConvertTo-Json
        [IO.File]::WriteAllText($Arguments[1], $applicationProof, [Text.UTF8Encoding]::new($false))
    } elseif ($Arguments -contains '--postgres-self-test') {
        $proof = @{ok=$true; server_version='18.6'; extension_version='0.8.7'; scram=$true;
            wrong_password_rejected=$true; clean_stop=$true; profile_removed=$true; dimension=384;
            cosine_rows=@(@(1,0),@(2,1),@(3,2)); execution_mode='frozen'; runtime_api='PostgresRuntime';
            restart_verified=$true; dpapi_verified=$true; private_acl_verified=$true;
            application_privileges_verified=$true; source_commit=$sourceCommit; app_version='0.1.0'; core_version='0.1.1';
            backup=@{method='logical-schema'; scope='synthetic-fixture-schema'; same_major=$true;
                runtime_marker_excluded=$true; dump_exit_code=0; bytes=1024; sha256=('a'*64)};
            restore=@{rows_equal=$true; restart_verified=$true; target_identity_preserved=$true;
                restore_exit_code=0; source_installation_id='source'; target_installation_id='target';
                dimension=384; cosine_rows=@(@(1,0),@(2,1),@(3,2))};
            preservation=@{source_verified=$true}}
        $proof | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Arguments[1] -Encoding ASCII
    } elseif ($Arguments -contains '--screenshot') {
        Set-Content -LiteralPath $Arguments[([array]::IndexOf($Arguments, '--screenshot') + 1)] -Value 'image fixture'
    } elseif ($Program -eq $Compiler) {
        $output = ($Arguments | Where-Object { $_.StartsWith('/DOutputDir=') }).Substring(12)
        Set-Content -LiteralPath (Join-Path $output 'ssearch-Setup-0.1.0.exe') -Value 'installer fixture'
    }
}
'@
$text = Get-Content -LiteralPath $Build -Raw
$text = $text.Remove($boundary.Extent.StartOffset,
    $boundary.Extent.EndOffset - $boundary.Extent.StartOffset).Insert($boundary.Extent.StartOffset, $fake)
Set-Content -LiteralPath $Build -Value $text -Encoding UTF8
$before = @{}
foreach ($name in @('LOCALAPPDATA','APPDATA','TEMP','TMP','PYTHONPATH','PYINSTALLER_CONFIG_DIR')) {
    $before[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
if ($Invalid -eq 'yes') {
    foreach ($options in @(@{Quick=$true}, @{Development=$true; Quick=$true; TestTargets=@()},
                          @{Development=$true; TestTargets=@('tests/test_sample.py')},
                          @{Development=$true; Quick=$true; TestTargets=@('--collect-only')})) {
        $rejected = $false
        try { & $Build -Python $Python -ISCC $Compiler @options } catch { $rejected = $true }
        if (-not $rejected) { throw 'Invalid build options accepted' }
    }
} elseif ($QuickMode -eq 'yes') {
    & $Build -Development -Quick -TestTargets @('tests/test_sample.py', 'tests/test_extra.py::test_selected') -Python $Python -ISCC $Compiler
} else {
    & $Build -Development -Python $Python -ISCC $Compiler
}
foreach ($name in $before.Keys) {
    if ([Environment]::GetEnvironmentVariable($name, 'Process') -ne $before[$name]) {
        throw ('Environment was not restored: ' + $name)
    }
}
''', encoding="utf-8-sig")
        calls = self.root / "calls.jsonl"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(harness), str(tools / "build.ps1"), sys.executable,
                                 str(compiler), str(calls), 'yes' if fail_postgres else 'no',
                                 'yes' if quick else 'no', str(pytest_exit), 'yes' if invalid else 'no'], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        if invalid:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(calls.exists(), result.stdout + result.stderr)
            self.assertEqual(list((self.root / "artifacts").iterdir()), [old])
            return
        if fail_postgres or pytest_exit:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            if fail_postgres:
                self.assertIn('PG integration failed: owned runtime unavailable', result.stdout + result.stderr)
            records = [json.loads(line) for line in calls.read_text(encoding="utf-8-sig").splitlines()]
            self.assertEqual(sum(pytest_runner in item['arguments'] for item in records), 1)
            self.assertFalse(any('PyInstaller' in item['arguments'] or
                                 'development-manifest' in item['arguments'] or
                                 item['program'] == str(compiler) for item in records))
            base = self.root / ('artifacts/development-quick' if quick else 'artifacts/development') / commit
            self.assertFalse((base / 'build-manifest.json').exists())
            self.assertFalse((base / 'qa/test-receipt.json').exists())
            self.assertEqual(old.read_bytes(), b'old release must survive')
            return
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = [json.loads(line) for line in calls.read_text(encoding="utf-8-sig").splitlines()]
        base = self.root / ("artifacts/development-quick" if quick else "artifacts/development") / commit
        pytest_calls = [item["arguments"] for item in records if pytest_runner in item["arguments"]]
        self.assertEqual(pytest_calls, [["-u", pytest_runner, "--timeout-seconds", "3600", *targets]])
        manifest = json.loads((base / "build-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["test_scope"], "targeted" if quick else "full")
        self.assertEqual(manifest["test_targets"], targets)
        self.assertIs(manifest["full_suite_passed"], not quick)
        freeze = next(item["arguments"] for item in records if "PyInstaller" in item["arguments"])
        self.assertEqual(freeze[freeze.index("--distpath") + 1], str(base / "dist"))
        self.assertEqual(freeze[freeze.index("--workpath") + 1], str(base / "build"))
        pip = next(item["arguments"] for item in records if "pip" in item["arguments"])
        self.assertEqual(Path(pip[-1]).parent, base / "wheel")
        for flag in ("--self-test", "--screenshot"):
            arguments = next(item["arguments"] for item in records if flag in item["arguments"])
            self.assertTrue(Path(arguments[arguments.index(flag) + 1]).is_relative_to(base / "qa"))
            self.assertTrue(Path(arguments[arguments.index("--data-dir") + 1]).is_relative_to(base / "qa"))
        invocation = next(item for item in records if '--postgres-self-test' in item['arguments'])
        self.assertEqual(Path(invocation['program']), base / 'dist/ssearch/ssearch.exe')
        proof = invocation['arguments']
        for flag in ('--data-dir', '--postgres-self-test'):
            self.assertTrue(Path(proof[proof.index(flag) + 1]).is_relative_to(base / 'qa'))
        compiled = next(item["arguments"] for item in records if item["program"] == str(compiler))
        self.assertIn("/DOutputDir=" + str(base / "artifacts"), compiled)
        self.assertIn("/DSourceDir=" + str(base / "dist/ssearch"), compiled)
        self.assertTrue((base / "build-manifest.json").is_file())
        self.assertEqual(old.read_bytes(), b"old release must survive")
        self.assertFalse((self.root / "dist").exists())
        self.assertFalse((self.root / "build").exists())

    def test_release_data_uses_one_copy_of_each_verified_model_file(self):
        data = self.module.collect_datas(self.root)
        model_data = [(Path(source).name, destination) for source, destination in data
                      if destination == "models"]
        self.assertCountEqual(model_data, [("model_quantized.onnx", "models"),
                                          ("tokenizer.json", "models"),
                                          ("manifest.json", "models")])
        self.assertIn((str(self.root / "resources"), "resources"), data)
        self.assertIn((str(self.root / "THIRD_PARTY_NOTICES.txt"), "."), data)

    def test_same_size_model_corruption_stops_packaging(self):
        model = self.root / "models" / "model_quantized.onnx"
        model.write_bytes(b"X" * model.stat().st_size)
        with self.assertRaisesRegex(self.module.BuildError, "SHA-256"):
            self.module.collect_datas(self.root)

    def test_changed_postgresql_dll_stops_source_and_bundle_packaging(self):
        bundle = self.make_bundle()
        for base in (self.root, bundle / '_internal'):
            (base / 'resources/postgresql/bin/libpq.dll').write_bytes(b'X' * 128)
        with self.assertRaisesRegex(self.module.BuildError, 'SHA-256'):
            self.module.collect_datas(self.root)
        with self.assertRaisesRegex(self.module.BuildError, 'SHA-256'):
            self.module.validate_bundle(bundle)

    def test_missing_model_stops_packaging_instead_of_downloading(self):
        (self.root / "models" / "model_quantized.onnx").unlink()
        with self.assertRaisesRegex(self.module.BuildError, "model_quantized.onnx"):
            self.module.collect_datas(self.root)

    def test_unpinned_or_traversing_model_manifest_is_rejected(self):
        for field, value in (("revision", "main"), ("onnx_file", "../outside.onnx")):
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = value
                self.write_manifest()
                with self.assertRaises(self.module.BuildError):
                    self.module.collect_datas(self.root)
                self.manifest[field] = original

    def test_distribution_cannot_omit_licenses(self):
        shutil.rmtree(self.root / "resources" / "licenses")
        with self.assertRaisesRegex(self.module.BuildError, "licenses"):
            self.module.collect_datas(self.root)

    def test_missing_application_icon_stops_packaging(self):
        (self.root / "resources/branding/atom-search.ico").unlink()
        with self.assertRaisesRegex(self.module.BuildError, "atom-search.ico"):
            self.module.collect_datas(self.root)

    def test_missing_first_party_license_stops_packaging(self):
        for relative in ("LICENSE.txt", "packages/atom-search-core/LICENSE.txt"):
            with self.subTest(relative=relative):
                path = self.root / relative
                content = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(self.module.BuildError, "LICENSE.txt"):
                    self.module.collect_datas(self.root)
                path.write_bytes(content)

    def test_retired_corporate_resources_cannot_enter_the_bundle(self):
        for relative in ("eula.txt", "branding/ksg.png", "branding/astralit.png", "branding/sources.json"):
            with self.subTest(relative=relative):
                path = self.root / "resources" / relative
                path.write_bytes(b"retired proprietary resource")
                with self.assertRaisesRegex(self.module.BuildError, "retired"):
                    self.module.collect_datas(self.root)
                path.unlink()

    def test_first_party_licenses_are_collected_and_required_in_frozen_bundle(self):
        datas = self.module.collect_datas(self.root)
        self.assertIn((str(self.root / "LICENSE.txt"), "."), datas)
        self.assertIn((str(self.root / "packages/atom-search-core/LICENSE.txt"),
                       "resources/licenses/ssearch-core"), datas)
        for relative in ("LICENSE.txt", "resources/licenses/ssearch-core/LICENSE.txt"):
            bundle = self.make_bundle()
            (bundle / "_internal" / relative).unlink()
            with self.assertRaisesRegex(self.module.BuildError, "LICENSE.txt"):
                self.module.validate_bundle(bundle)
            shutil.rmtree(bundle)


    def test_frozen_bundle_without_application_icon_is_rejected(self):
        bundle = self.make_bundle()
        (bundle / "_internal/resources/branding/atom-search.ico").unlink()
        with self.assertRaisesRegex(self.module.BuildError, "atom-search.ico"):
            self.module.validate_bundle(bundle)

    def test_complete_onedir_layout_is_accepted(self):
        self.module.validate_bundle(self.make_bundle())

    def test_service_only_bundle_requires_service_executable_and_excludes_qt(self):
        bundle = self.make_bundle()
        (bundle / 'ssearch.exe').rename(bundle / 'ssearch-service.exe')
        shutil.rmtree(bundle / '_internal/PySide6')
        notice = bundle / '_internal/resources/licenses/dependencies/shiboken6/LICENSE.txt'
        notice.parent.mkdir(parents=True)
        notice.write_text('Retained third-party notice')
        self.module.validate_bundle(bundle, service_only=True)
        leaked = bundle / '_internal/PySide6/Qt6Core.dll'
        leaked.parent.mkdir()
        leaked.write_bytes(b'GUI toolkit is not a service dependency')
        with self.assertRaisesRegex(self.module.BuildError, 'GUI'):
            self.module.validate_bundle(bundle, service_only=True)

    def test_service_only_bundle_keeps_driver_model_and_private_data_guards(self):
        bundle = self.make_bundle()
        (bundle / 'ssearch.exe').rename(bundle / 'ssearch-service.exe')
        shutil.rmtree(bundle / '_internal/PySide6')
        driver = bundle / '_internal/psycopg_binary/pq.cp312-win_amd64.pyd'
        original = driver.read_bytes()
        driver.unlink()
        with self.assertRaises(self.module.BuildError):
            self.module.validate_bundle(bundle, service_only=True)
        driver.write_bytes(original)
        shutil.copy(bundle / '_internal/models/model_quantized.onnx', bundle)
        with self.assertRaisesRegex(self.module.BuildError, 'model_quantized.onnx'):
            self.module.validate_bundle(bundle, service_only=True)
        (bundle / 'model_quantized.onnx').unlink()
        (bundle / 'owner.json').write_text('{}')
        with self.assertRaises(self.module.BuildError):
            self.module.validate_bundle(bundle, service_only=True)

    def test_combined_desktop_payload_binds_identity_and_has_one_model_copy(self):
        original = self.make_bundle()
        bundle = self.root / 'dist/desktop'
        shutil.copytree(original, bundle / 'service')
        (bundle / 'service/ssearch.exe').rename(bundle / 'service/ssearch-service.exe')
        shutil.rmtree(bundle / 'service/_internal/PySide6')
        for relative in ('ssearch.exe', 'resources/app.asar', 'LICENSE',
                         'LICENSES.chromium.html', 'locales/en-US.pak'):
            target = bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'local Electron fixture')
        metadata = dict(schema_version=1, source_commit='a' * 40, app_version='0.1.0', core_version='0.1.1',
                        product=dict(product_name='ssearch', product_name_ru='Simple Search',
                                     support_email='author@example.invalid', author_name='Author',
                                     author_title='developer', organization_group='upstream', copyright_holder='Author'))
        target = bundle / 'resources/product-metadata.json'
        target.write_text(json.dumps(metadata), encoding='utf-8')
        options = dict(source_commit='a' * 40, app_version='0.1.0', core_version='0.1.1')
        inventory = self.module.validate_desktop_bundle(bundle, **options)
        assert inventory['ssearch.exe']['sha256'] == hashlib.sha256(b'local Electron fixture').hexdigest()
        assert 'service/ssearch-service.exe' in inventory
        inventory_path = self.root / 'payload-inventory.json'
        document = dict(schema_version=1, **options, files=inventory, inventory_only=True)
        inventory_path.write_text(json.dumps(document), encoding='utf-8')
        self.module.verify_desktop_inventory(bundle, inventory_path, **options)
        document['files']['ssearch.exe']['sha256'] = '0' * 64
        inventory_path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaisesRegex(self.module.BuildError, 'inventory'):
            self.module.verify_desktop_inventory(bundle, inventory_path, **options)
        metadata['source_commit'] = 'b' * 40
        target.write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaises(self.module.BuildError):
            self.module.validate_desktop_bundle(bundle, **options)
        metadata['source_commit'] = 'a' * 40
        target.write_text(json.dumps(metadata), encoding='utf-8')
        shutil.copy(bundle / 'service/_internal/models/model_quantized.onnx', bundle)
        with self.assertRaisesRegex(self.module.BuildError, 'model_quantized.onnx'):
            self.module.validate_desktop_bundle(bundle, **options)

    def test_combined_payload_rejects_other_reparse_tags_before_traversal(self):
        from types import SimpleNamespace
        original_lstat = Path.lstat
        def reparse_metadata(path):
            details = original_lstat(path)
            if path == self.root:
                return SimpleNamespace(st_mode=details.st_mode, st_file_attributes=0x400)
            return details
        with patch.object(Path, 'lstat', reparse_metadata), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('must not traverse reparse entry')):
            with self.assertRaisesRegex(self.module.BuildError, 'reparse'):
                self.module.validate_desktop_bundle(self.root, source_commit='a' * 40,
                                                    app_version='0.1.0', core_version='0.1.1')

    def test_packaged_runtime_is_complete_without_user_database_or_credentials(self):
        bundle = self.make_bundle()
        self.module.validate_bundle(bundle)
        for name in ("leaked/PG_VERSION", "leaked/global/pg_control", "leaked/owner.json",
                     "leaked/credentials/application.dpapi", "leaked/bootstrap-secret.pw",
                     "leaked/pgpass.conf"):
            with self.subTest(name=name):
                payload = bundle / name
                payload.parent.mkdir(parents=True, exist_ok=True)
                payload.write_bytes(b"synthetic private payload")
                try:
                    with self.assertRaises(self.module.BuildError):
                        self.module.validate_bundle(bundle)
                finally:
                    payload.unlink()

    def test_onedir_layout_without_windows_qt_plugin_is_rejected(self):
        bundle = self.make_bundle()
        (bundle / "_internal/PySide6/plugins/platforms/qwindows.dll").unlink()
        with self.assertRaisesRegex(self.module.BuildError, "qwindows"):
            self.module.validate_bundle(bundle)

    def test_onedir_without_binary_postgres_driver_or_timezone_is_rejected(self):
        bundle = self.make_bundle()
        for name in ('psycopg_binary/pq.cp312-win_amd64.pyd',
                     'psycopg_binary.libs/libpq-fixture.dll',
                     'psycopg_binary.libs/libssl-fixture.dll',
                     'psycopg_binary.libs/libcrypto-fixture.dll',
                     'vcruntime140.dll', 'tzdata/zoneinfo/UTC'):
            target = bundle / '_internal' / name
            original = target.read_bytes()
            target.unlink()
            with self.assertRaises(self.module.BuildError):
                self.module.validate_bundle(bundle)
            target.write_bytes(original)

    def test_duplicate_frozen_model_copy_is_rejected(self):
        bundle = self.make_bundle()
        shutil.copy(bundle / "_internal/models/model_quantized.onnx", bundle)
        with self.assertRaisesRegex(self.module.BuildError, "model_quantized.onnx"):
            self.module.validate_bundle(bundle)

    def test_frozen_bundle_does_not_ship_a_personal_catalog(self):
        bundle = self.make_bundle()
        (bundle / "catalog.sqlite3").touch()
        with self.assertRaisesRegex(self.module.BuildError, "catalog.sqlite3"):
            self.module.validate_bundle(bundle)

    def test_preflight_cli_reports_verified_inputs_and_clear_missing_file_error(self):
        args = [sys.executable, str(SUPPORT), "preflight", "--root", str(self.root)]
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0.1.0", result.stdout)
        (self.root / "models/tokenizer.json").unlink()
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tokenizer.json", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless((ROOT / "dist/ssearch/ssearch.exe").is_file(),
                         "Frozen application has not been built yet")
    def test_existing_frozen_artifact_must_meet_current_runtime_contract(self):
        bundle = ROOT / 'dist/ssearch'
        if not (bundle / '_internal/resources/postgresql/manifest.json').is_file():
            # The preserved 0.1.1 installer/bundle predates PostgreSQL. Do not
            # silently accept it as an output of the new Task 2 build contract.
            with self.assertRaises(self.module.BuildError):
                self.module.validate_bundle(bundle)
        else:
            self.module.validate_bundle(bundle)

    def test_headless_diagnostic_startup_failure_does_not_open_a_modal_window(self):
        if os.name != 'nt':
            self.skipTest('Windows launcher')
        spec = importlib.util.spec_from_file_location('atom_launcher', ROOT / 'packaging/launcher.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with patch('atom_search.desktop.app.main', side_effect=RuntimeError('QA startup failure')), \
                patch.object(sys, 'argv', ['AtomSearch', '--self-test', 'report.json']), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}), \
                patch('ctypes.windll.user32.MessageBoxW') as message:
            self.assertEqual(launcher.launch(), 1)
            message.assert_not_called()
            self.assertIn('QA startup failure',
                          (self.root / 'AtomSearch/startup-error.log').read_text(encoding='utf-8'))


if __name__ == "__main__":
    unittest.main()
