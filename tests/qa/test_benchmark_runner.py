"""Tiny explicit synthetic inputs; real ServiceOwner/PG, no ambient corpus/DSN."""
import importlib.util
import json
from pathlib import Path
import threading
from contextlib import contextmanager

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('benchmark_runner', ROOT / 'tools/qa/benchmark_search.py')
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.fixture
def inputs(tmp_path):
    corpus = tmp_path / 'synthetic-input'
    (corpus / 'folder').mkdir(parents=True)
    (corpus / 'folder/needle.pdf').write_text('PRIVATE DOCUMENT CONTENT', encoding='utf-8')
    (corpus / 'other.pdf').write_text('PRIVATE DOCUMENT CONTENT', encoding='utf-8')
    judgments = tmp_path / 'judgments.json'
    judgments.write_text(json.dumps({'schema_version': 1,
        'judgments': {'needle': ['folder/needle.pdf']}}), encoding='utf-8')
    return corpus, judgments, tmp_path / 'owned-profile'


def run(inputs, **kwargs):
    corpus, judgments, profile = inputs
    return RUNNER.run_benchmark(corpus, judgments, profile=profile,
                                queries=('needle',), repetitions=2, **kwargs)


def capture_real_owners(monkeypatch):
    import atom_search.service.owner as module
    original = module.open_service_owner
    owners = []
    def opened(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner)
        return owner
    monkeypatch.setattr(module, 'open_service_owner', opened)
    return owners


def test_actual_metadata_pg_quality_latency_and_owned_cleanup(inputs, monkeypatch):
    owners = capture_real_owners(monkeypatch)
    result = run(inputs)
    assert result['status'] == result['coverage'] == 'complete'
    assert result['total'] == 2 and result['measurements'] == 2
    assert result['quality']['hit_at_k'] == result['quality']['recall_at_k'] == 1.0
    assert result['latency_ms']['p50'] >= 0 and result['latency_ms']['p95'] >= result['latency_ms']['p50']
    assert len(owners) == 1 and not owners[0].attached
    assert owners[0].runtime.status().state == 'stopped'
    assert not inputs[2].exists()
    serialized = json.dumps(result)
    for forbidden in ('needle', 'folder', 'pdf', str(inputs[0]), str(inputs[1]),
                      str(inputs[2]), 'PRIVATE DOCUMENT', owners[0].generation):
        assert forbidden not in serialized


def test_empty_judgments_are_unknown_on_real_pg(inputs):
    inputs[1].write_text(json.dumps({'schema_version': 1, 'judgments': {}}), encoding='utf-8')
    result = run(inputs)
    assert result['status'] == 'complete'
    assert all(value is None for value in result['quality'].values())
    assert not inputs[2].exists()


def test_cancelled_scan_is_partial_and_prior_sql_records_survive(inputs, monkeypatch):
    from atom_search.service.contracts import SearchRequest
    from atom_search_core.source import FileSystemSource
    original_scan = RUNNER._scan_metadata
    original_snapshots = FileSystemSource.snapshots
    cancellation = threading.Event()
    checked = []
    def seeded_scan(owner, source_id, *, cancel=None):
        assert original_scan(owner, source_id)['status'] == 'complete'
        facade = owner.facade(owner.owner_access)
        before = facade.search(SearchRequest('needle', mode='literal')).results
        assert len(before) == 1
        (inputs[0] / 'folder/needle.pdf').unlink()
        def interrupted(source, root, cancel, **kwargs):
            for snapshot in original_snapshots(source, root, cancel, **kwargs):
                yield snapshot
                # Root acknowledged; the removed child's directory remains unvisited.
                if snapshot.relative_path == '':
                    cancel.set()
                    cancellation.set()
        with monkeypatch.context() as patch:
            patch.setattr(FileSystemSource, 'snapshots', interrupted)
            report = original_scan(owner, source_id, cancel=cancel)
        after = facade.search(SearchRequest('needle', mode='literal')).results
        assert {item.result_id for item in after} == {item.result_id for item in before}
        assert report['status'] == 'cancelled' and cancellation.is_set()
        checked.append(True)
        return report
    monkeypatch.setattr(RUNNER, '_scan_metadata', seeded_scan)
    result = run(inputs, cancel=cancellation)
    assert result['status'] == result['coverage'] == 'partial'
    assert result['total'] is None
    assert checked == [True] and not inputs[2].exists()


@pytest.mark.parametrize('phase', ['startup', 'scan', 'search'])
def test_actual_owner_failure_closes_pg_and_only_owned_profile(inputs, monkeypatch, phase):
    from atom_search.service.owner import ServiceOwner
    from atom_search.service.application import AtomSearchService
    owners = capture_real_owners(monkeypatch)
    sentinel = inputs[2].parent / 'foreign-sentinel'
    sentinel.write_text('KEEP', encoding='utf-8')
    def failed(*args, **kwargs):
        raise RuntimeError('SECRET query filename path detail')
    if phase == 'startup':
        original_start = ServiceOwner.start
        def started_then_failed(owner):
            original_start(owner)
            failed()
        monkeypatch.setattr(ServiceOwner, 'start', started_then_failed)
    elif phase == 'scan':
        monkeypatch.setattr(RUNNER, '_scan_metadata', failed)
    else:
        monkeypatch.setattr(AtomSearchService, 'search', failed)
    with pytest.raises(RuntimeError, match='^benchmark_failed$') as caught:
        run(inputs)
    assert str(caught.value) == 'benchmark_failed'
    assert len(owners) == 1 and owners[0].runtime.status().state == 'stopped'
    assert not inputs[2].exists() and sentinel.read_text(encoding='utf-8') == 'KEEP'


def test_existing_profile_is_never_adopted_or_deleted(inputs):
    inputs[2].mkdir()
    sentinel = inputs[2] / 'keep'
    sentinel.write_text('foreign', encoding='utf-8')
    with pytest.raises(ValueError, match='^benchmark_profile_exists$'):
        run(inputs)
    assert sentinel.read_text(encoding='utf-8') == 'foreign'


@pytest.mark.parametrize('mutation', ['relative_profile', 'inside_corpus', 'malformed_judgments',
    'judgment_escape', 'query_unknown_type', 'repetitions_bool', 'repetitions_zero', 'file_bound'])
def test_explicit_input_guards_refuse_before_owner_creation(inputs, monkeypatch, mutation):
    owners = capture_real_owners(monkeypatch)
    corpus, judgments, profile = inputs
    queries, repetitions, max_files = ('needle',), 2, 128
    if mutation == 'relative_profile':
        profile = Path('relative-profile')
    elif mutation == 'inside_corpus':
        profile = corpus / 'owned-profile'
    elif mutation == 'malformed_judgments':
        judgments.write_text('not json', encoding='utf-8')
    elif mutation == 'judgment_escape':
        judgments.write_text(json.dumps({'schema_version': 1,
            'judgments': {'needle': ['../foreign.pdf']}}), encoding='utf-8')
    elif mutation == 'query_unknown_type':
        queries = ('needle', None)
    elif mutation == 'repetitions_bool':
        repetitions = True
    elif mutation == 'repetitions_zero':
        repetitions = 0
    else:
        max_files = 1
    with pytest.raises(ValueError, match='^benchmark_input_invalid$'):
        RUNNER.run_benchmark(corpus, judgments, profile=profile, queries=queries,
                             repetitions=repetitions, max_files=max_files)
    assert owners == [] and not profile.exists()


@pytest.mark.parametrize('phase', ['creation', 'cleanup'])
def test_foreign_profile_swap_is_blocked_by_retained_authority(inputs, monkeypatch, phase):
    """Attempt real rename at either current path seam or retained-handle seam."""
    import shutil
    from atom_search.onboarding._windows_files import WindowsFiles
    profile = inputs[2]
    foreign = profile.parent / 'foreign-profile'
    foreign.mkdir()
    sentinel = foreign / 'keep'
    sentinel.write_text('DO NOT ADOPT OR DELETE', encoding='utf-8')
    displaced = profile.parent / 'displaced-owned'
    attempts, root_identity = [], []

    def attack():
        if attempts:
            return
        attempts.append(True)
        try:
            profile.rename(displaced)
            foreign.rename(profile)
        except OSError:
            pass  # Retained Windows authority must deny rename/replacement.

    original_mkdir, original_rmtree = Path.mkdir, shutil.rmtree
    def mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if phase == 'creation' and path == profile:
            attack()
        return result
    def rmtree(path, *args, **kwargs):
        if phase == 'cleanup' and Path(path) == profile:
            attack()
        return original_rmtree(path, *args, **kwargs)
    original_handle, original_dispose = WindowsFiles._handle, WindowsFiles._dispose
    @contextmanager
    def handle(files, parent, name, **kwargs):
        with original_handle(files, parent, name, **kwargs) as opened:
            if kwargs.get('create') and kwargs.get('directory') and name == profile.name:
                root_identity.append(files._identity(opened, directory=True)[0])
                if phase == 'creation':
                    attack()
            yield opened
    def dispose(files, opened):
        if phase == 'cleanup' and root_identity:
            try:
                is_root = files._identity(opened, directory=True)[0] == root_identity[0]
            except (OSError, ValueError):
                is_root = False
            if is_root:
                attack()
        return original_dispose(files, opened)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    monkeypatch.setattr(shutil, 'rmtree', rmtree)
    monkeypatch.setattr(WindowsFiles, '_handle', handle)
    monkeypatch.setattr(WindowsFiles, '_dispose', dispose)
    result = run(inputs)
    assert attempts == [True], 'the real replacement attempt must execute'
    assert sentinel.read_text(encoding='utf-8') == 'DO NOT ADOPT OR DELETE'
    assert not displaced.exists() and not profile.exists()
    assert result['status'] == 'complete'


def test_wide_directory_enumeration_stops_at_discovery_budget(inputs, monkeypatch):
    corpus = inputs[0]
    for index in range(100):
        (corpus / f'empty-{index}').mkdir()
    observed = []
    original = Path.iterdir
    def counted(directory):
        for entry in original(directory):
            if directory == corpus:
                observed.append(entry.name)
            yield entry
    monkeypatch.setattr(Path, 'iterdir', counted)
    with pytest.raises(ValueError, match='^benchmark_input_invalid$'):
        run(inputs, max_files=2)
    assert len(observed) <= 43, 'directory discovery must be bounded before enqueue/probe'
    assert not inputs[2].exists()
