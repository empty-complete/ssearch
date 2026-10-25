"""Frozen headless CLI contracts; real owner/PG/frozen acceptance is separate."""
import builtins
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'packaging/service_launcher.py'


@pytest.fixture
def launcher(monkeypatch):
    original_import = builtins.__import__
    gui_imports = []
    def no_gui(name, *args, **kwargs):
        if name.startswith(('PySide6', 'PyQt', 'atom_search.desktop')):
            gui_imports.append(name)
            raise ImportError('gui_import_forbidden')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_gui)
    spec = importlib.util.spec_from_file_location('ssearch_service_launcher', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    assert gui_imports == [], 'headless launcher attempted a GUI import'


def route(monkeypatch, module_name, function, operation):
    module = types.ModuleType(module_name)
    setattr(module, function, operation)
    monkeypatch.setitem(sys.modules, module_name, module)
    parent, _, child = module_name.rpartition('.')
    monkeypatch.setattr(importlib.import_module(parent), child, module, raising=False)


@pytest.mark.parametrize('arguments, module_name, expected', [
    (['--profile-info'], 'desktop_host', ['--profile-info']),
    (['--profile-info', '--data-dir', 'chosen profile'], 'desktop_host', ['--profile-info', '--data-dir', 'chosen profile']),
    (['--desktop-client', '--data-dir', 'chosen profile'], 'desktop_host', ['--desktop-client', '--data-dir', 'chosen profile']),
    (['--serve', '--parent-lease', '--data-dir', 'chosen profile', '--model-dir', 'offline model'],
     'desktop_host', ['--serve', '--parent-lease', '--data-dir', 'chosen profile', '--model-dir', 'offline model']),
    (['--serve', '--data-dir', 'chosen profile'], 'headless', ['--data-dir', 'chosen profile']),
    (['--serve', '--data-dir', 'chosen profile', '--model-dir', 'offline model', '--control-stdin'],
     'headless', ['--data-dir', 'chosen profile', '--model-dir', 'offline model', '--control-stdin']),
])
def test_cli_routes_only_declared_headless_roles(launcher, monkeypatch, capsys, arguments, module_name, expected):
    calls = []
    monkeypatch.setenv('PGSERVICE', 'ambient-owner-value')
    def main(argv):
        calls.append(argv)
        assert all(os.environ[name] == value for name, value in (
            ('HF_HUB_OFFLINE', '1'), ('TRANSFORMERS_OFFLINE', '1'),
            ('HF_HUB_DISABLE_TELEMETRY', '1'), ('TOKENIZERS_PARALLELISM', 'false')))
        return 7
    route(monkeypatch, 'atom_search.service.' + module_name, 'main', main)
    assert launcher.launch(arguments) == 7
    assert calls == [expected]
    assert os.environ['PGSERVICE'] == 'ambient-owner-value', 'library routing must not sanitize the caller'
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('flag', ['--profile-info', '--desktop-client'])
def test_profile_and_broker_stdout_remain_protocol_only(launcher, monkeypatch, capsys, flag):
    def main(argv):
        assert argv == [flag, '--data-dir', 'chosen profile']
        print('protocol-only-record')
        return 0
    route(monkeypatch, 'atom_search.service.desktop_host', 'main', main)
    assert launcher.launch([flag, '--data-dir', 'chosen profile']) == 0
    assert capsys.readouterr() == ('protocol-only-record\n', '')


@pytest.mark.parametrize('arguments', [
    [], ['--serve'], ['--desktop-client'],
    ['--profile-info', '--serve', '--data-dir', 'PRIVATE_SENTINEL'],
    ['--profile-info', '--model-dir', 'PRIVATE_SENTINEL'],
    ['--profile-info', '--control-stdin'],
    ['--desktop-client', '--data-dir', 'profile', '--model-dir', 'PRIVATE_SENTINEL'],
    ['--serve', '--parent-lease', '--data-dir', 'PRIVATE_SENTINEL'],
    ['--serve', '--parent-lease', '--data-dir', 'profile', '--model-dir', 'model', '--control-stdin'],
    ['--self-test', 'report', '--parent-lease'],
    ['--service-test', 'PRIVATE_SENTINEL'], ['--service-attach-test', 'PRIVATE_SENTINEL'],
    ['--postgres-self-test', 'PRIVATE_SENTINEL'],
    ['--self-test', 'report', '--service-test-scenario', 'two-clients'],
    ['--self-test', 'report', '--postgres-qa-lifecycle', 'seed'],
    ['--postgres-self-test', 'report', '--data-dir', 'profile', '--postgres-qa-run', 'PRIVATE_SENTINEL'],
    ['--serve', '--data-dir', 'profile', '--root', 'PRIVATE_SENTINEL'],
    ['--serve', '--data-dir', 'profile', '--token', 'PRIVATE_SENTINEL'],
    ['--smoke-test', '--screenshot', 'PRIVATE_SENTINEL'],
    ['--serv', '--data-dir', 'PRIVATE_SENTINEL'],
    ['--serve', '--data-dir', ''],
])
def test_invalid_cli_has_no_private_usage_output_or_delegate(launcher, monkeypatch, capsys, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid CLI reached a service')
    for name in ('desktop_host', 'headless'):
        route(monkeypatch, 'atom_search.service.' + name, 'main', forbidden)
    assert launcher.launch(arguments) == 2
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('scenario, function', [
    ('two-clients', 'run_service_self_test'), ('multi-source', 'run_multi_source_service_test'),
    ('health-offline', 'run_health_offline_service_test'), ('project-settings', 'run_project_settings_service_test'),
    ('project-context', 'run_project_context_service_test'), ('project-retrieval', 'run_project_retrieval_service_test'),
    ('project-golden', 'run_project_search_service_test'),
])
def test_existing_service_diagnostic_scenarios_remain_qt_free(launcher, monkeypatch, scenario, function):
    calls = []
    route(monkeypatch, 'atom_search.diagnostics', function,
          lambda *args, **kwargs: calls.append((args, kwargs)) or 9)
    assert launcher.launch(['--service-test', 'report.json', '--service-test-scenario', scenario,
                            '--data-dir', 'profile', '--model-dir', 'model']) == 9
    assert calls == [((Path('report.json'),), dict(data_dir=Path('profile'), model_dir=Path('model')))]


@pytest.mark.parametrize('arguments, function, kwargs', [
    (['--self-test', 'report.json'], 'run_self_test', dict(data_dir=None, model_dir=None)),
    (['--service-test', 'report.json', '--data-dir', 'profile'], 'run_service_self_test',
     dict(data_dir=Path('profile'), model_dir=None)),
    (['--service-attach-test', 'report.json', '--data-dir', 'profile'], 'run_service_attach_test',
     dict(data_dir=Path('profile'), model_dir=None)),
    (['--postgres-self-test', 'report.json', '--data-dir', 'profile'], 'run_postgres_self_test',
     dict(data_dir=Path('profile'))),
    (['--postgres-self-test', 'report.json', '--data-dir', 'profile', '--postgres-qa-lifecycle', 'verify',
      '--postgres-qa-run', 'a' * 32], 'run_postgres_self_test',
     dict(data_dir=Path('profile'), lifecycle='verify', run_token='a' * 32)),
])
def test_existing_frozen_diagnostic_cli_is_retained(launcher, monkeypatch, arguments, function, kwargs):
    calls = []
    route(monkeypatch, 'atom_search.diagnostics', function,
          lambda *args, **values: calls.append((args, values)) or 0)
    assert launcher.launch(arguments) == 0
    assert calls == [((Path('report.json'),), kwargs)]


@pytest.mark.parametrize('error_type', [RuntimeError, SystemExit])
def test_private_delegate_failure_is_not_rendered_or_raised(launcher, monkeypatch, capsys, error_type):
    def failure(argv):
        raise error_type('PRIVATE_SENTINEL password=hidden sql=hidden')
    route(monkeypatch, 'atom_search.service.desktop_host', 'main', failure)
    assert launcher.launch(['--desktop-client', '--data-dir', 'profile']) == 1
    assert capsys.readouterr() == ('', '')


def test_serving_process_sanitizes_before_routing_and_exits_after_terminal_failure(tmp_path):
    # A non-daemon worker demonstrates why ordinary SystemExit is insufficient.
    # No real owner/PG is replaced: this tests only the public process boundary.
    script = r'''
import os, runpy, sys, threading, types
from pathlib import Path
marker = Path(sys.argv[2])
for name in ('atom_search', 'atom_search.runtime', 'atom_search.service'):
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
postgres = types.ModuleType('atom_search.runtime.postgres')
def sanitize():
    os.environ.pop('PGSERVICE', None)
    os.environ['SSEARCH_TEST_SANITIZED'] = '1'
postgres.sanitize_host_environment = sanitize
sys.modules[postgres.__name__] = postgres
headless = types.ModuleType('atom_search.service.headless')
def main(argv):
    assert os.environ.get('SSEARCH_TEST_SANITIZED') == '1'
    assert 'PGSERVICE' not in os.environ
    assert os.environ['HF_HUB_OFFLINE'] == '1'
    threading.Thread(target=lambda: threading.Event().wait(60), daemon=False).start()
    marker.write_bytes(b'delegated-after-sanitize')
    return 1
headless.main = main
sys.modules[headless.__name__] = headless
entry = sys.argv[1]
sys.argv = ['ssearch-service', '--serve', '--data-dir', 'synthetic-profile']
runpy.run_path(entry, run_name='__main__')
'''
    marker = tmp_path / 'delegated'
    result = subprocess.run([sys.executable, '-c', script, str(ENTRY), str(marker)], cwd=tmp_path,
                            env=dict(os.environ, PGSERVICE='ambient-owner-value'),
                            capture_output=True, timeout=10)
    assert result.returncode == 1
    assert result.stdout == result.stderr == b''
    assert marker.read_bytes() == b'delegated-after-sanitize'


def test_invalid_process_cli_fails_before_loading_native_boundary(tmp_path):
    script = r'''
import importlib.abc, runpy, sys
class DenyService(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.startswith('atom_search'):
            raise ImportError('PRIVATE_SENTINEL')
sys.meta_path.insert(0, DenyService())
entry = sys.argv[1]
sys.argv = ['ssearch-service', '--not-supported', 'PRIVATE_SENTINEL']
runpy.run_path(entry, run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', script, str(ENTRY)], cwd=tmp_path,
                            capture_output=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == result.stderr == b''
