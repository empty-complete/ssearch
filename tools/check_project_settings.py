"""Hidden old-EXE schema1 seed → new-EXE owner migration/apply/restart proof."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--previous-executable', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    old, new, directory, report = (path.absolute() for path in
        (args.previous_executable, args.executable, args.data_dir, args.report))
    if (directory.exists() or report.exists() or report.is_relative_to(directory)
            or not old.is_file() or not new.is_file()):
        parser.error('executables required; profile/report must be new and separate')
    hashes = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (old, new))
    seed = directory / 'schema1-seed.json'
    result = subprocess.run([str(old), '--self-test', str(seed), '--data-dir', str(directory)],
                            timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not seed.is_file():
        return 1
    result = subprocess.run([str(new), '--service-test', str(report), '--service-test-scenario',
                             'project-settings', '--data-dir', str(directory)],
                            timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not report.is_file():
        return 1
    if tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (old, new)) != hashes:
        return 1
    value = json.loads(report.read_bytes())
    flags = ('migration_published_once', 'original_bytes_preserved', 'owner_apply',
        'restart_idempotent', 'source_settings_preserved', 'catalog_fingerprint_preserved',
        'public_ids_preserved', 'scoped_metadata_search', 'no_document_reread', 'project_capabilities_ready')
    return 0 if (value.get('ok') is True and value.get('execution_mode') == 'frozen'
        and value.get('scenario') == 'project-settings' and value.get('backend') == 'postgresql'
        and value.get('factory') == 'open_service_owner' and value.get('clean_stop') is True
        and all(value.get(name) is True for name in flags)
        and value.get('settings_schema') == 3 and value.get('catalog_schema') == 5
        and type(value.get('prior_revision')) is int
        and value.get('migration_revision') == value['prior_revision'] + 1
        and value.get('applied_revision') == value['migration_revision'] + 1
        and value.get('restarted_revision') == value['applied_revision']
        and value.get('seed_sha256') == hashlib.sha256(seed.read_bytes()).hexdigest()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
