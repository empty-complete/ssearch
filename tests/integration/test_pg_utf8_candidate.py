"""Optional source-only experiment; canonical/native platform gates remain open."""
import importlib.util
import json
import os
from pathlib import Path
import zipfile

import pytest

from atom_search._postgres_proof import _failure_detail
from atom_search.diagnostics import run_postgres_self_test
from atom_search.runtime import _windows
from atom_search.runtime.manifest import sha256, verify_runtime
from atom_search.runtime.postgres import PostgresRuntime


pytestmark = pytest.mark.skipif(os.environ.get('BUILD_MODE') != 'source',
                                reason='isolated source-only native manifest experiment')


def test_utf8_manifest_candidate_restores_unicode_profile_with_absolute_arguments(tmp_path, monkeypatch):
    tool = None
    try:
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('ssearch_pg_utf8_candidate', root / 'tools/pg_utf8_candidate.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        work = _windows.private_directory(tmp_path / 'ssearch-utf8-probe')
        candidate = work / 'runtime'
        audit = tool.prepare_candidate(candidate)
        for name in tool.EXECUTABLES:
            path = candidate / 'bin' / name
            before = sha256(path)
            tool.patch_private_executable(path)
            if sha256(path) != before:
                raise tool.ProbeError('candidate_not_idempotent')
        original_run = PostgresRuntime._run
        absolute_calls = []

        def absolute_initdb(instance, executable, arguments, **kwargs):
            if executable == 'initdb.exe':
                arguments = list(arguments)
                for flag in ('-D', '--pwfile'):
                    index = arguments.index(flag) + 1
                    value = Path(arguments[index])
                    arguments[index] = value if value.is_absolute() else instance.cluster_dir / value
                absolute_calls.append(all(Path(arguments[arguments.index(flag) + 1]).is_absolute()
                                          for flag in ('-D', '--pwfile')))
            return original_run(instance, executable, arguments, **kwargs)

        monkeypatch.setattr(PostgresRuntime, '_run', absolute_initdb)
        report = work / 'proof.json'
        result = run_postgres_self_test(report, data_dir=work / 'Кириллица с пробелами', runtime_dir=candidate)
        if not report.is_file():
            raise tool.ProbeError('native_utf8_report_unavailable')
        _windows.assert_private(report)
        value = json.loads(report.read_text(encoding='utf-8'))
        if result != 0:
            detail = value.get('failure_detail', {})
            pytest.fail(json.dumps({'code': 'native_unicode_proof_failed',
                                    'failure_type': detail.get('type'),
                                    'locations': detail.get('locations', [])}), pytrace=False)
        restore, backup = value.get('restore', {}), value.get('backup', {})
        flags = {name: value.get(name) is True for name in (
            'scram', 'wrong_password_rejected', 'dpapi_verified', 'private_acl_verified',
            'application_privileges_verified', 'clean_stop', 'profile_removed')}
        flags.update({name: value.get('checks', {}).get(name) == 'pass'
                      for name in ('runtime', 'source_restart', 'restore')})
        flags.update(absolute_initdb_arguments=len(absolute_calls) >= 2 and all(absolute_calls),
                     manifest_idempotent=True, rows_equal=restore.get('rows_equal') is True,
                     restore_restart=restore.get('restart_verified') is True,
                     target_identity_preserved=restore.get('target_identity_preserved') is True,
                     distinct_installations=restore.get('source_installation_id') != restore.get('target_installation_id'),
                     dimension=restore.get('dimension') == 384,
                     cosine_rows=restore.get('cosine_rows') == [[1, 0.0], [2, 1.0], [3, 2.0]],
                     logical_backup=backup.get('method') == 'logical-schema' and backup.get('bytes', 0) > 0,
                     runtime_marker_excluded=backup.get('runtime_marker_excluded') is True,
                     source_preserved=value.get('preservation', {}).get('source_verified') is True)
        if value.get('ok') is not True or not all(flags.values()):
            raise tool.ProbeError('native_utf8_proof_flags_failed')
        archive, receipt = tool.archive_candidate(candidate, audit, flags)
        expected = sorted([asset.path for asset in verify_runtime(candidate).files] + ['manifest.json'])
        with zipfile.ZipFile(archive) as package:
            if package.namelist() != expected or package.testzip() is not None:
                raise tool.ProbeError('candidate_archive_invalid')
        public = json.loads(receipt.read_text(encoding='utf-8'))
        if (public['archive_sha256'] != sha256(archive) or public['proof_flags'] != flags
                or public['windows_validation'] != [] or public['win10_1809_legacy_fallback'] != 'OPEN'):
            raise tool.ProbeError('candidate_receipt_invalid')
    except Exception as error:
        code = (error.code if tool is not None and isinstance(error, getattr(tool, 'ProbeError', ()))
                else 'native_utf8_probe_failed')
        failure = {'code': code, 'failure_detail': _failure_detail(error)}
    else:
        return
    # Raise after leaving the handler so pytest cannot render a native context.
    pytest.fail(json.dumps(failure), pytrace=False)
