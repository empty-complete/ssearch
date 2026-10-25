"""Integration checks for the standalone Python inventory."""
import csv
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'corp_tree.py'
POWERSHELL = os.path.join(os.environ['SystemRoot'], 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')


class InventoryTests(unittest.TestCase):
    def run_scan(self, root, out):
        self.assertTrue(SCRIPT.is_file(), 'The runnable inventory script must exist')
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--root', str(root), '--out', str(out)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)

    def rows(self, out):
        with (out / 'files.tsv').open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream, delimiter='\t'))

    def test_missing_or_blank_root_is_rejected_before_inventory(self):
        spec = importlib.util.spec_from_file_location('corp_tree', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for arguments in ([], ['--root', ''], ['--root', '   ']):
            with self.subTest(arguments=arguments), patch.object(module, 'inventory') as scan:
                with self.assertRaises(SystemExit) as failure:
                    module.main(arguments)
                self.assertEqual(failure.exception.code, 2)
                scan.assert_not_called()

    def test_dfs_reparse_directories_are_traversed(self):
        # Real scandir/stat metadata, with only the DFS attribute/tag supplied by a server.
        spec = importlib.util.spec_from_file_location('corp_tree', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for tag in (0x8000000A, 0x80000012):
            with self.subTest(reparse_tag=hex(tag)), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, out = base / 'root', base / 'report'
                nested = root / 'DFS-папка' / 'Проект' / 'Глубже'
                nested.mkdir(parents=True)
                (nested / 'чертёж.pdf').write_bytes(b'abc')
                real_scandir = os.scandir

                class DfsEntry:
                    def __init__(self, entry):
                        self.entry = entry
                        self.name = entry.name

                    def stat(self, *, follow_symlinks):
                        info = self.entry.stat(follow_symlinks=follow_symlinks)
                        values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
                        values['st_file_attributes'] |= 0x400
                        values['st_reparse_tag'] = tag
                        return SimpleNamespace(**values)

                @contextmanager
                def scandir(path):
                    with real_scandir(path) as entries:
                        yield (DfsEntry(entry) if entry.name == 'DFS-папка' else entry for entry in entries)

                with patch.object(module.os, 'scandir', side_effect=scandir):
                    result = module.inventory(root, out)
                self.assertEqual(result, 0)
                self.assertEqual([row['relative_path'] for row in self.rows(out)],
                                 ['DFS-папка/Проект/Глубже/чертёж.pdf'])
                summary = (out / 'summary.txt').read_text(encoding='utf-8-sig')
                self.assertIn('files=1; directories=3; bytes=3; errors=0', summary)
                self.assertIn('Пропущено ссылок: 0', summary)

    def test_complete_inventory_includes_empty_hidden_unicode_and_zero_byte_entries(self):
        # Catches omitted hidden/empty entries, broken encoding and incorrect totals.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, out = base / 'root', base / 'report'
            (root / 'Проект [1]' / 'Пусто').mkdir(parents=True)
            (root / 'Проект [1]' / 'АР-1.PDF').write_bytes(b'abc')
            (root / 'без расширения').write_bytes(b'')
            (root / 'a&b.txt').write_bytes(b'hello')
            hidden = root / '.hidden.txt'
            hidden.write_bytes(b'xy')
            subprocess.run(['attrib', '+H', str(hidden)], check=True, capture_output=True)
            result = self.run_scan(root, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = self.rows(out)
            self.assertEqual({row['relative_path'] for row in rows}, {
                'Проект [1]/АР-1.PDF', 'без расширения', 'a&b.txt', '.hidden.txt'})
            self.assertEqual(sum(int(row['size_bytes']) for row in rows), 10)
            self.assertTrue(all(row['full_path'].startswith(str(root) + '\\') for row in rows))
            self.assertEqual(next(row for row in rows if row['extension'] == 'pdf')['category'], 'PDF')
            tree = (out / 'tree.txt').read_text(encoding='utf-8-sig')
            self.assertIn('Пусто/', tree)
            self.assertIn('АР-1.PDF', tree)
            self.assertIn('files=4; directories=2; bytes=10; errors=0',
                          (out / 'summary.txt').read_text(encoding='utf-8-sig'))
            self.assertEqual((out / 'errors.txt').read_text(encoding='utf-8-sig'), '')

    def test_unavailable_root_creates_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = self.run_scan(base / 'missing', base / 'report')
            self.assertEqual(result.returncode, 1)
            self.assertFalse((base / 'report').exists())

    def test_unreadable_branch_is_reported_and_other_branches_continue(self):
        # Catches aborting the complete scan on one network ACL error, or hiding it.
        spec = importlib.util.spec_from_file_location('corp_tree', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, out = base / 'root', base / 'report'
            denied = root / 'Закрытая папка'
            readable = root / 'Доступная папка'
            denied.mkdir(parents=True)
            readable.mkdir()
            (readable / 'документ.txt').write_bytes(b'ok')
            real_scandir = os.scandir

            def scandir(path):
                if str(path) == module.native(denied):
                    raise PermissionError(13, 'Access denied', str(path))
                return real_scandir(path)

            with patch.object(module.os, 'scandir', side_effect=scandir):
                result = module.inventory(root, out)
            self.assertEqual(result, 2)
            self.assertEqual([row['relative_path'] for row in self.rows(out)],
                             ['Доступная папка/документ.txt'])
            errors = (out / 'errors.txt').read_text(encoding='utf-8-sig')
            self.assertIn('Закрытая папка', errors)
            self.assertIn('Статус: complete_with_errors',
                          (out / 'summary.txt').read_text(encoding='utf-8-sig'))

    def test_report_cannot_be_written_inside_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_scan(root, root / 'report')
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / 'report').exists())

    def test_repeat_scans_preserve_source_and_refuse_overwriting_reports(self):
        # Catches destructive reruns and reports accidentally becoming part of a scan.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'root'
            root.mkdir()
            original = root / 'protected.bin'
            original.write_bytes(b'original data')
            before = original.stat()
            # Exclusive handle: listing metadata must work without opening file contents.
            command = (
                "$f = [IO.File]::Open($env:INVENTORY_LOCK_FILE, 'Open', 'ReadWrite', 'None'); "
                "Write-Output 'LOCKED'; [Console]::ReadLine() | Out-Null; $f.Dispose()")
            env = dict(os.environ, INVENTORY_LOCK_FILE=str(original))
            lock = subprocess.Popen([POWERSHELL, '-NoProfile', '-Command', command], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertEqual(lock.stdout.readline().strip(), b'LOCKED')
                one, two = base / 'one', base / 'two'
                self.assertEqual(self.run_scan(root, one).returncode, 0)
                self.assertEqual(self.run_scan(root, two).returncode, 0)
                self.assertEqual(self.rows(one), self.rows(two))
                snapshot = (one / 'tree.txt').read_bytes()
                self.assertEqual(self.run_scan(root, one).returncode, 1)
                self.assertEqual((one / 'tree.txt').read_bytes(), snapshot)
            finally:
                lock.communicate(b'\r\n', timeout=10)
            self.assertEqual(original.read_bytes(), b'original data')
            self.assertEqual(original.stat().st_mtime_ns, before.st_mtime_ns)

    def test_long_paths_and_junction_cycles(self):
        # Catches the 260 character limit and infinite traversal through directory links.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, out = base / 'root', base / 'report'
            deep = root
            for _ in range(7):
                deep /= 'очень длинная папка номер 123456'
            deep.mkdir(parents=True)
            (deep / 'данные.txt').write_bytes(b'data')
            junction = root / 'loop'
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(root)],
                           check=True, capture_output=True)
            try:
                result = self.run_scan(root, out)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = self.rows(out)
                self.assertEqual(len(rows), 1)
                self.assertGreater(int(rows[0]['path_length']), 260)
                self.assertIn('[ссылка: обход пропущен]', (out / 'tree.txt').read_text(encoding='utf-8-sig'))
                self.assertIn('Статус: complete_with_skipped_links',
                              (out / 'summary.txt').read_text(encoding='utf-8-sig'))
            finally:
                os.rmdir(junction)


if __name__ == '__main__':
    unittest.main(verbosity=2)
