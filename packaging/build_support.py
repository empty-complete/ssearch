"""Local build validation, shared by PyInstaller, build.ps1, and tests.

Load this file explicitly rather than importing a package called ``packaging``:
that name belongs to a dependency used by the build tools themselves.
"""

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tomllib
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from atom_search.runtime.manifest import RuntimeValidationError, verify_runtime
MODEL_FILES = ("model_quantized.onnx", "tokenizer.json")


class BuildError(RuntimeError):
    """A release input is unavailable or fails its integrity check."""


def validate_application_proof(proof, *, source_commit, app_version, core_version):
    """A synthetic P0 server or historical SQLite report cannot qualify the app."""
    expected = {'ok': True, 'model_ready': True, 'clean_stop': True,
                'source_clean': True, 'source_commit': source_commit,
                'app_version': app_version, 'core_version': core_version,
                'execution_mode': 'frozen', 'backend': 'postgresql',
                'factory': 'open_search_service', 'schema_version': 5,
                'prior_schema_version': 5, 'public_identity_preserved': True,
                'server_version': '18.6', 'extension_version': '0.8.7',
                'scan_status': 'complete', 'files': 3, 'vector_count': 3, 'dimension': 384,
                'literal_result': 'КМ-101 узлы фасада.dwg',
                'semantic_result': 'Контракт монтажных работ.docx'}
    if (not isinstance(proof, dict) or any(proof.get(key) != value for key, value in expected.items())
            or type(proof.get('catalog_id')) is not int or proof['catalog_id'] <= 0
            or not re.fullmatch('[0-9a-f-]{36}', proof.get('installation_id', ''))
            or not re.fullmatch('[0-9a-f]{64}', proof.get('model_manifest_digest', ''))
            or not proof.get('model_key', '').endswith('|filename-path-root-project-v2')):
        raise BuildError('postgres_application_proof_invalid')
    def valid_uuid(value):
        try:
            return type(value) is str and str(UUID(value)) == value
        except ValueError:
            return False
    files = proof.get('public_file_ids')
    if (not valid_uuid(proof.get('public_source_id')) or type(files) is not dict or len(files) != 3
            or any(type(path) is not str or not path or not valid_uuid(value) for path, value in files.items())
            or len(set(files.values())) != 3):
        raise BuildError('postgres_application_proof_invalid')


def validate_test_selection(repository: Path, *, development=False, quick=False, test_targets=()):
    """Accept only explicit local test files/nodeids, never arbitrary pytest options."""
    targets = list(test_targets)
    if quick and (not development or not targets):
        raise BuildError("Quick requires Development and nonempty TestTargets.")
    if not quick and targets:
        raise BuildError("TestTargets requires Quick; the default build runs the full suite.")
    root = Path(repository).resolve()
    normalized = []
    for target in targets:
        if not isinstance(target, str) or not target or any(char in target for char in '\x00\r\n'):
            raise BuildError("Invalid test target.")
        filename, separator, node = target.partition("::")
        relative = Path(filename.replace("\\", "/"))
        path = (root / relative).resolve()
        if (relative.is_absolute() or relative.drive or ".." in relative.parts
                or not relative.parts or relative.parts[0] != "tests" or ":" in filename
                or relative.suffix != ".py" or not relative.name.startswith("test_")
                or not path.is_relative_to(root / "tests") or not path.is_file()
                or (separator and not node)):
            raise BuildError(f"Test target must be an existing tests/test_*.py file or nodeid: {target}")
        normalized.append(relative.as_posix() + (separator + node if separator else ""))
    return {"scope": "targeted" if quick else "full", "test_targets": normalized}


def validate_desktop_test_selection(repository: Path, *, development=False, quick=False,
                                    test_targets=(), frontend_test_targets=(), desktop_test_targets=()):
    """Validate the union before any Python, frontend or desktop build work.

    An empty Python list in a UI-only Quick means no pytest invocation. It must
    never be passed to a runner whose empty selection means the full suite.
    """
    python, frontend, desktop = list(test_targets), list(frontend_test_targets), list(desktop_test_targets)
    if quick and (not development or not (python or frontend or desktop)):
        raise BuildError('Quick requires Development and a nonempty combined test selection.')
    if not quick and (python or frontend or desktop):
        raise BuildError('Explicit test targets require Quick.')
    selection = validate_test_selection(repository, development=development,
                                        quick=bool(quick and python), test_targets=python)
    root = Path(repository).resolve()

    def validate_typescript(project, targets, suffixes):
        normalized = []
        for target in targets:
            if (not isinstance(target, str) or not target
                    or any(char in target for char in '\x00\r\n:*?[]')):
                raise BuildError('Invalid TypeScript test target.')
            relative = Path(target.replace('\\', '/'))
            path = (root / project / relative).resolve()
            if (relative.is_absolute() or relative.drive or '..' in relative.parts
                    or not relative.parts or relative.parts[0] != 'tests'
                    or not any(relative.name.endswith(suffix) for suffix in suffixes)
                    or not path.is_relative_to(root / project / 'tests') or not path.is_file()):
                raise BuildError('TypeScript target must be an existing test file in its own project.')
            normalized.append(relative.as_posix())
        return normalized

    return {'scope': 'targeted' if quick else 'full', 'test_targets': selection['test_targets'],
            'frontend_test_targets': validate_typescript('frontend', frontend, ('.test.ts', '.test.tsx')),
            'desktop_test_targets': validate_typescript('desktop', desktop, ('.test.ts',))}


def _build_test_selection(repository: Path, *, development=False, quick=False, test_targets=(),
                          desktop_stack=False, frontend_test_targets=(), desktop_test_targets=()):
    if desktop_stack:
        return validate_desktop_test_selection(
            repository, development=development, quick=quick, test_targets=test_targets,
            frontend_test_targets=frontend_test_targets, desktop_test_targets=desktop_test_targets)
    if frontend_test_targets or desktop_test_targets:
        raise BuildError('Frontend and desktop targets require the desktop stack.')
    return validate_test_selection(repository, development=development, quick=quick, test_targets=test_targets)


def write_test_receipt(repository: Path, commit: str, *, quick=False, test_targets=(), exit_code: int,
                       desktop_stack=False, frontend_test_targets=(), desktop_test_targets=()):
    """Called by build.ps1 only after its checked pytest process has returned zero."""
    selection = _build_test_selection(repository, development=True, quick=quick, test_targets=test_targets,
                                      desktop_stack=desktop_stack, frontend_test_targets=frontend_test_targets,
                                      desktop_test_targets=desktop_test_targets)
    if type(exit_code) is not int or exit_code != 0:
        raise BuildError("A failed pytest run cannot create a successful test receipt.")
    verify_source_commit(repository, commit)
    layout = development_layout(repository, commit, quick=quick)
    receipt = {"source_commit": commit, **selection, "full_suite_passed": not quick, "exit_code": 0}
    target = layout["qa"] / "test-receipt.json"
    try:
        with target.open("x", encoding="utf-8") as output:
            output.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError as error:
        raise BuildError("Test receipt already exists; attempts cannot be overwritten.") from error
    return target


def development_layout(repository: Path, commit: str, *, quick=False) -> dict[str, Path]:
    """Compute isolated output paths without creating or deleting anything."""
    if not isinstance(commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        raise BuildError("Development commit должен быть полным 40-hex SHA.")
    repository = Path(repository).resolve()
    base = repository / "artifacts" / ("development-quick" if quick else "development") / commit.lower()
    layout = {"base": base, **{name: base / name for name in
                               ("build", "dist", "artifacts", "wheel", "qa")}}
    # Resolve existing junctions/symlinks as well as lexical '..' components.
    if any(not path.resolve().is_relative_to(base) for path in layout.values()):
        raise BuildError("Development output выходит за каталог commit.")
    return layout


def verify_source_commit(repository: Path, commit: str | None = None) -> str:
    """Require a clean Git source tree and, when supplied, the same HEAD."""
    def git(*arguments):
        result = subprocess.run(["git", "-C", str(repository), *arguments],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode:
            raise BuildError("Не удалось проверить Git source/HEAD: " + result.stderr.strip())
        return result.stdout.strip()

    actual = git("rev-parse", "--verify", "HEAD")
    development_layout(repository, actual)
    if commit is not None and actual != commit:
        raise BuildError("HEAD изменился во время development-сборки.")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise BuildError("Для development-сборки требуется чистое дерево исходников Git.")
    return actual


def prepare_development_build(repository: Path, *, quick=False, test_targets=(), desktop_stack=False,
                              frontend_test_targets=(), desktop_test_targets=()) -> dict[str, Path]:
    """Reserve a new commit directory; never overwrite a previous attempt."""
    _build_test_selection(repository, development=True, quick=quick, test_targets=test_targets,
                          desktop_stack=desktop_stack, frontend_test_targets=frontend_test_targets,
                          desktop_test_targets=desktop_test_targets)
    layout = development_layout(repository, verify_source_commit(repository), quick=quick)
    try:
        layout["base"].mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise BuildError(f"Каталог development-сборки уже существует: {layout['base']}") from error
    return layout


def generate_build_identity(repository: Path, work_directory: Path) -> Path:
    """Embed identity in the PYZ, never infer it from a report/checkout at runtime."""
    identity = {"source_commit": verify_source_commit(repository), "source_clean": True,
                "app_version": project_version(repository),
                "core_version": project_version(repository / "packages/atom-search-core")}
    directory = Path(work_directory) / "build-identity"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "_atom_build_identity.py"
    target.write_text("IDENTITY = " + repr(identity) + "\n", encoding="utf-8")
    return target


def write_development_manifest(repository: Path, commit: str, *, quick=False, test_targets=()) -> Path:
    """Finalize only verified local QA outputs from the unchanged source SHA."""
    selection = validate_test_selection(repository, development=True, quick=quick, test_targets=test_targets)
    layout = development_layout(repository, commit, quick=quick)
    verify_source_commit(repository, commit)
    receipt_path = layout["qa"] / "test-receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        expected = {"source_commit": commit, **selection, "full_suite_passed": not quick, "exit_code": 0}
        if (receipt != expected or type(receipt.get("exit_code")) is not int
                or type(receipt.get("full_suite_passed")) is not bool):
            raise ValueError("Receipt does not match this build")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise BuildError("Missing, failed or mismatched test receipt.") from error
    version = project_version(repository)
    installer = layout["artifacts"] / f"ssearch-Setup-{version}.exe"
    executable = layout["dist"] / "ssearch/ssearch.exe"
    qa = {"self_test": layout["qa"] / "frozen-self-test.json",
          "screenshot": layout["qa"] / "build-smoke.png", "log": layout["qa"] / "build.log",
          'postgresql': layout['qa'] / 'postgres-runtime-proof.json', 'tests': receipt_path}
    for path in (installer, executable, *qa.values()):
        if not path.resolve().is_relative_to(layout["base"]):
            raise BuildError("Development output выходит за каталог commit.")
        _require_file(path)
    try:
        proof = json.loads(qa["self_test"].read_text(encoding="utf-8"))
        validate_application_proof(proof, source_commit=commit, app_version=version,
                                   core_version=project_version(repository / 'packages/atom-search-core'))
    except (ValueError, TypeError, AttributeError) as error:
        raise BuildError("Frozen self-test с моделью не подтверждён.") from error
    try:
        postgres = json.loads(qa['postgresql'].read_text(encoding='utf-8'))
        backup, restore = postgres.get("backup", {}), postgres.get("restore", {})
        if (any(postgres.get(key) is not True for key in
                ('ok', 'scram', 'wrong_password_rejected', 'clean_stop', 'profile_removed',
                 'restart_verified', 'dpapi_verified', 'private_acl_verified',
                 'application_privileges_verified'))
                or postgres.get('execution_mode') != 'frozen'
                or postgres.get('runtime_api') != 'PostgresRuntime'
                or postgres.get('server_version') != '18.6'
                or postgres.get('extension_version') != '0.8.7'
                or postgres.get('dimension') != 384
                or postgres.get('cosine_rows') != [[1, 0.0], [2, 1.0], [3, 2.0]]
                or postgres.get("source_commit") != commit
                or postgres.get("app_version") != version
                or postgres.get("core_version") != project_version(repository / "packages/atom-search-core")
                or backup.get("method") != "logical-schema"
                or backup.get("scope") != "synthetic-fixture-schema"
                or backup.get("same_major") is not True
                or backup.get("runtime_marker_excluded") is not True
                or backup.get("dump_exit_code") != 0
                or type(backup.get("bytes")) is not int or backup["bytes"] <= 0
                or not re.fullmatch("[0-9a-f]{64}", backup.get("sha256", ""))
                or any(restore.get(key) is not True for key in
                       ("rows_equal", "restart_verified", "target_identity_preserved"))
                or restore.get("restore_exit_code") != 0
                or not restore.get("source_installation_id")
                or not restore.get("target_installation_id")
                or restore["source_installation_id"] == restore["target_installation_id"]
                or restore.get("dimension") != 384
                or restore.get("cosine_rows") != [[1, 0.0], [2, 1.0], [3, 2.0]]
                or postgres.get("preservation", {}).get("source_verified") is not True):
            raise ValueError('Required PostgreSQL proof did not pass')
    except (ValueError, TypeError, AttributeError) as error:
        raise BuildError('Disposable PostgreSQL SCRAM/vector proof не подтверждён.') from error
    digest = hashlib.sha256()
    with installer.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    manifest = {
        "source_commit": commit, "app_version": version,
        "test_scope": selection["scope"], "test_targets": selection["test_targets"],
        "full_suite_passed": not quick,
        "core_version": project_version(repository / "packages/atom-search-core"),
        "catalog": {"backend": "postgresql", "schema_version": proof['schema_version']},
        "capabilities": {"postgresql": "application_verified"}, "windows_validation": [],
        "qa": {key: path.relative_to(layout["base"]).as_posix() for key, path in qa.items()},
        "installer": {"path": installer.relative_to(layout["base"]).as_posix(),
                      "size": installer.stat().st_size, "sha256": digest.hexdigest()},
        "executable": {"path": executable.relative_to(layout["base"]).as_posix(),
                       "size": executable.stat().st_size,
                       "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()},
    }
    # Hashing a large installer takes time; check again immediately before publication.
    verify_source_commit(repository, commit)
    target = layout["base"] / "build-manifest.json"
    try:
        with target.open("x", encoding="utf-8") as output:
            output.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError as error:
        raise BuildError("Development manifest уже существует.") from error
    return target


def project_version(root: Path = ROOT) -> str:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("Expected major.minor.patch")
        return version
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BuildError("Не удалось прочитать версию из pyproject.toml.") from error


def export_product_metadata(repository: Path, destination: Path) -> Path:
    """Export the sole product catalog without importing application code.

    Forge and the shared installer consume this exact clean-source input. An
    existing export is never replaced, so a stale build cannot silently acquire
    the identity of a later source tree.
    """
    repository, destination = Path(repository).resolve(), Path(destination)
    commit = verify_source_commit(repository)
    try:
        tree = ast.parse((repository / 'src/atom_search/content.py').read_text(encoding='utf-8'))
        definitions = [node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'PRODUCT'
                               for target in node.targets)]
        if len(definitions) != 1:
            raise ValueError('one product catalog required')
        product = ast.literal_eval(definitions[0])
        required = {'product_name', 'product_name_ru', 'support_email', 'author_name',
                    'author_title', 'organization_group', 'copyright_holder'}
        if (type(product) is not dict or set(product) != required
                or any(type(key) is not str or not key or type(value) is not str
                       or not value.strip() or len(value) > 512
                       or any(ord(char) < 32 or ord(char) == 127 for char in value)
                       for key, value in product.items())):
            raise ValueError('invalid product catalog')
    except (OSError, SyntaxError, ValueError, TypeError) as error:
        raise BuildError('Product catalog is missing or invalid.') from error
    metadata = {'schema_version': 1, 'source_commit': commit,
                'app_version': project_version(repository),
                'core_version': project_version(repository / 'packages/atom-search-core'),
                'product': product}
    verify_source_commit(repository, commit)
    try:
        with destination.open('x', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    except FileExistsError as error:
        raise BuildError('Product metadata export already exists.') from error
    return destination


def _require_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise BuildError(f"Отсутствует или пуст обязательный файл: {path}")


def verify_model(models: Path) -> None:
    """Check prepared local model assets; never fetch or repair build inputs."""
    manifest_path = models / "manifest.json"
    _require_file(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Expected an object")
        if not re.fullmatch(r"[a-fA-F0-9]{40}", manifest.get("revision", "")):
            raise ValueError("Model revision must be a pinned commit")
        if manifest.get("dimension") != 384:
            raise ValueError("Expected 384 dimensions")
        if (manifest.get("onnx_file") != MODEL_FILES[0]
                or manifest.get("tokenizer_file") != MODEL_FILES[1]):
            raise ValueError("Unexpected model filenames")
        files = manifest["files"]
        if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
            raise ValueError("Expected a file list")
        records = {item["path"]: item for item in files}
        if len(records) != len(files):
            raise ValueError("Duplicate manifest entries")
        for name in MODEL_FILES:
            record = records[name]
            if (not isinstance(record.get("size"), int) or isinstance(record.get("size"), bool)
                    or record["size"] <= 0
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", record.get("sha256", ""))):
                raise ValueError("Expected a byte size and SHA-256")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BuildError(f"Некорректный manifest модели: {manifest_path}") from error

    for name in MODEL_FILES:
        path = models / name
        _require_file(path)
        record = records[name]
        if path.stat().st_size != record["size"]:
            raise BuildError(f"Размер модели не соответствует manifest: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().lower() != record["sha256"].lower():
            raise BuildError(f"SHA-256 модели не соответствует manifest: {path}")


def _verify_resources(base: Path) -> None:
    for retired in ("eula.txt", "branding/ksg.png", "branding/astralit.png", "branding/sources.json"):
        if (base / "resources" / retired).exists():
            raise BuildError(f"Remove retired corporate resource: {retired}")
    _require_file(base / "LICENSE.txt")
    for relative in ("help.html", "branding/atom-search.ico"):
        _require_file(base / "resources" / relative)
    licenses = base / "resources" / "licenses"
    if not licenses.is_dir() or not any(item.is_file() and item.stat().st_size
                                       for item in licenses.rglob("*")):
        raise BuildError(f"Отсутствуют тексты лицензий: {licenses}")
    _require_file(base / "THIRD_PARTY_NOTICES.txt")


def collect_datas(root: Path = ROOT) -> list[tuple[str, str]]:
    """Return the actual release inputs, with one copy of the model weights."""
    verify_model(root / "models")
    _verify_resources(root)
    _require_file(root / "packages/atom-search-core/LICENSE.txt")
    _verify_postgresql(root / 'resources/postgresql')
    corpus = root / 'tests/fixtures/project-search-v2'
    _verify_project_corpus(corpus)
    return [(str(root / "resources"), "resources"),
            (str(corpus), 'resources/project-search-v2'),
            (str(root / "THIRD_PARTY_NOTICES.txt"), "."),
            (str(root / "LICENSE.txt"), "."),
            (str(root / "packages/atom-search-core/LICENSE.txt"), "resources/licenses/ssearch-core"),
            *((str(root / "models" / name), "models")
              for name in (*MODEL_FILES, "manifest.json"))]


def _verify_project_corpus(directory):
    from atom_search.project_checks import load_project_corpus
    try:
        load_project_corpus(directory)
    except (OSError, ValueError, KeyError, TypeError) as failure:
        raise BuildError('Project acceptance corpus is missing or invalid.') from failure


def _verify_postgresql(directory: Path) -> None:
    try:
        verify_runtime(directory)
    except RuntimeValidationError as error:
        raise BuildError(f'PostgreSQL runtime verification failed: {error}') from error


def verify_dependencies(root: Path = ROOT) -> None:
    if os.name != "nt" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise BuildError("Для сборки требуется Windows и Python 3.12 x64.")
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        core = tomllib.loads((root / "packages/atom-search-core/pyproject.toml").read_text(
            encoding="utf-8"))["project"]
        requirements = [*project["dependencies"], *project["optional-dependencies"]["build"],
                        *core["dependencies"], *core["optional-dependencies"]["semantic"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BuildError("Не удалось прочитать зависимости приложения и отдельного ядра.") from error
    for requirement in requirements:
        # Extras select dependencies; installed distribution metadata is looked
        # up by the base name (ssearch-core, without [semantic]).
        pin = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*\])?"
            r"\s*==\s*([A-Za-z0-9][A-Za-z0-9.!+_-]*)", str(requirement).strip())
        if pin is None:
            raise BuildError(f"Зависимость сборки должна иметь точную версию: {requirement}")
        name, expected = pin.groups()
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise BuildError(f"Не установлена зависимость сборки: {requirement}") from error
        if actual != expected:
            raise BuildError(f"Требуется {requirement}, установлена версия {actual}.")


def build_core_wheel(root: Path = ROOT, output_directory: Path | None = None) -> Path:
    """Build the local core with the installed backend, without pip or downloads."""
    root = Path(root).resolve()
    core = root / "packages/atom-search-core"
    project_file = core / "pyproject.toml"
    _require_file(project_file)
    try:
        configuration = tomllib.loads(project_file.read_text(encoding="utf-8"))
        if (configuration["project"]["name"] != "ssearch-core"
                or configuration["build-system"]["build-backend"] != "setuptools.build_meta"):
            raise ValueError("Expected the local ssearch-core setuptools project")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BuildError(f"Некорректный проект отдельного ядра: {project_file}") from error
    output = (Path(output_directory) if output_directory is not None
              else root / "artifacts/core-wheel").resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUTF8="1")
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import sys; from setuptools.build_meta import build_wheel; "
             "print(build_wheel(sys.argv[1]))", str(output)], cwd=core, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError("Не удалось выполнить локальную сборку wheel ядра.") from error
    if result.returncode != 0:
        raise BuildError("Локальная сборка wheel ядра завершилась ошибкой:\n"
                         + (result.stderr.strip() or result.stdout.strip()))
    lines = result.stdout.strip().splitlines()
    wheel = output / (lines[-1] if lines else "")
    if wheel.resolve().parent != output or wheel.suffix != ".whl":
        raise BuildError("Сборщик ядра не вернул путь к локальному wheel.")
    _require_file(wheel)
    return wheel


def _reject_private_payload(item: Path) -> None:
    name = item.name.lower()
    if item.is_file() and (name in {'settings.json', 'pg_version', 'pg_control', 'owner.json',
                                  'pgpass', '.pgpass', 'pgpass.conf', 'qa-owner.json',
                                  'postmaster.pid', 'postmaster.opts'}
                          or '.sqlite' in name or item.suffix.lower() in {'.db', '.dpapi'}
                          or (name.startswith('bootstrap-') and name.endswith('.pw'))
                          or name.startswith('pgpass-')):
        raise BuildError(f'В поставку попали персональные данные: {item.name}')


def validate_bundle(directory: Path, *, service_only=False) -> None:
    """Check real onedir output before it is handed to the installer compiler."""
    _require_file(directory / ('ssearch-service.exe' if service_only else 'ssearch.exe'))
    internal = directory / "_internal"
    _verify_resources(internal)
    _require_file(internal / "resources/licenses/ssearch-core/LICENSE.txt")
    _verify_project_corpus(internal / 'resources/project-search-v2')
    verify_model(internal / "models")
    _verify_postgresql(internal / 'resources/postgresql')
    patterns = ("python312.dll",
                "numpy/_core/_multiarray_umath*.pyd", "numpy.libs/*.dll",
                "onnxruntime/capi/onnxruntime_pybind11_state.pyd",
                "onnxruntime/capi/onnxruntime_providers_shared.dll", "tokenizers/tokenizers.pyd",
                'psycopg_binary/pq*.pyd', 'psycopg_binary/_psycopg*.pyd',
                'psycopg_binary.libs/libpq-*.dll', 'psycopg_binary.libs/libssl-*.dll',
                'psycopg_binary.libs/libcrypto-*.dll', 'vcruntime140.dll', 'tzdata/zoneinfo/UTC')
    if not service_only:
        patterns += ('PySide6/Qt6Core.dll', 'PySide6/Qt6Gui.dll', 'PySide6/Qt6Widgets.dll',
                     'PySide6/plugins/platforms/qwindows.dll')
    for pattern in patterns:
        if not any(item.is_file() and item.stat().st_size for item in internal.glob(pattern)):
            raise BuildError(f"В автономной папке отсутствует компонент: {pattern}")
    for name in MODEL_FILES:
        matches = [item for item in directory.rglob(name) if item.is_file()]
        if matches != [internal / "models" / name]:
            raise BuildError(f"Ожидается одна копия {name} в _internal/models.")
    for item in directory.rglob("*"):
        name = item.name.lower()
        parts = tuple(part.lower() for part in item.relative_to(directory).parts)
        if service_only and ((len(parts) >= 2 and parts[0] == '_internal'
                              and parts[1] in {'pyside6', 'shiboken6', 'pyqt5', 'pyqt6', 'qtpy'})
                             or (item.suffix.lower() in {'.dll', '.pyd'} and name.startswith(('qt5', 'qt6')))):
            raise BuildError('GUI toolkit found in service-only bundle.')
        _reject_private_payload(item)


def validate_desktop_bundle(directory: Path, *, source_commit, app_version, core_version) -> dict:
    """Inventory the common payload; execution proofs remain separate gates."""
    directory = Path(directory).absolute()
    inventory = {}
    folded = set()
    pending = [directory]
    while pending:
        item = pending.pop()
        attributes = getattr(item.lstat(), 'st_file_attributes', 0)
        if (attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction())
                or os.path.normcase(str(item.resolve())) != os.path.normcase(str(item.absolute()))):
            raise BuildError('Desktop payload contains a linked or reparse path.')
        if item.is_dir():
            pending.extend(item.iterdir())
            continue
        if not item.is_file():
            raise BuildError('Desktop payload contains a non-file entry.')
        _reject_private_payload(item)
        relative = item.relative_to(directory).as_posix()
        if relative.casefold() in folded:
            raise BuildError('Desktop payload contains duplicate names.')
        folded.add(relative.casefold())
        with item.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        inventory[relative] = {'size': item.stat().st_size, 'sha256': digest}
    for relative in ('ssearch.exe', 'resources/app.asar', 'resources/product-metadata.json',
                     'LICENSE', 'LICENSES.chromium.html', 'locales/en-US.pak'):
        _require_file(directory / relative)
    metadata_path = directory / 'resources/product-metadata.json'
    try:
        if metadata_path.stat().st_size > 65536:
            raise ValueError('oversized metadata')
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        keys = {'product_name', 'product_name_ru', 'support_email', 'author_name',
                'author_title', 'organization_group', 'copyright_holder'}
        if (not re.fullmatch('[0-9a-f]{40}', source_commit)
                or not re.fullmatch(r'\d+\.\d+\.\d+', app_version)
                or not re.fullmatch(r'\d+\.\d+\.\d+', core_version)
                or type(metadata) is not dict
                or set(metadata) != {'schema_version', 'source_commit', 'app_version', 'core_version', 'product'}
                or type(metadata['schema_version']) is not int or metadata['schema_version'] != 1
                or metadata['source_commit'] != source_commit or metadata['app_version'] != app_version
                or metadata['core_version'] != core_version
                or type(metadata['product']) is not dict or set(metadata['product']) != keys
                or any(type(value) is not str or not value.strip() or len(value) > 512
                       or any(ord(char) < 32 or ord(char) == 127 for char in value)
                       for value in metadata['product'].values())):
            raise ValueError('mismatched metadata')
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BuildError('Desktop product identity does not match this build.') from error
    validate_bundle(directory / 'service', service_only=True)
    for name in MODEL_FILES:
        copies = [item for item in inventory if Path(item).name.casefold() == name.casefold()]
        if copies != [f'service/_internal/models/{name}']:
            raise BuildError(f'Ожидается одна копия {name} в service/_internal/models.')
    return dict(sorted(inventory.items()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ssearch offline build verification")
    parser.add_argument("command", choices=("version", "core-wheel", "preflight", "verify-bundle",
                                             "development-start", "development-manifest", "verify-source",
                                             "test-selection", "test-receipt", "export-product",
                                             "desktop-test-selection", "verify-service-bundle",
                                             "verify-desktop-bundle"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-dependencies", action="store_true")
    parser.add_argument("--commit")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--test-target", action="append", default=[])
    parser.add_argument("--frontend-test-target", action="append", default=[])
    parser.add_argument("--desktop-test-target", action="append", default=[])
    parser.add_argument('--desktop-stack', action='store_true')
    parser.add_argument("--exit-code", type=int)
    args = parser.parse_args(argv)
    try:
        if args.desktop_stack and args.command not in {'development-start', 'test-receipt'}:
            raise BuildError('This command does not support the desktop stack.')
        if (args.command != 'desktop-test-selection' and not args.desktop_stack
                and (args.frontend_test_target or args.desktop_test_target)):
            raise BuildError('This command does not accept frontend or desktop targets.')
        options = {"quick": args.quick, "test_targets": args.test_target}
        if args.desktop_stack:
            options.update(desktop_stack=True, frontend_test_targets=args.frontend_test_target,
                           desktop_test_targets=args.desktop_test_target)
        if args.command == 'verify-desktop-bundle':
            if args.directory is None or args.commit is None or args.output is None:
                raise BuildError('Desktop bundle verification requires --directory, --commit and --output.')
            if args.output.resolve().is_relative_to(args.directory.resolve()):
                raise BuildError('Inventory output must be outside the packaged payload.')
            commit = verify_source_commit(args.root, args.commit)
            version, core = project_version(args.root), project_version(args.root / 'packages/atom-search-core')
            inventory = validate_desktop_bundle(args.directory, source_commit=commit,
                                                app_version=version, core_version=core)
            verify_source_commit(args.root, commit)
            with args.output.open('x', encoding='utf-8') as output:
                json.dump({'schema_version': 1, 'source_commit': commit, 'app_version': version,
                           'core_version': core, 'files': inventory, 'inventory_only': True}, output, indent=2)
                output.write('\n')
            print(args.output)
        elif args.command == "desktop-test-selection":
            print(json.dumps(validate_desktop_test_selection(
                args.root, development=args.development, **options,
                frontend_test_targets=args.frontend_test_target, desktop_test_targets=args.desktop_test_target)))
        elif args.command == "export-product":
            if args.output is None:
                raise BuildError('Product metadata export requires --output.')
            print(export_product_metadata(args.root, args.output))
        elif args.command == "test-selection":
            print(json.dumps(validate_test_selection(args.root, development=args.development, **options)))
        elif args.command == "test-receipt":
            if args.commit is None:
                raise BuildError("Test receipt requires --commit.")
            print(write_test_receipt(args.root, args.commit, exit_code=args.exit_code, **options))
        elif args.command == "verify-source":
            print(verify_source_commit(args.root, args.commit))
        elif args.command == "development-start":
            print(json.dumps({key: str(path) for key, path in
                              prepare_development_build(args.root, **options).items()}))
        elif args.command == "development-manifest":
            if args.commit is None:
                raise BuildError("Укажите --commit для development manifest.")
            print(write_development_manifest(args.root, args.commit, **options))
        elif args.command == "version":
            print(project_version(args.root))
        elif args.command == "core-wheel":
            print(build_core_wheel(args.root, args.directory))
        elif args.command == "preflight":
            collect_datas(args.root)
            if args.check_dependencies:
                verify_dependencies(args.root)
            print(f"ssearch {project_version(args.root)}: локальные входные файлы проверены.")
        else:
            if args.directory is None:
                raise BuildError("Укажите --directory с автономной папкой приложения.")
            validate_bundle(args.directory, service_only=args.command == 'verify-service-bundle')
            print(f"Автономная папка проверена: {args.directory}")
        return 0
    except (BuildError, OSError) as error:
        print(f"Сборка остановлена: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
