"""Propose pinned MCP wheels and prove their offline closure; never build an app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SDK = {
    'mcp': dict(version='2.3.0', filename='mcp-2.3.0-py3-none-any.whl', size=368664,
        sha256='dd0c44c089d16453e8ae31a3877a0054d7a2314caaa81f5e0541b9b1734b2377',
        source='https://pypi.org/pypi/mcp/2.3.0/json'),
    'mcp-types': dict(version='2.3.0', filename='mcp_types-2.3.0-py3-none-any.whl', size=69108,
        sha256='968efdbdaedfab06adae40d378a34395f1090c5921d4be3c9cde283aaf76d91d',
        source='https://pypi.org/pypi/mcp-types/2.3.0/json'),
}
SDK_SOURCE = 'https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v2.3.0/pyproject.toml'
ENVIRONMENT = dict(implementation_name='cpython', python_full_version='3.12.10',
                   sys_platform='win32', platform_machine='AMD64', os_name='nt')
AGENT_LOCK_SHA = 'bc813c42a295ca8415162e96dae39854ead335bc7d76ade5da75a46cb575c0ae'
BASE_LOCK_SHA = 'efb8d062547d38cdd58b3ed3dcfafc39a41e4013711f951aaf72075772d68335'
NAME = r'[A-Za-z0-9][A-Za-z0-9._-]*'
VERSION = r'[0-9][A-Za-z0-9.!+_-]*'


class ResolverError(RuntimeError):
    """Fixed public codes only; pip output and private paths remain private."""


def require(value, code):
    if not value:
        raise ResolverError(code)


def normalized(name):
    require(type(name) is str and re.fullmatch(NAME, name), 'agent_report_invalid')
    return re.sub('[-_.]+', '-', name).lower()


def project_constraints(raw):
    try:
        versions = {}
        for line in raw.decode('utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(f'({NAME})(?:\\[[A-Za-z0-9_,.-]+\\])?==({VERSION})', line)
            require(match is not None, 'agent_base_constraints_invalid')
            name = normalized(match[1])
            require(name not in versions, 'agent_base_constraints_invalid')
            versions[name] = match[2]
        require(bool(versions), 'agent_base_constraints_invalid')
        return ''.join(f'{name}=={version}\n' for name, version in versions.items())
    except (UnicodeError, TypeError, AttributeError):
        raise ResolverError('agent_base_constraints_invalid') from None


def checked_record(record):
    require(type(record) is dict and set(record) == {'name', 'version', 'filename', 'url', 'sha256'}, 'agent_report_invalid')
    name = normalized(record['name'])
    require(name == record['name'] and type(record['version']) is str
        and re.fullmatch(VERSION, record['version']) and type(record['sha256']) is str
        and re.fullmatch('[0-9a-f]{64}', record['sha256']), 'agent_report_invalid')
    require(type(record['url']) is str, 'agent_report_invalid')
    parsed = urlsplit(record['url'])
    require(parsed.scheme == 'https' and parsed.netloc == 'files.pythonhosted.org'
        and parsed.path.startswith('/packages/') and not parsed.query and not parsed.fragment
        and all(part not in ('', '.', '..') for part in parsed.path.split('/')[1:]), 'agent_report_invalid')
    filename = record['filename']
    require(type(filename) is str and re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', filename)
        and parsed.path.rsplit('/', 1)[-1] == filename, 'agent_report_invalid')
    parts = filename[:-4].split('-')
    require(len(parts) in (5, 6) and normalized(parts[0]) == name and parts[1] == record['version']
        and parts[-1] in ('any', 'win_amd64'), 'agent_report_invalid')
    return record


def select_closure(report, base):
    require(type(report) is dict and report.get('version') == '1'
        and type(report.get('pip_version')) is str and re.fullmatch(VERSION, report['pip_version'])
        and type(report.get('environment')) is dict
        and all(report['environment'].get(key) == value for key, value in ENVIRONMENT.items())
        and type(report.get('install')) is list and 0 < len(report['install']) <= 128, 'agent_report_invalid')
    constraints = dict(line.split('==') for line in project_constraints(base).splitlines())
    records = []
    names = set()
    for item in report['install']:
        require(type(item) is dict and type(item.get('metadata')) is dict
            and type(item.get('download_info')) is dict and item.get('is_direct') is False, 'agent_report_invalid')
        metadata, download = item['metadata'], item['download_info']
        name, version = normalized(metadata.get('name')), metadata.get('version')
        require(type(version) is str and re.fullmatch(VERSION, version) and name not in names, 'agent_report_invalid')
        names.add(name)
        require(name not in constraints or version == constraints[name], 'agent_base_version_conflict')
        require(type(download.get('archive_info')) is dict
            and type(download['archive_info'].get('hashes')) is dict and type(download.get('url')) is str, 'agent_report_invalid')
        digest = download['archive_info']['hashes'].get('sha256')
        filename = urlsplit(download['url']).path.rsplit('/', 1)[-1]
        if name in SDK:
            require(version == SDK[name]['version'] and filename == SDK[name]['filename']
                    and digest == SDK[name]['sha256'], 'agent_sdk_root_mismatch')
        records.append(checked_record(dict(name=name, version=version, filename=filename,
                                           url=download['url'], sha256=digest)))
    require(set(SDK).issubset(names), 'agent_sdk_root_mismatch')
    return sorted(records, key=lambda record: record['name'])


def proposal_lock(records):
    require(type(records) is list and records and len({item['name'] for item in records}) == len(records), 'agent_report_invalid')
    return ''.join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
                   for item in sorted(map(checked_record, records), key=lambda record: record['name']))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def no_links(path):
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink() and not (getattr(item.lstat(), 'st_file_attributes', 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT), 'agent_wheelhouse_invalid')


def verify_wheelhouse(records, directory):
    no_links(directory)
    require(directory.is_dir() and {item.name for item in directory.iterdir()} == {item['filename'] for item in records},
            'agent_wheelhouse_invalid')
    inventory = []
    for record in records:
        checked_record(record)
        path = directory / record['filename']
        no_links(path)
        require(path.is_file() and 0 < path.stat().st_size <= 128 * 1024 * 1024
                and digest(path) == record['sha256'], 'agent_wheelhouse_invalid')
        if record['name'] in SDK:
            require(path.stat().st_size == SDK[record['name']]['size'], 'agent_wheelhouse_invalid')
        inventory.append(dict(filename=path.name, size=path.stat().st_size, sha256=record['sha256']))
    return inventory


def run(arguments, code):
    try:
        result = subprocess.run(list(map(str, arguments)), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, cwd=ROOT)
        require(result.returncode == 0, code)
    except (OSError, subprocess.TimeoutExpired):
        raise ResolverError(code) from None


def install_locked(python, cache, lock, *, force_reinstall=False):
    lines = lock.read_text(encoding='utf-8').splitlines()
    require(lines and all(re.fullmatch(f'{NAME}=={VERSION} --hash=sha256:[0-9a-f]{{64}}', line)
                          for line in lines), 'agent_report_invalid')
    arguments = [python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
        '--no-index', '--no-deps', '--require-hashes', '--find-links', cache, '-r', lock]
    if force_reinstall:
        arguments.append('--force-reinstall')
    run(arguments, 'agent_offline_install_failed')
    run([python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check'], 'agent_offline_check_failed')


def verify_source(source):
    for arguments, expected in ((['rev-parse', 'HEAD'], source), (['status', '--porcelain', '--untracked-files=all'], '')):
        result = subprocess.run(['git', '-C', str(ROOT), *arguments], capture_output=True, text=True, timeout=30)
        require(result.returncode == 0 and result.stdout.strip() == expected, 'agent_source_invalid')


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def validate_source_lock(lock, base):
    require(hashlib.sha256(base).hexdigest() == BASE_LOCK_SHA, 'agent_source_base_mismatch')
    require(hashlib.sha256(lock).hexdigest() == AGENT_LOCK_SHA, 'agent_source_lock_mismatch')
    records = []
    for line in lock.decode('utf-8').splitlines():
        match = re.fullmatch(f'({NAME})==({VERSION}) --hash=sha256:([0-9a-f]{{64}})', line)
        require(match is not None, 'agent_source_lock_mismatch')
        records.append(dict(name=normalized(match[1]), version=match[2], sha256=match[3]))
    selected = {item['name']: item for item in records}
    require(len(records) == len(selected) == 29 and all(name in selected
        and selected[name]['version'] == pin['version'] and selected[name]['sha256'] == pin['sha256']
        for name, pin in SDK.items()), 'agent_source_lock_mismatch')
    return records


def verify_versions(actual, expected):
    require(type(actual) is dict and all(actual.get(name) == version for name, version in expected.items()),
            'agent_source_environment_mismatch')


def installed_versions(python, expected):
    code = "import importlib.metadata as m,json,sys; print(json.dumps({name:m.version(name) for name in json.loads(sys.argv[1])}))"
    result = subprocess.run([str(python), '-I', '-c', code, json.dumps(sorted(expected))],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0 and len(result.stdout) <= 65536, 'agent_source_environment_mismatch')
    try:
        actual = json.loads(result.stdout)
    except ValueError:
        raise ResolverError('agent_source_environment_mismatch') from None
    verify_versions(actual, expected)


def source_identity(source):
    verify_source(source)
    lock_path, base_path = ROOT / 'requirements-agent.lock.txt', ROOT / 'requirements.lock.txt'
    no_links(lock_path)
    no_links(base_path)
    base = base_path.read_bytes()
    records = validate_source_lock(lock_path.read_bytes(), base)
    python = ROOT / '.venv/Scripts/python.exe'
    require(os.path.normcase(str(Path(sys.executable).absolute())) == os.path.normcase(str(python.absolute())),
            'agent_source_interpreter_mismatch')
    expected_base = dict(line.split('==') for line in project_constraints(base).splitlines())
    installed_versions(python, expected_base)
    return python, lock_path, records, expected_base


def verify_source_cache(records, cache):
    no_links(cache)
    require(cache.is_dir(), 'agent_wheelhouse_invalid')
    selected = {record['name']: record for record in records}
    found = set()
    for path in cache.iterdir():
        no_links(path)
        require(path.is_file() and re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', path.name), 'agent_wheelhouse_invalid')
        parts = path.name[:-4].split('-')
        require(len(parts) in (5, 6), 'agent_wheelhouse_invalid')
        name = normalized(parts[0])
        require(name in selected and name not in found and parts[1] == selected[name]['version']
            and parts[-1] in ('any', 'win_amd64') and 0 < path.stat().st_size <= 128 * 1024 * 1024
            and digest(path) == selected[name]['sha256'], 'agent_wheelhouse_invalid')
        if name in SDK:
            require(path.name == SDK[name]['filename'] and path.stat().st_size == SDK[name]['size'], 'agent_wheelhouse_invalid')
        found.add(name)
    require(found == set(selected), 'agent_wheelhouse_invalid')


def source_sdk_identity(python, records):
    installed_versions(python, {item['name']: item['version'] for item in records})
    code = ("import importlib.metadata as m,pathlib,sys,mcp; from mcp import Client; "
            "assert pathlib.Path(mcp.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()); "
            "assert m.version('mcp')=='2.3.0'; assert m.version('mcp-types')=='2.3.0'")
    run([python, '-I', '-c', code], 'agent_sdk_import_failed')


def prepare_source(source):
    python, lock, records, base = source_identity(source)
    cache = ROOT / '.tools/agent-source' / source
    output = ROOT / 'artifacts/ci-agent-source' / source
    for path in (cache, output):
        no_links(path)
        require(not path.exists(), 'agent_output_not_fresh')
        path.mkdir(parents=True)
    run([python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'download',
        '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--no-deps', '--require-hashes',
        '--dest', cache, '-r', lock], 'agent_wheel_download_failed')
    verify_source_cache(records, cache)
    install_locked(python, cache, lock, force_reinstall=True)
    installed_versions(python, base)
    source_sdk_identity(python, records)
    source_identity(source)
    write_json(output / 'input-receipt.json', dict(schema_version=1, source_commit=source,
        agent_lock_sha256=AGENT_LOCK_SHA, base_lock_sha256=BASE_LOCK_SHA,
        sdk_versions={name: pin['version'] for name, pin in SDK.items()},
        base_versions_preserved=True, closure_reinstalled=True, no_index=True, no_dependency_resolution=True,
        pip_check=True, prepared=True, verified_before_tests=False, full_suite_passed=False, windows_validation=[]))
    verify_source(source)


def verify_prepared_source(source):
    path = ROOT / 'artifacts/ci-agent-source' / source / 'input-receipt.json'
    try:
        no_links(path)
        require(path.is_file() and path.stat().st_size <= 65536, 'agent_source_receipt_invalid')
        receipt = json.loads(path.read_text(encoding='utf-8'))
        expected = dict(schema_version=1, source_commit=source, agent_lock_sha256=AGENT_LOCK_SHA,
            base_lock_sha256=BASE_LOCK_SHA, sdk_versions={name: pin['version'] for name, pin in SDK.items()},
            base_versions_preserved=True, closure_reinstalled=True, no_index=True, no_dependency_resolution=True,
            pip_check=True, prepared=True, verified_before_tests=False, full_suite_passed=False, windows_validation=[])
        require(type(receipt) is dict and set(receipt) == set(expected)
            and type(receipt.get('verified_before_tests')) is bool, 'agent_source_receipt_invalid')
        expected['verified_before_tests'] = receipt['verified_before_tests']
        require(all(type(receipt[key]) is type(value) and receipt[key] == value for key, value in expected.items()),
                'agent_source_receipt_invalid')
    except (OSError, ValueError):
        raise ResolverError('agent_source_receipt_invalid') from None
    python, _, records, _ = source_identity(source)
    source_sdk_identity(python, records)
    receipt['verified_before_tests'] = True
    temporary = path.with_name('verified-input.next.json')
    write_json(temporary, receipt)
    os.replace(temporary, path)
    verify_source(source)
    print('CI agent SDK: source_venv=true approved_lock=true base_versions=true sdk_identity=true verified_before_tests=true')


def resolve(source):
    require(os.name == 'nt' and sys.maxsize > 2**32 and sys.version_info[:3] == (3, 12, 10)
        and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and os.environ.get('GITHUB_SHA') == source, 'agent_platform_invalid')
    verify_source(source)
    base_path = ROOT / 'requirements.lock.txt'
    base = base_path.read_bytes()
    constraints = project_constraints(base)
    work = ROOT / '.tools/agent-resolver' / source
    output = ROOT / 'artifacts/ci-agent-resolver' / source
    for path in (work, output):
        no_links(path)
        require(not path.exists(), 'agent_output_not_fresh')
        path.mkdir(parents=True)
    constraint_path = work / 'base-constraints.txt'
    constraint_path.write_text(constraints, encoding='utf-8', newline='\n')
    raw_report = work / 'pip-report.json'
    run([sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
        '--dry-run', '--ignore-installed', '--only-binary=:all:', '--index-url', 'https://pypi.org/simple',
        '--constraint', constraint_path, '--report', raw_report, 'mcp==2.3.0', 'mcp-types==2.3.0'], 'agent_resolution_failed')
    require(raw_report.stat().st_size <= 8 * 1024 * 1024, 'agent_report_invalid')
    report = json.loads(raw_report.read_text(encoding='utf-8'))
    records = select_closure(report, base)
    lock = output / 'agent-requirements.proposal.txt'
    lock.write_text(proposal_lock(records), encoding='utf-8', newline='\n')
    cache = work / 'wheelhouse'
    cache.mkdir()
    run([sys.executable, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'download',
        '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--no-deps', '--require-hashes',
        '--dest', cache, '-r', lock], 'agent_wheel_download_failed')
    inventory = verify_wheelhouse(records, cache)
    environment = work / 'offline'
    run([sys.executable, '-I', '-m', 'venv', environment], 'agent_offline_venv_failed')
    python = environment / 'Scripts/python.exe'
    install_locked(python, cache, lock)
    tool = subprocess.run([str(python), '-I', '-c', "import importlib.metadata; print(importlib.metadata.version('pip'))"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    offline_pip = tool.stdout.strip()
    require(tool.returncode == 0 and re.fullmatch(VERSION, offline_pip), 'agent_offline_check_failed')
    run([python, '-I', '-c', "import importlib.metadata as m; import mcp; from mcp import Client; "
        "assert m.version('mcp') == '2.3.0'; assert m.version('mcp-types') == '2.3.0'"], 'agent_sdk_import_failed')
    require(base_path.read_bytes() == base, 'agent_base_changed')
    verify_source(source)
    write_json(output / 'resolver-report.json', dict(schema_version=1, source_commit=source,
        environment=ENVIRONMENT, pip_version=report['pip_version'], selected=records))
    write_json(output / 'offline-receipt.json', dict(schema_version=1, source_commit=source, ok=True,
        wheel_hashes_verified=True, no_index=True, no_dependency_resolution=True, pip_check=True,
        sdk_import=True, sdk_versions={name: item['version'] for name, item in SDK.items()},
        full_suite_passed=False, application_tests_run=False, packaging_run=False, windows_validation=[]))
    write_json(output / 'provenance.json', dict(schema_version=1, source_commit=source,
        helper_sha256=digest(Path(__file__)), base_lock_sha256=hashlib.sha256(base).hexdigest(),
        constraints_sha256=digest(constraint_path), proposal_sha256=digest(lock),
        resolver_report_sha256=digest(output / 'resolver-report.json'),
        offline_receipt_sha256=digest(output / 'offline-receipt.json'),
        environment=ENVIRONMENT, pip_version=report['pip_version'], offline_pip_version=offline_pip,
        sdk_sources=dict(pyproject=SDK_SOURCE, distributions=SDK), wheels=inventory))
    verify_source(source)
    print(json.dumps(dict(ok=True, source_commit=source, closure_count=len(records), full_suite_passed=False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--prepare-source', action='store_true')
    parser.add_argument('--verify-source', action='store_true')
    args = parser.parse_args()
    try:
        require(re.fullmatch('[0-9a-f]{40}', args.source_commit), 'agent_source_invalid')
        if args.prepare_source or args.verify_source:
            require(not (args.prepare_source and args.verify_source) and os.name == 'nt'
                and sys.version_info[:3] == (3, 12, 10) and sys.maxsize > 2**32
                and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('BUILD_MODE') == 'source'
                and os.environ.get('AGENT_SDK') == 'true' and os.environ.get('DESKTOP_STACK') == 'false'
                and os.environ.get('SOURCE_SHA') == args.source_commit, 'agent_source_scope_invalid')
            (prepare_source if args.prepare_source else verify_prepared_source)(args.source_commit)
        else:
            resolve(args.source_commit)
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, ResolverError) else 'agent_resolver_failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
