"""Offline wheel tooling and dependency validation for the standalone core."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "packaging/build_support.py"


@pytest.fixture
def support():
    spec = importlib.util.spec_from_file_location("atom_core_build_support", SUPPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('''
[project]
name = "ssearch"
version = "0.1.1"
dependencies = ["PySide6-Essentials==6.8.3", "ssearch-core[semantic]==0.1.1"]
[project.optional-dependencies]
build = ["pyinstaller==6.16.0", "pytest==8.4.2"]
''', encoding="utf-8")
    core = tmp_path / "packages/atom-search-core"
    package = core / "src/atom_search_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VALUE = "standalone fixture"\n', encoding="utf-8")
    (core / "pyproject.toml").write_text('''
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
[project]
name = "ssearch-core"
version = "0.1.1"
requires-python = ">=3.12"
dependencies = []
[project.optional-dependencies]
semantic = ["numpy==2.2.6", "onnxruntime==1.22.1", "tokenizers==0.21.4"]
[tool.setuptools.packages.find]
where = ["src"]
''', encoding="utf-8")
    installed = tmp_path / "installed"
    installed.mkdir()
    for name, version in (("PySide6-Essentials", "6.8.3"), ("ssearch-core", "0.1.1"),
                          ("pyinstaller", "6.16.0"), ("pytest", "8.4.2"),
                          ("numpy", "2.2.6"), ("onnxruntime", "1.22.1"), ("tokenizers", "0.21.4")):
        metadata = installed / (name.replace("-", "_") + "-" + version + ".dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(installed))
    return tmp_path


def test_dependency_verification_resolves_core_extra_and_all_semantic_pins(support, release):
    support.verify_dependencies(release)


def test_app_and_core_versions_can_differ_without_wrong_wheel_selection(support, release):
    project = release / "pyproject.toml"
    project.write_text(project.read_text(encoding="utf-8").replace(
        'version = "0.1.1"', 'version = "0.2.0"'), encoding="utf-8")
    output = release / "artifacts/development" / ("a" * 40) / "wheel"
    output.mkdir(parents=True)
    (output / "ssearch_core-0.2.0-py3-none-any.whl").write_bytes(b"stale app-version wheel")
    wheel = support.build_core_wheel(release, output)
    assert wheel.name == "ssearch_core-0.1.1-py3-none-any.whl"
    support.verify_dependencies(release)


def test_dependency_verification_rejects_wrong_core_semantic_version(support, release):
    metadata = release / "installed/numpy-2.2.6.dist-info/METADATA"
    metadata.write_text("Metadata-Version: 2.1\nName: numpy\nVersion: 99.0.0\n", encoding="utf-8")
    with pytest.raises(support.BuildError, match="numpy==2.2.6"):
        support.verify_dependencies(release)


def test_dependency_verification_rejects_unpinned_core_semantic_requirement(support, release):
    project = release / "packages/atom-search-core/pyproject.toml"
    project.write_text(project.read_text(encoding="utf-8").replace("numpy==2.2.6", "numpy>=2.2.6"),
                       encoding="utf-8")
    with pytest.raises(support.BuildError, match="точную версию.*numpy"):
        support.verify_dependencies(release)


def test_core_wheel_is_built_from_local_source_and_importable_without_desktop(support, release):
    assert callable(getattr(support, "build_core_wheel", None)), "Offline core-wheel builder is missing"
    output = release / "artifacts/core-wheel"
    wheel = support.build_core_wheel(release, output)
    assert wheel == output / "ssearch_core-0.1.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        assert "atom_search_core/__init__.py" in archive.namelist()
        assert not any(name.startswith("atom_search/") for name in archive.namelist())
    result = subprocess.run([sys.executable, "-I", "-S", "-c",
                             "import sys; sys.path.insert(0, sys.argv[1]); "
                             "import atom_search_core; print(atom_search_core.VALUE)", str(wheel)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "standalone fixture"


def test_core_wheel_cli_returns_only_the_new_local_wheel_path(release):
    output = release / "wheel with spaces"
    result = subprocess.run([sys.executable, str(SUPPORT), "core-wheel", "--root", str(release),
                             "--directory", str(output)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == str(output / "ssearch_core-0.1.1-py3-none-any.whl")
    assert Path(result.stdout.strip()).is_file()
