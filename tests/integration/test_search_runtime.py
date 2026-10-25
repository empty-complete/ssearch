"""Real owned PostgreSQL host, canonical storage and application lifecycle."""
import json
import hashlib
import uuid
import subprocess
import os
import sys
import time
from pathlib import Path

import pytest

from atom_search.runtime.postgres import PostgresRuntime, PostgresRuntimeError
from atom_search.runtime.storage import StorageBudgetError
from atom_search.runtime import _windows

RUNTIME = Path(__file__).resolve().parents[2] / 'resources/postgresql'
EVIDENCE = RUNTIME.parents[1] / '.superpowers/sdd/2026-10-20-postgres-search-backend/task-5-evidence'


@pytest.fixture(autouse=True)
def native_receipt(request, monkeypatch):
    records = []
    runtimes = []
    original_init = PostgresRuntime.__init__
    def observed(runtime, *args, **kwargs):
        original_init(runtime, *args, **kwargs)
        runtimes.append(runtime)
    monkeypatch.setattr(PostgresRuntime, '__init__', observed)
    paths = ('src/atom_search/search_runtime.py', 'src/atom_search/runtime/storage.py',
             'src/atom_search/runtime/postgres.py', 'src/atom_search/runtime/_windows.py',
             'packages/atom-search-core/src/atom_search_core/service.py',
             'tests/integration/test_search_runtime.py')
    hashes = lambda: {p: hashlib.sha256((RUNTIME.parents[1] / p).read_bytes()).hexdigest() for p in paths}
    before = hashes()
    yield records
    # Only runtime objects created by this invocation may be cleaned or recorded.
    for runtime in runtimes:
        record = {'engine': str(runtime.profile_dir), 'installation_id':
                  (runtime._record or {}).get('installation_id')}
        try:
            runtime.stop()
            record.update(clean_stop=runtime.status().state == 'stopped',
                          owned_process_absent=runtime._record is None or runtime._claimed_process() is None)
        except Exception as error:
            record['cleanup_error_type'] = type(error).__name__
        records.append(record)
    EVIDENCE.mkdir(exist_ok=True)
    receipt = {'test': request.node.name, 'profiles': records, 'source_hashes': {
        **before}, 'source_hashes_after': hashes(), 'invocation': os.environ.get('ATOM_TEST_INVOCATION')}
    (EVIDENCE / (request.node.name + '-' + uuid.uuid4().hex + '.json')).write_text(
        json.dumps(receipt, indent=2), encoding='utf-8')


def remember(service, receipts):
    record = {'installation_id': service.runtime.status().endpoint.installation_id,
              'engine': str(service.runtime.profile_dir), 'clean_stop': False}
    receipts.append(record)
    return record


def cleanup_host_child(profile):
    """A waited/terminated child can leave only this explicitly selected profile.

    Reclaim through normal verified Runtime ownership, never PID-only killing or
    deleting PGDATA. A foreign/uncertain claim still fails closed.
    """
    runtime = PostgresRuntime(RUNTIME, Path(profile) / 'engine')
    if runtime._intent_path.exists():
        runtime._record = runtime._read_record()
        if runtime._claimed_process() is not None:
            runtime.start()
            runtime.stop()


def test_factory_import_then_absent_legacy_restart_never_reimports(tmp_path, native_receipt):
    import importlib.util
    assert importlib.util.find_spec('atom_search.search_runtime'), 'PG application factory is required'
    from atom_search.search_runtime import open_search_service
    from atom_search_core import SearchService
    profile = tmp_path / 'profile'
    profile.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'original.pdf').touch()
    legacy = profile / 'catalog.sqlite3'
    old = SearchService(legacy)
    old.scan(str(source))
    old.close()
    original = legacy.read_bytes()
    service = open_search_service(profile, tmp_path / 'missing-model')
    record = remember(service, native_receipt)
    try:
        assert service.status()['backend'] == 'postgresql'
        assert service.search('original')[0].name == 'original.pdf'
        assert legacy.read_bytes() == original
        (source / 'new.pdf').touch()
        assert service.scan(str(source))['status'] == 'complete'
        installation = service.runtime.status().endpoint.installation_id
    finally:
        service.close()
        record['clean_stop'] = service.runtime.status().state == 'stopped'
    legacy.unlink()
    service = open_search_service(profile, tmp_path / 'missing-model')
    record = remember(service, native_receipt)
    try:
        assert service.search('new')[0].name == 'new.pdf'
        assert service.runtime.status().endpoint.installation_id == installation
        assert not legacy.exists()
    finally:
        service.close()
        record['clean_stop'] = service.runtime.status().state == 'stopped'


@pytest.mark.parametrize('phase', ['factory', 'scan', 'embedding'])
def test_process_shutdown_waits_for_real_host_and_jobs(tmp_path, native_receipt, phase):
    root = RUNTIME.parents[1]
    script = '''import sys,json,pathlib,threading,time
from PySide6.QtCore import QCoreApplication
from atom_search.desktop.workers import Workers
from atom_search.search_runtime import open_search_service
p=json.loads(sys.stdin.readline()); app=QCoreApplication([])
entered=threading.Event(); release=threading.Event(); state={}
def factory():
    service=open_search_service(pathlib.Path(p['profile']),pathlib.Path(p['model']))
    state['service']=service
    state['installation_id']=service.runtime.status().endpoint.installation_id
    close=service.close
    def closing():
        close()
        pathlib.Path(p['done']).write_text(json.dumps({'installation_id':state['installation_id'],
            'engine':str(service.runtime.profile_dir),'clean_stop':service.runtime.status().state=='stopped',
            'owned_process_absent':service.runtime._claimed_process() is None}))
    service.close=closing
    if p['phase']=='factory':
        entered.set(); release.wait(15)
    return service
def operation(service,progress,cancel):
    if p['phase']=='scan':
        def snapshots(root,cancel=None, *, excluded_roots=()):
            entered.set(); cancel.wait(15)
            return iter(())
        service.source.snapshots=snapshots
        service.scan(p['source'],cancel=cancel)
    elif p['phase']=='embedding':
        service.scan(p['source'])
        original=service._embedder.encode
        def encode(values, **kwargs):
            entered.set(); cancel.wait(15)
            return original(values, **kwargs)
        service._embedder.encode=encode
        service.index_embeddings(cancel=cancel)
workers=Workers(factory,app); workers.start('native-close',operation)
assert entered.wait(120), 'job did not reach shutdown phase'
workers.shutdown(); release.set()
# Return from main without explicit wait: non-daemon ownership must keep process alive.
'''
    source = tmp_path / 'source'; source.mkdir(); (source / 'retained.pdf').touch()
    done = tmp_path / 'stopped.json'
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(root / p) for p in
        ('src', 'packages/atom-search-core/src', '.venv/Lib/site-packages')))
    payload = dict(profile=str(tmp_path / 'profile'), model=str(root / 'models'), source=str(source), done=str(done), phase=phase)
    try:
        result = subprocess.run([sys._base_executable, '-c', script], input=json.dumps(payload).encode()+b'\n',
            capture_output=True, timeout=180, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        cleanup_host_child(payload['profile'])
    assert result.returncode == 0, result.stderr.decode(errors='replace')[-2000:]
    assert done.exists()
    record = json.loads(done.read_text()); native_receipt.append(record)
    assert record['clean_stop'] and record['owned_process_absent']


def test_factory_ready_metadata_quota_refusal_preserves_catalog(tmp_path, native_receipt):
    import importlib.util
    assert importlib.util.find_spec('atom_search.search_runtime'), 'PG application factory is required'
    from atom_search.search_runtime import open_search_service
    profile = tmp_path / 'profile'
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'retained.pdf').touch()
    service = open_search_service(profile, tmp_path / 'missing')
    record = remember(service, native_receipt)
    try:
        service.scan(str(source))
        budget = service.runtime.storage_budget
        used = _windows.allocated_bytes(budget.roots, read_only_files=budget.read_only_files)
        with budget.reserve(budget.quota_bytes - used - 512 * 1024, 'competitor'):
            (source / 'rejected.pdf').touch()
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                service.scan(str(source))
            assert not service.search('rejected')
            assert service.search('retained')
        assert service.scan(str(source))['status'] == 'complete'
        assert service.search('rejected')
    finally:
        service.close()
        record['clean_stop'] = service.runtime.status().state == 'stopped'


def test_factory_failure_cleans_only_owned_runtime(tmp_path, monkeypatch, native_receipt):
    import atom_search.search_runtime as host
    observed = []
    original = host.PostgresRuntime
    class Observed(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            observed.append(self)
    monkeypatch.setattr(host, 'PostgresRuntime', Observed)
    def failed(_):
        raise ValueError('injected_catalog_initialization_failure')
    monkeypatch.setattr(host, 'apply_catalog_migrations', failed)
    with pytest.raises(ValueError, match='injected_catalog_initialization_failure'):
        host.open_search_service(tmp_path / 'profile', tmp_path / 'missing')
    assert len(observed) == 1
    assert observed[0].status().state == 'stopped'
    assert observed[0]._claimed_process() is None
    native_receipt.append({'engine': str(observed[0].profile_dir),
        'installation_id': observed[0]._record['installation_id'], 'clean_stop': True})
    assert not (tmp_path / 'profile/catalog.sqlite3').exists()


def test_quota_refuses_before_kaizen_snapshot_and_preserves_source_acl(tmp_path):
    from atom_search.search_runtime import open_search_service
    legacy = tmp_path / 'KaizenSearch'
    legacy.mkdir()
    source = legacy / 'catalog.sqlite3'
    source.write_bytes(b'unchanged legacy')
    (legacy / 'settings.json').write_text(json.dumps({'storage': {'quota_bytes': 1024, 'min_free_bytes': 0}}))
    def acl():
        return subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            '[System.IO.File]::GetAccessControl($env:ATOM_ACL_TEST_PATH).Sddl'],
            env=dict(os.environ, ATOM_ACL_TEST_PATH=str(source)), capture_output=True, check=True).stdout
    before = acl()
    with pytest.raises(StorageBudgetError, match='quota_exceeded'):
        open_search_service(tmp_path / 'AtomSearch', tmp_path / 'missing', discover_legacy=True)
    assert source.read_bytes() == b'unchanged legacy'
    assert acl() == before
    assert not list((tmp_path / 'AtomSearch').rglob('snapshot.sqlite3'))


def test_ready_embedding_quota_refusal_and_deletion_estimate(tmp_path, native_receipt):
    from atom_search.search_runtime import open_search_service
    from atom_search_core.models import DirectorySnapshot
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'Кронштейн.pdf').touch()
    service = open_search_service(tmp_path / 'profile', RUNTIME.parents[1] / 'models')
    record = remember(service, native_receipt)
    try:
        assert service.scan(str(source))['status'] == 'complete'
        assert service.status()['metadata_ready'] and not service.status()['embeddings_ready']
        budget = service.runtime.storage_budget
        used = _windows.allocated_bytes(budget.roots)
        with budget.reserve(budget.quota_bytes - used - 8 * 1024**2, 'competing-job'):
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                service.index_embeddings()
            with service.runtime.open_connection() as connection:
                assert connection.execute('SELECT count(*) FROM atom_search.file_embeddings').fetchone()[0] == 0
        assert service.index_embeddings()['indexed'] == 1
        root_id = service.catalog.resolve_scope(str(source)).source_ids[0]
        assert service.catalog._existing_bytes(root_id) > 8192
        stale = service.catalog.pending_embeddings(service._model_key, 384, 1)
        assert stale == []
        with service.runtime.open_connection() as connection:
            assert connection.execute('SELECT count(*) FROM atom_search.file_project_context').fetchone() == (1,)
        used = _windows.allocated_bytes(budget.roots)
        with budget.reserve(budget.quota_bytes - used - 8 * 1024**2, 'competing-delete'):
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                service.catalog.reconcile(root_id, DirectorySnapshot('', ()))
            with service.runtime.open_connection() as connection:
                assert connection.execute('SELECT count(*) FROM atom_search.file_project_context').fetchone() == (1,)
        assert service.catalog.reconcile(root_id, DirectorySnapshot('', ())).removed == 1
        assert not service.search('Кронштейн')
        with service.runtime.open_connection() as connection:
            assert connection.execute('SELECT count(*) FROM atom_search.file_embeddings').fetchone()[0] == 0
            assert connection.execute('SELECT count(*) FROM atom_search.file_project_context').fetchone() == (0,)
    finally:
        service.close()
        record['clean_stop'] = service.runtime.status().state == 'stopped'


def test_default_peer_loads_canonical_host_domain_and_checks_ready_reservations(tmp_path, native_receipt):
    engine = _windows.private_directory(tmp_path / 'engine')
    source = tmp_path / 'catalog.sqlite3'
    source.write_bytes(b'legacy')
    _windows.atomic_json(engine / 'settings.json', {'storage': {
        'quota_bytes': 400 * 1024**2, 'min_free_bytes': 0}})
    files = (source, Path(str(source) + '-wal'), Path(str(source) + '-shm'))
    owner = PostgresRuntime(RUNTIME, engine, accounting_roots=(engine,), read_only_files=files)
    peer = PostgresRuntime(RUNTIME, engine)
    try:
        endpoint = owner.start()
        record = {'engine': str(engine), 'installation_id': endpoint.installation_id, 'clean_stop': False}
        native_receipt.append(record)
        assert peer.start().installation_id == endpoint.installation_id
        assert peer.storage_budget.roots == (engine,)
        assert peer.storage_budget.read_only_files == tuple(sorted(files, key=str))
        assert peer.storage_budget.ledger == engine / 'postgresql/storage-reservations.json'
        used = _windows.allocated_bytes((engine,), read_only_files=files)
        with owner.storage_budget.reserve(400 * 1024**2 - used - 128 * 1024, 'peer-test'):
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                peer.storage_budget.reserve(256 * 1024, 'competing')
        source.unlink()
        assert peer.start().installation_id == endpoint.installation_id
    finally:
        peer.stop()
        owner.stop()
        if native_receipt:
            native_receipt[-1]['clean_stop'] = owner.status().state == 'stopped'


def test_host_interrupted_import_resumes_retained_none_policy_then_activation_rollback(tmp_path, monkeypatch, native_receipt):
    from tests.integration.test_pg_import import legacy
    import atom_search.search_runtime as host
    profile = tmp_path / 'profile'
    profile.mkdir()
    source = legacy(profile / 'catalog.sqlite3', 1001)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    marker = tmp_path / 'committed.json'
    child_code = '''import sys,json,pathlib,os
import atom_search.search_runtime as h
p=json.loads(sys.stdin.readline()); original=h.stage_legacy_catalog
def stage(snapshot,connect,**kw):
    admit=kw['reserve_bytes']
    def reserve(value):
        admit(value)
        with connect() as c:
            row=c.execute('SELECT last_file_id,files_imported,vectors_reused FROM atom_search.legacy_imports').fetchone()
            active=c.execute("SELECT id FROM atom_search.catalogs WHERE state='active'").fetchone()[0]
        if row and row[1]==1000:
            pathlib.Path(p['marker']).write_text(json.dumps({'pid':os.getpid(),'checkpoint':row,'active':active}))
            sys.stdin.read(1)
    kw['reserve_bytes']=reserve
    return original(snapshot,connect,**kw)
h.stage_legacy_catalog=stage
h.open_search_service(pathlib.Path(p['profile']),pathlib.Path(p['missing']))
'''
    root = RUNTIME.parents[1]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(str(root / p) for p in
        ('src', 'packages/atom-search-core/src', '.venv/Lib/site-packages')))
    child = subprocess.Popen([sys._base_executable, '-c', child_code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        child.stdin.write((json.dumps(dict(profile=str(profile), marker=str(marker), missing=str(tmp_path/'missing'))) + '\n').encode())
        child.stdin.flush()
        deadline = time.monotonic() + 120
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert marker.exists(), 'host import did not reach its committed checkpoint'
        checkpoint = json.loads(marker.read_text())
        assert checkpoint == {'pid': child.pid, 'checkpoint': [1041, 1000, 0], 'active': 1}
        child.terminate()
        child.wait(timeout=20)
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=20)
        child.stdin.close(); child.stdout.close(); child.stderr.close()
        cleanup_host_child(profile)
    state_path = profile / 'engine/lifecycle.json'
    state = json.loads(state_path.read_text())
    assert state['target_manifest'] is None and not state['used']
    snapshot = profile / 'engine/legacy-import/snapshot.sqlite3'
    snapshot_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    # Fail inside the activation SQL transaction, after the old pointer changes.
    actual_activate = host.activate_catalog
    def rollback(catalog_id, connect):
        def wrapped():
            c = connect()
            c.execute('''CREATE OR REPLACE FUNCTION atom_search.reject_activation() RETURNS trigger
                LANGUAGE plpgsql AS $$BEGIN IF NEW.state='active' AND OLD.state='staging'
                THEN RAISE EXCEPTION 'injected_activation_rollback'; END IF; RETURN NEW; END$$''')
            c.execute('CREATE TRIGGER reject_activation BEFORE UPDATE ON atom_search.catalogs FOR EACH ROW EXECUTE FUNCTION atom_search.reject_activation()')
            c.commit()
            return c
        actual_activate(catalog_id, wrapped)
    monkeypatch.setattr(host, 'activate_catalog', rollback)
    with pytest.raises(Exception, match='injected_activation_rollback'):
        host.open_search_service(profile, root / 'models')
    # A newly available model cannot replace the initial None migration policy.
    runtime = PostgresRuntime(RUNTIME, profile / 'engine')
    try:
        endpoint = runtime.start()
        with runtime.open_connection(role='maintenance') as c:
            assert c.execute("SELECT id FROM atom_search.catalogs WHERE state='active'").fetchone() == (1,)
            assert c.execute('SELECT target_model_key,files_imported FROM atom_search.legacy_imports').fetchone() == (None, 1001)
            assert c.execute('SELECT count(*) FROM atom_search.embedding_models').fetchone() == (0,)
            c.execute('DROP TRIGGER reject_activation ON atom_search.catalogs')
            c.execute('DROP FUNCTION atom_search.reject_activation()')
    finally:
        runtime.stop()
    monkeypatch.setattr(host, 'activate_catalog', actual_activate)
    service = host.open_search_service(profile, root / 'models')
    record = remember(service, native_receipt)
    try:
        assert service.status()['files'] == 1001
        assert json.loads(state_path.read_text())['target_manifest'] is None
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == snapshot_hash
        assert json.loads(service.runtime.storage_budget.ledger.read_text()) == []
        with service.runtime.open_connection(role='maintenance') as c:
            assert c.execute('SELECT vectors_reused,files_imported-vectors_reused FROM atom_search.legacy_imports').fetchone() == (0, 1001)
        record.update(checkpoint=checkpoint, source_sha256=before, snapshot_sha256=snapshot_hash,
                      activation_rollback=True, retained_target=None, resumed_files=1001)
    finally:
        service.close()
        record['clean_stop'] = service.runtime.status().state == 'stopped'

def unicode_model(tmp_path):
    from tests.integration.test_pg_import import MANIFEST
    manifest = dict(MANIFEST, description='Локальная модель — 日本語')
    directory = tmp_path / 'unicode-model'
    directory.mkdir()
    for item in manifest['files']:
        (directory / item['path']).hardlink_to(RUNTIME.parents[1] / 'models' / item['path'])
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return directory, manifest


def test_unicode_manifest_import_matches_ordinary_model_registration(tmp_path, native_receipt):
    from tests.integration.test_pg_import import legacy
    from atom_search.search_runtime import open_search_service
    directory, manifest = unicode_model(tmp_path)
    profile = tmp_path / 'profile'; profile.mkdir()
    legacy(profile / 'catalog.sqlite3')
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()
    service = open_search_service(profile, directory)
    try:
        assert service.status()['files'] == 1
        assert service.index_embeddings()['indexed'] == 1
        with service.runtime.open_connection(role='maintenance') as c:
            assert c.execute('SELECT target_manifest_digest FROM atom_search.legacy_imports').fetchone() == (expected,)
            assert c.execute('SELECT manifest_digest FROM atom_search.embedding_models').fetchone() == (expected,)
    finally:
        service.close()


def test_pending_dict_import_refuses_missing_or_changed_assets_but_used_catalog_is_offline(tmp_path, monkeypatch, native_receipt):
    from tests.integration.test_pg_import import legacy
    import atom_search.search_runtime as host
    directory, manifest = unicode_model(tmp_path)
    profile = tmp_path / 'profile'; profile.mkdir()
    source = legacy(profile / 'catalog.sqlite3')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original = host.stage_legacy_catalog
    def interrupt(*args, **kwargs):
        raise RuntimeError('injected_before_staging')
    monkeypatch.setattr(host, 'stage_legacy_catalog', interrupt)
    with pytest.raises(RuntimeError, match='injected_before_staging'):
        host.open_search_service(profile, directory)
    monkeypatch.setattr(host, 'stage_legacy_catalog', original)
    state_path = profile / 'engine/lifecycle.json'
    snapshot = profile / 'engine/legacy-import/snapshot.sqlite3'
    state_before, snapshot_before = state_path.read_bytes(), snapshot.read_bytes()
    for changed in (tmp_path / 'missing', RUNTIME.parents[1] / 'models'):
        opened = None
        try:
            with pytest.raises(PostgresRuntimeError, match='legacy_target_assets_unverified'):
                opened = host.open_search_service(profile, changed)
        finally:
            if opened is not None:
                opened.close()
        assert state_path.read_bytes() == state_before
        assert snapshot.read_bytes() == snapshot_before
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    runtime = PostgresRuntime(RUNTIME, profile / 'engine')
    try:
        runtime.start()
        with runtime.open_connection(role='maintenance') as c:
            assert c.execute("SELECT id FROM atom_search.catalogs WHERE state='active'").fetchone() == (1,)
            assert c.execute('SELECT count(*) FROM atom_search.legacy_imports').fetchone() == (0,)
    finally:
        runtime.stop()
    service = host.open_search_service(profile, directory)
    try:
        assert service.status()['files'] == 1
    finally:
        service.close()
    service = host.open_search_service(profile, tmp_path / 'missing')
    try:
        assert not service.status()['semantic_ready']
        assert service.search('Ёж')
    finally:
        service.close()

def test_admitted_sql_rollback_concurrency_late_quota_cancel_and_descendant_delete(tmp_path, monkeypatch, native_receipt):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from atom_search.search_runtime import open_search_service
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    profile = tmp_path / 'profile'
    source = tmp_path / 'source'; source.mkdir()
    child = source / 'child'; child.mkdir()
    (source / 'retained.pdf').touch(); (child / 'descendant.pdf').touch()
    service = open_search_service(profile, RUNTIME.parents[1] / 'models')
    peer = None
    record = remember(service, native_receipt)
    try:
        service.scan(str(source)); service.index_embeddings()
        peer = open_search_service(profile, RUNTIME.parents[1] / 'models')
        budget = service.runtime.storage_budget
        root_id = service.catalog.resolve_scope(str(source)).source_ids[0]
        def db_state():
            with service.runtime.open_connection() as c:
                return (c.execute('SELECT id,relative_path,size,revision FROM atom_search.files ORDER BY id').fetchall(),
                        c.execute('SELECT file_id,file_revision,embedding::text FROM atom_search.file_embeddings ORDER BY file_id').fetchall())
        before = db_state()
        with service.runtime.open_connection(role='maintenance') as c:
            c.execute("""CREATE FUNCTION atom_search.reject_test_write() RETURNS trigger LANGUAGE plpgsql AS
                $$BEGIN RAISE EXCEPTION 'injected_admitted_sql_failure'; END$$""")
            c.execute('CREATE TRIGGER reject_test_write BEFORE INSERT OR UPDATE ON atom_search.files FOR EACH ROW EXECUTE FUNCTION atom_search.reject_test_write()')
        try:
            with pytest.raises(Exception, match='injected_admitted_sql_failure'):
                service.catalog.reconcile(root_id, DirectorySnapshot('', (FileMetadata('rollback.pdf','rollback.pdf',1,1),), ('child',)))
            assert db_state() == before
            assert json.loads(budget.ledger.read_text()) == []
            assert service.status()['storage_blocked']
        finally:
            with service.runtime.open_connection(role='maintenance') as c:
                c.execute('DROP TRIGGER reject_test_write ON atom_search.files')
        # Real embedding transaction refusal retains the already published descendant vector.
        (source / 'retained.pdf').write_bytes(b'new revision')
        service.scan(str(source))
        embedding_before = db_state()
        assert len(embedding_before[1]) == 1
        with service.runtime.open_connection(role='maintenance') as c:
            c.execute('CREATE TRIGGER reject_test_write BEFORE INSERT OR UPDATE ON atom_search.file_embeddings FOR EACH ROW EXECUTE FUNCTION atom_search.reject_test_write()')
        try:
            with pytest.raises(Exception, match='injected_admitted_sql_failure'):
                service.index_embeddings()
            assert db_state() == embedding_before
            assert json.loads(budget.ledger.read_text()) == []
        finally:
            with service.runtime.open_connection(role='maintenance') as c:
                c.execute('DROP TRIGGER reject_test_write ON atom_search.file_embeddings')
                c.execute('DROP FUNCTION atom_search.reject_test_write()')
        assert service.index_embeddings()['indexed'] == 1
        # One admitted write pauses at the adapter boundary while another actual host waits.
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        original = service.catalog._catalog.reconcile
        snapshots = list(service.source.snapshots(str(source), threading.Event()))
        root_snapshot, child_snapshot = snapshots
        def blocked(*args):
            entered.set(); assert release.wait(30)
            return original(*args)
        monkeypatch.setattr(service.catalog._catalog, 'reconcile', blocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.catalog.reconcile, root_id, root_snapshot)
            try:
                assert entered.wait(30)
                def competing():
                    second_started.set()
                    return peer.catalog.reconcile(root_id, child_snapshot)
                second = pool.submit(competing)
                assert second_started.wait(5)
                assert not second.done()
                assert len(json.loads(budget.ledger.read_text())) == 1
                used = _windows.allocated_bytes(budget.roots, read_only_files=budget.read_only_files)
                with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                    peer.runtime.storage_budget.reserve(budget.quota_bytes-used-1024*1024, 'competing_host')
            finally:
                release.set()
            first.result(40); second.result(40)
        monkeypatch.setattr(service.catalog._catalog, 'reconcile', original)
        assert json.loads(budget.ledger.read_text()) == []
        # First directory commits; actual quota refuses the later descendant directory.
        real_source = service.source
        first_snapshot = DirectorySnapshot('', (FileMetadata('committed.pdf','committed.pdf',2,2),), ('child',))
        second_snapshot = DirectorySnapshot('child', (FileMetadata('child/refused.pdf','refused.pdf',2,2),))
        held = []
        class LateQuotaSource:
            def snapshots(self, root, cancel=None, *, excluded_roots=()):
                yield first_snapshot
                used = _windows.allocated_bytes(budget.roots, read_only_files=budget.read_only_files)
                held.append(budget.reserve(budget.quota_bytes-used-8*1024**2,'late_competitor'))
                yield second_snapshot
        service.source = LateQuotaSource()
        try:
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                service.scan(str(source))
        finally:
            for reservation in held:
                reservation.__exit__(None,None,None)
            service.source = real_source
        assert any(hit.relative_path == 'committed.pdf' for hit in service.search('committed'))
        assert any(hit.relative_path == 'child/descendant.pdf' for hit in service.search('descendant'))
        with service.runtime.open_connection() as c:
            assert c.execute('SELECT relative_path FROM atom_search.files ORDER BY relative_path').fetchall() == [
                ('child/descendant.pdf',), ('committed.pdf',)]
        assert not service.status()['metadata_ready']
        assert json.loads(budget.ledger.read_text()) == []
        cancel = threading.Event()
        report = service.scan(str(source), cancel=cancel, progress=lambda _: cancel.set())
        assert report['status'] == 'cancelled'
        assert json.loads(budget.ledger.read_text()) == []
        service.scan(str(source)); service.index_embeddings()
        before = db_state(); assert len(before[0]) == len(before[1]) == 2
        admitted = []
        reserve = budget.reserve
        def observation(value, operation):
            if operation == 'reconcile': admitted.append(value)
            return reserve(value, operation)
        monkeypatch.setattr(budget, 'reserve', observation)
        assert service.catalog.reconcile(root_id, DirectorySnapshot('')).removed == 2
        assert admitted[0] >= 32*1024**2 + 32*(2*8192+2*1536)
        assert db_state() == ([], [])
        assert json.loads(budget.ledger.read_text()) == []
        record.update(metadata_sql_rollback=True, embedding_sql_rollback=True,
            concurrent_host_admission=True, earlier_directory_retained=True,
            cancellation_released=True, descendant_vectors_deleted=2, deletion_admission=admitted[0])
    finally:
        if peer is not None: peer.close()
        service.close()
        record['clean_stop'] = service.runtime.status().state == 'stopped'


@pytest.mark.parametrize('history', ['used_then_empty','source_only','attempted_scan',
                                     'schema_upgrade','schema_upgrade_v4'])
def test_existing_catalog_history_prevents_legacy_reimport(tmp_path, monkeypatch, native_receipt, history):
    from tests.integration.test_pg_import import legacy
    import atom_search.search_runtime as host
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    profile = tmp_path / 'profile'; profile.mkdir()
    legacy(profile / 'catalog.sqlite3')
    original_import = host._Owner.import_legacy
    monkeypatch.setattr(host._Owner, 'import_legacy', lambda *args, **kwargs: None)
    service = host.open_search_service(profile, tmp_path / 'missing')
    try:
        if history == 'attempted_scan':
            def failed(_): raise RuntimeError('injected_begin_failure')
            monkeypatch.setattr(service.catalog._catalog, 'begin_root', failed)
            with pytest.raises(RuntimeError, match='injected_begin_failure'):
                service.scan(str(tmp_path / 'unavailable'))
        else:
            root_id = service.catalog._catalog.begin_root('Z:/preexisting')
            if history == 'used_then_empty':
                service.catalog._catalog.reconcile(root_id, DirectorySnapshot('', (FileMetadata('used.pdf','used.pdf',0,1),)))
                with service.runtime.open_connection(role='maintenance') as c:
                    c.execute('DELETE FROM atom_search.sources')
        if history in ('schema_upgrade', 'schema_upgrade_v4'):
            with service.runtime.open_connection(role='maintenance') as c:
                # A genuine prior schema has none of the later objects. Keep
                # the original v2 history proof and the accepted v4 boundary;
                # removing version rows alone leaves an impossible catalog.
                c.execute('''DROP TABLE atom_search.file_project_context,
                    atom_search.project_bindings, atom_search.projects,
                    atom_search.project_context_state''')
                prior_version = 4 if history == 'schema_upgrade_v4' else 2
                if prior_version == 2:
                    c.execute('DROP TABLE atom_search.legacy_file_map, atom_search.legacy_source_map, atom_search.legacy_imports')
                    c.execute('DROP TABLE atom_search.file_public_ids, atom_search.source_public_ids')
                c.execute('DELETE FROM atom_search.schema_migrations WHERE version>%s', (prior_version,))
                assert c.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone() == (prior_version,)
                assert c.execute("SELECT to_regclass('atom_search.project_context_state')").fetchone() == (None,)
        with service.runtime.open_connection() as c:
            assert c.execute('SELECT count(*) FROM atom_search.files').fetchone() == (0,)
    finally:
        service.close()
    monkeypatch.setattr(host._Owner, 'import_legacy', original_import)
    service = host.open_search_service(profile, tmp_path / 'missing')
    try:
        assert service.status()['files'] == 0
        with service.runtime.open_connection(role='maintenance') as c:
            assert c.execute('SELECT count(*) FROM atom_search.legacy_imports').fetchone() == (0,)
            assert c.execute('SELECT max(version) FROM atom_search.schema_migrations').fetchone() == (5,)
        assert json.loads((profile / 'engine/lifecycle.json').read_text())['used'] is True
        assert not (profile / 'engine/legacy-import/snapshot.sqlite3').exists()
    finally:
        service.close()


def test_concurrent_initialization_activation_serializes_existing_host_write(tmp_path, monkeypatch, native_receipt):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tests.integration.test_pg_import import legacy
    import atom_search.search_runtime as host
    profile=tmp_path/'profile'; profile.mkdir(); legacy(profile/'catalog.sqlite3')
    source=tmp_path/'source'; source.mkdir(); (source/'ordinary.pdf').touch()
    original_import=host._Owner.import_legacy
    monkeypatch.setattr(host._Owner,'import_legacy',lambda *args,**kwargs:None)
    service=host.open_search_service(profile,tmp_path/'missing')
    monkeypatch.setattr(host._Owner,'import_legacy',original_import)
    entered,release,write_started=threading.Event(),threading.Event(),threading.Event()
    original_stage=host.stage_legacy_catalog
    def stage(*args,**kwargs):
        entered.set(); assert release.wait(40)
        return original_stage(*args,**kwargs)
    monkeypatch.setattr(host,'stage_legacy_catalog',stage)
    peers=[]
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending=pool.submit(host.open_search_service,profile,tmp_path/'missing')
            try:
                assert entered.wait(120)
                another=pool.submit(host.open_search_service,profile,tmp_path/'missing')
                def write():
                    write_started.set(); return service.scan(str(source))
                writer=pool.submit(write)
                assert write_started.wait(5)
                assert not writer.done() and not another.done()
            finally: release.set()
            peers.extend([pending.result(120),another.result(120)])
            assert writer.result(120)['status']=='complete'
        assert service.search('Ёж') and service.search('ordinary')
        with service.runtime.open_connection(role='maintenance') as c:
            assert c.execute('SELECT count(*) FROM atom_search.legacy_imports').fetchone()==(1,)
            assert c.execute("SELECT count(*) FROM atom_search.catalogs WHERE state='active'").fetchone()==(1,)
        assert json.loads(service.runtime.storage_budget.ledger.read_text())==[]
    finally:
        for peer in peers: peer.close()
        service.close()

@pytest.mark.parametrize('phase', ['factory', 'scan', 'embedding'])
def test_real_qt_event_loop_quit_drains_host_once(tmp_path, native_receipt, phase):
    root=RUNTIME.parents[1]
    source=tmp_path/'source'; source.mkdir(); (source/'retained.pdf').touch()
    done=tmp_path/'qt-close.json'
    script=r'''import sys,json,pathlib,threading,os,traceback
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import atom_search.search_runtime as host
from atom_search.desktop.window import SearchWindow
from atom_search.desktop.workers import Workers
from atom_search.desktop.app import main
p=json.loads(sys.stdin.readline()); entered=threading.Event(); release=threading.Event(); state={'close_count':0}
worker_init=Workers.__init__
def observed_worker(workers,factory,parent):
    def observed_factory():
        try: return factory()
        except Exception as error:
            code=getattr(error,'code',None)
            state.setdefault('factory_errors',[]).append({'type':type(error).__name__,'code':code,
                'frames':[(pathlib.Path(frame.filename).name,frame.name,frame.lineno)
                          for frame in traceback.extract_tb(error.__traceback__)]})
            raise
    worker_init(workers,observed_factory,parent)
Workers.__init__=observed_worker
original=host.open_search_service
def factory(*args,**kwargs):
    service=original(*args,**kwargs); state['service']=service
    state['installation_id']=service.runtime.status().endpoint.installation_id
    close=service.close
    def closing():
        state['close_count']+=1; close()
    service.close=closing
    if p['phase']=='factory':
        entered.set(); assert release.wait(180)
    elif p['phase']=='scan':
        snapshots=service.source.snapshots
        def slow(root,cancel=None,*,excluded_roots=()):
            entered.set(); assert cancel.wait(180)
            return snapshots(root,cancel,excluded_roots=excluded_roots)
        service.source.snapshots=slow
    elif p['phase']=='embedding':
        index=service.index_embedding_batch
        def blocked(*,scope=None,cancel=None,publish_if=None):
            encode=service._embedder.encode
            def slow(values,**kwargs):
                entered.set(); assert cancel.wait(180)
                return encode(values,**kwargs)
            service._embedder.encode=slow
            try: return index(scope=scope,cancel=cancel,publish_if=publish_if)
            finally: service._embedder.encode=encode
        service.index_embedding_batch=blocked
    return service
host.open_search_service=factory
init=SearchWindow.__init__
def initialize(window,*args,**kwargs):
    init(window,*args,**kwargs)
    timer=QTimer(window)
    def check():
        if entered.is_set():
            timer.stop(); window.close(); release.set(); QApplication.instance().quit()
    timer.timeout.connect(check); timer.start(25)
    QTimer.singleShot(210000,lambda:(window.close(),release.set(),QApplication.instance().quit()))
SearchWindow.__init__=initialize
result=main(['--data-dir',p['profile'],'--model-dir',p['model'],'--root',p['source']])
service=state['service']; runtime=service.runtime
pathlib.Path(p['done']).write_text(json.dumps({'engine':str(runtime.profile_dir),
 'installation_id':state['installation_id'],'phase_reached':entered.is_set(),'exit_code':result,
 'clean_stop':runtime.status().state=='stopped','owned_process_absent':runtime._claimed_process() is None,
 'close_count':state['close_count'],'ledger_empty':json.loads(runtime.storage_budget.ledger.read_text())==[],
 'factory_errors':state.get('factory_errors',[])}))
'''
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONPATH=os.pathsep.join(str(root/p) for p in
        ('src','packages/atom-search-core/src','.venv/Lib/site-packages')))
    try:
        result=subprocess.run([sys._base_executable,'-c',script],input=(json.dumps(dict(profile=str(tmp_path/'profile'),
            model=str(root/'models'),source=str(source),done=str(done),phase=phase))+'\n').encode(),
            capture_output=True,timeout=260,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        cleanup_host_child(tmp_path/'profile')
    assert result.returncode==0, result.stderr.decode(errors='replace')[-2000:]
    record=json.loads(done.read_text()); native_receipt.append(record)
    assert record['phase_reached'] and record['exit_code']==0, record
    assert record['clean_stop'] and record['owned_process_absent'] and record['ledger_empty']
    assert record['close_count']==1


def test_guarded_import_refuses_unreadable_source_preserving_acl_pointer_and_reservations(tmp_path, native_receipt):
    from tests.integration.test_pg_import import legacy
    from atom_search.search_runtime import open_search_service
    profile=tmp_path/'profile'; profile.mkdir()
    source=legacy(profile/'catalog.sqlite3')
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    env=dict(os.environ,ATOM_TEST_LEAF=str(source))
    def ps(command):
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            "$ErrorActionPreference='Stop'; "+command],capture_output=True,env=env,timeout=20)
        assert result.returncode==0, 'owned ACL fixture failed'
        return result.stdout.decode().strip()
    original_acl=ps('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl')
    env['ATOM_TEST_ACL']=original_acl
    ps("$f=[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF); $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; $f.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadData','Deny')); [System.IO.File]::SetAccessControl($env:ATOM_TEST_LEAF,$f)")
    denied_acl=ps('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl')
    opened=None
    try:
        with pytest.raises(PermissionError): source.read_bytes()
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            opened=open_search_service(profile,tmp_path/'missing')
        assert ps('[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl')==denied_acl
        assert not (profile/'engine/legacy-import/snapshot.sqlite3').exists()
        assert json.loads((profile/'engine/postgresql/storage-reservations.json').read_text())==[]
        assert not json.loads((profile/'engine/lifecycle.json').read_text())['used']
    finally:
        if opened is not None: opened.close()
        ps("$f=[System.Security.AccessControl.FileSecurity]::new(); $f.SetSecurityDescriptorSddlForm($env:ATOM_TEST_ACL,[System.Security.AccessControl.AccessControlSections]::Access); [System.IO.File]::SetAccessControl($env:ATOM_TEST_LEAF,$f)")
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    runtime=PostgresRuntime(RUNTIME,profile/'engine')
    try:
        runtime.start()
        with runtime.open_connection(role='maintenance') as c:
            assert c.execute("SELECT id FROM atom_search.catalogs WHERE state='active'").fetchone()==(1,)
            assert c.execute('SELECT count(*) FROM atom_search.legacy_imports').fetchone()==(0,)
            assert c.execute('SELECT count(*) FROM atom_search.files').fetchone()==(0,)
    finally: runtime.stop()


def test_empty_directory_tree_is_reserved_before_descendant_deletion(tmp_path, native_receipt):
    from atom_search.search_runtime import open_search_service
    from atom_search_core.models import DirectorySnapshot
    service = open_search_service(tmp_path/'profile', tmp_path/'missing')
    try:
        root_id = service.catalog.begin_root(str(tmp_path/'empty-tree'))
        names = tuple(f'branch-{i:04d}-' + 'x'*160 for i in range(1024))
        service.catalog.reconcile(root_id, DirectorySnapshot('', directories=names))
        budget = service.runtime.storage_budget
        with service.runtime.open_connection() as c:
            assert c.execute('SELECT count(*) FROM atom_search.directories').fetchone() == (1025,)
            assert c.execute('SELECT count(*) FROM atom_search.files').fetchone() == (0,)
        used = _windows.allocated_bytes(budget.roots, read_only_files=budget.read_only_files)
        with budget.reserve(budget.quota_bytes-used-64*1024**2, 'empty_tree_competitor'):
            with pytest.raises(StorageBudgetError, match='quota_exceeded'):
                service.catalog.reconcile(root_id, DirectorySnapshot(''))
            with service.runtime.open_connection() as c:
                assert c.execute('SELECT count(*) FROM atom_search.directories').fetchone() == (1025,)
        assert json.loads(budget.ledger.read_text()) == []
        service.catalog.reconcile(root_id, DirectorySnapshot(''))
        with service.runtime.open_connection() as c:
            assert c.execute('SELECT count(*) FROM atom_search.directories').fetchone() == (1,)
    finally:
        service.close()


def test_ordinary_kaizen_settings_reach_first_window_and_scoped_catalog(tmp_path, monkeypatch, native_receipt):
    import threading
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from atom_search_core import SearchService
    from atom_search.desktop.data import initialize_data_directory
    from atom_search.desktop.window import SearchWindow
    from atom_search.desktop.workers import Workers
    from atom_search.search_runtime import open_search_service
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    legacy=tmp_path/'KaizenSearch'; legacy.mkdir()
    documents=tmp_path/'remembered-documents'; documents.mkdir(); (documents/'scoped-drawing.pdf').touch()
    old=SearchService(legacy/'catalog.sqlite3'); old.scan(str(documents)); old.close()
    (legacy/'settings.json').write_text(json.dumps({'root':str(documents),'refresh_days':9}),encoding='utf-8')
    before={p.name:p.read_bytes() for p in legacy.iterdir()}
    original_paths = [legacy/'catalog.sqlite3', legacy/'settings.json', documents/'scoped-drawing.pdf']
    def acl(path):
        return subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            '[System.IO.File]::GetAccessControl($env:ATOM_TEST_LEAF).Sddl'],
            env=dict(os.environ, ATOM_TEST_LEAF=str(path)), text=True, timeout=15).strip()
    acls = {path: acl(path) for path in original_paths}
    document_bytes = (documents/'scoped-drawing.pdf').read_bytes()
    profile=initialize_data_directory(model_dir=tmp_path/'missing')
    service=open_search_service(profile,tmp_path/'missing',discover_legacy=True)
    window=None
    factory_errors=[]
    factory_completed=threading.Event()
    factory_timing={}
    worker_init=Workers.__init__
    def observed_worker(workers,factory,parent):
        def observed_factory():
            began=time.monotonic()
            try: return factory()
            except Exception as error:
                factory_errors.append({'type':type(error).__name__, 'code':getattr(error,'code',None)})
                raise
            finally:
                factory_timing['elapsed_seconds']=round(time.monotonic()-began,3)
                factory_completed.set()
        worker_init(workers,observed_factory,parent)
    monkeypatch.setattr(Workers,'__init__',observed_worker)
    try:
        app=QApplication.instance() or QApplication([])
        window=SearchWindow(data_dir=profile,service_factory=lambda:service,auto_scan=False)
        assert window.root==str(documents) and window.refresh_days==9
        window.show(); window.search_box.setText('scoped-drawing')
        # The first query is queued during real registry/budget warmup. A
        # pre-opened P1 service does not mean the window's factory is ready.
        # Keep native startup separate from the unchanged query-response bound.
        # Explicitly yield Python: QTest.qWait can starve ctypes-heavy admission.
        startup_began=time.monotonic()
        deadline=startup_began+120
        while not factory_completed.is_set() and time.monotonic()<deadline:
            app.processEvents(); time.sleep(.01)
        startup_elapsed=round(time.monotonic()-startup_began,3)
        native_receipt.append({'phase':'window_registry_ready','factory_timing':factory_timing,
                               'startup_elapsed_seconds':startup_elapsed})
        assert factory_completed.is_set() and not factory_errors and window.source_registry is not None, {
            'factory_completed':factory_completed.is_set(),'factory_errors':factory_errors,
            'ui_error':window.message.text(),'startup_elapsed_seconds':startup_elapsed,
            'factory_timing':factory_timing}
        deadline=time.monotonic()+10
        while not window.results.count() and time.monotonic()<deadline:
            app.processEvents(); QTest.qWait(10)
        assert window.results.count()==1, {'ui_error':window.message.text()}
        assert service.search('scoped-drawing',root=window.root)[0].name=='scoped-drawing.pdf'
        assert {name:(legacy/name).read_bytes() for name in before}==before
        assert (documents/'scoped-drawing.pdf').read_bytes()==document_bytes
        assert {path:acl(path) for path in original_paths}==acls
        source=legacy/'catalog.sqlite3'
        leaves=(source,Path(str(source)+'-wal'),Path(str(source)+'-shm'))
        assert set(service.runtime.storage_budget.read_only_files)==set(leaves)
        assert {p.name for p in legacy.iterdir()} <= set(before)|{p.name for p in leaves}
        for leaf in leaves:
            _windows.assert_read_only_file(leaf)
        assert _windows.allocated_bytes((),read_only_files=leaves)>0
    finally:
        try:
            if window is not None:
                window.close(); assert window.workers.wait()
        finally:
            # Factory acquisition may not yet have happened on an early failure.
            # SearchService.close is idempotent and invokes Runtime.stop once.
            service.close()


def test_verified_assets_retain_dict_policy_when_inference_loader_fails(tmp_path, monkeypatch, native_receipt):
    from tests.integration.test_pg_import import legacy, MANIFEST
    from atom_search_core import embeddings
    from atom_search.search_runtime import open_search_service, _prepare_profile
    profile=tmp_path/'profile'; profile.mkdir(); legacy(profile/'catalog.sqlite3')
    def unavailable(_): raise RuntimeError('injected_local_inference_failure')
    monkeypatch.setattr(embeddings,'OnnxEmbedder',unavailable)
    with _prepare_profile(profile,RUNTIME.parents[1]/'models'): pass
    assert json.loads((profile/'engine/lifecycle.json').read_text())['target_manifest']==MANIFEST
    service=open_search_service(profile,RUNTIME.parents[1]/'models')
    try:
        assert not service.status()['semantic_ready']
        assert service.search('Ёж')[0].name.startswith('Ёж')
        assert json.loads((profile/'engine/lifecycle.json').read_text())['target_manifest']==MANIFEST
        with service.runtime.open_connection(role='maintenance') as c:
            assert c.execute('SELECT count(*) FROM atom_search.legacy_imports WHERE target_manifest_digest IS NOT NULL').fetchone()==(1,)
    finally: service.close()


def test_native_prior_index_fingerprint_and_missing_vector_refusal(tmp_path, native_receipt):
    from atom_search.diagnostics import run_self_test
    from atom_search.search_runtime import open_search_service
    qa=tmp_path/'qa'; model=RUNTIME.parents[1]/'models'
    reports=[]
    for number in (1,2):
        destination=tmp_path/f'self-{number}.json'
        assert run_self_test(destination,data_dir=qa,model_dir=model)==0
        reports.append(json.loads(destination.read_text(encoding='utf-8')))
    assert not reports[0]['prior_index_verified']
    assert reports[1]['prior_index_verified']
    assert reports[1]['prior_index_fingerprint']==reports[0]['index_fingerprint']==reports[1]['index_fingerprint']
    profile=qa/'application-self-test/profile'
    service=open_search_service(profile,model)
    try:
        with service.runtime.open_connection(role='maintenance') as c:
            c.execute('DELETE FROM atom_search.file_embeddings WHERE file_id=(SELECT max(file_id) FROM atom_search.file_embeddings)')
    finally: service.close()
    destination=tmp_path/'missing-vector.json'
    assert run_self_test(destination,data_dir=qa,model_dir=model)==1
    report=json.loads(destination.read_text(encoding='utf-8'))
    assert not report['prior_index_verified'] and report['prior_vector_count']==2 and report['clean_stop']
    assert 'scan_status' not in report and 'index_fingerprint' not in report
    runtime=PostgresRuntime(RUNTIME,profile/'engine')
    try:
        runtime.start()
        with runtime.open_connection() as c:
            assert c.execute('SELECT count(*) FROM atom_search.file_embeddings').fetchone()==(2,)
    finally: runtime.stop()
