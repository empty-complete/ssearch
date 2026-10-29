"""Catalog contract against pinned native PostgreSQL, not an optional DSN."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import importlib
import time

import psycopg
import pytest

from atom_search_core.models import (DirectorySnapshot, EmbeddingModel, EmbeddingWrite,
                                     FileMetadata, SearchScope)

KEY = "fixture-model|filename-parent-v1"
MODEL = EmbeddingModel(KEY, 384, "filename-parent-v1", "a" * 64)


def api():
    return (importlib.import_module("atom_search_core.storage.migrations").apply_catalog_migrations,
            importlib.import_module("atom_search_core.storage.postgres").PostgresCatalog)


def rows(connections, sql, args=()):
    with connections["maintenance"]() as connection:
        cursor = connection.execute(sql, args)
        return cursor.fetchall() if cursor.description is not None else []


def state(connections):
    with connections["maintenance"]() as connection:
        return {table: connection.execute(f"SELECT * FROM atom_search.{table} ORDER BY 1,2").fetchall()
                for table in ("schema_migrations", "catalogs", "sources", "directories", "files",
                              "embedding_models", "file_embeddings")}


def metadata(path="one.txt", size=1, mtime=1234567890123456789):
    return FileMetadata(path, path.rsplit("/", 1)[-1], size, mtime)


def seed(catalog):
    source = catalog.begin_root("C:/fictional/documents")
    catalog.reconcile(source, DirectorySnapshot("", files=(metadata(),)))
    return source


def write(item, vector=None):
    return EmbeddingWrite(item, vector if vector is not None else (1.,) + (0.,) * 383,
                          hashlib.sha256(item.text.encode("utf-8")).hexdigest())


def embed_all(catalog):
    pending = catalog.pending_embeddings(KEY, 384, 100)
    assert catalog.save_embeddings([write(item) for item in pending], KEY) == len(pending)
    return pending


def test_pg_schema_and_extension_versions(pg_connections):
    # Runtime starts before loading the missing implementation: RED is a real live gate.
    assert rows(pg_connections, "SHOW server_version") == [("18.6",)]
    assert rows(pg_connections, "SELECT extversion FROM pg_extension WHERE extname='vector'") == [("0.8.7",)]
    migrate, Catalog = api()
    assert migrate(pg_connections["maintenance"]) == 6
    before = state(pg_connections)
    assert migrate(pg_connections["maintenance"]) == 6
    assert state(pg_connections) == before
    assert rows(pg_connections, "SELECT current_user") == [("atom_bootstrap",)]
    with pg_connections["application"]() as connection:
        assert connection.execute("SELECT current_user").fetchone() == ("atom_app",)
    for sql in ("CREATE SCHEMA forbidden", "CREATE TABLE atom_search.forbidden(id int)",
                "CREATE EXTENSION hstore", "SET ROLE atom_bootstrap",
                "UPDATE atom_search.catalogs SET state='retired'",
                "DELETE FROM atom_search.embedding_models",
                "UPDATE atom_search.embedding_models SET manifest_digest='relabel'",
                "INSERT INTO atom_search.catalogs(state,generation) VALUES ('staging',0)"):
        with pg_connections["application"]() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(sql)
            connection.rollback()
    catalog = Catalog(pg_connections["application"], model=MODEL)
    source = seed(catalog)
    embed_all(catalog)
    catalog.finish_root(source, "complete")
    assert catalog.status(scope=catalog.resolve_scope(None))["files"] == 1
    catalog.reconcile(source, DirectorySnapshot(""))
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(0,)]
    catalog.close()
    with pg_connections["application"]() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_directory_commit_is_atomic(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    embed_all(pg_catalog)
    before = state(pg_connections)
    # The trigger runs after metadata writes/invalidation, at the final generation change.
    rows(pg_connections, """CREATE FUNCTION atom_search.reject_generation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected generation failure'; END $$""")
    rows(pg_connections, """CREATE TRIGGER reject_generation BEFORE UPDATE ON atom_search.catalogs
        FOR EACH ROW EXECUTE FUNCTION atom_search.reject_generation()""")
    with pytest.raises(psycopg.errors.RaiseException, match="injected generation failure"):
        pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(size=2), metadata("two.txt"))))
    assert state(pg_connections) == before


def test_complete_snapshot_deletes_subtree_and_embeddings(pg_catalog, pg_connections):
    source = pg_catalog.begin_root("C:/fictional")
    names = ("a%b", "axb", "a_b", "acb", "dir", "directory", "д'ело")
    pg_catalog.reconcile(source, DirectorySnapshot("", directories=names))
    for name in names:
        pg_catalog.reconcile(source, DirectorySnapshot(name, files=(metadata(name + "/one.txt"),), directories=("sub",)))
        pg_catalog.reconcile(source, DirectorySnapshot(name + "/sub", files=(metadata(name + "/sub/two.txt"),)))
    embed_all(pg_catalog)
    result = pg_catalog.reconcile(source, DirectorySnapshot("", directories=("axb", "acb", "directory"), protected_names=("д'ело",)))
    assert (result.added, result.updated, result.removed) == (0, 0, 6)
    assert {r[0] for r in rows(pg_connections, "SELECT parent FROM atom_search.files")} == {
        "axb", "axb/sub", "acb", "acb/sub", "directory", "directory/sub", "д'ело", "д'ело/sub"}
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(8,)]


@pytest.mark.parametrize("status", ["partial", "cancelled", "unavailable"])
def test_denied_partial_cancelled_snapshot_preserves_unknown_branches(pg_catalog, pg_connections, status):
    source = seed(pg_catalog)
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(),), directories=("a%b", "axb")))
    for name in ("a%b", "axb"):
        pg_catalog.reconcile(source, DirectorySnapshot(name, files=(metadata(name + "/hidden.txt"),)))
    embed_all(pg_catalog)
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(size=2),), complete=False))
    pg_catalog.finish_root(source, status, "denied/cancelled")
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.files") == [(3,)]
    pg_catalog.reconcile(source, DirectorySnapshot("", protected_names=("one.txt", "a%b"), directories=("axb",)))
    pg_catalog.reconcile(source, DirectorySnapshot("axb", complete=False))
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.files") == [(3,)]
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(2,)]


def test_int64_mtime_and_quoted_unicode_paths_round_trip(pg_catalog, pg_connections):
    source = pg_catalog.begin_root("C:/д'ело%_42")
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata("ф'айл%_.dwg", 2**63-1, -(2**63)),)))
    item = pg_catalog.pending_embeddings(KEY, 384, 1)[0]
    assert (item.file.name, item.file.size, item.file.modified_ns) == ("ф'айл%_.dwg", 2**63-1, -(2**63))
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata("ф'айл%_.dwg", 0, 2**63-1),)))
    assert pg_catalog.pending_embeddings(KEY, 384, 1)[0].file.modified_ns == 2**63-1
    before = state(pg_connections)
    for size, mtime in ((-1, 0), (2**63, 0), (0, 2**63), (0, -2**63-1)):
        with pytest.raises((ValueError, psycopg.Error)):
            pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata("new.txt"), metadata("bad", size, mtime))))
        assert state(pg_connections) == before


def test_concurrent_schema_migration_runs_once(pg_connections):
    migrate, _ = api()
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(lambda _: migrate(pg_connections["maintenance"]), range(4))) == [6]*4
    assert rows(pg_connections, "SELECT version FROM atom_search.schema_migrations ORDER BY version") == [(1,), (2,), (3,), (4,), (5,), (6,)]
    assert rows(pg_connections, "SELECT state,generation FROM atom_search.catalogs") == [("active", 0)]


def test_newer_schema_is_not_modified(pg_connections):
    migrate, _ = api()
    migrate(pg_connections["maintenance"])
    from atom_search_core.storage.migrations import _VERSION
    rows(pg_connections, "INSERT INTO atom_search.schema_migrations(version) VALUES (%s)", (_VERSION + 1,))
    rows(pg_connections, "REVOKE USAGE ON SCHEMA atom_search FROM atom_app")
    rows(pg_connections, "REVOKE SELECT ON atom_search.files FROM atom_app")
    before = state(pg_connections)
    privileges = rows(pg_connections, "SELECT nspowner,nspacl FROM pg_namespace WHERE nspname='atom_search'")
    tables = rows(pg_connections, "SELECT relname,relowner,relacl FROM pg_class WHERE relnamespace='atom_search'::regnamespace ORDER BY relname")
    with pytest.raises(ValueError, match="newer|version"):
        migrate(pg_connections["maintenance"])
    assert state(pg_connections) == before
    assert rows(pg_connections, "SELECT nspowner,nspacl FROM pg_namespace WHERE nspname='atom_search'") == privileges
    assert rows(pg_connections, "SELECT relname,relowner,relacl FROM pg_class WHERE relnamespace='atom_search'::regnamespace ORDER BY relname") == tables


def test_foreign_schema_owner_is_rejected_without_catalog_ddl(pg_connections):
    migrate, _ = api()
    rows(pg_connections, "CREATE SCHEMA atom_search AUTHORIZATION atom_app")
    with pytest.raises(ValueError, match="owner"):
        migrate(pg_connections["maintenance"])
    assert rows(pg_connections, "SELECT count(*) FROM pg_tables WHERE schemaname='atom_search'") == [(0,)]


def test_missing_extension_fails_closed_and_does_not_install_it(pg_connections):
    migrate, _ = api()
    connection = pg_connections["maintenance"]()
    try:
        # Transaction-local fault: migration must roll back rather than recreate
        # the extension. P0's extension remains intact after this failed call.
        connection.execute("DROP EXTENSION vector")
        with pytest.raises(ValueError, match="vector 0.8.7"):
            migrate(lambda: connection)
    finally:
        connection.close()
    assert rows(pg_connections, "SELECT extversion FROM pg_extension WHERE extname='vector'") == [("0.8.7",)]
    assert rows(pg_connections, "SELECT to_regnamespace('atom_search')") == [(None,)]


def test_stale_embedding_write_is_rejected(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    old = embed_all(pg_catalog)[0]
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(size=2),)))
    assert pg_catalog.save_embeddings([write(old)], KEY) == 0
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(0,)]
    current = pg_catalog.pending_embeddings(KEY, 384, 1)[0]
    assert current.file.revision == old.file.revision + 1
    assert pg_catalog.save_embeddings([write(current)], KEY) == 1
    before = state(pg_connections)
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(size=2),)))
    assert rows(pg_connections, "SELECT revision FROM atom_search.files") == [(current.file.revision,)]
    assert rows(pg_connections, "SELECT * FROM atom_search.file_embeddings") == before["file_embeddings"]
    assert pg_catalog.pending_embeddings(KEY, 384, 10) == []
    pg_catalog.reconcile(source, DirectorySnapshot(""))
    assert pg_catalog.save_embeddings([write(current)], KEY) == 0


@pytest.mark.parametrize("field,value", [("manifest_digest", "b"*64), ("dimension", 383), ("input_schema", "other")])
def test_model_key_collision_does_not_relabel_vectors(pg_catalog, pg_connections, field, value):
    _, Catalog = api()
    seed(pg_catalog)
    embed_all(pg_catalog)
    before = state(pg_connections)
    with pytest.raises(ValueError):
        Catalog(pg_connections["application"], model=replace(MODEL, **{field: value}))
    assert state(pg_connections) == before
    same = Catalog(pg_connections["application"], model=MODEL)
    assert same.pending_embeddings(KEY, 384, 5) == []


def test_model_identity_concurrent_registration(pg_connections):
    migrate, Catalog = api()
    migrate(pg_connections["maintenance"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        catalogs = list(executor.map(lambda _: Catalog(pg_connections["application"], model=MODEL), range(2)))
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.embedding_models") == [(1,)]
    for catalog in catalogs:
        catalog.close()


def test_lexical_only_catalog_cannot_publish_or_schedule_vectors(pg_catalog, pg_connections):
    _, Catalog = api()
    source = seed(pg_catalog)
    item = pg_catalog.pending_embeddings(KEY, 384, 1)[0]
    lexical = Catalog(pg_connections["application"])
    lexical.reconcile(source, DirectorySnapshot("", files=(metadata(),)))
    assert lexical.pending_embeddings(KEY, 384, 1) == []
    with pytest.raises(ValueError):
        lexical.save_embeddings([write(item)], KEY)
    for key, dimension in (("foreign", 384), (KEY, 383)):
        with pytest.raises(ValueError):
            pg_catalog.pending_embeddings(key, dimension, 1)
    assert pg_catalog.pending_embeddings(KEY, 384, 0) == []


@pytest.mark.parametrize("vector", [(0.,)*384, (float("nan"),)+(0.,)*383,
    (float("inf"),)+(0.,)*383, (1e39,)+(0.,)*383, (1e-99,)+(0.,)*383, (1.,)*383])
def test_bad_vector_rolls_back_whole_batch(pg_catalog, pg_connections, vector):
    source = seed(pg_catalog)
    pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(), metadata("two.txt"))))
    first, second = pg_catalog.pending_embeddings(KEY, 384, 2)
    with pytest.raises(ValueError):
        pg_catalog.save_embeddings([write(first), write(second, vector)], KEY)
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(0,)]


def test_schema_hash_and_passage_are_verified_against_metadata(pg_catalog, pg_connections):
    source = pg_catalog.begin_root("C:/fictional")
    pg_catalog.reconcile(source, DirectorySnapshot("folder/sub", files=(metadata("folder/sub/one.txt"),)))
    item = pg_catalog.pending_embeddings(KEY, 384, 1)[0]
    assert item.text == "one.txt | folder / sub"
    bad = [replace(write(item), input_hash="f"*64), write(replace(item, text="fabricated")),
           write(replace(item, input_schema="other")),
           write(replace(item, file=replace(item.file, name="fabricated")))]
    for record in bad:
        with pytest.raises(ValueError):
            pg_catalog.save_embeddings([write(item), record], KEY)
        assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(0,)]


def test_cas_matches_file_source_path_and_active_catalog(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    item = pg_catalog.pending_embeddings(KEY, 384, 1)[0]
    wrong = [replace(item, file=replace(item.file, **change)) for change in (
        {"file_id": item.file.file_id+100}, {"source_id": source+100}, {"relative_path": "other.txt"}, {"revision": 100})]
    assert pg_catalog.save_embeddings([*(write(i) for i in wrong), write(item)], KEY) == 1
    for invalid in (0, -1, 2**63):
        with pytest.raises(ValueError):
            pg_catalog.save_embeddings([write(replace(item, file=replace(item.file, file_id=invalid)))], KEY)
    rows(pg_connections, "UPDATE atom_search.catalogs SET state='retired'")
    assert pg_catalog.save_embeddings([write(item)], KEY) == 0
    assert pg_catalog.pending_embeddings(KEY, 384, 10) == []
    assert pg_catalog.resolve_scope(None) == SearchScope(())
    assert pg_catalog.status(scope=SearchScope((source,)))["files"] == 0
    before = state(pg_connections)
    for action in (lambda: pg_catalog.reconcile(source, DirectorySnapshot("")),
                   lambda: pg_catalog.finish_root(source, "complete"),
                   lambda: pg_catalog.begin_root("C:/fictional/new")):
        with pytest.raises(ValueError):
            action()
        assert state(pg_connections) == before


def test_mixed_scope_and_staging_ids_never_expose_foreign_catalog(pg_catalog, pg_connections):
    active_source = seed(pg_catalog)
    foreign_catalog = rows(pg_connections, "INSERT INTO atom_search.catalogs(state,generation) VALUES ('staging',0) RETURNING id")[0][0]
    foreign_source = rows(pg_connections, "INSERT INTO atom_search.sources(catalog_id,path,status) VALUES (%s,'staging','new') RETURNING id", (foreign_catalog,))[0][0]
    rows(pg_connections, """INSERT INTO atom_search.files(source_id,relative_path,parent,name,size,modified_ns,revision,compact_name,compact_stem,compact_path,fts_russian,fts_simple)
         VALUES (%s,'secret','','secret',1,0,1,'secret','secret','secret',to_tsvector('simple','secret'),to_tsvector('simple','secret'))""", (foreign_source,))
    status = pg_catalog.status(scope=SearchScope((active_source, foreign_source)))
    assert status["files"] == 1 and [root["id"] for root in status["roots"]] == [active_source]
    assert pg_catalog.status(scope=SearchScope(())) == {"files": 0, "roots": [], "last_success": None}
    assert pg_catalog.status(scope=SearchScope((999999,)))["roots"] == []
    assert [item.file.source_id for item in pg_catalog.pending_embeddings(KEY, 384, 10)] == [active_source]
    with pytest.raises(ValueError):
        pg_catalog.reconcile(foreign_source, DirectorySnapshot(""))


def test_generation_and_revision_overflow_roll_back(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    rows(pg_connections, "UPDATE atom_search.files SET revision=%s", (2**63-1,))
    before = state(pg_connections)
    with pytest.raises((ValueError, psycopg.Error)):
        pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(size=2),)))
    assert state(pg_connections) == before
    rows(pg_connections, "UPDATE atom_search.catalogs SET generation=%s", (2**63-1,))
    before = state(pg_connections)
    with pytest.raises((ValueError, psycopg.Error)):
        pg_catalog.reconcile(source, DirectorySnapshot("", files=(metadata(), metadata("two.txt"))))
    assert state(pg_connections) == before


def test_last_success_survives_incomplete_scan(pg_catalog):
    source = seed(pg_catalog)
    pg_catalog.finish_root(source, "complete")
    scope = pg_catalog.resolve_scope(None)
    success = pg_catalog.status(scope=scope)["last_success"]
    assert success is not None
    assert pg_catalog.begin_root("C:/fictional/documents") == source
    pg_catalog.finish_root(source, "partial", "permission denied")
    status = pg_catalog.status(scope=scope)
    assert status["last_success"] == success
    assert status["roots"][0]["error"] == "permission denied"


def test_cas_waits_for_reconciliation_then_rejects_stale_vector(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    item = embed_all(pg_catalog)[0]
    _, Catalog = api()
    writer = Catalog(pg_connections["application"], model=MODEL)
    # A trigger lets the test hold reconcile after metadata/invalidation but
    # before commit. Observe real server lock waits, not a scheduling sleep.
    rows(pg_connections, """CREATE FUNCTION atom_search.pause_generation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(7219384); RETURN NEW; END $$""")
    rows(pg_connections, """CREATE TRIGGER pause_generation BEFORE UPDATE ON atom_search.catalogs
        FOR EACH ROW EXECUTE FUNCTION atom_search.pause_generation()""")

    def wait_for_lock(connection, query_fragment):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = connection.execute("""SELECT count(*) FROM pg_stat_activity
                WHERE pid<>pg_backend_pid() AND wait_event_type='Lock' AND position(%s in query)>0""",
                                         (query_fragment,)).fetchone()[0]
            if waiting:
                return
            time.sleep(.02)
        pytest.fail("Concurrent operation never reached its expected database lock")

    with pg_connections["maintenance"]() as blocker:
        blocker.autocommit = True
        blocker.execute("SELECT pg_advisory_lock(7219384)")
        with ThreadPoolExecutor(max_workers=2) as executor:
            changed = executor.submit(pg_catalog.reconcile, source, DirectorySnapshot("", files=(metadata(size=2),)))
            try:
                wait_for_lock(blocker, "generation=generation+1")
                saved = executor.submit(writer.save_embeddings, [write(item)], KEY)
                wait_for_lock(blocker, "state='active' FOR UPDATE")
                assert not changed.done() and not saved.done()
            finally:
                blocker.execute("SELECT pg_advisory_unlock(7219384)")
            assert changed.result(timeout=10).updated == 1
            assert saved.result(timeout=10) == 0
    assert rows(pg_connections, "SELECT count(*) FROM atom_search.file_embeddings") == [(0,)]
    writer.close()


def test_catalog_state_change_cannot_race_authorized_mutation(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    before = state(pg_connections)
    with pg_connections["maintenance"]() as maintenance:
        maintenance.execute("UPDATE atom_search.catalogs SET state='retired'")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(pg_catalog.reconcile, source, DirectorySnapshot(""))
            # The active row is already locked by the state change; commit makes
            # the adapter re-evaluate active membership before touching files.
            maintenance.commit()
            with pytest.raises(ValueError, match="active"):
                pending.result(timeout=10)
    assert rows(pg_connections, "SELECT * FROM atom_search.files ORDER BY 1,2") == before["files"]
    assert rows(pg_connections, "SELECT generation FROM atom_search.catalogs") == [(1,)]


def test_reopen_restores_intended_grants_and_active_uniqueness(pg_catalog, pg_connections):
    migrate, Catalog = api()
    source = seed(pg_catalog)
    rows(pg_connections, "REVOKE SELECT ON atom_search.files FROM atom_app")
    assert migrate(pg_connections["maintenance"]) == 6
    reopened = Catalog(pg_connections["application"], model=MODEL)
    assert reopened.status(scope=SearchScope((source,)))["files"] == 1
    with pg_connections["maintenance"]() as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute("INSERT INTO atom_search.catalogs(state,generation) VALUES ('active',0)")
        connection.rollback()
    reopened.close()


def test_underscore_protection_is_literal_and_directory_boundary_is_exact(pg_catalog, pg_connections):
    source = pg_catalog.begin_root("C:/fictional")
    pg_catalog.reconcile(source, DirectorySnapshot("", directories=("a_b", "acb")))
    for name in ("a_b", "acb"):
        pg_catalog.reconcile(source, DirectorySnapshot(name, files=(metadata(name + "/one.txt"),)))
    result = pg_catalog.reconcile(source, DirectorySnapshot("", protected_names=("a_b",)))
    assert result.removed == 1
    assert rows(pg_connections, "SELECT relative_path FROM atom_search.files") == [("a_b/one.txt",)]


def test_snapshot_validation_cannot_change_other_directory(pg_catalog, pg_connections):
    source = seed(pg_catalog)
    before = state(pg_connections)
    for snapshot in (DirectorySnapshot("other", files=(metadata(),)),
                     DirectorySnapshot("../outside"), DirectorySnapshot("", directories=("../other",)),
                     DirectorySnapshot("", files=(metadata(), metadata()))):
        with pytest.raises(ValueError):
            pg_catalog.reconcile(source, snapshot)
        assert state(pg_connections) == before
