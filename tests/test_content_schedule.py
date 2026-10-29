"""Owner-driven queue seam; no worker, scanner, model session or database."""
from dataclasses import replace
import importlib

import pytest

from atom_search_core.models import CatalogFile, PreparedEmbeddingInput


def surface(module, *names):
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        assert exc.name == module or module.startswith(exc.name + '.')
        loaded = None
    assert loaded is not None, f'required content boundary is absent: {module}'
    for name in names:
        assert callable(getattr(loaded, name, None)), f'required content surface: {name}'
    return loaded


def file(file_id=1, source_id=10, **changes):
    return replace(CatalogFile(file_id, source_id, 'owned-fixture', f'{file_id}.pdf',
                               f'{file_id}.pdf', 100, 200, 'online', 1), **changes)


@pytest.fixture
def queue():
    c = surface('atom_search.content_indexing.contracts', 'ContentScheduleState')
    s = surface('atom_search.content_indexing.scheduler', 'ContentScheduler')
    states = {}
    metadata = [False]
    clock = [100.0]
    current = [True]
    default = c.ContentScheduleState(profile='balanced', content_generation=1,
        profile_generation=1, context_revision=1, extractor_version='fixture-v1',
        chunker_version='fixture-v1', successful_fingerprint=None)
    scheduler = s.ContentScheduler(state_for=lambda f: states.get(f.file_id, default),
        metadata_pending=lambda: metadata[0], clock=lambda: clock[0],
        is_current=lambda token: current[0],
        max_pending=2, max_attempts=2, retry_delay=1.0)
    return scheduler, states, default, metadata, clock, current


def test_new_or_changed_file_queues_once_after_all_metadata_across_sources(queue):
    scheduler, _, _, metadata, _, _ = queue
    first, second = file(), file(2, 20)
    tokens = scheduler.on_metadata_commit((first, second), catalog_generation=3)
    assert len(tokens) == 2
    assert tokens[0].file_revision == 1
    assert tokens[0].catalog_generation == 3
    assert scheduler.on_metadata_commit((first,), catalog_generation=4) == ()
    metadata[0] = True  # Includes metadata work for OTHER sources.
    assert scheduler.next_work() is None
    metadata[0] = False
    assert scheduler.next_work() == tokens[0]
    assert scheduler.next_work() is None  # One host-owned worker at a time.
    assert scheduler.finish_work(tokens[0], status='complete') is True
    assert scheduler.next_work() == tokens[1]


@pytest.mark.parametrize('change', [{'size': 101}, {'modified_ns': 201}])
def test_size_or_mtime_change_admits_exactly_one_new_token(queue, change):
    scheduler, states, state, _, _, _ = queue
    states[1] = replace(state, successful_fingerprint=(100, 200))
    assert scheduler.on_metadata_commit((file(),), catalog_generation=1) == ()
    changed = file(revision=2, **change)
    tokens = scheduler.on_metadata_commit((changed,), catalog_generation=2)
    assert len(tokens) == 1
    assert scheduler.on_metadata_commit((changed,), catalog_generation=2) == ()
    assert (tokens[0].size, tokens[0].modified_ns) == (changed.size, changed.modified_ns)


def test_successful_unchanged_file_never_reopens_for_profile_context_or_parser_changes(queue):
    scheduler, states, state, _, _, _ = queue
    states[1] = replace(state, successful_fingerprint=(100, 200), profile='detailed',
                         profile_generation=8, context_revision=9, extractor_version='new-v2')
    # Durable successful fingerprint remains authoritative even if derived cache
    # was evicted. Reprocessing from cache/manual refresh belongs to later slices.
    assert scheduler.on_metadata_commit((file(),), catalog_generation=99) == ()
    assert scheduler.next_work() is None


def test_move_is_delete_add_and_cancelled_work_cannot_complete_replacement(queue):
    scheduler, _, _, _, _, _ = queue
    old = scheduler.on_metadata_commit((file(),), catalog_generation=1)[0]
    assert scheduler.next_work() == old
    scheduler.cancel_source(10)
    moved = file(2, relative_path='moved.pdf', revision=1)
    new = scheduler.on_metadata_commit((moved,), catalog_generation=2)[0]
    assert new.file_id != old.file_id
    assert scheduler.finish_work(old, status='complete') is False
    assert scheduler.next_work() == new


def test_changed_file_supersedes_old_token_without_stale_ack_removing_new_work(queue):
    scheduler, _, _, _, _, _ = queue
    old = scheduler.on_metadata_commit((file(),), catalog_generation=1)[0]
    assert scheduler.next_work() == old
    new = scheduler.on_metadata_commit((file(size=111, revision=2),), catalog_generation=2)[0]
    assert scheduler.finish_work(old, status='complete') is False
    assert scheduler.next_work() == new


def test_queue_capacity_does_not_mark_dropped_files_processed(queue):
    scheduler, _, _, _, _, _ = queue
    batch = (file(), file(2), file(3))
    admitted = scheduler.on_metadata_commit(batch, catalog_generation=1)
    assert len(admitted) == 2
    assert scheduler.next_work() == admitted[0]
    assert scheduler.finish_work(admitted[0], status='complete')
    later = scheduler.on_metadata_commit((batch[2],), catalog_generation=1)
    assert len(later) == 1
    assert later[0].file_id == 3


def test_retry_is_bounded_uses_backoff_and_preserves_token_identity(queue):
    scheduler, _, _, _, clock, _ = queue
    token = scheduler.on_metadata_commit((file(),), catalog_generation=1)[0]
    assert scheduler.next_work() == token
    assert scheduler.finish_work(token, status='failed', retryable=True)
    assert scheduler.next_work() is None
    clock[0] += 1.0
    assert scheduler.next_work() == token
    assert scheduler.finish_work(token, status='failed', retryable=True)
    clock[0] += 100.0
    assert scheduler.next_work() is None


def test_cancel_removes_only_target_source_and_never_resurrects_retry(queue):
    scheduler, _, _, _, clock, _ = queue
    a, b = scheduler.on_metadata_commit((file(), file(2, 20)), catalog_generation=1)
    assert scheduler.next_work() == a
    assert scheduler.finish_work(a, status='failed', retryable=True)
    scheduler.cancel_source(10)
    clock[0] += 10
    assert scheduler.next_work() == b
    assert scheduler.finish_work(b, status='complete')
    assert scheduler.next_work() is None


def test_profile_off_has_no_extraction_work(queue):
    scheduler, states, state, _, _, _ = queue
    states[1] = replace(state, profile='off')
    assert scheduler.on_metadata_commit((file(),), catalog_generation=1) == ()


@pytest.mark.parametrize('running', [False, True])
def test_deleted_or_replaced_file_fails_owner_freshness_before_dequeue_or_ack(queue, running):
    scheduler, _, _, _, _, current = queue
    token = scheduler.on_metadata_commit((file(),), catalog_generation=1)[0]
    if running:
        assert scheduler.next_work() == token
    current[0] = False
    if running:
        assert scheduler.finish_work(token, status='complete') is False
    assert scheduler.next_work() is None


def test_core_chunk_dto_reuses_prepared_embedding_input_without_service_imports():
    c = surface('atom_search_core.content_models', 'ChunkDraft', 'ChunkWrite')
    prepared = PreparedEmbeddingInput('passage: fixture', 'fixture-hash', 4)
    chunk = c.ChunkDraft(0, 'page:1', 'fixture', 'text-hash', prepared)
    assert chunk.prepared_input is prepared
    assert c.ChunkWrite(chunk, None, None).vector is None
