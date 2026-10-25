from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages/atom-search-core/src'))


class TinyEmbedder:
    model_id = 'test-semantic-v1'
    dimension = 3

    def __init__(self):
        self.passages = 0

    def encode(self, texts, *, kind='passage'):
        result = []
        if kind == 'passage':
            self.passages += len(texts)
        for text in texts:
            value = text.casefold()
            result.append([1, 0, 0] if 'кронштейн' in value or 'крепление' in value else
                          [0, 1, 0] if 'смета' in value or 'стоимость' in value else [0, 0, 1])
        return np.asarray(result, dtype=np.float32).reshape(-1, 3)


class SemanticServiceTests(unittest.TestCase):
    def test_cancellation_during_encoding_does_not_publish_batch(self):
        import threading
        from atom_search_core.service import SearchService
        cancel = threading.Event()
        class Cancelled(TinyEmbedder):
            def encode(self, texts, *, kind='passage'):
                result = super().encode(texts, kind=kind)
                cancel.set()
                return result
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            (source / 'file.pdf').touch()
            service = SearchService(base / 'catalog.sqlite3', embedder=Cancelled())
            try:
                service.scan(str(source))
                report = service.index_embeddings(cancel=cancel)
                self.assertEqual(report, {'indexed': 0, 'cancelled': True})
                with closing(sqlite3.connect(base / 'catalog.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM files WHERE embedding IS NOT NULL').fetchone()[0], 0)
            finally:
                service.close()
    def test_metadata_ready_before_blocked_embedding(self):
        from atom_search_core.service import SearchService
        class Blocked(TinyEmbedder):
            def encode(self, texts, *, kind='passage'):
                raise AssertionError('metadata scan must not invoke embedding')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'source'
            root.mkdir()
            (root / 'drawing.pdf').touch()
            service = SearchService(base / 'index.sqlite', embedder=Blocked())
            try:
                report = service.scan(str(root))
                self.assertNotIn('model_error', report)
                self.assertEqual(report['status'], 'complete')
                self.assertTrue(service.status()['metadata_ready'])
                self.assertFalse(service.status()['embeddings_ready'])
            finally:
                service.close()
    def test_selected_root_scopes_semantics_before_ranking_and_cache_reuse(self):
        from atom_search_core.models import DirectorySnapshot, FileMetadata
        from atom_search_core.service import SearchService
        from atom_search_core.source import canonical_root

        class OrderedEmbedder(TinyEmbedder):
            def encode(self, texts, *, kind='passage'):
                values = super().encode(texts, kind=kind)
                for index, text in enumerate(texts):
                    if 'КронштейнB' in text:
                        values[index] = [0.8, 0.6, 0]
                return values

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first, second = base / 'first', base / 'second'
            service = SearchService(base / 'index.sqlite', embedder=OrderedEmbedder())
            first_id = service.store.begin_root(canonical_root(str(first)))
            names = [f'Кронштейн{number}.pdf' for number in range(350)]
            service.store.reconcile(first_id, DirectorySnapshot('', tuple(
                FileMetadata(name, name, 0, 0) for name in names)))
            service.store.finish_root(first_id, 'complete')
            second_id = service.store.begin_root(canonical_root(str(second)))
            service.store.reconcile(second_id, DirectorySnapshot('', (
                FileMetadata('КронштейнB.pdf', 'КронштейнB.pdf', 0, 0),)))
            service.store.finish_root(second_id, 'complete')
            service.index_embeddings()
            self.assertEqual([item.name for item in service.search('крепление', root=str(second))],
                             ['КронштейнB.pdf'])
            self.assertEqual({item.root for item in service.search('крепление', root=str(first))},
                             {canonical_root(str(first))})
            self.assertEqual([item.name for item in service.search('крепление', root=str(second))],
                             ['КронштейнB.pdf'])
            self.assertEqual(service.search('крепление', root=str(base / 'unknown')), [])
            self.assertGreater(len(service.search('крепление')), 1)

    def test_synonyms_are_found_and_unchanged_names_reuse_vectors(self):
        from atom_search_core.service import SearchService
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'источник'
            root.mkdir()
            (root / 'Кронштейн.pdf').write_bytes(b'')
            (root / 'Смета.xlsx').write_bytes(b'')
            embedder = TinyEmbedder()
            service = SearchService(base / 'index.sqlite', embedder=embedder)
            report = service.scan(str(root))
            service.index_embeddings()
            self.assertEqual(report['status'], 'complete')
            self.assertTrue(service.status()['semantic_ready'])
            self.assertEqual(service.search('крепление')[0].name, 'Кронштейн.pdf')
            self.assertEqual(service.search('стоимость')[0].name, 'Смета.xlsx')
            self.assertEqual(embedder.passages, 2)
            service.scan(str(root))
            service.index_embeddings()
            self.assertEqual(embedder.passages, 2)
            with closing(sqlite3.connect(base / 'index.sqlite')) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM files WHERE embedding IS NOT NULL').fetchone()[0], 2)
            (root / 'Смета.xlsx').rename(root / 'Смета новая.xlsx')
            service.scan(str(root))
            service.index_embeddings()
            self.assertEqual(embedder.passages, 3)
            self.assertEqual(service.search('стоимость')[0].name, 'Смета новая.xlsx')

    def test_model_version_change_rebuilds_vectors(self):
        from atom_search_core.service import SearchService
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'source'
            root.mkdir()
            (root / 'Кронштейн.pdf').write_bytes(b'')
            first = TinyEmbedder()
            initial = SearchService(base / 'index.sqlite', embedder=first)
            initial.scan(str(root))
            initial.index_embeddings()
            initial.close()
            second = TinyEmbedder()
            second.model_id = 'test-semantic-v2'
            service = SearchService(base / 'index.sqlite', embedder=second)
            service.scan(str(root))
            service.index_embeddings()
            self.assertEqual(second.passages, 1)
            self.assertEqual(service.search('крепление')[0].name, 'Кронштейн.pdf')

    def test_missing_model_keeps_lexical_search_and_never_uses_network(self):
        from unittest.mock import patch
        from atom_search_core.service import SearchService
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'source'
            root.mkdir()
            (root / 'Расчёт.xlsx').write_bytes(b'')
            with patch('socket.socket', side_effect=AssertionError('Network is forbidden')):
                service = SearchService(base / 'index.sqlite', model_dir=base / 'missing-model')
                service.scan(str(root))
                self.assertFalse(service.status()['semantic_ready'])
                self.assertEqual(service.search('расчет')[0].name, 'Расчёт.xlsx')


if __name__ == '__main__':
    unittest.main()
