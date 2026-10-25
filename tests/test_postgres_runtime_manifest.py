"""Runtime integrity must fail closed before any executable is invoked."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from unittest.mock import patch

import pytest


def pe_bytes(machine=0x8664):
    data = bytearray(128)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 64)
    data[64:68] = b'PE\0\0'
    struct.pack_into('<H', data, 68, machine)
    return bytes(data)


def make_runtime(root):
    names = ['bin/postgres.exe', 'bin/initdb.exe', 'bin/pg_ctl.exe', 'bin/psql.exe',
             'bin/pg_dump.exe', 'bin/pg_restore.exe',
             'bin/libpq.dll', 'lib/vector.dll', 'share/extension/vector.control',
             'share/extension/vector--0.8.7.sql', 'licenses/postgresql.txt',
             'licenses/pgvector.txt', 'licenses/third-party.txt',
             'bin/icudt77.dll', 'bin/icuin77.dll', 'bin/icuuc77.dll',
             'bin/libcrypto-3-x64.dll', 'bin/libiconv-2.dll', 'bin/libintl-9.dll',
             'bin/liblz4.dll', 'bin/libssl-3-x64.dll', 'bin/libwinpthread-1.dll',
             'bin/libxml2.dll', 'bin/libzstd.dll', 'bin/msvcp140.dll',
             'bin/vcruntime140.dll', 'bin/vcruntime140_1.dll', 'bin/zlib1.dll',
             'licenses/MSVC-runtime-license.html', 'licenses/MSVC-redistribution.html']
    files = []
    for name in names:
        data = pe_bytes() if name.endswith(('.exe', '.dll')) else b'license or SQL fixture'
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append(dict(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
    manifest = dict(schema_version=1, postgresql_version='18.6', pgvector_version='0.8.7',
                    architecture='x64', files=files)
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


def module():
    # A missing implementation is a clear assertion failure in the initial RED run.
    assert (Path(__file__).resolve().parents[1] / 'src/atom_search/runtime/manifest.py').is_file(), 'Runtime verifier is missing'
    return importlib.import_module('atom_search.runtime.manifest')


def test_git_checkout_preserves_pinned_postgresql_metadata_bytes(tmp_path):
    """A fresh source checkout must retain the prepared runtime's exact bytes."""
    source = Path(__file__).resolve().parents[1]
    names = ('manifest.json', 'provenance.json', 'wheels.lock.txt')
    relative = ['resources/postgresql/' + name for name in names]
    original = {name: (source / 'resources/postgresql' / name).read_bytes()
                for name in names}
    manifest = json.loads(original['manifest.json'])
    pins = {item['path']: item for item in manifest['files']}
    for name in names[1:]:
        assert len(original[name]) == pins[name]['size']
        assert hashlib.sha256(original[name]).hexdigest() == pins[name]['sha256']

    repository = tmp_path / 'repository'
    repository.mkdir()
    empty = tmp_path / 'git-empty'
    empty.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(empty / 'config'),
                       GIT_ATTR_NOSYSTEM='1')

    def git(*arguments):
        result = subprocess.run(
            ['git', '-c', 'core.autocrlf=true', '-c', 'core.safecrlf=false',
             '-c', 'core.attributesFile=' + str(empty / 'attributes'),
             '-c', 'core.hooksPath=' + str(empty),
             '-c', 'user.name=Packaging QA', '-c', 'user.email=qa@example.invalid',
             *arguments], cwd=repository, env=environment, capture_output=True,
            text=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert result.returncode == 0, result.stderr
        return result.stdout

    (repository / '.gitattributes').write_bytes((source / '.gitattributes').read_bytes())
    for name, path in zip(names, relative):
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original[name])
    git('init', '--quiet', '--template=' + str(empty))
    git('add', '--', '.gitattributes', *relative)
    git('commit', '--quiet', '-m', 'Prepared PostgreSQL metadata fixture')
    for path in relative:
        (repository / path).unlink()
    git('checkout', '--force', 'HEAD', '--', *relative)

    changed = [name for name, path in zip(names, relative)
               if (repository / path).read_bytes() != original[name]]
    assert not changed, 'Git checkout changed prepared metadata bytes: ' + ', '.join(changed)
    assert git('status', '--porcelain') == ''


def test_runtime_rejects_changed_dll_before_execution(tmp_path):
    make_runtime(tmp_path)
    (tmp_path / 'bin/libpq.dll').write_bytes(b'X' * 128)
    api = module()
    with patch.object(subprocess, 'run') as run, patch.object(subprocess, 'Popen') as popen:
        with pytest.raises(api.RuntimeValidationError, match='SHA-256'):
            api.verify_runtime(tmp_path)
        run.assert_not_called()
        popen.assert_not_called()


@pytest.mark.parametrize("name", ["bin/pg_dump.exe", "bin/pg_restore.exe"])
def test_backup_tools_must_be_manifest_listed_and_present(tmp_path, name):
    manifest = make_runtime(tmp_path)
    (tmp_path / name).unlink(missing_ok=True)
    manifest["files"] = [item for item in manifest["files"] if item["path"] != name]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(module().RuntimeValidationError, match="[Rr]equired"):
        module().verify_runtime(tmp_path)


@pytest.mark.parametrize('name', ['share/extension/vector.control', 'share/extension/vector--0.8.7.sql'])
def test_runtime_rejects_missing_vector_control_or_sql(tmp_path, name):
    manifest = make_runtime(tmp_path)
    (tmp_path / name).unlink()
    manifest['files'] = [item for item in manifest['files'] if item['path'] != name]
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    api = module()
    with patch.object(subprocess, 'run') as run:
        with pytest.raises(api.RuntimeValidationError, match='[Rr]equired'):
            api.verify_runtime(tmp_path)
        run.assert_not_called()


@pytest.mark.parametrize('mutation', ['escape', 'x86', 'pe_x86', 'absolute', 'backslash', 'duplicate', 'dot_component'])
def test_runtime_rejects_path_escape_and_wrong_architecture(tmp_path, mutation):
    manifest = make_runtime(tmp_path)
    if mutation == 'escape':
        manifest['files'][0]['path'] = '../outside.exe'
    elif mutation == 'absolute':
        manifest['files'][0]['path'] = 'C:/outside.exe'
    elif mutation == 'backslash':
        manifest['files'][0]['path'] = 'bin\\postgres.exe'
    elif mutation == 'duplicate':
        manifest['files'].append(manifest['files'][0])
    elif mutation == 'x86':
        manifest['architecture'] = 'x86'
    elif mutation == 'pe_x86':
        data = pe_bytes(0x14c)
        (tmp_path / 'bin/postgres.exe').write_bytes(data)
        manifest['files'][0].update(sha256=hashlib.sha256(data).hexdigest())
    else:
        manifest['files'][0]['path'] = 'bin/./postgres.exe'
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    api = module()
    with patch.object(subprocess, 'run') as run:
        with pytest.raises(api.RuntimeValidationError):
            api.verify_runtime(tmp_path)
        run.assert_not_called()


def test_runtime_accepts_complete_hash_verified_x64_payload(tmp_path):
    make_runtime(tmp_path)
    result = module().verify_runtime(tmp_path)
    assert result.postgres_version == '18.6'
    assert result.vector_version == '0.8.7'
    assert result.directory == tmp_path.resolve()
    assert all(isinstance(item, module().RuntimeAsset) for item in result.files)
    assert set(result.licenses) == {'licenses/postgresql.txt', 'licenses/pgvector.txt',
                                    'licenses/third-party.txt',
                                    'licenses/MSVC-runtime-license.html',
                                    'licenses/MSVC-redistribution.html'}


def test_public_runtime_dto_contract_and_license_integrity(tmp_path):
    make_runtime(tmp_path)
    api = module()
    asset = api.RuntimeAsset(path='bin/postgres.exe', size=128, sha256='a' * 64)
    dto = api.RuntimeManifest(postgres_version='18.6', vector_version='0.8.7',
                              architecture='x64', files=(asset,), licenses=('licenses/postgresql.txt',))
    assert dto.files[0].path == 'bin/postgres.exe'
    verified = api.verify_runtime(tmp_path)
    (tmp_path / verified.licenses[0]).write_bytes(b'changed license')
    with pytest.raises(api.RuntimeValidationError):
        api.load_runtime_manifest(tmp_path)


def test_runtime_rejects_unlisted_executable(tmp_path):
    make_runtime(tmp_path)
    (tmp_path / 'bin/surprise.dll').write_bytes(pe_bytes())
    api = module()
    with pytest.raises(api.RuntimeValidationError, match='[Uu]nlisted'):
        api.verify_runtime(tmp_path)


@pytest.mark.parametrize('name', ['bin/vcruntime140.dll', 'bin/icuin77.dll', 'bin/libssl-3-x64.dll'])
def test_runtime_rejects_removed_required_dependency_even_if_manifest_omits_it(tmp_path, name):
    manifest = make_runtime(tmp_path)
    (tmp_path / name).unlink()
    manifest['files'] = [item for item in manifest['files'] if item['path'] != name]
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    api = module()
    with pytest.raises(api.RuntimeValidationError, match='[Rr]equired'):
        api.verify_runtime(tmp_path)


def test_runtime_accepts_empty_optional_metadata_but_rejects_empty_required_file(tmp_path):
    manifest = make_runtime(tmp_path)
    empty_hash = hashlib.sha256(b'').hexdigest()
    (tmp_path / 'include/empty-marker').parent.mkdir()
    (tmp_path / 'include/empty-marker').write_bytes(b'')
    manifest['files'].append(dict(path='include/empty-marker', size=0, sha256=empty_hash))
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    api = module()
    api.verify_runtime(tmp_path)
    manifest['files'][0].update(size=0, sha256=empty_hash)
    (tmp_path / manifest['files'][0]['path']).write_bytes(b'')
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(api.RuntimeValidationError, match='[Ee]mpty'):
        api.verify_runtime(tmp_path)


def test_sql_proof_refuses_changed_payload_before_subprocess_or_profile_creation(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    make_runtime(runtime)
    (runtime / 'bin/libpq.dll').write_bytes(b'X' * 128)
    checker_path = Path(__file__).resolve().parents[1] / 'tools/check_postgres_runtime.py'
    assert checker_path.is_file(), 'SQL proof checker is missing'
    import importlib.util
    spec = importlib.util.spec_from_file_location('postgres_checker', checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    with patch.object(subprocess, 'run') as run:
        with pytest.raises(module().RuntimeValidationError, match='SHA-256'):
            checker.check_runtime(runtime, tmp_path / 'profiles')
        run.assert_not_called()
    assert not (tmp_path / 'profiles').exists()


def test_proof_tool_does_not_wait_for_inherited_child_output_pipe(tmp_path):
    from atom_search.runtime.postgres import _run_logged
    child = tmp_path / 'child-finished'
    child_code = ('import time; from pathlib import Path; time.sleep(2); '
                  f'Path({str(child)!r}).touch()')
    parent_code = ('import subprocess,sys; '
                   f'subprocess.Popen([sys.executable,"-c",{child_code!r}]); print("parent done")')
    begin = time.monotonic()
    code, output = _run_logged(Path(sys.executable), ['-c', parent_code], tmp_path,
                               dict(__import__('os').environ), timeout=1)
    assert code == 0
    assert output.read_text().strip() == 'parent done'
    assert time.monotonic() - begin < 1.5
    deadline = time.monotonic() + 4
    while not child.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child.exists()


def test_host_startup_sanitizes_only_its_owned_process_environment(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv('PGSERVICE', 'unrelated-service')
    monkeypatch.setenv('PGOPTIONS', '-c statement_timeout=123')
    program = ("from atom_search.runtime.postgres import sanitize_host_environment; "
               "import os; sanitize_host_environment(); "
               "assert not any(k.upper().startswith('PG') for k in os.environ)")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-c', program], env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert os.environ['PGSERVICE'] == 'unrelated-service'
    assert os.environ['PGOPTIONS'] == '-c statement_timeout=123'


@pytest.mark.parametrize('redirect_profile', [False, True], ids=['invalid-owner', 'junction'])
@pytest.mark.parametrize('initdb_fails', [False, True], ids=['post-initdb', 'initdb-error'])
def test_proof_never_deletes_or_writes_after_profile_ownership_changes(
        tmp_path, monkeypatch, redirect_profile, initdb_fails):
    """Retargeted Task2 regression: shared supervisor guards every post-tool write."""
    import _winapi
    import uuid
    from atom_search.runtime.postgres import PostgresRuntime
    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir()
    make_runtime(runtime_dir)
    runtime = PostgresRuntime(runtime_dir, tmp_path / 'private')
    external = tmp_path / 'outside-owned-boundary'
    external.mkdir()
    (external / 'data').mkdir()
    changed = None
    unowned = None
    password_name = None
    unowned_before = None

    def native_tool(program, arguments, **kwargs):
        nonlocal changed, unowned, password_name, unowned_before
        assert program == 'initdb.exe', 'Unexpected server start after ownership change'
        changed = runtime.cluster_dir
        password_name = Path(arguments[arguments.index('--pwfile') + 1]).name
        record = json.loads((changed / 'owner.json').read_text())
        record['installation_id'] = str(uuid.uuid4())
        if redirect_profile:
            changed.rename(changed.parent / 'original-owned-cluster')
            _winapi.CreateJunction(str(external), str(changed))
            unowned = external
        else:
            unowned = changed
            (unowned / 'data').mkdir()
        (unowned / 'owner.json').write_text(json.dumps(record))
        (unowned / password_name).write_bytes(b'unowned secret sentinel')
        unowned_before = {str(path.relative_to(unowned)): path.read_bytes()
                          for path in unowned.rglob('*') if path.is_file()}
        if initdb_fails:
            raise RuntimeError('Simulated initdb failure after replacement')
        return 0

    monkeypatch.setattr(runtime, '_run', native_tool)
    try:
        with pytest.raises(RuntimeError):
            runtime.start()
        assert unowned is not None
        assert (unowned / password_name).read_bytes() == b'unowned secret sentinel'
        assert json.loads((unowned / 'owner.json').read_text())['installation_id'] != runtime._record['installation_id']
        assert not (unowned / 'proof-error.txt').exists()
        assert not any((unowned / 'data').iterdir())
        assert {str(path.relative_to(unowned)): path.read_bytes()
                for path in unowned.rglob('*') if path.is_file()} == unowned_before
    finally:
        if changed is not None and changed.is_junction():
            changed.rmdir()
