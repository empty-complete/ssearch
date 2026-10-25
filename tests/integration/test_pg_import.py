"""Legacy import contracts against the owned native runtime; no ambient DSN."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import time
import uuid

import pytest

from atom_search_core.models import DirectorySnapshot, EmbeddingModel, EmbeddingWrite, FileMetadata, SearchScope
from atom_search_core.storage.migrations import apply_catalog_migrations
from atom_search_core.storage.postgres import PostgresCatalog
from atom_search_core.storage.postgres_retrieval import PostgresRetrieval
from atom_search.runtime.storage import StorageBudget, StorageBudgetError
from atom_search.runtime import _windows

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'models/manifest.json').read_text(encoding='utf-8'))
KEY = MANIFEST['model_id'] + '|filename-parent-v1'
VECTOR = (0.5, -0.25) + (0.,) * 382
EVIDENCE = ROOT / 'artifacts/ci-native/pg-import'


def api():
    name = 'atom_search_core.storage.legacy_import'
    assert importlib.util.find_spec(name) is not None, 'Task4 safe legacy importer is missing'
    return importlib.import_module(name)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(factories, sql, args=()):
    with factories['maintenance']() as connection:
        result = connection.execute(sql, args)
        return result.fetchall() if result.description else []


def legacy(path, count=1, version=2, vector=VECTOR):
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''CREATE TABLE roots(id INTEGER PRIMARY KEY,path TEXT UNIQUE,last_attempt TEXT,
            last_success TEXT,status TEXT,error TEXT);
            CREATE TABLE files(id INTEGER PRIMARY KEY,root_id INTEGER,relative_path TEXT,parent TEXT,name TEXT,
            size INTEGER,modified_ns INTEGER,compact_name TEXT,compact_path TEXT,
            embedding BLOB,embedding_model TEXT,input_hash TEXT,UNIQUE(root_id,relative_path));''')
        db.execute('PRAGMA user_version=' + str(version))
        db.execute("""INSERT INTO roots VALUES (3,'Z:/offline', '2025-01-01T12:00:00+00:00',
                   '2025-01-01T12:00:00+00:00','unavailable','offline')""")
        if version == 2:
            db.executescript('''ALTER TABLE files ADD COLUMN compact_stem TEXT;
                CREATE TABLE directories(root_id INTEGER,relative_path TEXT,parent TEXT,PRIMARY KEY(root_id,relative_path));
                INSERT INTO directories VALUES (3,'Папка','');
                CREATE TABLE catalog_state(name TEXT PRIMARY KEY,value INTEGER);
                INSERT INTO catalog_state VALUES ('generation',7);''')
        for index in range(count):
            name = f'Ёж-Ａ{index}.txt'
            passage = name + ' | Папка'
            blob = vector if isinstance(vector, bytes) else struct.pack('<384f', *vector)
            db.execute('''INSERT INTO files(id,root_id,relative_path,parent,name,size,modified_ns,
                compact_name,compact_path,embedding,embedding_model,input_hash)
                VALUES (?,?,?,?,?,5,123,'untrusted','untrusted',?,?,?)''',
                (42 + index, 3, 'Папка/' + name, 'wrong-parent', name, blob, KEY,
                 hashlib.sha256(passage.encode()).hexdigest()))
        db.commit()
    return path


def stage(path, factories, **kwargs):
    return api().stage_legacy_catalog(path, factories['maintenance'], model_manifest=MANIFEST,
                                     reserve_bytes=kwargs.pop('reserve_bytes', lambda _: None), **kwargs)


def evidence(kind, data):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / (kind + '-' + uuid.uuid4().hex + '.json')
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return path


def allocated_observation(budget, transient):
    # DDL can invalidate pg_internal.init between enumeration and opening. This
    # diagnostic observation retries that transient; real budget calls do not.
    for attempt in range(5):
        try:
            return _windows.allocated_bytes(budget.roots)
        except _windows.WindowsError as error:
            if str(error) != 'allocation_probe_failed' or attempt == 4:
                raise
            transient.append(str(error))
            time.sleep(.05)


def test_import_accepts_known_v1_v2_and_rejects_newer_schema(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    for version in (1, 2):
        source = legacy(tmp_path / f'v{version}.sqlite', version=version)
        before = digest(source)
        target = tmp_path / f'snapshot{version}.sqlite'
        assert api().snapshot_legacy_database(source, target) == digest(target)
        report = stage(target, pg_connections)
        assert (report.status, report.files, report.vectors_reused, report.vectors_pending) == ('staged', 1, 0, 1)
        assert digest(source) == before
    future = legacy(tmp_path / 'future.sqlite', version=3)
    with pytest.raises(ValueError, match='schema'):
        api().snapshot_legacy_database(future, tmp_path / 'rejected.sqlite')
    assert not (tmp_path / 'rejected.sqlite').exists()
    assert stage(future, pg_connections).error_code == 'legacy_schema_unsupported'


def test_offline_sources_keep_stale_metadata(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'legacy.sqlite')
    before = digest(source)
    report = stage(source, pg_connections)
    assert report.status == 'staged'
    assert rows(pg_connections, "SELECT path,status,error FROM atom_search.sources WHERE catalog_id=%s", (report.catalog_id,)) == [('z:\\offline', 'unavailable', 'offline')]
    assert rows(pg_connections, 'SELECT parent,compact_name,compact_stem,compact_path FROM atom_search.files') == [('Папка', 'ежa0txt', 'ежa0', 'папкаежa0txt')]
    assert rows(pg_connections, 'SELECT relative_path FROM atom_search.directories') == [('Папка',)]
    assert rows(pg_connections, 'SELECT legacy_source_id,new_source_id FROM atom_search.legacy_source_map')[0][0] == 3
    assert rows(pg_connections, 'SELECT legacy_file_id,new_file_id FROM atom_search.legacy_file_map')[0] == (42, 1)
    api().activate_catalog(report.catalog_id, pg_connections['maintenance'])
    catalog = PostgresCatalog(pg_connections['application'])
    assert catalog.status(scope=catalog.resolve_scope(None))['files'] == 1
    assert digest(source) == before


@pytest.mark.parametrize('change', ['none', 'revision', 'onnx', 'tokenizer', 'dimension', 'model_key', 'schema', 'hash', 'length', 'nan', 'inf', 'zero', 'valid'])
def test_reuse_requires_matching_model_tokenizer_and_input_hash(pg_connections, tmp_path, change):
    apply_catalog_migrations(pg_connections['maintenance'])
    prior = deepcopy(MANIFEST)
    source = legacy(tmp_path / 'legacy.sqlite')
    if change == 'none':
        prior = None
    elif change == 'revision':
        prior['revision'] = '0' * 40
    elif change in ('onnx', 'tokenizer'):
        prior['files'][0 if change == 'onnx' else 1]['sha256'] = '0' * 64
    elif change == 'dimension':
        prior['dimension'] = 768
    with closing(sqlite3.connect(source)) as db:
        if change in ('schema', 'model_key'):
            db.execute('UPDATE files SET embedding_model=?', (KEY.replace('filename-parent-v1', 'project-context-v2') if change == 'schema' else 'other',))
        if change == 'hash':
            db.execute("UPDATE files SET input_hash='wrong'")
        if change in ('length', 'nan', 'inf', 'zero'):
            blob = b'bad' if change == 'length' else struct.pack('<384f', *({'nan': (float('nan'),) + VECTOR[1:], 'inf': (float('inf'),) + VECTOR[1:], 'zero': (0.,) * 384}[change]))
            db.execute('UPDATE files SET embedding=?', (blob,))
        db.commit()
    report = stage(source, pg_connections, legacy_model_manifest=prior)
    assert report.status == 'staged'
    assert (report.vectors_reused, report.vectors_pending) == ((1, 0) if change == 'valid' else (0, 1))
    if change == 'valid':
        result = rows(pg_connections, 'SELECT embedding::text FROM atom_search.file_embeddings')[0][0]
        assert tuple(json.loads(result)) == VECTOR
        scope = SearchScope(tuple(r[0] for r in rows(pg_connections, 'SELECT id FROM atom_search.sources')))
        retrieval = PostgresRetrieval(pg_connections['application'])
        assert retrieval.vector_candidates(VECTOR, model_key=KEY, scope=scope, limit=10) == []
        api().activate_catalog(report.catalog_id, pg_connections['maintenance'])
        assert len(retrieval.vector_candidates(VECTOR, model_key=KEY, scope=scope, limit=10)) == 1


def test_failed_staging_is_never_searchable(pg_catalog, pg_connections, tmp_path):
    old = pg_catalog.begin_root('C:/old')
    pg_catalog.reconcile(old, DirectorySnapshot('', files=(FileMetadata('old.txt', 'old.txt', 1, 1),)))
    source = legacy(tmp_path / 'legacy.sqlite', 1001)
    calls = []
    def refuse_after_batch(remaining):
        copied = rows(pg_connections, 'SELECT count(*) FROM atom_search.legacy_file_map')[0][0]
        calls.append((remaining, copied))
        if copied == 1000:
            raise OSError('secret content must never enter report')
    report = stage(source, pg_connections, legacy_model_manifest=MANIFEST, reserve_bytes=refuse_after_batch)
    assert report.status == 'failed' and report.error_code == 'storage_reservation_failed'
    assert 'secret' not in repr(report)
    assert report.files == 1000
    new_ids = tuple(r[0] for r in rows(pg_connections, 'SELECT id FROM atom_search.sources WHERE catalog_id=%s', (report.catalog_id,)))
    retrieval = PostgresRetrieval(pg_connections['application'])
    assert retrieval.lexical_candidates('Ёж', scope=SearchScope(new_ids), limit=10) == []
    assert retrieval.vector_candidates(VECTOR, model_key=KEY, scope=SearchScope(new_ids), limit=10) == []
    assert len(retrieval.lexical_candidates('old', scope=SearchScope((old,)), limit=10)) == 1
    with pytest.raises(ValueError):
        api().activate_catalog(report.catalog_id, pg_connections['maintenance'])
    assert calls[0][1] == 0 and calls[-1][1] == 1000 and calls[-1][0] < calls[0][0]


def test_interrupt_resume_and_activation_rollback(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'legacy.sqlite', 1001)
    before = digest(source)
    rows(pg_connections, """CREATE FUNCTION atom_search.reject_second() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.legacy_file_id=1042 THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_second BEFORE INSERT ON atom_search.legacy_file_map
        FOR EACH ROW EXECUTE FUNCTION atom_search.reject_second()""")
    interrupted = stage(source, pg_connections, legacy_model_manifest=MANIFEST)
    assert interrupted.status == 'failed' and interrupted.files == 1000
    assert rows(pg_connections, 'SELECT last_file_id FROM atom_search.legacy_imports') == [(1041,)]
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.files') == [(1000,)]
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.legacy_file_map') == [(1000,)]
    rows(pg_connections, 'DROP TRIGGER reject_second ON atom_search.legacy_file_map')
    for prior in (None, dict(MANIFEST, revision='other')):
        refused = stage(source, pg_connections, legacy_model_manifest=prior)
        assert refused.status == 'failed' and refused.error_code == 'legacy_provenance_conflict'
        assert rows(pg_connections, 'SELECT count(*) FROM atom_search.files') == [(1000,)]
    complete = stage(source, pg_connections, legacy_model_manifest=MANIFEST)
    assert (complete.catalog_id, complete.files, complete.vectors_reused) == (interrupted.catalog_id, 1001, 1001)
    rows(pg_connections, """CREATE FUNCTION atom_search.reject_activation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.state='active' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_activation BEFORE UPDATE ON atom_search.catalogs
        FOR EACH ROW EXECUTE FUNCTION atom_search.reject_activation()""")
    with pytest.raises(Exception, match='injected'):
        api().activate_catalog(complete.catalog_id, pg_connections['maintenance'])
    assert rows(pg_connections, "SELECT id FROM atom_search.catalogs WHERE state='active'") == [(1,)]
    assert rows(pg_connections, 'SELECT state,validated FROM atom_search.legacy_imports') == [('validated', True)]
    rows(pg_connections, 'DROP TRIGGER reject_activation ON atom_search.catalogs')
    api().activate_catalog(complete.catalog_id, pg_connections['maintenance'])
    api().activate_catalog(complete.catalog_id, pg_connections['maintenance'])
    replay = stage(source, pg_connections)
    assert (replay.status, replay.files, replay.vectors_reused) == ('active', 1001, 1001)
    assert digest(source) == before


def test_replay_keeps_initial_none_and_historical_counts_after_indexing(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'legacy.sqlite', 2)
    first = stage(source, pg_connections)
    api().activate_catalog(first.catalog_id, pg_connections['maintenance'])
    identity = hashlib.sha256(json.dumps(MANIFEST, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    catalog = PostgresCatalog(pg_connections['application'], model=EmbeddingModel(KEY, 384, 'filename-parent-v1', identity))
    pending = catalog.pending_embeddings(KEY, 384, 10)
    assert catalog.save_embeddings([EmbeddingWrite(item, VECTOR, hashlib.sha256(item.text.encode()).hexdigest()) for item in pending], KEY) == 2
    root = catalog.resolve_scope(None).source_ids[0]
    catalog.reconcile(root, DirectorySnapshot('Папка', files=(FileMetadata('Папка/Ёж-Ａ0.txt', 'Ёж-Ａ0.txt', 5, 123),)))
    replay = stage(source, pg_connections, legacy_model_manifest=MANIFEST)
    assert (replay.status, replay.files, replay.vectors_reused, replay.vectors_pending) == ('active', 2, 0, 2)
    assert catalog.status(scope=catalog.resolve_scope(None))['files'] == 1
    conflicting = api().stage_legacy_catalog(source, pg_connections['maintenance'], model_manifest=dict(MANIFEST, license_note='different'), reserve_bytes=lambda _: None)
    assert conflicting.error_code == 'target_identity_conflict'
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.catalogs') == [(2,)]


def test_explicit_metadata_only_import_without_target_model(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'legacy.sqlite')
    def metadata_only():
        return api().stage_legacy_catalog(source, pg_connections['maintenance'], model_manifest=None,
            legacy_model_manifest=MANIFEST, reserve_bytes=lambda _: None)
    first = metadata_only()
    assert (first.status, first.files, first.vectors_pending) == ('staged', 1, 1)
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.embedding_models') == [(0,)]
    assert rows(pg_connections, 'SELECT target_model_key,target_manifest_digest,history_fingerprint FROM atom_search.legacy_imports') == [(None, None, None)]
    catalog = PostgresCatalog(pg_connections['application'])
    retrieval = PostgresRetrieval(pg_connections['application'])
    ids = tuple(r[0] for r in rows(pg_connections, 'SELECT id FROM atom_search.sources'))
    assert retrieval.lexical_candidates('Ёж', scope=SearchScope(ids), limit=10) == []
    api().activate_catalog(first.catalog_id, pg_connections['maintenance'])
    found = retrieval.lexical_candidates('Ёж', scope=catalog.resolve_scope(None), limit=10)
    assert len(found) == 1 and found[0].file.source_status == 'unavailable'
    assert stage(source, pg_connections).error_code == 'target_identity_conflict'
    identity = hashlib.sha256(json.dumps(MANIFEST, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    encoder = PostgresCatalog(pg_connections['application'], model=EmbeddingModel(KEY, 384, 'filename-parent-v1', identity))
    pending = encoder.pending_embeddings(KEY, 384, 10)
    assert encoder.save_embeddings([EmbeddingWrite(item, VECTOR, hashlib.sha256(item.text.encode()).hexdigest()) for item in pending], KEY) == 1
    replay = metadata_only()
    assert (replay.status, replay.files, replay.vectors_reused, replay.vectors_pending) == ('active', 1, 0, 1)
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.catalogs') == [(2,)]


@pytest.fixture
def owned_import(pg_runtime):
    runtime, _ = pg_runtime
    folder = runtime.cluster_dir / ('legacy-test-' + uuid.uuid4().hex)
    folder.mkdir()
    budget = StorageBudget((runtime.cluster_dir,), quota_bytes=5368709120, min_free_bytes=2147483648)
    try:
        yield runtime, folder, budget
    finally:
        # Only our known files outside PGDATA; no recursive deletion or sidecar manipulation.
        assert folder.resolve().parent == runtime.cluster_dir.resolve()
        for path in folder.iterdir():
            assert path.is_file() and not path.is_symlink()
            path.unlink()
        folder.rmdir()
        evidence('cleanup', {'owned_folder': str(folder), 'folder_exists': folder.exists(),
            'runtime_profile': str(runtime.profile_dir), 'ledger': json.loads(budget.ledger.read_text()),
            'pgdata_removed': False})


def test_import_includes_committed_wal_without_checkpoint(pg_connections, owned_import):
    apply_catalog_migrations(pg_connections['maintenance'])
    runtime, folder, budget = owned_import
    source = legacy(folder / 'source.sqlite')
    marker = folder / 'writer-ready.json'
    writer_code = '''import sqlite3, sys, json, pathlib
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("UPDATE files SET size=77"); c.commit()
c.execute("UPDATE files SET size=99")
pathlib.Path(sys.argv[2]).write_text(json.dumps({'sqlite_version':sqlite3.sqlite_version}))
sys.stdin.read(1)
c.rollback(); c.close()
'''
    writer = subprocess.Popen([sys._base_executable, '-c', writer_code, str(source), str(marker)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and writer.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert marker.exists(), 'owned WAL producer did not become ready'
        wal = Path(str(source) + '-wal')
        before = {'main': digest(source), 'wal': digest(wal)}
        assert wal.stat().st_size > 0
        with budget.reserve(256 * 1024**2, 'legacy_import') as reservation:
            snapshot = folder / 'snapshot.sqlite'
            snapshot_hash = api().snapshot_legacy_database(source, snapshot)
            report = stage(snapshot, pg_connections, reserve_bytes=reservation.ensure_remaining)
            api().activate_catalog(report.catalog_id, pg_connections['maintenance'])
            # Retain this one bounded consistent snapshot as the import backup.
            snapshot.rename(folder / 'legacy-backup.sqlite')
        assert report.status == 'staged' and report.files == 1
        assert rows(pg_connections, 'SELECT size FROM atom_search.files') == [(77,)]
        after = {'main': digest(source), 'wal': digest(wal)}
        assert after == before  # producer is paused; SHM/lock bookkeeping intentionally excluded.
        assert json.loads(budget.ledger.read_text()) == []
        evidence('wal', {'source': str(source), 'before': before, 'after': after, 'snapshot_sha256': snapshot_hash,
            'backup_sha256': digest(folder / 'legacy-backup.sqlite'), 'report': asdict(report),
            'producer': json.loads(marker.read_text()), 'reader_sqlite_version': sqlite3.sqlite_version,
            'committed_size': 77, 'uncommitted_size': 99, 'reservation_released': True,
            'runtime_profile': str(runtime.profile_dir), 'domain': [str(runtime.cluster_dir)],
            'cleanup': 'producer rolled back/closed; owned fixture files removed by finalizer; runtime fixture stops owner'})
    finally:
        if writer.poll() is None:
            writer.communicate(b'x', timeout=20)
        assert writer.returncode == 0


def test_import_reserves_peak_space_before_batch(pg_connections, owned_import):
    apply_catalog_migrations(pg_connections['maintenance'])
    runtime, folder, budget = owned_import
    source = legacy(folder / 'source.sqlite', 1001)
    before = digest(source)
    transient = []
    # Hold a real competing reservation using the identical domain/policy. It consumes
    # quota headroom without writing gigabytes or testing physical disk exhaustion.
    with budget.reserve(256 * 1024**2, 'legacy_import') as reservation:
        snapshot = folder / 'snapshot.sqlite'
        api().snapshot_legacy_database(source, snapshot)
        reservation.ensure_remaining(0)
        allocated = allocated_observation(budget, transient)
        headroom = budget.quota_bytes - allocated - 8 * 1024**2
        with budget.reserve(headroom, 'fixture_competing_operation'):
            refusal_codes = []
            def real_admission(value):
                try:
                    reservation.ensure_remaining(value)
                except StorageBudgetError as error:
                    refusal_codes.append(str(error))
                    raise
            report = stage(snapshot, pg_connections, reserve_bytes=real_admission)
            assert report.status == 'failed' and report.error_code == 'storage_reservation_failed'
            assert refusal_codes == ['quota_exceeded']
            assert rows(pg_connections, 'SELECT count(*) FROM atom_search.files') == [(0,)]
            assert rows(pg_connections, "SELECT id FROM atom_search.catalogs WHERE state='active'") == [(1,)]
        assert digest(source) == before
    assert json.loads(budget.ledger.read_text()) == []
    with budget.reserve(256 * 1024**2, 'legacy_import') as reservation:
        observed = []
        def remaining(value):
            reservation.ensure_remaining(value)
            live = json.loads(budget.ledger.read_text())
            assert len(live) == 1 and live[0]['remaining'] == value
            observed.append((value, allocated_observation(budget, transient)))
        complete = stage(snapshot, pg_connections, reserve_bytes=remaining)
        assert complete.files == 1001 and complete.status == 'staged'
        api().activate_catalog(complete.catalog_id, pg_connections['maintenance'])
    assert json.loads(budget.ledger.read_text()) == []
    assert len(observed) >= 3 and observed[0][0] > observed[-1][0] > 0
    assert digest(source) == before
    evidence('quota', {'runtime_profile': str(runtime.profile_dir), 'domain': [str(runtime.cluster_dir)],
        'policy': [budget.quota_bytes, budget.min_free_bytes], 'source_sha256_before': before,
        'source_sha256_after': digest(source), 'refusal': asdict(report), 'native_refusal_codes': refusal_codes,
        'resume': asdict(complete),
        'remaining_allocated_samples': observed, 'diagnostic_probe_transients': transient,
        'ledger_after': [], 'physical_ENOSPC_tested': False,
        'cleanup': 'owned fixture files removed by finalizer; parent fixture stops runtime'})


def test_native_process_interruption_resume(pg_connections, owned_import):
    api()
    apply_catalog_migrations(pg_connections['maintenance'])
    runtime, folder, budget = owned_import
    source = legacy(folder / 'source.sqlite', 1001)
    before = digest(source)
    snapshot = folder / 'snapshot.sqlite'
    marker = folder / 'batch-committed.json'
    outcome = folder / 'import-outcome.json'
    # Connection parameters travel only over stdin, never argv, disk, evidence or report.
    with pg_connections['maintenance']() as connection:
        parameters = connection.info.get_parameters()
        parameters['password'] = connection.info.password
    child_code = '''import sys,json,time,pathlib,os,psycopg
from dataclasses import asdict
from atom_search.runtime.storage import StorageBudget
from atom_search_core.storage.legacy_import import snapshot_legacy_database,stage_legacy_catalog
p=json.loads(sys.stdin.readline())
b=StorageBudget((pathlib.Path(p['cluster']),),quota_bytes=5368709120,min_free_bytes=2147483648)
def connect(): return psycopg.connect(**p['connection'])
with b.reserve(256*1024**2,'legacy_import') as reservation:
    snapshot_legacy_database(pathlib.Path(p['source']),pathlib.Path(p['snapshot']))
    def reserve(value):
        reservation.ensure_remaining(value)
        with connect() as c:
            row=c.execute('SELECT last_file_id,files_imported,vectors_reused FROM atom_search.legacy_imports').fetchone()
        if row and row[1]==1000:
            pathlib.Path(p['marker']).write_text(json.dumps({'checkpoint':row,'remaining':value,'pid':os.getpid()}))
            sys.stdin.read(1)
    report=stage_legacy_catalog(pathlib.Path(p['snapshot']),connect,model_manifest=p['manifest'],reserve_bytes=reserve)
    pathlib.Path(p['outcome']).write_text(json.dumps(asdict(report)))
'''
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / 'src'), str(ROOT / 'packages/atom-search-core/src'), str(ROOT / '.venv/Lib/site-packages'))))
    child = subprocess.Popen([sys._base_executable, '-c', child_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        payload = dict(cluster=str(runtime.cluster_dir), connection=parameters, source=str(source),
                       snapshot=str(snapshot), marker=str(marker), outcome=str(outcome), manifest=MANIFEST)
        child.stdin.write((json.dumps(payload) + '\n').encode()); child.stdin.flush()
        deadline = time.monotonic() + 60
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        import_outcome = json.loads(outcome.read_text()) if outcome.exists() else None
        assert marker.exists(), {'boundary': 'committed_batch_not_reached',
                                 'child_exit': child.poll(), 'report': import_outcome}
        checkpoint = json.loads(marker.read_text())
        assert checkpoint['pid'] == child.pid  # Popen must own interpreter, not a venv launcher.
        assert checkpoint['checkpoint'] == [1041, 1000, 0]
        assert rows(pg_connections, 'SELECT count(*) FROM atom_search.legacy_file_map') == [(1000,)]
        assert rows(pg_connections, "SELECT id FROM atom_search.catalogs WHERE state='active'") == [(1,)]
        child.terminate(); child.wait(timeout=20)
        assert digest(source) == before
        # The real ledger reclaims only a proven dead process reservation.
        with budget.reserve(256 * 1024**2, 'legacy_import') as reservation:
            ledger = json.loads(budget.ledger.read_text())
            assert len(ledger) == 1 and ledger[0]['owner']['pid'] == os.getpid()
            def bounded_maintenance():
                connection = pg_connections['maintenance']()
                connection.execute("SET statement_timeout='30s'")
                connection.commit()
                return connection
            admission_failures = []
            admission_contexts = []
            def resume_admission(value):
                try:
                    reservation.ensure_remaining(value)
                except (StorageBudgetError, _windows.WindowsError) as error:
                    admission_failures.append(type(error).__name__ + ':' + str(error))
                    context = error
                    for _ in range(4):
                        if context is None:
                            break
                        phase = getattr(context, 'phase', None)
                        if phase in ('open', 'query', 'type', 'attributes', 'allocation', 'enumerate'):
                            detail = {'phase': phase}
                            native_code = getattr(context, 'winerror', None)
                            if type(native_code) is int:
                                detail['winerror'] = native_code
                            admission_contexts.append(detail)
                        context = context.__context__
                    raise
            resumed = stage(snapshot, dict(pg_connections, maintenance=bounded_maintenance), reserve_bytes=resume_admission, legacy_model_manifest=MANIFEST)
            evidence('interruption-attempt', {'runtime_profile': str(runtime.profile_dir),
                'checkpoint': checkpoint, 'report': asdict(resumed), 'admission_failures': admission_failures,
                'admission_contexts': admission_contexts,
                'source_sha256_before': before, 'source_sha256_after': digest(source)})
            assert (resumed.status, resumed.files, resumed.vectors_reused, resumed.vectors_pending) == ('staged', 1001, 0, 1001), asdict(resumed)
            api().activate_catalog(resumed.catalog_id, pg_connections['maintenance'])
            snapshot.rename(folder / 'legacy-backup.sqlite')
        assert rows(pg_connections, 'SELECT count(*) FROM atom_search.files') == [(1001,)]
        assert json.loads(budget.ledger.read_text()) == []
        evidence('interruption', {'runtime_profile': str(runtime.profile_dir), 'domain': [str(runtime.cluster_dir)],
            'source_sha256_before': before, 'source_sha256_after': digest(source), 'checkpoint': checkpoint,
            'terminated_child_pid': child.pid, 'child_exit_code': child.returncode, 'resumed': asdict(resumed),
            'old_active_before_termination': 1, 'file_maps_before_termination': 1000,
            'files_after_resume': 1001,
            'backup_sha256': digest(folder / 'legacy-backup.sqlite'), 'reservation_reclaimed_and_released': True,
            'cleanup': 'only owned importer terminated; owned fixture files removed by finalizer; runtime owner stops parent fixture'})
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=20)
        child.stdin.close(); child.stdout.close(); child.stderr.close()


def test_stage_rejects_live_wal_as_snapshot(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'still-live.sqlite')
    with closing(sqlite3.connect(source)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE files SET size=88'); writer.commit()
        before = digest(source)
        report = stage(source, pg_connections)
        assert report.error_code == 'legacy_snapshot_not_frozen'
        assert rows(pg_connections, 'SELECT count(*) FROM atom_search.legacy_imports') == [(0,)]
        assert digest(source) == before


def test_orphan_directory_is_rejected_without_partial_import(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'legacy.sqlite')
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("INSERT INTO directories VALUES (77,'orphan','')"); writer.commit()
    report = stage(source, pg_connections)
    assert report.error_code == 'legacy_rows_invalid'
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.legacy_imports') == [(0,)]


def test_v2_upgrades_without_rewriting_catalog_rows(pg_connections):
    from atom_search_core.storage import migrations
    with pg_connections['maintenance']() as connection:
        connection.execute(migrations._SCHEMA_V1)
        connection.execute(migrations._SCHEMA_V2)
        migrations._backfill_v2(connection)
        connection.execute('INSERT INTO atom_search.schema_migrations(version) VALUES (1),(2)')
        connection.execute("INSERT INTO atom_search.sources(catalog_id,path,status) VALUES (1,'retained','unavailable')")
    assert apply_catalog_migrations(pg_connections['maintenance']) == 5
    assert rows(pg_connections, 'SELECT id,path,status FROM atom_search.sources') == [(1, 'retained', 'unavailable')]
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.legacy_imports') == [(0,)]
    with pg_connections['application']() as connection:
        import psycopg
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('SELECT * FROM atom_search.legacy_imports')
        connection.rollback()


def test_invalid_target_and_global_collision_leave_catalog_unchanged(pg_connections, tmp_path):
    apply_catalog_migrations(pg_connections['maintenance'])
    source = legacy(tmp_path / 'legacy.sqlite')
    invalid = api().stage_legacy_catalog(source, pg_connections['maintenance'], model_manifest={}, reserve_bytes=lambda _: None)
    assert invalid.error_code == 'target_manifest_invalid'
    PostgresCatalog(pg_connections['application'], model=EmbeddingModel(KEY, 384, 'filename-parent-v1', 'a' * 64))
    collision = stage(source, pg_connections)
    assert collision.error_code == 'target_identity_conflict'
    assert rows(pg_connections, 'SELECT count(*) FROM atom_search.catalogs') == [(1,)]
    assert rows(pg_connections, 'SELECT manifest_digest FROM atom_search.embedding_models') == [('a' * 64,)]

def test_schema_dump_restore_keeps_search_without_local_runtime_paths(tmp_path):
    """IT handoff: only the application schema, with separately verified model assets."""
    from contextlib import contextmanager
    from atom_search.search_runtime import open_search_service
    from atom_search.runtime.postgres import PostgresRuntime
    from atom_search_core.embeddings import _manifest_and_assets
    from atom_search_core.project_context import INPUT_SCHEMA
    from atom_search_core.source import canonical_root
    from psycopg.conninfo import make_conninfo
    profile = tmp_path / 'profile'; profile.mkdir()
    source = legacy(profile / 'catalog.sqlite3', 2)
    unc = r'\\server\share\Документы'
    windows_path = 'Z:/offline'
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO roots VALUES (4,?,NULL,NULL,'unavailable',NULL)", (unc,))
        db.execute('UPDATE files SET root_id=4 WHERE id=43'); db.commit()
    service = None
    target = PostgresRuntime(ROOT / 'resources/postgresql', tmp_path / 'restore-private')
    report = {'invocation': os.environ.get('ATOM_TEST_INVOCATION'), 'profiles': []}
    passwords = []
    @contextmanager
    def tool_connection(runtime, folder):
        with runtime.open_connection(role='maintenance') as c:
            info = c.info
            assert info.host == '127.0.0.1' and info.user == 'atom_bootstrap'
            password = info.password; passwords.append(password)
            passfile = folder / ('pass-' + uuid.uuid4().hex)
            escape = lambda value: str(value).replace('\\', '\\\\').replace(':', '\\:')
            passfile.write_text(':'.join(map(escape, (info.host, info.port, info.dbname,
                                                     info.user, password)))+'\n', encoding='ascii')
            _windows.assert_private(passfile)
            identity = _windows._file_identity(passfile)
            connection = make_conninfo(host=info.host, hostaddr=info.host, port=info.port,
                dbname=info.dbname, user=info.user, passfile=str(passfile), sslmode='disable',
                gssencmode='disable', connect_timeout=5, options='', client_encoding='UTF8')
        try:
            yield connection
        finally:
            _windows.assert_private(passfile)
            assert _windows._file_identity(passfile) == identity
            passfile.unlink()
    def tool(name, args):
        result = subprocess.run([str(ROOT / 'resources/postgresql/bin' / name), *args],
            capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, name + ' failed'
        return result.stdout
    def state(runtime):
        with runtime.open_connection() as c:
            return {
                'sources': c.execute('SELECT id,path,status FROM atom_search.sources ORDER BY id').fetchall(),
                'files': c.execute('SELECT id,source_id,relative_path,name,revision FROM atom_search.files ORDER BY id').fetchall(),
                'models': c.execute('SELECT * FROM atom_search.embedding_models ORDER BY model_key').fetchall(),
                'vectors': c.execute('SELECT file_id,model_key,file_revision,input_hash,embedding::text FROM atom_search.file_embeddings ORDER BY file_id').fetchall()}
    try:
        service = open_search_service(profile, ROOT / 'models')
        assert service.index_embeddings()['indexed'] == 2
        runtime = service.runtime
        source_id = runtime.status().endpoint.installation_id
        expected = state(runtime)
        # Host indexing uses the current prepared metadata input identity.
        # KEY remains the distinct old filename-parent fixture for import tests.
        current_key = MANIFEST['model_id'] + '|' + INPUT_SCHEMA
        assert len(expected['vectors']) == 2
        assert {row[1] for row in expected['vectors']} == {current_key}
        assert {row[1] for row in expected['sources']} == {canonical_root(windows_path), canonical_root(unc)}
        folder = _windows.private_directory(runtime.profile_dir / 'transfer')
        archive = folder / 'application.dump'
        with runtime.storage_budget.reserve(64 * 1024**2, 'schema_export'):
            with tool_connection(runtime, folder) as conninfo:
                tool('pg_dump.exe', ['--format=custom','--schema=atom_search','--strict-names',
                    '--no-owner','--no-privileges','--no-password','--file='+str(archive),'--dbname='+conninfo])
            sql = tool('pg_restore.exe', ['--no-owner','--no-privileges','--file=-',str(archive)])
            if any(value.encode() in sql for value in [str(runtime.data_dir), 'host=127.0.0.1', *passwords]):
                raise AssertionError('archive_contains_private_runtime_data')
            assert b'atom_runtime' not in sql and b'CREATE EXTENSION' not in sql
        endpoint = target.start()
        assert endpoint.installation_id != source_id
        with target.storage_budget.reserve(128 * 1024**2, 'schema_restore'):
            with tool_connection(target, target.profile_dir) as conninfo:
                tool('pg_restore.exe', ['--single-transaction','--exit-on-error','--no-owner',
                    '--no-privileges','--no-password','--dbname='+conninfo,str(archive)])
            # Grants are deliberately absent from the portable archive.
            with target.open_connection() as c:
                import psycopg
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute('SELECT count(*) FROM atom_search.files')
                c.rollback()
            assert apply_catalog_migrations(lambda: target.open_connection(role='maintenance')) == 5
        assert state(target) == expected
        verified_manifest = _manifest_and_assets((ROOT / 'models').resolve())
        assert verified_manifest == MANIFEST  # separately supplied, verified assets; not SQL payload
        retrieval = PostgresRetrieval(lambda: target.open_connection())
        catalog = PostgresCatalog(lambda: target.open_connection())
        scope = catalog.resolve_scope(None)
        assert len(retrieval.lexical_candidates('Ёж', scope=scope, limit=10)) == 2
        vector = tuple(json.loads(expected['vectors'][0][4]))
        assert retrieval.vector_candidates(vector, model_key=current_key, scope=scope, limit=10)[0].file.file_id == expected['files'][0][0]
        assert retrieval.vector_candidates(vector, model_key=KEY, scope=scope, limit=10) == []
        target.stop(); assert target.start().installation_id == endpoint.installation_id
        assert state(target) == expected
        report.update(source_installation_id=source_id, target_installation_id=endpoint.installation_id,
            archive_sha256=digest(archive), files=2, vectors=2, grants_reapplied=True,
            windows_and_unc_paths_preserved=True, model_manifest_separately_verified=True,
            restart_verified=True, archive_excludes_credentials_and_runtime=True)
    finally:
        for runtime in ([service.runtime] if service is not None else []) + [target]:
            if service is not None and runtime is service.runtime:
                service.close()
            else:
                runtime.stop()
            report['profiles'].append({'profile':str(runtime.profile_dir),
                'installation_id': (runtime._record or {}).get('installation_id'),
                'clean_stop':runtime.status().state=='stopped',
                'owned_process_absent':runtime._record is None or runtime._claimed_process() is None})
        destination = ROOT / 'artifacts/ci-native/search-runtime'
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ('schema-transfer-'+uuid.uuid4().hex+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
