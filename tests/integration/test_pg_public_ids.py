"""Durable public identities on the owned native PostgreSQL runtime."""
from copy import deepcopy
import importlib
from contextlib import contextmanager
from pathlib import Path
import subprocess
import uuid

import psycopg
import pytest

from atom_search_core.models import DirectorySnapshot, FileMetadata, SearchScope
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
from tests.integration.test_pg_catalog import rows, state

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'


def public(catalog):
    return importlib.import_module('atom_search_core.storage.public_ids').PostgresPublicIds(catalog)


def metadata(name='one.txt'):
    return FileMetadata(name, name, 1, 123)


def test_existing_p1_source_binds_once_without_reindexing(pg_connections):
    from atom_search_core.storage import migrations
    with pg_connections['maintenance']() as connection:
        connection.execute(migrations._SCHEMA_V1)
        connection.execute(migrations._SCHEMA_V2)
        connection.execute(migrations._SCHEMA_V3)
        connection.execute("INSERT INTO atom_search.schema_migrations(version) VALUES (1),(2),(3)")
        connection.execute("INSERT INTO atom_search.sources(id,catalog_id,path,status) VALUES (41,1,'c:\\docs','complete')")
        connection.execute("""INSERT INTO atom_search.files(id,source_id,relative_path,parent,name,size,modified_ns,
            revision,compact_name,compact_stem,compact_path,fts_russian,fts_simple)
            VALUES (51,41,'one.txt','','one.txt',1,123,7,'onetxt','one','onetxt','','')""")
    before = state(pg_connections)
    assert apply_catalog_migrations(pg_connections['maintenance']) == 5
    assert {key: value for key, value in state(pg_connections).items() if key != 'schema_migrations'} == {
        key: value for key, value in before.items() if key != 'schema_migrations'}
    catalog = PostgresCatalog(pg_connections['application'])
    identities = public(catalog)
    assert identities.bind_many(((A, 'C:/DOCS'),)) == (41,)
    assert identities.bind_many(((A, 'c:\\docs\\'),)) == (41,)
    result_id = identities.public_file_id(51)
    assert str(uuid.UUID(result_id)) == result_id and result_id != '51'
    assert identities.source_uuid('C:/docs') == A
    assert catalog.get_file(51, scope=SearchScope((41,))).revision == 7
    assert apply_catalog_migrations(pg_connections['maintenance']) == 5
    assert public(PostgresCatalog(pg_connections['application'])).public_file_id(51) == result_id


def test_result_id_fetch_cannot_escape_source_scope(pg_catalog, pg_connections, monkeypatch):
    identities = public(pg_catalog)
    a, b = identities.bind_many(((A, 'C:/a'), (B, 'D:/b')))
    for source in (a, b):
        pg_catalog.reconcile(source, DirectorySnapshot('', files=(metadata(),)))
    first, second = rows(pg_connections, 'SELECT id FROM atom_search.files ORDER BY id')
    result_id = identities.public_file_id(first[0])
    original_exists, original_open = Path.exists, Path.open
    def guarded(original):
        def call(path, *args, **kwargs):
            if str(path).lower().startswith(('c:\\a', 'd:\\b')):
                raise AssertionError('document access')
            return original(path, *args, **kwargs)
        return call
    monkeypatch.setattr(Path, 'exists', guarded(original_exists))
    monkeypatch.setattr(Path, 'open', guarded(original_open))
    assert identities.internal_file_id(result_id, scope=SearchScope((b,))) is None
    assert identities.internal_file_id(result_id, scope=SearchScope(())) is None
    assert identities.internal_file_id(str(uuid.uuid4()), scope=SearchScope((a,))) is None
    assert identities.internal_file_id(result_id, scope=SearchScope((a,))) == first[0]
    assert pg_catalog.get_file(first[0], scope=SearchScope((b,))) is None
    assert pg_catalog.get_file(first[0], scope=SearchScope((a,))).name == 'one.txt'
    assert PostgresRetrieval(pg_connections['application']).lexical_candidates(
        'one', scope=SearchScope((b,)), limit=1)[0].file.file_id == second[0]
    rows(pg_connections, "UPDATE atom_search.catalogs SET state='retired'")
    assert identities.internal_file_id(result_id, scope=SearchScope((a,))) is None
    assert pg_catalog.get_file(first[0], scope=SearchScope((a,))) is None


def test_mapping_reconcile_rollback_cascade_and_partial_identity(pg_catalog, pg_connections):
    identities = public(pg_catalog)
    source, = identities.bind_many(((A, 'C:/a'),))
    pg_catalog.reconcile(source, DirectorySnapshot('', files=(metadata(),)))
    file_id = rows(pg_connections, 'SELECT id FROM atom_search.files')[0][0]
    result_id = identities.public_file_id(file_id)
    pg_catalog.reconcile(source, DirectorySnapshot('', complete=False))
    pg_catalog.finish_root(source, 'unavailable')
    assert identities.public_file_id(file_id) == result_id
    rows(pg_connections, """CREATE FUNCTION atom_search.reject_generation() RETURNS trigger LANGUAGE plpgsql
        AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$""")
    rows(pg_connections, """CREATE TRIGGER reject_generation BEFORE UPDATE ON atom_search.catalogs
        FOR EACH ROW EXECUTE FUNCTION atom_search.reject_generation()""")
    before = rows(pg_connections, 'SELECT * FROM atom_search.file_public_ids')
    with pytest.raises(psycopg.errors.RaiseException):
        pg_catalog.reconcile(source, DirectorySnapshot('', files=(metadata('renamed.txt'),)))
    assert rows(pg_connections, 'SELECT * FROM atom_search.file_public_ids') == before
    rows(pg_connections, 'DROP TRIGGER reject_generation ON atom_search.catalogs')
    pg_catalog.reconcile(source, DirectorySnapshot('', files=(metadata('renamed.txt'),)))
    assert identities.internal_file_id(result_id, scope=SearchScope((source,))) is None
    replacement = rows(pg_connections, 'SELECT id FROM atom_search.files')[0][0]
    assert identities.public_file_id(replacement) != result_id
    rows(pg_connections, 'DELETE FROM atom_search.sources WHERE id=%s', (source,))
    assert rows(pg_connections, 'SELECT * FROM atom_search.file_public_ids') == []
    assert rows(pg_connections, 'SELECT * FROM atom_search.source_public_ids') == []


@pytest.mark.parametrize('crash_after_sql', [False, True])
def test_settings_crash_reconciles_persisted_uuid_without_early_pg(tmp_path, pg_catalog, pg_connections, monkeypatch, crash_after_sql):
    from atom_search.settings.repository import SettingsRepository
    from atom_search.service.mutations import MutationCoordinator
    from atom_search.service.sources import SourceRegistry
    import json
    from atom_search.settings.migration import default_settings
    selected = default_settings()
    selected['sources'] = [dict(id=A, path='C:/old', label='', enabled=True)]
    (tmp_path / 'settings.json').write_text(json.dumps(selected), encoding='utf-8')
    repo = SettingsRepository(tmp_path)
    before = repo.load()
    subject = SourceRegistry(repo, public(pg_catalog)); subject.activate()
    old_id = before.settings['sources'][0]['id']
    candidate = deepcopy(before.settings)
    candidate['sources'][0].update(id=B, path='D:/new')
    def interrupted(snapshot):
        if crash_after_sql:
            subject.apply(snapshot)
        raise SystemExit('crash')
    with pytest.raises(SystemExit):
        MutationCoordinator(repo, apply=interrupted).owner_apply(candidate, before.revision)
    recovered = SettingsRepository(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(pg_catalog, '_factory', lambda: (_ for _ in ()).throw(AssertionError('early PG')))
        snapshot = recovered.load()
    assert snapshot.revision == 3 and snapshot.settings['sources'][0]['id'] == old_id
    resumed = SourceRegistry(recovered, public(pg_catalog)); resumed.activate()
    assert resumed.resolve(None, allowed=(old_id,)).source_ids
    assert resumed.resolve(None, allowed=(B,)) == SearchScope(())
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.source_public_ids') == [(2 if crash_after_sql else 1,)]


def test_public_uuid_survives_restart_and_dump_restore(pg_catalog, pg_connections, pg_runtime, tmp_path):
    from atom_search.runtime.postgres import PostgresRuntime
    from atom_search.runtime import _windows
    from psycopg.conninfo import make_conninfo
    source_runtime, _ = pg_runtime
    identities = public(pg_catalog)
    source, = identities.bind_many(((A, 'Z:/offline'),))
    pg_catalog.reconcile(source, DirectorySnapshot('', files=(metadata(),)))
    pg_catalog.finish_root(source, 'unavailable')
    file_id = rows(pg_connections, 'SELECT id FROM atom_search.files')[0][0]
    result_id = identities.public_file_id(file_id)
    root = Path(__file__).resolve().parents[2]
    target = PostgresRuntime(root / 'resources/postgresql', tmp_path / 'restore')
    folder = _windows.private_directory(source_runtime.profile_dir / 'transfer')
    archive = folder / 'application.dump'
    secrets = []

    @contextmanager
    def conninfo(runtime, directory):
        with runtime.open_connection(role='maintenance') as connection:
            info = connection.info
            secrets.append(info.password)
            passfile = directory / ('pass-' + uuid.uuid4().hex)
            escape = lambda value: str(value).replace('\\', '\\\\').replace(':', '\\:')
            passfile.write_text(':'.join(map(escape, (info.host, info.port, info.dbname,
                                                     info.user, info.password))) + '\n', encoding='ascii')
            _windows.assert_private(passfile)
            identity = _windows._file_identity(passfile)
            value = make_conninfo(host=info.host, hostaddr=info.host, port=info.port, dbname=info.dbname,
                user=info.user, passfile=str(passfile), sslmode='disable', gssencmode='disable',
                connect_timeout=5, options='', client_encoding='UTF8')
        try:
            yield value
        finally:
            _windows.assert_private(passfile)
            assert _windows._file_identity(passfile) == identity
            passfile.unlink()

    def tool(name, arguments):
        completed = subprocess.run([str(root / 'resources/postgresql/bin' / name), *arguments],
            capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        assert completed.returncode == 0, name + ' failed'
        return completed.stdout

    try:
        source_installation = source_runtime.status().endpoint.installation_id
        with source_runtime.storage_budget.reserve(64 * 1024**2, 'public_ids_export'):
            with conninfo(source_runtime, folder) as connection:
                tool('pg_dump.exe', ['--format=custom', '--schema=atom_search', '--strict-names',
                    '--no-owner', '--no-privileges', '--no-password', '--file=' + str(archive), '--dbname=' + connection])
            sql = tool('pg_restore.exe', ['--no-owner', '--no-privileges', '--file=-', str(archive)])
            assert b'atom_runtime' not in sql and b'CREATE EXTENSION' not in sql
            assert all(secret.encode() not in sql for secret in secrets)
        target_installation = target.start().installation_id
        assert target_installation != source_installation
        with target.storage_budget.reserve(128 * 1024**2, 'public_ids_restore'):
            with conninfo(target, target.profile_dir) as connection:
                tool('pg_restore.exe', ['--single-transaction', '--exit-on-error', '--no-owner',
                    '--no-privileges', '--no-password', '--dbname=' + connection, str(archive)])
            assert apply_catalog_migrations(lambda: target.open_connection(role='maintenance')) == 5
        restored_catalog = PostgresCatalog(lambda: target.open_connection())
        restored = public(restored_catalog)
        assert restored.bind_many(((A, 'z:/OFFLINE'),)) == (source,)
        assert restored.public_file_id(file_id) == result_id
        assert restored.internal_file_id(result_id, scope=SearchScope((source,))) == file_id
        assert restored_catalog.get_file(file_id, scope=SearchScope((source,))).source_status == 'unavailable'
        # Advancing identity sequences must never regenerate a public UUID.
        with target.open_connection(role='maintenance') as connection:
            connection.execute("SELECT setval('atom_search.sources_id_seq',1000)")
            connection.execute("SELECT setval('atom_search.files_id_seq',2000)")
        assert restored.bind_many(((A, 'Z:/offline'),)) == (source,)
        restored_catalog.close()
        target.stop()
        assert target.start().installation_id == target_installation
        restarted = public(PostgresCatalog(lambda: target.open_connection()))
        assert restarted.source_uuid('Z:/offline') == A
        assert restarted.public_file_id(file_id) == result_id
    finally:
        target.stop()


def test_imported_file_mapping_is_atomic_and_registry_stays_historical_v3(pg_connections, tmp_path):
    from tests.integration.test_pg_import import legacy
    from atom_search_core.storage.legacy_import import stage_legacy_catalog
    apply_catalog_migrations(pg_connections['maintenance'])
    report = stage_legacy_catalog(legacy(tmp_path / 'legacy.sqlite'), pg_connections['maintenance'],
                                  model_manifest=None, reserve_bytes=lambda _: None)
    assert report.status == 'staged'
    assert rows(pg_connections, 'SELECT target_schema_version FROM atom_search.legacy_imports') == [(3,)]
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.file_public_ids') == [(1,)]
    from atom_search_core.storage.migrations import _VERSION
    rows(pg_connections, 'INSERT INTO atom_search.schema_migrations(version) VALUES (%s)', (_VERSION + 1,))
    future = stage_legacy_catalog(legacy(tmp_path / 'other.sqlite'), pg_connections['maintenance'],
                                  model_manifest=None, reserve_bytes=lambda _: None)
    assert future.error_code == 'target_schema_unsupported'


def test_same_root_activation_transfers_public_identity_atomically(pg_catalog, pg_connections, tmp_path):
    from tests.integration.test_pg_import import legacy, stage
    from atom_search_core.storage.legacy_import import activate_catalog
    identities = public(pg_catalog)
    old_source, = identities.bind_many(((A, 'Z:/offline'),))
    pg_catalog.reconcile(old_source, DirectorySnapshot('Папка',
        files=(FileMetadata('Папка/Ёж-Ａ0.txt', 'Ёж-Ａ0.txt', 1, 123),)))
    file_id = rows(pg_connections, 'SELECT id FROM atom_search.files')[0][0]
    result_id = identities.public_file_id(file_id)
    report = stage(legacy(tmp_path / 'import.sqlite'), pg_connections)
    rows(pg_connections, """CREATE FUNCTION atom_search.reject_activation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.state='active' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_activation BEFORE UPDATE ON atom_search.catalogs
        FOR EACH ROW EXECUTE FUNCTION atom_search.reject_activation()""")
    with pytest.raises(psycopg.errors.RaiseException):
        activate_catalog(report.catalog_id, pg_connections['maintenance'])
    assert identities.source_uuid('Z:/offline') == A
    assert identities.public_file_id(file_id) == result_id
    rows(pg_connections, 'DROP TRIGGER reject_activation ON atom_search.catalogs')
    activate_catalog(report.catalog_id, pg_connections['maintenance'])
    activate_catalog(report.catalog_id, pg_connections['maintenance'])
    new_source, = identities.bind_many(((A, 'Z:/offline'),))
    assert new_source != old_source
    new_file = identities.internal_file_id(result_id, scope=SearchScope((new_source,)))
    assert new_file is not None and new_file != file_id
    assert pg_catalog.get_file(new_file, scope=SearchScope((new_source,))).relative_path == 'Папка/Ёж-Ａ0.txt'
