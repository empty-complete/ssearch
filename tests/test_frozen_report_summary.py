"""Diagnostic output is bounded and redacted; it never qualifies a frozen gate."""
import builtins
import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = 'PRIVATE_SENTINEL'


@pytest.fixture
def formatter():
    path = ROOT / 'tools/summarize_frozen_report.py'
    assert path.is_file(), 'Safe frozen report summary is required'
    spec = importlib.util.spec_from_file_location('frozen_report_summary', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_retains_known_failure_facts_without_private_fields(formatter, tmp_path):
    report = tmp_path / 'report.json'
    report.write_text(json.dumps({'ok': False, 'clean_stop': False, 'model_constructions': 0,
        'runtime_starts': 0, 'phase': 'imports', 'error_code': 'runtime_start_failed',
        'error_type': 'ModuleNotFoundError', 'failure_detail': {'exceptions': [
            {'type': 'ModuleNotFoundError', 'locations': [
                {'module': 'atom_search.diagnostics', 'line': 91},
                {'module': PRIVATE, 'line': 20}], 'message': PRIVATE, 'locals': PRIVATE}]},
        'pid': 12345, 'owner_pid': 12345, 'generation': PRIVATE, 'token': PRIVATE,
        'path': PRIVATE, 'args': PRIVATE, 'raw_stderr': PRIVATE}), encoding='utf-8')
    summary = formatter.summarize_report(report, 'service-owner', 1, not_before_ms=0)
    assert summary['diagnostic_only'] is True
    assert summary['role'] == 'service-owner' and summary['process_exit_code'] == 1
    assert summary['report_state'] == 'read'
    assert summary['fields']['ok'] is False
    assert summary['fields']['model_constructions'] == 0
    assert summary['fields']['error_code'] == 'runtime_start_failed'
    assert summary['fields']['failure_detail']['exceptions'] == [
        {'type': 'ModuleNotFoundError', 'locations': [{'module': 'atom_search.diagnostics', 'line': 91}]}]
    rendered = json.dumps(summary)
    assert PRIVATE not in rendered and '12345' not in rendered
    assert not {'pid', 'path', 'generation', 'args', 'token', 'raw_stderr'} & summary['fields'].keys()


def test_unknown_values_in_known_fields_are_redacted(formatter, tmp_path):
    report = tmp_path / 'report.json'
    report.write_text(json.dumps({'ok': PRIVATE, 'model_ready': PRIVATE, 'model_constructions': True,
        'runtime_starts': -1, 'phase': PRIVATE, 'error_code': PRIVATE, 'error_type': PRIVATE,
        'failure_detail': {'exceptions': [{'type': PRIVATE, 'locations': [
            {'module': 'atom_search.' + PRIVATE, 'line': 2}]}]}}), encoding='utf-8')
    summary = formatter.summarize_report(report, 'service-owner', 1, not_before_ms=0)
    assert PRIVATE not in json.dumps(summary)
    assert not {'ok', 'model_ready', 'model_constructions', 'runtime_starts', 'phase',
                'error_code', 'error_type'} & summary['fields'].keys()


@pytest.mark.parametrize('state', ['missing', 'malformed', 'oversized', 'stale'])
def test_unavailable_report_is_only_a_fixed_diagnostic(formatter, tmp_path, state):
    report = tmp_path / 'report.json'
    if state == 'malformed':
        report.write_text(PRIVATE, encoding='utf-8')
    elif state == 'oversized':
        report.write_bytes(b'x' * 65537)
    elif state == 'stale':
        report.write_text('{"ok":true}', encoding='utf-8')
        os.utime(report, ns=(1_000_000_000, 1_000_000_000))
    summary = formatter.summarize_report(report, 'service-owner', 1, not_before_ms=2000)
    assert summary == {'diagnostic_only': True, 'role': 'service-owner',
                       'process_exit_code': 1, 'report_state': 'unavailable', 'fields': {}}


def test_early_service_delegate_import_failure_writes_quiet_failed_report(monkeypatch, tmp_path, capsys):
    from atom_search import diagnostics
    original = builtins.__import__
    attempted = []
    def fail_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'atom_search' and 'search_runtime' in fromlist:
            attempted.append('delegate')
            raise ModuleNotFoundError(PRIVATE)
        if name.startswith(('atom_search.service.owner', 'atom_search.runtime.postgres', 'PySide6')):
            attempted.append('forbidden')
            raise AssertionError(PRIVATE)
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', fail_import)
    report = tmp_path / 'failed.json'
    profile = tmp_path / 'unused-profile'
    assert diagnostics.run_service_self_test(report, data_dir=profile) == 1
    proof = json.loads(report.read_text(encoding='utf-8'))
    assert proof['ok'] is False and proof['clean_stop'] is False
    assert proof['phase'] == 'imports' and proof['error_type'] == 'ModuleNotFoundError'
    assert all(proof[name] == 0 for name in ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes'))
    assert attempted == ['delegate'] and not profile.exists()
    assert proof['failure_detail']['exceptions'][0]['type'] == 'ModuleNotFoundError'
    assert any(location['module'] == 'atom_search.diagnostics'
               for error in proof['failure_detail']['exceptions'] for location in error['locations'])
    assert PRIVATE not in report.read_text(encoding='utf-8')
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_service_model_counter_preserves_keyword_only_preparer(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from atom_search import diagnostics, search_runtime
    from atom_search.service import owner as owner_module

    class ProbeStopped(RuntimeError):
        pass

    model = tmp_path / 'unused-model'
    preparer = object()
    delegated, factories = [], []
    original = search_runtime._load_model

    def observed_load(model_dir, manifest, *, preparer=None):
        delegated.append((model_dir, manifest, preparer))
        # The real delegate's manifest=None branch constructs no model or PG.
        return original(model_dir, manifest, preparer=preparer)

    monkeypatch.setattr(search_runtime, '_load_model', observed_load)

    def start():
        assert search_runtime._load_model(model, None, preparer=preparer) == (None, None)
        raise ProbeStopped()

    fake_owner = SimpleNamespace(
        repository=SimpleNamespace(snapshot=lambda: SimpleNamespace(settings={'sources': []}, revision=1)),
        coordinator=SimpleNamespace(owner_apply=lambda settings, revision: None),
        start=start, close=lambda: None,
        runtime=SimpleNamespace(status=lambda: SimpleNamespace(state='stopped')))

    def factory(profile, model_dir, *, automatic_catalog):
        factories.append((profile, model_dir, automatic_catalog))
        return fake_owner

    monkeypatch.setattr(owner_module, 'open_service_owner', factory)
    report = tmp_path / 'probe.json'
    data = tmp_path / 'owned-probe'
    assert diagnostics.run_service_self_test(report, data_dir=data, model_dir=model) == 1
    proof = json.loads(report.read_text(encoding='utf-8'))
    assert factories == [(data / 'profile', model, False)]
    assert delegated == [(model, None, preparer)]
    assert proof['model_constructions'] == 1 and proof['runtime_starts'] == 0
    assert proof['ok'] is False and proof['error_type'] == 'ProbeStopped'
    assert proof['phase'] == 'owner_start'
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''
