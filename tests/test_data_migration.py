"""Per-user rebrand migration exercises real SQLite snapshots and local paths."""
import json
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from atom_search import paths


def initialize(data_dir=None, **kwargs):
    assert importlib.util.find_spec("atom_search.desktop.data") is not None, "Legacy profile migration is missing"
    from atom_search.desktop import data
    return data.initialize_data_directory(data_dir, **kwargs)


def legacy_catalog(tmp_path):
    legacy = tmp_path / "KaizenSearch"
    legacy.mkdir()
    connection = sqlite3.connect(legacy / "catalog.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE saved_files (name TEXT)")
    connection.execute("INSERT INTO saved_files VALUES ('Чертёж КМ-101.pdf')")
    connection.commit()
    (legacy / "settings.json").write_text(
        json.dumps({"root": r"D:\Рабочие документы", "refresh_days": 3}), encoding="utf-8")
    return legacy, connection


def test_default_data_path_uses_atom_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.data_path() == tmp_path / "AtomSearch"


def test_discovery_does_not_copy_live_wal_before_owner_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy, connection = legacy_catalog(tmp_path)
    try:
        original = {path.name: path.read_bytes() for path in legacy.iterdir()}
        target = initialize()
        assert not (target / 'catalog.sqlite3').exists()
        assert not (target / 'engine/legacy-import').exists()
        assert json.loads((target / 'settings.json').read_text())['root'] == r'D:\Рабочие документы'
        assert {path.name: path.read_bytes() for path in legacy.iterdir()} == original
    finally:
        connection.close()


@pytest.mark.parametrize("existing", ["catalog.sqlite3", "settings.json", "custom-data.txt"])
def test_existing_atom_data_is_never_overwritten(monkeypatch, tmp_path, existing):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy, connection = legacy_catalog(tmp_path)
    connection.close()
    target = tmp_path / "AtomSearch"
    target.mkdir()
    (target / existing).write_bytes(b"existing Atom data")
    assert initialize() == target
    assert (target / existing).read_bytes() == b"existing Atom data"
    assert sorted(item.name for item in target.iterdir()) == [existing]
    assert (legacy / "catalog.sqlite3").is_file()


def test_explicit_data_directory_does_not_import_another_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _, connection = legacy_catalog(tmp_path)
    connection.close()
    target = tmp_path / "isolated-qa"
    assert initialize(target) == target
    assert list(target.iterdir()) == []
    assert not (tmp_path / "AtomSearch").exists()


def test_invalid_legacy_data_is_retained_for_owner_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy = tmp_path / "KaizenSearch"
    legacy.mkdir()
    (legacy / "catalog.sqlite3").write_bytes(b"not a database")
    target = initialize()
    assert list(target.iterdir()) == []
    assert (legacy / "catalog.sqlite3").read_bytes() == b"not a database"


def test_first_launch_without_legacy_creates_empty_local_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    target = initialize()
    assert target == tmp_path / "AtomSearch"
    assert target.is_dir() and list(target.iterdir()) == []


def test_settings_only_upgrade_precedes_window_and_does_not_load_inference(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    from atom_search_core import embeddings
    monkeypatch.setattr(embeddings, 'OnnxEmbedder', lambda _: pytest.fail('Qt preparation loaded inference'))
    legacy = tmp_path / 'KaizenSearch'; legacy.mkdir()
    value = {'root': r'D:\Сохранённая папка', 'refresh_days': 7,
             'last_attempt': 12.5, 'last_refresh': 10, 'storage': {'quota_bytes': 100000000, 'min_free_bytes': 0}}
    source = legacy / 'settings.json'; source.write_text(json.dumps(value), encoding='utf-8')
    before = source.read_bytes()
    target = initialize(model_dir=tmp_path / 'missing')
    assert json.loads((target / 'settings.json').read_text()) == value
    state = json.loads((target / 'engine/lifecycle.json').read_text())
    assert state['source'] is None and state['target_manifest'] is None
    assert not (target / 'engine/postgresql/18').exists()
    assert source.read_bytes() == before
    assert json.loads((target / 'engine/postgresql/storage-reservations.json').read_text()) == []


@pytest.mark.parametrize('value', [{'root': 12}, {'refresh_days': True}, {'last_attempt': float('nan')},
                                 {'schema_version': 99, 'root': r'D:\Saved'}])
def test_invalid_legacy_settings_fail_without_copy(monkeypatch, tmp_path, value):
    from atom_search.desktop.data import DataMigrationError
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    legacy = tmp_path / 'KaizenSearch'; legacy.mkdir()
    source = legacy / 'settings.json'; source.write_text(json.dumps(value), encoding='utf-8')
    before = source.read_bytes()
    with pytest.raises(DataMigrationError): initialize(model_dir=tmp_path / 'missing')
    assert source.read_bytes() == before
    assert not (tmp_path / 'AtomSearch/settings.json').exists()


@pytest.mark.parametrize('storage,expected', [
    ({'quota_bytes': 900000000, 'min_free_bytes': 0}, (900000000, 0)),
    ({}, (5368709120, 2147483648)),
    (None, (1200000000, 0)),
])
def test_initial_atom_storage_precedence_then_canonical_engine(tmp_path, storage, expected):
    from atom_search.search_runtime import _prepare_profile
    from atom_search.runtime.postgres import _storage_policy
    legacy, connection = legacy_catalog(tmp_path)
    connection.close()
    (legacy / 'settings.json').write_text(json.dumps({'storage': {'quota_bytes': 1200000000, 'min_free_bytes': 0}}))
    profile = tmp_path / 'AtomSearch'; profile.mkdir()
    value = {'root': r'C:\current'}
    if storage is not None: value['storage'] = storage
    path = profile / 'settings.json'; path.write_text(json.dumps(value))
    before = path.read_bytes()
    with _prepare_profile(profile, tmp_path / 'missing', discover_legacy=True) as (engine, state, _, _):
        assert _storage_policy(engine) == expected
        assert state['source'] == str(legacy / 'catalog.sqlite3')
    assert path.read_bytes() == before
    path.write_text(json.dumps({'storage': {'quota_bytes': 1, 'min_free_bytes': 0}}))
    with _prepare_profile(profile, tmp_path / 'missing', discover_legacy=True) as (engine, _, _, _):
        assert _storage_policy(engine) == expected


@pytest.mark.parametrize('value', [{'storage': []}, {'storage': {'quota_bytes': -1}}, 'not-json', 'read-failure', 'oversized'])
def test_invalid_atom_policy_never_falls_back_to_kaizen(tmp_path, monkeypatch, value):
    from atom_search.search_runtime import _prepare_profile
    from atom_search.runtime.postgres import PostgresRuntimeError
    legacy, connection = legacy_catalog(tmp_path); connection.close()
    profile = tmp_path / 'AtomSearch'; profile.mkdir()
    path = profile / 'settings.json'
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)
    if value == 'oversized':
        path.write_text(json.dumps({'ignored': 'x' * 65536, 'storage': {'quota_bytes': 900000000, 'min_free_bytes': 0}}))
    if value == 'read-failure':
        original = Path.open
        def denied(self, *args, **kwargs):
            if self == path: raise PermissionError('controlled Atom settings read denial')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(PostgresRuntimeError, match='storage_policy_invalid'):
        with _prepare_profile(profile, tmp_path / 'missing', discover_legacy=True): pass
    assert not (profile / 'engine/lifecycle.json').exists()


@pytest.mark.parametrize('outcome', ['failed_changed', 'failed_invalid', 'success_changed', 'success_wrong_id', 'failed_unchanged', 'success_unchanged'])
def test_model_session_is_bound_to_postconstruction_verified_assets(tmp_path, monkeypatch, outcome):
    import hashlib
    from atom_search_core import embeddings
    from atom_search.search_runtime import _load_model
    model = tmp_path / 'model'; model.mkdir()
    assets = []
    for name in ('model_quantized.onnx', 'tokenizer.json'):
        data = b'controlled asset bytes, not an ONNX inference model'
        (model / name).write_bytes(data)
        assets.append({'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'schema_version': 1, 'dimension': 384, 'max_tokens': 512, 'model_id': 'original',
                'revision': 'a' * 40, 'onnx_file': 'model_quantized.onnx', 'tokenizer_file': 'tokenizer.json', 'files': assets}
    path = model / 'manifest.json'; path.write_text(json.dumps(manifest))
    initial = embeddings._manifest_and_assets(model.resolve())
    calls = []
    def construct(directory, *, preparer=None):
        calls.append(directory)
        if outcome in ('failed_changed', 'success_changed'):
            path.write_text(json.dumps(dict(manifest, model_id='changed')))
        if outcome == 'failed_invalid': (model / 'tokenizer.json').write_bytes(b'broken')
        if outcome.startswith('failed'): raise RuntimeError('controlled inference failure')
        return SimpleNamespace(model_id='wrong' if outcome == 'success_wrong_id' else 'original')
    monkeypatch.setattr(embeddings, 'OnnxEmbedder', construct)
    embedder, verified = _load_model(model, initial)
    assert calls == [model]
    if outcome == 'failed_invalid': assert verified is None
    else:
        assert verified == embeddings._manifest_and_assets(model.resolve())
    assert (embedder is not None) == (outcome == 'success_unchanged')
