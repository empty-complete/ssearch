"""Frozen Task2 gate: accepted v4 application seed -> v5 context/backfill/restart."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--previous-executable', required=True, type=Path)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    old, new = args.previous_executable.resolve(), args.executable.resolve()
    directory, report = args.data_dir.absolute(), args.report.absolute()
    if (old == new or not old.is_file() or not new.is_file() or directory.exists()
            or report.exists() or report.is_relative_to(directory)):
        parser.error('different executables required; profile/report must be new and separate')
    hashes = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (old, new))
    directory.mkdir(parents=True)
    seed = directory / 'schema4-seed.json'
    result = subprocess.run([str(old), '--self-test', str(seed), '--data-dir', str(directory)],
        timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not seed.is_file():
        return 1
    value = json.loads(seed.read_bytes())
    if (value.get('ok') is not True or value.get('schema_version') != 4
            or value.get('execution_mode') != 'frozen' or value.get('clean_stop') is not True):
        return 1
    result = subprocess.run([str(new), '--service-test', str(report), '--service-test-scenario',
        'project-context', '--data-dir', str(directory)], timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not report.is_file():
        return 1
    if tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (old, new)) != hashes:
        return 1
    value = json.loads(report.read_bytes())
    flags = ('source_settings_preserved', 'public_ids_preserved', 'old_vectors_preserved',
        'same_manifest_new_input_key', 'prepared_payload_verified', 'token_budget_verified',
        'stale_vectors_ineligible', 'current_health_counts', 'queue_backfill', 'no_document_reread',
        'restart_idempotent', 'project_capabilities_ready', 'clean_stop')
    return 0 if (value.get('ok') is True and value.get('execution_mode') == 'frozen'
        and value.get('scenario') == 'project-context' and value.get('backend') == 'postgresql'
        and value.get('factory') == 'open_service_owner' and all(value.get(flag) is True for flag in flags)
        and value.get('catalog_schema') == 5 and value.get('prior_catalog_schema') == 4
        and value.get('vectors') == 3 and type(value.get('context_revision')) is int
        and value['context_revision'] >= 3
        and value.get('seed_sha256') == hashlib.sha256(seed.read_bytes()).hexdigest()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
