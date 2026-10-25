from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages/atom-search-core/src'))


class SearchTests(unittest.TestCase):
    def test_exact_filename_survives_broad_candidate_limit(self):
        from atom_search_core.models import DirectorySnapshot, FileMetadata
        from atom_search_core.service import SearchService
        with tempfile.TemporaryDirectory() as tmp:
            service = SearchService(Path(tmp) / 'index.sqlite')
            root_id = service.store.begin_root(r'C:\source')
            names = [f'match{number}.pdf' for number in range(600)] + ['match.pdf']
            service.store.reconcile(root_id, DirectorySnapshot('', tuple(
                FileMetadata(name, name, 0, 0) for name in names)))
            service.store.finish_root(root_id, 'complete')
            self.assertEqual(service.search('match')[0].name, 'match.pdf')
            self.assertEqual(service.search('match.pdf')[0].name, 'match.pdf')

    def test_selected_root_scopes_lexical_candidates_and_status(self):
        from atom_search_core.models import DirectorySnapshot, FileMetadata
        from atom_search_core.service import SearchService
        from atom_search_core.source import canonical_root
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            service = SearchService(base / 'index.sqlite')
            first, second = base / 'first', base / 'second'
            first_id = service.store.begin_root(canonical_root(str(first)))
            names = [f'match{number}.pdf' for number in range(600)]
            service.store.reconcile(first_id, DirectorySnapshot('', tuple(
                FileMetadata(name, name, 0, 0) for name in names)))
            service.store.finish_root(first_id, 'complete')
            second_id = service.store.begin_root(canonical_root(str(second)))
            service.store.reconcile(second_id, DirectorySnapshot('', (
                FileMetadata('matchselected.pdf', 'matchselected.pdf', 0, 0),)))
            service.store.finish_root(second_id, 'complete')
            with service.store.connection() as connection:
                connection.execute('UPDATE roots SET last_success=? WHERE id=?',
                                   ('2026-10-20T12:00:00+00:00', first_id))
                connection.execute('UPDATE roots SET last_success=? WHERE id=?',
                                   ('2026-10-19T12:00:00+00:00', second_id))
            selected = str(second).upper() + '\\'
            self.assertEqual([item.name for item in service.search('match', root=selected)],
                             ['matchselected.pdf'])
            self.assertEqual(service.search('match0', root=selected), [])
            self.assertEqual(service.status(root=selected)['files'], 1)
            self.assertEqual(service.status(root=selected)['last_success'],
                             '2026-10-19T12:00:00+00:00')
            self.assertEqual(len(service.status(root=selected)['roots']), 1)
            self.assertEqual(service.status(root=str(base / 'unknown'))['files'], 0)
            self.assertEqual(service.search('match', root=str(base / 'unknown')), [])
            self.assertEqual(service.status()['files'], 601)
            self.assertTrue(service.search('match0'))

    def test_function_words_do_not_outrank_semantic_matches(self):
        from atom_search_core.search import lexical_score
        self.assertEqual(lexical_score('договор с заказчиком', 'График отпусков сотрудников.xlsx',
                                       'График отпусков сотрудников.xlsx'), 0)
        self.assertGreater(lexical_score('расчет для кронштейна', 'Расчёт кронштейна.xlsx',
                                         'Расчёт кронштейна.xlsx'), 0)

    def test_cyrillic_engineering_numbers_paths_and_unsafe_query(self):
        module = Path(__file__).resolve().parents[1] / 'packages/atom-search-core/src/atom_search_core/service.py'
        self.assertTrue(module.is_file(), 'Search service must exist')
        from atom_search_core.service import SearchService
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'source'
            (root / 'Пермь Арена').mkdir(parents=True)
            (root / 'Пермь Арена' / 'Расчёт кронштейна.xlsx').write_bytes(b'')
            (root / 'АР-1.pdf').write_bytes(b'')
            (root / 'Общая записка.docx').write_bytes(b'')
            service = SearchService(base / 'index.sqlite')
            service.scan(str(root))
            self.assertEqual(service.search('расчет кронштейна')[0].name, 'Расчёт кронштейна.xlsx')
            self.assertEqual(service.search('АР1')[0].name, 'АР-1.pdf')
            self.assertEqual(service.search('пермь арена')[0].relative_path,
                             'Пермь Арена/Расчёт кронштейна.xlsx')
            self.assertEqual(service.search(''), [])
            self.assertIsInstance(service.search("' OR 1=1; DROP TABLE files; --"), list)
            self.assertEqual(service.status()['files'], 3)
            self.assertEqual(service.search('точнонеттакого'), [])
            self.assertLessEqual(len(service.search('а', limit=1)), 1)


if __name__ == '__main__':
    unittest.main()
