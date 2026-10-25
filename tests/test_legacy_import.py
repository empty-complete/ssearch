"""Read-only snapshot failure boundaries do not need PostgreSQL."""
import importlib
from pathlib import Path
import sqlite3

import pytest


def api():
    name = 'atom_search_core.storage.legacy_import'
    assert importlib.util.find_spec(name) is not None, 'Task4 safe legacy importer is missing'
    return importlib.import_module(name)


def test_snapshot_does_not_overwrite_existing_destination_or_source(tmp_path):
    source = tmp_path / 'source.sqlite'
    with sqlite3.connect(source) as connection:
        connection.execute('PRAGMA user_version=2')
    original = source.read_bytes()
    with pytest.raises(FileExistsError):
        api().snapshot_legacy_database(source, source)
    assert source.read_bytes() == original
    target = tmp_path / 'target.sqlite'
    target.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        api().snapshot_legacy_database(source, target)
    assert target.read_bytes() == b'keep'


def test_snapshot_refuses_missing_corrupt_and_incomplete_known_schema(tmp_path):
    for kind in ('missing', 'corrupt', 'incomplete'):
        source = tmp_path / (kind + '.sqlite')
        target = tmp_path / (kind + '-snapshot.sqlite')
        if kind == 'corrupt':
            source.write_bytes(b'not a database')
        if kind == 'incomplete':
            with sqlite3.connect(source) as connection:
                connection.execute('PRAGMA user_version=2')
        before = source.read_bytes() if source.exists() else None
        with pytest.raises((ValueError, sqlite3.Error, OSError)):
            api().snapshot_legacy_database(source, target)
        assert not target.exists()
        assert (source.read_bytes() if source.exists() else None) == before
