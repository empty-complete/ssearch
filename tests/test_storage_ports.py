"""Behavior at the storage boundary, including the temporary legacy adapter."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import hashlib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages/atom-search-core/src'))


def catalog_file(identifier=7, name='Расчёт.pdf', status='complete'):
    from atom_search_core.models import CatalogFile
    return CatalogFile(identifier, 3, r'C:\documents', name, name, 123,
                       1700000000000000123, status, 1)


class Ports:
    def __init__(self):
        self.events = []

    def resolve_scope(self, root):
        from atom_search_core.models import SearchScope
        self.events.append(('scope', root))
        return SearchScope((3,))

    def lexical_candidates(self, query, *, scope, limit):
        from atom_search_core.models import LexicalHit
        assert scope.source_ids == (3,)
        return [LexicalHit(catalog_file(), 8.5)]

    def status(self, *, scope):
        assert scope.source_ids == (3,)
        return {'files': 1, 'roots': [], 'last_success': None}

    def close(self):
        self.events.append('catalog closed')


def test_injected_ports_do_not_construct_sqlite(monkeypatch):
    import sqlite3
    from atom_search_core.service import SearchService
    from atom_search_core.store import CatalogStore

    def forbidden(*args, **kwargs):
        pytest.fail('Injected storage must not construct or connect SQLite')

    monkeypatch.setattr(CatalogStore, '__init__', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    ports = Ports()
    service = SearchService(catalog=ports, retrieval=ports)
    # A future FTS backend can match a different word form. Core must trust its hit.
    result = service.search('вычисления')[0]
    assert (result.file_id, result.name, result.score) == (7, 'Расчёт.pdf', 8.5)
    assert service.status()['files'] == 1
    service.close()


@pytest.mark.parametrize('selection', ['none', 'catalog', 'retrieval', 'mixed', 'mixed_catalog'])
def test_partial_dependency_injection_is_rejected(tmp_path, monkeypatch, selection):
    from atom_search_core.service import SearchService
    from atom_search_core.store import CatalogStore
    import atom_search_core.service as module

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid dependency selection must be rejected before resource construction')

    monkeypatch.setattr(CatalogStore, '__init__', forbidden)
    monkeypatch.setattr(module, 'FileSystemSource', forbidden)
    ports = Ports()
    choices = {
        'none': {}, 'catalog': {'catalog': ports}, 'retrieval': {'retrieval': ports},
        'mixed': {'database': tmp_path / 'unwanted.sqlite', 'catalog': ports, 'retrieval': ports},
        'mixed_catalog': {'database': tmp_path / 'unwanted.sqlite', 'catalog': ports},
    }
    with pytest.raises(ValueError):
        SearchService(model_dir=tmp_path / 'missing-model', **choices[selection])
    assert not (tmp_path / 'unwanted.sqlite').exists()


@pytest.mark.parametrize('close_fails', [False, True])
def test_close_orders_catalog_before_owner_and_runs_once(close_fails):
    from atom_search_core.service import SearchService
    ports = Ports()
    def close():
        ports.events.append('catalog closed')
        if close_fails:
            raise RuntimeError('catalog close failed')
    ports.close = close
    service = SearchService(catalog=ports, retrieval=ports,
                            on_close=lambda: ports.events.append('owner closed'))
    if close_fails:
        with pytest.raises(RuntimeError, match='catalog close failed'):
            service.close()
    else:
        service.close()
    service.close()
    assert ports.events == ['catalog closed', 'owner closed']


def backend_with_sources(tmp_path):
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    from atom_search_core.storage.legacy_sqlite import LegacySqliteBackend
    backend = LegacySqliteBackend(tmp_path / 'catalog.sqlite')
    ids = []
    for root, names in [
        (r'C:\excluded', [f'match{n}.pdf' for n in range(620)]),
        (r'C:\included', ['match-selected.pdf', 'match-second.pdf']),
        (r'C:\also-included', ['match-third.pdf']),
    ]:
        identifier = backend.begin_root(root)
        ids.append(identifier)
        backend.reconcile(identifier, DirectorySnapshot('', tuple(
            FileMetadata(name, name, 123, 1700000000000000123) for name in names)))
        backend.finish_root(identifier, 'complete')
    return backend, ids


def test_empty_and_unknown_source_scope_return_no_hits(tmp_path):
    from atom_search_core.models import SearchScope
    backend, ids = backend_with_sources(tmp_path)
    assert backend.resolve_scope(None).source_ids == tuple(ids)
    assert backend.resolve_scope(r'C:\INCLUDED\\').source_ids == (ids[1],)
    assert backend.resolve_scope(r'C:\missing') == SearchScope(())
    for scope in (SearchScope(()), SearchScope((99999,))):
        assert backend.lexical_candidates('match', scope=scope, limit=1) == []
        assert backend.vector_candidates((1., 0., 0.), model_key='tiny',
                                         scope=scope, limit=1) == []
        assert backend.status(scope=scope) == {'files': 0, 'roots': [], 'last_success': None}


def test_scoped_service_modes_apply_source_predicate_before_limit(tmp_path):
    from atom_search_core.models import SearchScope
    from atom_search_core.service import SearchService
    backend, ids = backend_with_sources(tmp_path)
    service = SearchService(catalog=backend, retrieval=backend)
    try:
        for mode in ('literal', 'lexical', 'hybrid'):
            assert service.search_scoped('match', scope=SearchScope((ids[1],)), limit=1, mode=mode)[0].root == r'C:\included'
        assert service.search_scoped('match', scope=SearchScope(())) == []
        for options in ({'mode': 'deep'}, {'limit': 0}, {'limit': True}):
            with pytest.raises(ValueError):
                service.search_scoped('match', scope=SearchScope((ids[1],)), **options)
    finally:
        service.close()


def test_multi_source_scope_precedes_candidate_limits_and_returns_full_dtos(tmp_path):
    from atom_search_core.models import CatalogFile, EmbeddingWrite, SearchScope
    backend, ids = backend_with_sources(tmp_path)
    scope = SearchScope((ids[2], ids[1], ids[1], 99999))
    lexical = backend.lexical_candidates('match', scope=scope, limit=2)
    assert [hit.file.name for hit in lexical] == ['match-selected.pdf', 'match-second.pdf']
    assert {hit.file.source_id for hit in lexical} == {ids[1]}
    pending = backend.pending_embeddings('tiny', 3, 1000)
    selected = next(item for item in pending if item.file.name == 'match-selected.pdf')
    assert selected.file == CatalogFile(621, ids[1], r'C:\included', 'match-selected.pdf',
                                       'match-selected.pdf', 123, 1700000000000000123,
                                       'complete', 1)
    assert selected.text == 'match-selected.pdf'
    assert selected.input_schema == 'filename-parent-v1'
    with pytest.raises(FrozenInstanceError):
        selected.file.size = 0
    writes = [EmbeddingWrite(item, (1., 0., 0.) if item.file.source_id == ids[0]
                             else (0.8, 0.6, 0.), 'digest') for item in pending]
    assert backend.save_embeddings(writes, 'tiny') == 623
    vector = backend.vector_candidates((1., 0., 0.), model_key='tiny', scope=scope, limit=2)
    assert [hit.file.file_id for hit in vector] == [621, 622]
    assert vector[0].similarity == pytest.approx(0.8)
    assert vector[0].file == selected.file
    assert backend.status(scope=scope)['files'] == 3
    assert [item['id'] for item in backend.status(scope=scope)['roots']] == ids[1:]


def test_pending_input_parent_and_save_count_after_deletion(tmp_path):
    from atom_search_core.models import DirectorySnapshot, EmbeddingWrite, FileMetadata
    from atom_search_core.storage.legacy_sqlite import LegacySqliteBackend
    backend = LegacySqliteBackend(tmp_path / 'catalog.sqlite')
    identifier = backend.begin_root(r'C:\source')
    backend.reconcile(identifier, DirectorySnapshot('Проект/Раздел', (
        FileMetadata('Проект/Раздел/АР-1.pdf', 'АР-1.pdf', 42, 99),
        FileMetadata('Проект/Раздел/АР-2.pdf', 'АР-2.pdf', 50, 100))))
    pending = backend.pending_embeddings('tiny', 3, 16)
    assert pending[0].text == 'АР-1.pdf | Проект / Раздел'
    assert (pending[0].file.size, pending[0].file.modified_ns,
            pending[0].file.source_status) == (42, 99, 'scanning')
    counts = backend.reconcile(identifier, DirectorySnapshot('Проект/Раздел', (
        FileMetadata('Проект/Раздел/АР-1.pdf', 'АР-1.pdf', 42, 99),)))
    assert (counts.added, counts.updated, counts.removed) == (0, 0, 1)
    assert backend.save_embeddings([
        EmbeddingWrite(item, (1., 0., 0.), hashlib.sha256(item.text.encode()).hexdigest())
        for item in pending], 'tiny') == 1
    assert backend.pending_embeddings('tiny', 3, 16) == []


def test_injected_embedding_pipeline_uses_input_and_actual_saved_count():
    from atom_search_core.models import EmbeddingInput
    from atom_search_core.service import SearchService
    ports = Ports()
    item = EmbeddingInput(catalog_file(), 'adapter prepared text', 'filename-parent-v1')
    batches = [[item], []]
    ports.pending_embeddings = lambda *args: batches.pop(0)
    def save(records, model_key):
        assert model_key == 'tiny|filename-parent-v1'
        assert records[0].item == item
        assert records[0].vector == (1., 0., 0.)
        assert records[0].input_hash == hashlib.sha256(b'adapter prepared text').hexdigest()
        return 0  # The file disappeared while the model was encoding it.
    ports.save_embeddings = save
    class Embedder:
        model_id = 'tiny'
        dimension = 3
        def encode(self, texts, *, kind):
            assert texts == ['adapter prepared text'] and kind == 'passage'
            return [[1., 0., 0.]]
    service = SearchService(catalog=ports, retrieval=ports, embedder=Embedder())
    progress = []
    assert service.index_embeddings(progress=progress.append) == {'indexed': 0, 'cancelled': False}
    assert progress[0]['indexed'] == 0


def test_hybrid_uses_backend_hits_and_preserves_rrf_weights():
    from atom_search_core.models import LexicalHit, VectorHit
    from atom_search_core.service import SearchService
    ports = Ports()
    ports.lexical_candidates = lambda *args, **kwargs: [
        LexicalHit(catalog_file(7, 'B.pdf'), 10.), LexicalHit(catalog_file(8, 'A.pdf'), 9.)]
    def vectors(vector, *, model_key, scope, limit):
        assert tuple(vector) == (1., 0., 0.)
        assert scope.source_ids == (3,) and model_key == 'tiny|filename-parent-v1'
        return [VectorHit(catalog_file(8, 'A.pdf'), .9),
                VectorHit(catalog_file(9, 'C.pdf', 'unavailable'), .8),
                VectorHit(catalog_file(10, 'D.pdf'), .2)]
    ports.vector_candidates = vectors
    class Embedder:
        model_id = 'tiny'
        dimension = 3
        def encode(self, texts, *, kind):
            return [[1., 0., 0.]]
    service = SearchService(catalog=ports, retrieval=ports, embedder=Embedder())
    result = service.search('different word form')
    assert [item.file_id for item in result] == [8, 7, 9]
    assert result[0].score == pytest.approx(2 / 62 + 1 / 61)
    assert result[1].score == pytest.approx(2 / 61)
    assert result[2].stale is True


def test_legacy_factory_keeps_existing_search_results(tmp_path):
    from atom_search_core.models import DirectorySnapshot, FileMetadata
    from atom_search_core.service import SearchService
    service = SearchService(tmp_path / 'catalog.sqlite')
    identifier = service.store.begin_root(r'C:\source')
    service.store.reconcile(identifier, DirectorySnapshot('', (
        FileMetadata('АР-1.pdf', 'АР-1.pdf', 123, 456),
        FileMetadata('АР-10.pdf', 'АР-10.pdf', 10, 20))))
    service.store.finish_root(identifier, 'unavailable')
    result = service.search('АР1')
    assert [(item.name, item.size, item.modified_ns, item.stale) for item in result] == [
        ('АР-1.pdf', 123, 456, True), ('АР-10.pdf', 10, 20, True)]
    service.close()


def test_ranked_results_accepts_catalog_dtos():
    from atom_search_core.search import ranked_results
    result = ranked_results('расчет', [catalog_file()], 10)
    assert [item.name for item in result] == ['Расчёт.pdf']
