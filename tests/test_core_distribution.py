"""Build/install the real standalone wheel, then prove stdlib-only retrieval."""
from email.parser import Parser
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "packages" / "atom-search-core"


def test_wheel_installs_offline_and_searches_without_desktop_or_semantic_dependencies(tmp_path):
    assert (CORE / "pyproject.toml").is_file(), "The standalone core distribution is missing"
    output = tmp_path / "wheel"
    output.mkdir()
    built = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])", str(output)],
        cwd=CORE, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(output.glob("ssearch_core-0.2.0-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert "atom_search_core/service.py" in names
        assert "atom_search_core/py.typed" in names
        assert not any("desktop/" in name or "windows/" in name or name.startswith("atom_search/") for name in names)
        metadata = Parser().parsestr(archive.read("ssearch_core-0.2.0.dist-info/METADATA").decode("utf-8"))
        assert metadata["Name"] == "ssearch-core"
        assert metadata["Version"] == "0.2.0"
        assert metadata["License-Expression"] == "MIT"
        dependencies = metadata.get_all("Requires-Dist") or []
        assert len(dependencies) == 4
        assert any(item.startswith('psycopg[binary]==3.3.6') and 'extra == "postgres"' in item
                   for item in dependencies)
        assert "atom_search_core/storage/postgres.py" in names
        assert "atom_search_core/storage/migrations.py" in names
        assert "atom_search_core/storage/postgres_retrieval.py" in names
        assert "atom_search_core/storage/legacy_import.py" in names
        for expected in ("numpy==2.2.6", "onnxruntime==1.22.1", "tokenizers==0.21.4"):
            assert any(item.startswith(expected) and 'extra == "semantic"' in item for item in dependencies)
        assert any(name.endswith("/licenses/LICENSE.txt") for name in names)
    target = tmp_path / "installed"
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile", "--target", str(target), str(wheels[0])],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    probe = r'''
import importlib.abc, json, pathlib, sys
target=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(target))
class NoOptionalDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6','PyQt6','atom_search','numpy','onnxruntime','tokenizers',
                                     'psycopg','psycopg2','http','httpx','requests','fastapi','flask'}:
            raise ImportError('Forbidden dependency imported: '+fullname)
sys.meta_path.insert(0,NoOptionalDependencies())
from atom_search_core import SearchService, FileMetadata, DirectorySnapshot, SearchResult, Source, Embedder, Cancellation, ProgressCallback
from atom_search_core import CatalogBackend, RetrievalBackend, CatalogFile, SearchScope, EmbeddingModel
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
from atom_search_core.storage.legacy_import import MigrationReport, snapshot_legacy_database, stage_legacy_catalog, activate_catalog
import atom_search_core
assert atom_search_core.__version__=='0.2.0'
assert pathlib.Path(atom_search_core.__file__).resolve().is_relative_to(target)
source=pathlib.Path(sys.argv[2])/'documents'
source.mkdir()
(source/'КМ-101 facade.dwg').write_text('fictional fixture',encoding='utf-8')
service=SearchService(pathlib.Path(sys.argv[2])/'catalog.sqlite3')
report=service.scan(str(source))
results=service.search('КМ101')
assert report['status']=='complete' and report['files']==1, report
assert isinstance(results[0],SearchResult) and results[0].name=='КМ-101 facade.dwg', results
assert service.index_embeddings()=={'indexed':0,'cancelled':False}
assert service.status()['semantic_ready'] is False
service.close()
assert not any(name.split('.')[0] in {'PySide6','PyQt6','atom_search','numpy','onnxruntime','tokenizers',
                                      'psycopg','psycopg2','http','httpx','requests','fastapi','flask'} for name in sys.modules)
print(json.dumps({'ok':True,'module':atom_search_core.__file__,'files':report['files']}))
'''
    executed = subprocess.run([sys.executable, "-I", "-S", "-c", probe, str(target), str(tmp_path)],
                              cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert json.loads(executed.stdout)["ok"] is True


def test_core_import_without_qt_http_numpy_or_psycopg(tmp_path):
    probe = r'''
import importlib.abc, pathlib, sys
sys.path.insert(0, sys.argv[1])
blocked = {'PySide6','PyQt6','atom_search','numpy','onnxruntime','tokenizers',
           'psycopg','psycopg2','http','httpx','requests','fastapi','flask','sqlite3'}
class NoDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise ImportError('Forbidden dependency imported: ' + fullname)
sys.meta_path.insert(0, NoDependencies())
from atom_search_core import SearchService, CatalogBackend, RetrievalBackend, CatalogFile, SearchScope
assert not blocked.intersection(name.split('.')[0] for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', probe, str(CORE / 'src')],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_and_semantic_extra_versions_are_pinned_to_the_core_distribution():
    assert (CORE / "pyproject.toml").is_file(), "The standalone core distribution is missing"
    core = tomllib.loads((CORE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    app = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert core["dependencies"] == []
    assert core["optional-dependencies"]["postgres"] == ["psycopg[binary]==3.3.6"]
    assert core["optional-dependencies"]["semantic"] == [
        "numpy==2.2.6", "onnxruntime==1.22.1", "tokenizers==0.21.4"]
    assert core["version"] == "0.2.0"
    assert app["version"] == "0.2.0"
    assert "ssearch-core[semantic,postgres]==0.2.0" in app["dependencies"]
