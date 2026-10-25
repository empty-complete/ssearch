"""Editable content reaches real launch/UI consumers, including failed bootstrap."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def catalog():
    from atom_search import content
    return content


def launcher():
    spec = importlib.util.spec_from_file_location("content_launcher", ROOT / "packaging/launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_startup_failure_uses_whole_editable_message_and_metadata(tmp_path, monkeypatch, capsys):
    content = catalog()
    from atom_search.desktop import app
    monkeypatch.setattr(app, "main", lambda: (_ for _ in ()).throw(RuntimeError("broken startup")))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["AtomSearch", "--self-test", "report.json"])
    entry = launcher()
    assert entry.launch() == 1
    log = tmp_path / "AtomSearch/startup-error.log"
    assert capsys.readouterr().err == (
        "Не удалось запустить ssearch. Переустановите приложение "
        "или обратитесь к автору: filipp.beloborodoff@yandex.ru.\n"
        f"Подробности для поддержки: {log}\n")
    monkeypatch.setitem(content.PRODUCT, "product_name", "Test Product")
    monkeypatch.setitem(content.PRODUCT, "support_email", "test@example.test")
    monkeypatch.setitem(content.MESSAGES, "startup.failure", "{support_email}: запуск {product_name} невозможен.{diagnostic}")
    monkeypatch.setitem(content.MESSAGES, "startup.details", "\nОтчёт: {path}")
    assert entry.launch() == 1
    assert capsys.readouterr().err == f"test@example.test: запуск Test Product невозможен.\nОтчёт: {log}\n"


def test_formatter_rejects_missing_keys_and_placeholders_without_echoing_values(monkeypatch):
    content = catalog()
    with pytest.raises(KeyError, match="unknown.message"):
        content.message("unknown.message", path="private-value")
    monkeypatch.setitem(content.MESSAGES, "test.missing", "{needed} {path}")
    with pytest.raises(ValueError) as caught:
        content.message("test.missing", path="private-value")
    assert "test.missing" in str(caught.value)
    assert "needed" in str(caught.value)
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("archived", [False, True], ids=["source", "frozen-like-zip"])
def test_catalog_and_failed_launcher_work_without_optional_dependencies(tmp_path, archived):
    # A zip import exercises resource-independent loading; actual PyInstaller
    # build/native acceptance is intentionally a separate integration gate.
    source = ROOT / "src"
    if archived:
        source = tmp_path / "application.zip"
        with zipfile.ZipFile(source, "w") as package:
            for filename in ("__init__.py", "content.py"):
                package.write(ROOT / "src/atom_search" / filename, "atom_search/" + filename)
    script = r'''
import importlib.abc, importlib.util, io, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class BlockDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'numpy', 'psycopg', 'psycopg_binary', 'atom_search_core'} or fullname == 'atom_search.desktop':
            raise ImportError('blocked dependency: ' + fullname)
sys.meta_path.insert(0, BlockDependencies())
from atom_search.content import message
assert message('app.name') == 'ssearch'
if sys.argv[4] == 'True':
    sys.frozen = True
    sys._MEIPASS = str(Path(sys.argv[2]) / 'nonexistent-assets')
spec = importlib.util.spec_from_file_location('launcher_under_test', sys.argv[3])
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
os.environ['LOCALAPPDATA'] = sys.argv[2]
sys.argv = ['AtomSearch', '--smoke-test']
sys.stderr = io.StringIO()
result = entry.launch()
try:
    print(json.dumps({'code': result, 'message': sys.stderr.getvalue()}, ensure_ascii=False))
except UnicodeEncodeError:
    print('content_result_serialization_unicode_encode_error', file=sys.__stderr__, flush=True)
    raise
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, str(source), str(tmp_path),
                             str(ROOT / "packaging/launcher.py"), str(archived)],
                            capture_output=True, text=True, encoding="utf-8", timeout=20,
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["code"] == 1
    assert "Не удалось запустить ssearch." in output["message"]
    assert "filipp.beloborodoff@yandex.ru" in output["message"]
    assert "Подробности для поддержки:" in output["message"]


def test_real_about_dialog_escapes_editable_metadata_and_whole_template(tmp_path, monkeypatch):
    content = catalog()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QLabel
    from atom_search.desktop.window import SearchWindow
    app = QApplication.instance() or QApplication([])
    monkeypatch.setitem(content.PRODUCT, "product_name", 'Product <test> & "team"')
    monkeypatch.setitem(content.PRODUCT, "support_email", 'test"&<@example.test')
    monkeypatch.setitem(content.PRODUCT, "author_name", '<b>Author</b>')
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: None, auto_scan=False)
    captured = []

    def capture():
        dialog = app.activeModalWidget()
        if isinstance(dialog, QDialog):
            try:
                captured.append((dialog.windowTitle(), dialog.findChild(QLabel).text()))
            finally:
                dialog.accept()

    try:
        QTimer.singleShot(0, capture)
        window.open_about()
        title, body = captured.pop()
        assert title == 'О Product <test> & "team"'
        assert '<h2>Product &lt;test&gt; &amp; &quot;team&quot; — Simple Search</h2>' in body
        assert '<b>&lt;b&gt;Author&lt;/b&gt;</b>' in body
        assert 'href="mailto:test&quot;&amp;&lt;@example.test"' in body
        monkeypatch.setitem(content.MESSAGES, "about.body", "<p>{product_name}: {version}</p>")
        QTimer.singleShot(0, capture)
        window.open_about()
        from atom_search import VERSION
        assert captured.pop()[1] == f'<p>Product &lt;test&gt; &amp; &quot;team&quot;: {VERSION}</p>'
    finally:
        window.close()


def test_launcher_postgres_error_does_not_render_or_log_raw_secret(tmp_path, monkeypatch, capsys):
    from atom_search import diagnostics
    from atom_search.runtime import postgres
    monkeypatch.setattr(diagnostics, "run_postgres_self_test", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private-password")))
    monkeypatch.setattr(postgres, "sanitize_host_environment", lambda: None)
    monkeypatch.setattr(sys, "argv", ["AtomSearch", "--postgres-self-test", str(tmp_path / "proof.json"), "--data-dir", str(tmp_path / "profile")])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert launcher().launch() == 1
    assert "private-password" not in str(capsys.readouterr())
    assert not (tmp_path / "AtomSearch/startup-error.log").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version-resource objects")
def test_executable_display_metadata_comes_from_catalog_without_renaming_identity(monkeypatch):
    # Evaluate the spec's real version-resource expression using PyInstaller's
    # real value objects; no Analysis, build, identity write or binary launch.
    import ast
    from PyInstaller.utils.win32 import versioninfo
    content = catalog()
    tree = ast.parse((ROOT / "packaging/atom.spec").read_text(encoding="utf-8"))
    resource = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "version_info" for target in node.targets))
    monkeypatch.setitem(content.PRODUCT, "product_name", "Catalogue Product")
    monkeypatch.setitem(content.PRODUCT, "support_email", "new@example.test")
    monkeypatch.setitem(content.PRODUCT, "copyright_holder", "Company")
    monkeypatch.setitem(content.PRODUCT, "author_name", "Author")
    namespace = dict(vars(versioninfo), PRODUCT=content.PRODUCT, message=content.message,
                     version="0.2.0", version_tuple=(0, 2, 0, 0))
    exec(compile(ast.Module(body=[resource], type_ignores=[]), str(ROOT / "packaging/atom.spec"), "exec"), namespace)
    strings = namespace["version_info"].kids[0].kids[0].kids
    fields = {item.name: item.val for item in strings}
    assert fields["ProductName"] == "Catalogue Product"
    assert fields["Comments"] == "new@example.test"
    assert fields["LegalCopyright"] == "Правообладатель: Company. Автор: Author"
    assert fields["InternalName"] == "ssearch"
    assert fields["OriginalFilename"] == "ssearch.exe"


@pytest.mark.skipif(os.name != "nt", reason="Windows startup dialog")
def test_startup_dialog_title_uses_metadata_even_when_log_cannot_be_written(tmp_path, monkeypatch):
    import ctypes
    from atom_search.desktop import app
    content = catalog()
    monkeypatch.setitem(content.PRODUCT, "product_name", "Editable Product")
    monkeypatch.setitem(content.PRODUCT, "support_email", "support@example.test")
    monkeypatch.setattr(app, "main", lambda: (_ for _ in ()).throw(RuntimeError("startup failed")))
    monkeypatch.setattr(sys, "argv", ["AtomSearch"])
    # LOCALAPPDATA is a file, so the real logging attempt fails naturally.
    blocked = tmp_path / "not-a-directory"
    blocked.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(blocked))
    calls = []
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *args: calls.append(args))
    assert launcher().launch() == 1
    assert calls == [(None,
        "Не удалось запустить Editable Product. Переустановите приложение или обратитесь к автору: support@example.test.",
        "Editable Product", 0x10)]


def test_window_labels_and_validation_message_follow_catalog_edits(tmp_path, monkeypatch):
    content = catalog()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from atom_search.desktop.window import SearchWindow
    app = QApplication.instance() or QApplication([])
    monkeypatch.setitem(content.PRODUCT, "product_name", "Editable Product")
    monkeypatch.setitem(content.MESSAGES, "search.placeholder", "Введите запрос")
    monkeypatch.setitem(content.MESSAGES, "settings.root_required", "Выберите источник для {product_name}.")
    window = SearchWindow(data_dir=tmp_path, service_factory=lambda: None, auto_scan=False)
    try:
        assert window.windowTitle() == "Editable Product — Simple Search"
        assert window.search_box.placeholderText() == "Введите запрос"
        assert not window.save_settings(" ", 1)
        assert window.message.text() == "Выберите источник для Editable Product."
    finally:
        window.close()


@pytest.mark.parametrize("template", ["{path:unknown}", "{path", "{}"])
def test_invalid_format_fails_clearly_without_echoing_dynamic_values(monkeypatch, template):
    content = catalog()
    monkeypatch.setitem(content.MESSAGES, "test.invalid", template)
    with pytest.raises(ValueError) as caught:
        content.message("test.invalid", path="private-value")
    assert "test.invalid" in str(caught.value)
    assert "private-value" not in str(caught.value)


def test_format_error_from_a_value_cannot_leak_its_exception_details(monkeypatch):
    content = catalog()
    class PrivateValue:
        def __format__(self, spec):
            raise KeyError("private-value-from-dynamic-object")
    monkeypatch.setitem(content.MESSAGES, "test.private", "{value}")
    with pytest.raises(ValueError) as caught:
        content.message("test.private", value=PrivateValue())
    assert "test.private" in str(caught.value)
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("template", ["{value[0]}", "{value.name}"])
def test_templates_require_named_fields_not_object_access(monkeypatch, template):
    content = catalog()
    monkeypatch.setitem(content.MESSAGES, "test.object_access", template)
    with pytest.raises(ValueError):
        content.message("test.object_access", value=["private-value"])
