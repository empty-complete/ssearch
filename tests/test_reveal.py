"""Explorer navigation uses metadata and never opens source file contents."""

import errno
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class RecordingShell:
    """Replace the external desktop side effect, keeping the filesystem real."""

    def __init__(self):
        self.actions = []

    def select_file(self, path):
        self.actions.append(("select", path))

    def open_folder(self, folder):
        self.actions.append(("open", folder))


class RevealTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("atom_search.windows.reveal")
        except ModuleNotFoundError:
            self.fail("The Explorer navigation adapter has not been implemented yet")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Источник"
        self.root.mkdir()
        self.shell = RecordingShell()

    def reveal(self, path, root=None, shell=None):
        return self.module.reveal(
            str(path), str(self.root if root is None else root),
            shell=self.shell if shell is None else shell,
        )

    def test_existing_unicode_file_is_selected_without_reading_its_contents(self):
        folder = self.root / "Проект, 100% & расчёт"
        folder.mkdir()
        target = folder / "Чертёж АР-1.pdf"
        target.touch()
        with patch("builtins.open", side_effect=AssertionError("Source opened")), \
                patch.object(Path, "open", side_effect=AssertionError("Source opened")):
            result = self.reveal(target)
        self.assertTrue(result.opened)
        self.assertTrue(result.selected)
        self.assertEqual(result.folder, str(folder))
        self.assertEqual(self.shell.actions, [("select", str(target))])

    def test_existing_folder_is_opened(self):
        folder = self.root / "Папка"
        folder.mkdir()
        result = self.reveal(folder)
        self.assertTrue(result.opened)
        self.assertFalse(result.selected)
        self.assertEqual(result.folder, str(folder))
        self.assertEqual(self.shell.actions, [("open", str(folder))])

    @unittest.skipUnless(os.name == "nt", "UNC behavior is specific to Windows")
    def test_unc_share_root_accepts_its_own_file(self):
        folder = self.root / "Проект"
        folder.mkdir()
        (folder / "файл.pdf").touch()
        network_root = r"\\files.example.test\public"
        network_file = network_root + r"\Проект\файл.pdf"
        real_stat = os.stat

        def metadata(path, *args, **kwargs):
            relative = os.path.relpath(os.fspath(path), network_root)
            return real_stat(self.root / relative, *args, **kwargs)

        with patch.object(self.module.os, "stat", side_effect=metadata):
            result = self.module.reveal(network_file, network_root, shell=self.shell)
        self.assertTrue(result.opened)
        self.assertTrue(result.selected)
        self.assertEqual(result.folder, network_root + r"\Проект")
        self.assertEqual(self.shell.actions, [("select", network_file)])

    @unittest.skipUnless(os.name == "nt", "UNC behavior is specific to Windows")
    def test_unc_share_missing_chain_opens_the_share_root_and_stops(self):
        network_root = r"\\files.example.test\public"
        network_file = network_root + r"\Пропавший\файл.pdf"
        real_stat = os.stat

        def metadata(path, *args, **kwargs):
            relative = os.path.relpath(os.fspath(path), network_root)
            return real_stat(self.root / relative, *args, **kwargs)

        with patch.object(self.module.os, "stat", side_effect=metadata):
            result = self.module.reveal(network_file, network_root, shell=self.shell)
        self.assertTrue(result.opened)
        self.assertFalse(result.selected)
        self.assertEqual(result.folder, network_root)
        self.assertEqual(self.shell.actions, [("open", network_root)])

    def test_moved_file_opens_its_nearest_existing_parent_and_explains_which(self):
        folder = self.root / "Проект"
        folder.mkdir()
        old = folder / "До переноса.pdf"
        old.touch()
        old.rename(folder / "После переноса.pdf")
        result = self.reveal(old)
        self.assertTrue(result.opened)
        self.assertFalse(result.selected)
        self.assertEqual(result.folder, str(folder))
        self.assertIn("не найден", result.message.lower())
        self.assertIn(str(folder), result.message)
        self.assertEqual(self.shell.actions, [("open", str(folder))])

    def test_missing_ancestor_chain_falls_back_to_configured_root_inclusively(self):
        result = self.reveal(self.root / "Пропавший" / "Раздел" / "файл.xlsx")
        self.assertTrue(result.opened)
        self.assertFalse(result.selected)
        self.assertEqual(result.folder, str(self.root))
        self.assertIn(str(self.root), result.message)
        self.assertEqual(self.shell.actions, [("open", str(self.root))])

    def test_missing_root_never_opens_a_parent_outside_the_source(self):
        self.root.rmdir()
        result = self.reveal(self.root / "файл.pdf")
        self.assertFalse(result.opened)
        self.assertIsNone(result.folder)
        self.assertIn("источник", result.message.lower())
        self.assertIn("недоступ", result.message.lower())
        self.assertEqual(self.shell.actions, [])

    def test_existing_file_outside_root_and_similar_root_prefix_are_rejected(self):
        outside = self.root.parent / (self.root.name + "-другая")
        outside.mkdir()
        target = outside / "файл.pdf"
        target.touch()
        for path in (target, self.root / ".." / outside.name / target.name):
            with self.subTest(path=path):
                result = self.reveal(path)
                self.assertFalse(result.opened)
                self.assertIn("источник", result.message.lower())
        self.assertEqual(self.shell.actions, [])

    def test_relative_and_malformed_paths_return_a_friendly_result(self):
        for path, root in (("relative.pdf", str(self.root)),
                           (str(self.root / "file.pdf"), "relative"),
                           ("", str(self.root)),
                           (str(self.root) + "\x00file.pdf", str(self.root))):
            with self.subTest(path=path, root=root):
                result = self.module.reveal(path, root, shell=self.shell)
                self.assertFalse(result.opened)
                self.assertIsNone(result.folder)
                self.assertIn("путь", result.message.lower())
        self.assertEqual(self.shell.actions, [])

    def test_access_denied_does_not_pretend_the_file_was_deleted(self):
        folder = self.root / "Закрыто"
        folder.mkdir()
        target = folder / "файл.pdf"
        target.touch()
        real_stat = os.stat

        def metadata(path, *args, **kwargs):
            if os.fspath(path) == str(target):
                raise PermissionError(errno.EACCES, "RAW PRIVATE DETAILS", str(target))
            return real_stat(path, *args, **kwargs)

        with patch.object(self.module.os, "stat", side_effect=metadata):
            result = self.reveal(target)
        self.assertFalse(result.opened)
        self.assertIn("доступ", result.message.lower())
        self.assertNotIn("не найден", result.message.lower())
        self.assertNotIn("RAW PRIVATE DETAILS", result.message)
        self.assertEqual(self.shell.actions, [])

    def test_unavailable_network_is_distinguished_from_access_denied(self):
        real_stat = os.stat

        def metadata(path, *args, **kwargs):
            if os.fspath(path) == str(self.root):
                error = OSError(errno.EIO, "RAW NETWORK DETAILS")
                error.winerror = 53
                raise error
            return real_stat(path, *args, **kwargs)

        with patch.object(self.module.os, "stat", side_effect=metadata):
            result = self.reveal(self.root / "файл.pdf")
        self.assertFalse(result.opened)
        self.assertIn("сет", result.message.lower())
        self.assertNotIn("прав", result.message.lower())
        self.assertNotIn("RAW NETWORK DETAILS", result.message)
        self.assertEqual(self.shell.actions, [])

    def test_network_file_not_found_error_is_not_treated_as_a_deleted_file(self):
        target = self.root / "файл.pdf"
        real_stat = os.stat

        def metadata(path, *args, **kwargs):
            if os.fspath(path) == str(target):
                error = FileNotFoundError(errno.ENOENT, "RAW NETWORK DETAILS")
                error.winerror = 53  # Windows uses this subclass for lost network paths.
                raise error
            return real_stat(path, *args, **kwargs)

        with patch.object(self.module.os, "stat", side_effect=metadata):
            result = self.reveal(target)
        self.assertFalse(result.opened)
        self.assertIn("сет", result.message.lower())
        self.assertNotIn("не найден", result.message.lower())
        self.assertEqual(self.shell.actions, [])

    def test_network_logon_failure_is_reported_as_access_denied(self):
        error = OSError(errno.EIO, "RAW AUTHENTICATION DETAILS")
        error.winerror = 1326
        with patch.object(self.module.os, "stat", side_effect=error):
            result = self.reveal(self.root / "файл.pdf")
        self.assertFalse(result.opened)
        self.assertIn("прав", result.message.lower())
        self.assertNotIn("RAW", result.message)
        self.assertEqual(self.shell.actions, [])

    def test_access_denied_at_root_is_reported_without_searching_above_it(self):
        real_stat = os.stat

        def metadata(path, *args, **kwargs):
            if os.fspath(path) == str(self.root):
                raise PermissionError(errno.EACCES, "RAW ACL DETAILS")
            return real_stat(path, *args, **kwargs)

        with patch.object(self.module.os, "stat", side_effect=metadata):
            result = self.reveal(self.root / "файл.pdf")
        self.assertFalse(result.opened)
        self.assertIn("прав", result.message.lower())
        self.assertNotIn("RAW ACL DETAILS", result.message)
        self.assertEqual(self.shell.actions, [])

    def test_file_disappearing_during_selection_falls_back_to_parent(self):
        folder = self.root / "Проект"
        folder.mkdir()
        target = folder / "файл.pdf"
        target.touch()

        class RacingShell(RecordingShell):
            def select_file(self, path):
                target.unlink()
                raise FileNotFoundError(errno.ENOENT, "deleted during selection")

        shell = RacingShell()
        result = self.reveal(target, shell=shell)
        self.assertTrue(result.opened)
        self.assertFalse(result.selected)
        self.assertEqual(result.folder, str(folder))
        self.assertEqual(shell.actions, [("open", str(folder))])

    def test_parent_disappearing_during_open_retries_the_next_ancestor(self):
        folder = self.root / "Проект"
        folder.mkdir()

        class RacingShell(RecordingShell):
            def open_folder(self, path):
                if path == str(folder):
                    folder.rmdir()
                    raise FileNotFoundError(errno.ENOENT, "deleted during opening")
                super().open_folder(path)

        shell = RacingShell()
        result = self.reveal(folder / "файл.pdf", shell=shell)
        self.assertTrue(result.opened)
        self.assertFalse(result.selected)
        self.assertEqual(result.folder, str(self.root))
        self.assertEqual(shell.actions, [("open", str(self.root))])

    def test_root_disappearing_during_shell_open_never_escapes_the_root(self):
        class RacingShell(RecordingShell):
            def open_folder(shell_self, path):
                self.root.rmdir()
                raise FileNotFoundError(errno.ENOENT, "deleted during opening")

        shell = RacingShell()
        result = self.reveal(self.root / "файл.pdf", shell=shell)
        self.assertFalse(result.opened)
        self.assertIsNone(result.folder)
        self.assertIn("недоступ", result.message.lower())
        self.assertEqual(shell.actions, [])

    def test_shell_access_denial_and_unknown_failure_are_friendly(self):
        target = self.root / "файл.pdf"
        target.touch()
        for error, expected in ((PermissionError(errno.EACCES, "RAW SHELL ACL"), "доступ"),
                                (RuntimeError("RAW SHELL FAILURE"), "проводник")):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.shell, "select_file", side_effect=error):
                    result = self.reveal(target)
                self.assertFalse(result.opened)
                self.assertIsNone(result.folder)
                self.assertIn(expected, result.message.lower())
                self.assertNotIn("RAW", result.message)


if __name__ == "__main__":
    unittest.main()
