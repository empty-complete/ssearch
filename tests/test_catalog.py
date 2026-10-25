import os
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages/atom-search-core/src'))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'источник'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def service(self):
        module = Path(__file__).resolve().parents[1] / 'packages/atom-search-core/src/atom_search_core/service.py'
        self.assertTrue(module.is_file(), 'Persistent catalog service must exist')
        from atom_search_core.service import SearchService
        return SearchService(self.base / 'index.sqlite')

    def test_v1_catalog_upgrade_preserves_files_vectors_and_reopens(self):
        from atom_search_core.search import compact, normalize
        from atom_search_core.source import canonical_root
        database = self.base / 'index.sqlite'
        root = canonical_root(str(self.root))
        name = 'АР-1.pdf'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE roots (id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE,
                  last_attempt TEXT,last_success TEXT,status TEXT NOT NULL DEFAULT 'new',error TEXT);
                CREATE TABLE files (id INTEGER PRIMARY KEY,root_id INTEGER NOT NULL,
                  relative_path TEXT NOT NULL,parent TEXT NOT NULL,name TEXT NOT NULL,
                  size INTEGER NOT NULL,modified_ns INTEGER NOT NULL,
                  compact_name TEXT NOT NULL,compact_path TEXT NOT NULL,
                  embedding BLOB,embedding_model TEXT,input_hash TEXT,
                  UNIQUE(root_id,relative_path));
                CREATE VIRTUAL TABLE file_search USING fts5(name,relative_path);
                PRAGMA user_version=1;
            ''')
            connection.execute("INSERT INTO roots(id,path,last_success,status) VALUES (3,?,?,'complete')",
                               (root, '2026-10-19T12:00:00+00:00'))
            connection.execute('''INSERT INTO files VALUES
                (42,3,?,'',?,123,456,?,?,?,'legacy-model','legacy-digest')''',
                (name, name, compact(name), compact(name), b'cached-vector'))
            connection.execute('INSERT INTO file_search(rowid,name,relative_path) VALUES (42,?,?)',
                               (normalize(name), normalize(name)))

        service = self.service()
        result = service.search('АР1', root=str(self.root))[0]
        self.assertEqual((result.file_id, result.name, result.size, result.modified_ns),
                         (42, 'АР-1.pdf', 123, 456))
        with service.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT compact_stem FROM files WHERE id=42').fetchone()[0],
                             'ар1')
            self.assertEqual(connection.execute('SELECT embedding,embedding_model,input_hash FROM files '
                                                'WHERE id=42').fetchone()[:],
                             (b'cached-vector', 'legacy-model', 'legacy-digest'))
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
        service.close()
        reopened = self.service()
        self.assertEqual(reopened.search('АР1')[0].file_id, 42)
        self.assertEqual(reopened.status()['files'], 1)

    def test_repeat_scan_and_reopen_are_persistent_and_idempotent(self):
        (self.root / 'АР-1.pdf').write_bytes(b'abc')
        service = self.service()
        first = service.scan(str(self.root))
        second = service.scan(str(self.root))
        self.assertEqual(first['added'], 1)
        self.assertEqual(second['added'], 0)
        self.assertEqual(second['updated'], 0)
        self.assertEqual(second['removed'], 0)
        self.assertEqual(service.status()['files'], 1)
        identity = service.search('АР-1')[0].file_id
        service.close()
        reopened = self.service()
        self.assertEqual(reopened.search('АР-1')[0].file_id, identity)
        self.assertEqual(reopened.status()['files'], 1)

    def test_moves_and_deleted_directories_remove_old_locations(self):
        folder = self.root / 'Старая папка'
        folder.mkdir()
        (folder / 'Смета.xlsx').write_bytes(b'original')
        service = self.service()
        service.scan(str(self.root))
        (folder / 'Смета.xlsx').rename(self.root / 'Новая смета.xlsx')
        folder.rmdir()
        report = service.scan(str(self.root))
        self.assertEqual(report['added'], 1)
        self.assertEqual(report['removed'], 1)
        results = service.search('смета')
        self.assertEqual([item.relative_path for item in results], ['Новая смета.xlsx'])

    def test_unavailable_root_preserves_cached_results_and_marks_stale(self):
        (self.root / 'План.docx').write_bytes(b'plan')
        service = self.service()
        service.scan(str(self.root))
        hidden = self.base / 'недоступный'
        self.root.rename(hidden)
        report = service.scan(str(self.root))
        self.assertEqual(report['status'], 'unavailable')
        result = service.search('план')[0]
        self.assertTrue(result.stale)
        self.assertEqual(service.status()['files'], 1)

    def test_one_denied_branch_does_not_erase_it_but_other_deletions_apply(self):
        denied, good = self.root / 'Закрытая', self.root / 'Открытая'
        denied.mkdir()
        good.mkdir()
        (denied / 'план.pdf').write_bytes(b'abc')
        (good / 'удалённый.pdf').write_bytes(b'def')
        service = self.service()
        service.scan(str(self.root))
        (good / 'удалённый.pdf').unlink()
        from atom_search_core import source
        actual_scandir = source.os.scandir

        def scandir(path):
            if os.path.normcase(str(path)) == os.path.normcase(source.native(denied)):
                raise PermissionError(13, 'Access denied', str(path))
            return actual_scandir(path)

        with patch.object(source.os, 'scandir', side_effect=scandir):
            report = service.scan(str(self.root))
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(len(report['errors']), 1)
        self.assertEqual(service.status()['files'], 1)
        self.assertEqual(service.search('план')[0].relative_path, 'Закрытая/план.pdf')

    def test_cancelled_scan_preserves_existing_catalog(self):
        (self.root / 'План.pdf').write_bytes(b'abc')
        service = self.service()
        service.scan(str(self.root))
        (self.root / 'План.pdf').unlink()
        cancelled = threading.Event()
        cancelled.set()
        report = service.scan(str(self.root), cancel=cancelled)
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(service.status()['files'], 1)

    def test_failed_callback_does_not_claim_success_or_leave_scan_locked(self):
        (self.root / 'План.pdf').write_bytes(b'abc')
        service = self.service()

        def progress(_report):
            raise RuntimeError('Consumer disconnected')

        with self.assertRaises(RuntimeError):
            service.scan(str(self.root), progress=progress)
        self.assertEqual(service.status()['roots'][0]['status'], 'partial')
        self.assertEqual(service.scan(str(self.root))['status'], 'complete')

    def test_file_content_is_never_opened(self):
        (self.root / 'Документ.docx').write_bytes(b'confidential content')
        service = self.service()
        # SQLite uses its native connection; a scanner must not use Python file I/O.
        with patch('builtins.open', side_effect=AssertionError('Source content opened')):
            report = service.scan(str(self.root))
        self.assertEqual(report['files'], 1)
        self.assertEqual(service.search('документ')[0].size, 20)


if __name__ == '__main__':
    unittest.main()
