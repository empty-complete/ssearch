"""Acquire pinned public inputs before the existing offline Windows build."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError(f'Build input path is a link: {item.name}')


def relative_name(name: str) -> str:
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or name.startswith('/') or any(ord(c) < 32 for c in name)
            or any(part in ('', '.', '..') or part.endswith((' ', '.'))
                   for part in name.split('/'))):
        raise ValueError('Unsafe runtime archive path')
    return PurePosixPath(name).as_posix()


def download(spec: dict, target: Path) -> Path:
    if (not spec['url'].startswith('https://github.com/')
            or type(spec['size']) is not int or spec['size'] <= 0
            or not re.fullmatch('[0-9a-f]{64}', spec['sha256'])):
        raise ValueError('Invalid pinned build input')
    no_links(target)
    if target.is_file() and target.stat().st_size == spec['size'] and sha256(target) == spec['sha256']:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            request = urllib.request.Request(spec['url'], headers={'User-Agent': 'ssearch-ci'})
            size = 0
            with urllib.request.urlopen(request, timeout=120) as response:
                if not response.url.startswith('https://'):
                    raise ValueError('Insecure build input redirect')
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > spec['size']:
                        raise ValueError('Build input exceeds pinned size')
                    output.write(block)
        if size != spec['size'] or sha256(temporary) != spec['sha256']:
            raise ValueError('Build input checksum mismatch')
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def restore_runtime(archive: Path, target: Path, manifest_sha256: str) -> None:
    """Check the complete archive before copying; never replace tracked inputs."""
    no_links(target)
    manifest_path = target / 'manifest.json'
    no_links(manifest_path)
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError('Tracked PostgreSQL manifest differs from the pinned input')
    manifest = json.loads(raw)
    inventory = {}
    folded_names = set()
    for record in manifest['files']:
        name = relative_name(record['path'])
        if (name == 'manifest.json' or name.casefold() in folded_names
                or type(record['size']) is not int or record['size'] < 0
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise ValueError('Invalid runtime inventory')
        inventory[name] = record
        folded_names.add(name.casefold())
    inventory['manifest.json'] = {'size': len(raw), 'sha256': manifest_sha256}
    with tempfile.TemporaryDirectory(prefix='ssearch-runtime-') as temporary:
        staged = Path(temporary)
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            names = [relative_name(entry.filename) for entry in entries]
            if len(names) != len(set(n.casefold() for n in names)) or set(names) != set(inventory):
                raise ValueError('Runtime archive inventory mismatch')
            for entry in entries:
                mode = entry.external_attr >> 16
                record = inventory[entry.filename]
                if (entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1
                        or entry.file_size != record['size']):
                    raise ValueError('Invalid runtime archive entry')
                destination = staged / entry.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as content, destination.open('xb') as output:
                    shutil.copyfileobj(content, output, 1024 * 1024)
                if sha256(destination) != record['sha256']:
                    raise ValueError('Runtime archive file checksum mismatch')
            if (staged / 'manifest.json').read_bytes() != raw:
                raise ValueError('Archive cannot replace the tracked manifest')
        # Validate every existing destination before writing any missing payload.
        for name, record in inventory.items():
            destination = target / name
            no_links(destination)
            if destination.exists() and (not destination.is_file()
                    or destination.stat().st_size != record['size']
                    or sha256(destination) != record['sha256']):
                raise ValueError(f'Existing runtime input differs: {name}')
        for name in inventory:
            destination = target / name
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (staged / name).open('rb') as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)


def run(*arguments: str | Path) -> None:
    subprocess.run([str(arg) for arg in arguments], cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='store_true', help='Prepare source tests without Inno Setup')
    args = parser.parse_args()
    config = json.loads((ROOT / 'resources/ci/build-inputs.json').read_text(encoding='utf-8'))
    if (config['schema_version'] != 1 or os.name != 'nt'
            or '.'.join(map(str, sys.version_info[:3])) != config['python_version']
            or sys.maxsize <= 2**32):
        raise ValueError('Windows x64 and the pinned CPython patch version are required')
    cache = ROOT / '.tools/ci-downloads'
    runtime = config['postgresql_runtime']
    restore_runtime(download(runtime, cache / 'postgresql-runtime.zip'),
                    ROOT / 'resources/postgresql', runtime['manifest_sha256'])
    if not args.source:
        inno = config['inno_setup']
        installer = download(inno, cache / 'innosetup.exe')
        compiler_dir = ROOT / '.tools/InnoSetup7'
        no_links(compiler_dir)
        subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                        '/CURRENTUSER', f'/DIR={compiler_dir}'], check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if sha256(compiler_dir / 'ISCC.exe') != inno['compiler_sha256']:
            raise ValueError('Installed Inno compiler checksum mismatch')
    run(sys.executable, '-m', 'venv', ROOT / '.venv')
    python = ROOT / '.venv/Scripts/python.exe'
    wheelhouse = ROOT / '.tools/wheelhouse'
    run(python, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'download',
        '--index-url', 'https://pypi.org/simple', '--only-binary=:all:',
        '--dest', wheelhouse, '-r', ROOT / 'requirements.lock.txt')
    run(python, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
        '--no-index', '--find-links', wheelhouse, '-r', ROOT / 'requirements.lock.txt')
    run(python, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
        '--no-index', '--no-deps', '--force-reinstall', '--require-hashes',
        '--find-links', wheelhouse, '-r', ROOT / 'resources/postgresql/wheels.lock.txt')
    if args.source:
        # Proof identity reads installed core metadata even with source imports.
        # PEP 660 uses the already pinned backend; no distributable is exported.
        run(python, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
            '--no-index', '--no-deps', '--no-build-isolation', '--editable',
            ROOT / 'packages/atom-search-core')
    run(python, ROOT / 'tools/fetch_model.py')
    print('Public inputs prepared; source tests can start.' if args.source else
          'Public inputs prepared; the offline build can start.')


if __name__ == '__main__':
    main()
