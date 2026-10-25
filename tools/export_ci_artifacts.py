"""Export only the installer and small receipts, never runtime QA profiles."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Oversized build receipt')
    return json.loads(path.read_text(encoding='utf-8'))


def export_artifacts(root: Path, commit: str, *, quick: bool = False) -> Path:
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('A full source commit is required')
    base = root / 'artifacts' / ('development-quick' if quick else 'development') / commit
    manifest_path = base / 'build-manifest.json'
    manifest = read_json(manifest_path)
    scope = 'targeted' if quick else 'full'
    if (manifest.get('source_commit') != commit or manifest.get('test_scope') != scope
            or manifest.get('full_suite_passed') is not (not quick)
            or not re.fullmatch(r'\d+\.\d+\.\d+', manifest.get('app_version', ''))):
        raise ValueError('Build manifest does not match the requested source and scope')
    installer_name = f"ssearch-Setup-{manifest['app_version']}.exe"
    installer = base / 'artifacts' / installer_name
    if (manifest['installer']['path'] != f'artifacts/{installer_name}'
            or installer.stat().st_size != manifest['installer']['size']
            or digest(installer) != manifest['installer']['sha256']):
        raise ValueError('Installer does not match its build manifest')
    receipt_path = base / 'qa/test-receipt.json'
    receipt = read_json(receipt_path)
    expected = {'source_commit': commit, 'scope': scope,
                'test_targets': manifest['test_targets'], 'full_suite_passed': not quick, 'exit_code': 0}
    if receipt != expected or type(receipt.get('exit_code')) is not int:
        raise ValueError('Test receipt does not match this build')
    proofs = {}
    for name, filename, checks in (
        ('application', 'frozen-self-test.json', ('model_ready', 'clean_stop', 'source_clean',
                                               'public_identity_preserved')),
        ('postgresql', 'postgres-runtime-proof.json', ('scram', 'wrong_password_rejected',
            'clean_stop', 'profile_removed', 'restart_verified', 'dpapi_verified',
            'private_acl_verified', 'application_privileges_verified')),
    ):
        path = base / 'qa' / filename
        proof = read_json(path)
        if (proof.get('ok') is not True or proof.get('source_commit') != commit
                or proof.get('execution_mode') != 'frozen'
                or any(proof.get(check) is not True for check in checks)):
            raise ValueError(f'Missing successful frozen {name} proof')
        # Original proof reports also contain disposable profile paths and IDs.
        proofs[name] = {'passed': True, 'execution_mode': 'frozen',
                        'original_receipt_sha256': digest(path),
                        'checks': {check: True for check in checks}}
    output = root / 'artifacts/ci-export'
    output.mkdir(parents=True, exist_ok=False)
    for source, name in ((installer, installer_name), (manifest_path, 'build-manifest.json'),
                         (receipt_path, 'test-receipt.json')):
        shutil.copyfile(source, output / name)
    evidence = {'schema_version': 1, 'source_commit': commit, 'test_scope': scope,
                'full_suite_passed': not quick, 'runner_platform': 'Windows Server 2025 x64',
                'windows_validation': [], 'release_channel': 'development-prerelease',
                'frozen_proofs': proofs}
    (output / 'ci-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    sums = ''.join(f'{digest(path)}  {path.name}\n' for path in sorted(output.iterdir()))
    (output / 'SHA256SUMS.txt').write_text(sums, encoding='utf-8')
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--quick', action='store_true')
    args = parser.parse_args()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()
    if head != args.commit or dirty:
        raise ValueError('Source changed after the offline build')
    print(export_artifacts(ROOT, args.commit, quick=args.quick))


if __name__ == '__main__':
    main()
