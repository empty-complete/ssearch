"""Resolver input/closure and real offline-wheel contracts; no app imports."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROOTS = (
    ('mcp', 'mcp-2.3.0-py3-none-any.whl', 'dd0c44c089d16453e8ae31a3877a0054d7a2314caaa81f5e0541b9b1734b2377'),
    ('mcp-types', 'mcp_types-2.3.0-py3-none-any.whl', '968efdbdaedfab06adae40d378a34395f1090c5921d4be3c9cde283aaf76d91d'),
)


@pytest.fixture
def helper():
    path = ROOT / 'tools/ci_agent_dependencies.py'
    if not path.is_file():
        class MissingContract:
            class ResolverError(RuntimeError):
                pass

            def __getattr__(self, name):
                def missing(*args, **kwargs):
                    pytest.fail('agent_dependency_resolver_contract_missing', pytrace=False)
                return missing
        return MissingContract()
    spec = importlib.util.spec_from_file_location('ssearch_agent_resolver_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selection(name, version, filename, digest, requested=False):
    return dict(metadata=dict(name=name, version=version), requested=requested, is_direct=False,
        download_info=dict(url='https://files.pythonhosted.org/packages/aa/bb/' + filename,
                           archive_info=dict(hashes=dict(sha256=digest))))


def report():
    return dict(version='1', pip_version='25.0.1', environment=dict(implementation_name='cpython',
        python_full_version='3.12.10', sys_platform='win32', platform_machine='AMD64', os_name='nt'),
        install=[selection(name, '2.3.0', filename, digest, True) for name, filename, digest in ROOTS]
        + [selection('idna', '3.20', 'idna-3.20-py3-none-any.whl', 'a' * 64)])


def test_constraints_projection_removes_only_extras_and_keeps_exact_pins(helper):
    original = b'# public base\npsycopg[binary]==3.3.6\npsycopg-binary==3.3.6\nTyping_Extensions==4.16.0\n'
    assert helper.project_constraints(original) == 'psycopg==3.3.6\npsycopg-binary==3.3.6\ntyping-extensions==4.16.0\n'
    assert original.startswith(b'# public base\npsycopg[binary]')


@pytest.mark.parametrize('raw', [b'pkg>=1\n', b'pkg @ https://private.invalid/x\n',
    b'--extra-index-url https://private.invalid\n', b'pkg==1\npkg==2\n', b'pkg_name==1\npkg-name==1\n',
    b'pkg==1; python_version>"3"\n'])
def test_unrepresentable_base_constraints_fail_without_silent_relaxation(helper, raw):
    with pytest.raises(helper.ResolverError, match='^agent_base_constraints_invalid$'):
        helper.project_constraints(raw)


def test_selected_roots_and_complete_candidate_lock_are_public_hash_only_inputs(helper):
    records = helper.select_closure(report(), b'idna==3.20\n')
    assert {item['name'] for item in records} == {'mcp', 'mcp-types', 'idna'}
    assert all(set(item) == {'name', 'version', 'filename', 'url', 'sha256'} for item in records)
    lock = helper.proposal_lock(records)
    assert lock == ('idna==3.20 --hash=sha256:' + 'a' * 64 + '\n'
        'mcp==2.3.0 --hash=sha256:' + ROOTS[0][2] + '\n'
        'mcp-types==2.3.0 --hash=sha256:' + ROOTS[1][2] + '\n')
    assert 'https:' not in lock and '--index' not in lock


@pytest.mark.parametrize('mutation,code', [
    ('root_missing', 'agent_sdk_root_mismatch'), ('root_hash', 'agent_sdk_root_mismatch'),
    ('root_version', 'agent_sdk_root_mismatch'), ('root_filename', 'agent_sdk_root_mismatch'),
    ('base_conflict', 'agent_base_version_conflict'), ('duplicate', 'agent_report_invalid'),
    ('sdist', 'agent_report_invalid'), ('foreign_url', 'agent_report_invalid'),
    ('url_userinfo', 'agent_report_invalid'), ('url_query', 'agent_report_invalid'),
    ('name_injection', 'agent_report_invalid'), ('version_injection', 'agent_report_invalid'),
    ('bad_digest', 'agent_report_invalid'), ('foreign_interpreter', 'agent_report_invalid'),
])
def test_resolution_cannot_substitute_roots_platform_or_unhashed_inputs(helper, mutation, code):
    value = report()
    if mutation == 'root_missing':
        value['install'].pop(0)
    elif mutation == 'root_hash':
        value['install'][0]['download_info']['archive_info']['hashes']['sha256'] = 'b' * 64
    elif mutation == 'root_version':
        value['install'][0]['metadata']['version'] = '2.3.1'
    elif mutation == 'root_filename':
        value['install'][0]['download_info']['url'] = 'https://files.pythonhosted.org/packages/other.whl'
    elif mutation == 'base_conflict':
        value['install'][2]['metadata']['version'] = '3.19'
    elif mutation == 'duplicate':
        value['install'].append(copy.deepcopy(value['install'][2]))
    elif mutation == 'foreign_interpreter':
        value['environment']['python_full_version'] = '3.12.9'
    else:
        item = value['install'][2]
        if mutation == 'sdist':
            item['download_info']['url'] = 'https://files.pythonhosted.org/packages/idna.tar.gz'
        elif mutation == 'foreign_url':
            item['download_info']['url'] = 'https://private.invalid/idna-3.20-py3-none-any.whl'
        elif mutation == 'url_userinfo':
            item['download_info']['url'] = 'https://PRIVATE_SENTINEL@files.pythonhosted.org/packages/idna-3.20-py3-none-any.whl'
        elif mutation == 'url_query':
            item['download_info']['url'] += '?token=PRIVATE_SENTINEL'
        elif mutation == 'name_injection':
            item['metadata']['name'] = 'idna\n--index-url PRIVATE_SENTINEL'
        elif mutation == 'version_injection':
            item['metadata']['version'] = '3.20 @ PRIVATE_SENTINEL'
        else:
            item['download_info']['archive_info']['hashes']['sha256'] = 'PRIVATE_SENTINEL'
    with pytest.raises(helper.ResolverError, match='^' + code + '$'):
        helper.select_closure(value, b'idna==3.20\n')


def toy_wheel(directory, missing_dependency=False):
    filename = 'ssearch_resolver_probe-1.0.0-py3-none-any.whl'
    path = directory / filename
    metadata = 'Metadata-Version: 2.1\nName: ssearch-resolver-probe\nVersion: 1.0.0\n'
    if missing_dependency == 'direct':
        metadata += 'Requires-Dist: ssearch-absent-probe @ file:///Z:/ssearch-missing/PRIVATE_SENTINEL.whl\n'
    elif missing_dependency:
        metadata += 'Requires-Dist: ssearch-absent-probe==1.0.0\n'
    prefix = 'ssearch_resolver_probe-1.0.0.dist-info/'
    with zipfile.ZipFile(path, 'w') as wheel:
        wheel.writestr('ssearch_resolver_probe.py', 'VALUE = 42\n')
        wheel.writestr(prefix + 'METADATA', metadata + '\n')
        wheel.writestr(prefix + 'WHEEL', 'Wheel-Version: 1.0\nGenerator: ssearch-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        wheel.writestr(prefix + 'RECORD', '')
    return dict(name='ssearch-resolver-probe', version='1.0.0', filename=filename,
        url='https://files.pythonhosted.org/packages/' + filename,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('mutation', ['missing', 'corrupt', 'extra'])
def test_missing_corrupt_or_foreign_wheelhouse_cannot_be_admitted(helper, tmp_path, mutation):
    cache = tmp_path / 'cache'
    cache.mkdir()
    record = toy_wheel(cache)
    if mutation == 'missing':
        (cache / record['filename']).unlink()
    elif mutation == 'corrupt':
        (cache / record['filename']).write_bytes(b'PRIVATE_SENTINEL')
    else:
        (cache / 'foreign.whl').write_bytes(b'PRIVATE_SENTINEL')
    with pytest.raises(helper.ResolverError, match='^agent_wheelhouse_invalid$'):
        helper.verify_wheelhouse([record], cache)


@pytest.mark.parametrize('missing_dependency', [False, True, 'direct'])
def test_real_offline_wheel_install_checks_dependency_closure(helper, tmp_path, missing_dependency):
    cache = tmp_path / 'cache'
    cache.mkdir()
    record = toy_wheel(cache, missing_dependency)
    helper.verify_wheelhouse([record], cache)
    lock = tmp_path / 'candidate.lock'
    lock.write_text(helper.proposal_lock([record]), encoding='utf-8')
    environment = tmp_path / 'isolated'
    created = subprocess.run([sys.executable, '-I', '-m', 'venv', str(environment)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
    clean_creation = created.returncode == 0
    assert clean_creation, 'toy_venv_creation_failed'
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if missing_dependency:
        with pytest.raises(helper.ResolverError, match='^agent_offline_check_failed$'):
            helper.install_locked(python, cache, lock)
    else:
        helper.install_locked(python, cache, lock)
        checked = subprocess.run([str(python), '-I', '-c', 'import ssearch_resolver_probe; assert ssearch_resolver_probe.VALUE == 42'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        clean_import = checked.returncode == 0
        assert clean_import, 'offline_toy_import_failed'
