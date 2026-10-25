"""Activation scale and real whole-engine admission for identity migration."""
from pathlib import Path

import pytest

from atom_search_core.models import DirectorySnapshot, FileMetadata
from atom_search_core.storage.public_ids import PostgresPublicIds
from atom_search_core.storage.legacy_import import activate_catalog
from tests.integration.test_pg_import import legacy, stage
from tests.integration.test_pg_catalog import rows


def test_activation_preserves_all_public_paths_with_bounded_database_round_trips(pg_catalog, pg_connections, tmp_path):
    identities = PostgresPublicIds(pg_catalog)
    source, = identities.bind_many((('11111111-1111-4111-8111-111111111111', 'Z:/offline'),))
    pg_catalog.reconcile(source, DirectorySnapshot('Папка', files=tuple(
        FileMetadata(f'Папка/Ёж-Ａ{index}.txt', f'Ёж-Ａ{index}.txt', 1, 123) for index in range(1001))))
    before = rows(pg_connections, """SELECT f.relative_path,p.file_uuid::text FROM atom_search.files f
        JOIN atom_search.file_public_ids p ON p.file_id=f.id ORDER BY f.relative_path""")
    report = stage(legacy(tmp_path / 'import.sqlite', count=1001), pg_connections)
    statements = []
    class Connection:
        def __enter__(self):
            self.connection = pg_connections['maintenance'](); self.connection.__enter__(); return self
        def __exit__(self, *args):
            return self.connection.__exit__(*args)
        def execute(self, sql, parameters=None):
            statements.append(None)
            return self.connection.execute(sql, parameters)
    activate_catalog(report.catalog_id, Connection)
    after = rows(pg_connections, """SELECT f.relative_path,p.file_uuid::text FROM atom_search.files f
        JOIN atom_search.file_public_ids p ON p.file_id=f.id JOIN atom_search.sources s ON s.id=f.source_id
        JOIN atom_search.catalogs c ON c.id=s.catalog_id WHERE c.state='active' ORDER BY f.relative_path""")
    assert after == before and len(after) == 1001
    # A bounded set of statements/batches must handle the complete source.
    assert len(statements) < 30


def test_v3_uuid_migration_refuses_insufficient_shared_budget_before_ddl(tmp_path, monkeypatch):
    import atom_search.search_runtime as host
    from atom_search.runtime import _windows
    from atom_search.runtime.storage import StorageBudgetError
    from atom_search_core.storage import migrations
    original_runtime = host.PostgresRuntime
    instances, held = [], []
    class Runtime(original_runtime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); instances.append(self)
        def start(self):
            endpoint = super().start()
            if not getattr(self, 'fixture_seeded', False):
                self.fixture_seeded = True
                with self.open_connection(role='maintenance') as connection:
                    if connection.execute("SELECT to_regclass('atom_search.files')").fetchone()[0] is not None:
                        return endpoint
                    connection.execute(migrations._SCHEMA_V1)
                    connection.execute(migrations._SCHEMA_V2)
                    connection.execute(migrations._SCHEMA_V3)
                    connection.execute('INSERT INTO atom_search.schema_migrations(version) VALUES (1),(2),(3)')
                    connection.execute("INSERT INTO atom_search.sources(id,catalog_id,path,status) VALUES (41,1,'c:\\docs','complete')")
                    connection.execute("""INSERT INTO atom_search.files(source_id,relative_path,parent,name,size,modified_ns,
                        revision,compact_name,compact_stem,compact_path,fts_russian,fts_simple)
                        SELECT 41,'file-'||n,'','file-'||n,1,123,1,'','','','','' FROM generate_series(1,1001) n""")
                budget = self.storage_budget
                used = _windows.allocated_bytes(budget.roots, read_only_files=budget.read_only_files)
                held.append(budget.reserve(budget.quota_bytes - used - 64*1024**2 - 256*1024, 'migration_pressure'))
            return endpoint
    monkeypatch.setattr(host, 'PostgresRuntime', Runtime)
    service = None
    try:
        with pytest.raises(StorageBudgetError, match='quota_exceeded'):
            service = host.open_search_service(tmp_path / 'profile', tmp_path / 'missing')
    finally:
        for reservation in held:
            reservation.__exit__(None, None, None)
        if service is not None:
            service.close()
    runtime = instances[0]
    try:
        runtime.start()
        with runtime.open_connection(role='maintenance') as connection:
            assert connection.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone() == (3,)
            assert connection.execute("SELECT to_regclass('atom_search.file_public_ids')").fetchone() == (None,)
            assert connection.execute('SELECT count(*) FROM atom_search.files').fetchone() == (1001,)
    finally:
        runtime.stop()
    retry = host.open_search_service(tmp_path / 'profile', tmp_path / 'missing')
    try:
        assert retry.runtime.storage_budget.ledger == runtime.storage_budget.ledger
        with retry.runtime.open_connection(role='maintenance') as connection:
            assert connection.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone() == (5,)
            assert connection.execute('SELECT count(*) FROM atom_search.file_public_ids').fetchone() == (1001,)
    finally:
        retry.close()
