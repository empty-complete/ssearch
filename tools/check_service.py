"""Run the real frozen two-client proof in a new disposable profile, hidden."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--scenario', choices=('two-clients', 'multi-source', 'health-offline', 'project-retrieval'), required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    executable, directory, report = (path.absolute() for path in (args.executable, args.data_dir, args.report))
    if directory.exists() or report.exists() or not executable.is_file() or report.is_relative_to(directory):
        parser.error('executable required; data directory and report must be new and separate')
    before = hashlib.sha256(executable.read_bytes()).hexdigest() if args.scenario == 'project-retrieval' else None
    result = subprocess.run([str(executable), '--service-test', str(report), '--service-test-scenario', args.scenario,
                             '--data-dir', str(directory)],
                            timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not report.is_file():
        return 1
    value = json.loads(report.read_bytes())
    if args.scenario == 'project-retrieval':
        return 0 if (hashlib.sha256(executable.read_bytes()).hexdigest() == before
            and value.get('ok') is True and value.get('execution_mode') == 'frozen'
            and value.get('scenario') == 'project-retrieval' and value.get('backend') == 'postgresql'
            and value.get('factory') == 'open_service_owner' and value.get('catalog_schema') == 5
            and value.get('input_schema') == 'filename-path-root-project-v2'
            and all(value.get(name) is True for name in ('full_name_reverse', 'mixed_fields_ranked',
                'direct_root', 'explicit_project_isolation', 'scoped_ambiguity', 'selection_keeps_query',
                'source_isolation', 'pending_canonical_fallback', 'alias_revoke_no_stale_hits',
                'offline_cached_metadata', 'no_document_reread', 'project_capabilities_ready',
                'current_input_schema', 'metadata_only', 'pending_alias_direct_root', 'clean_stop'))) else 1
    if args.scenario == 'health-offline':
        return 0 if (value.get('ok') is True and value.get('execution_mode') == 'frozen'
            and value.get('scenario') == 'health-offline' and value.get('backend') == 'postgresql'
            and value.get('factory') == 'open_service_owner' and value.get('clean_stop') is True
            and all(value.get(name) is True for name in ('cached_metadata_searchable', 'nonprobing_status',
                'scope_before_aggregation', 'truthful_counts', 'revision_generation', 'distinct_fact_timestamps',
                'probes_drained', 'second_launch', 'one_owner', 'one_queue', 'physical_storage_measured'))
            and all(type(value.get('storage_observation', {}).get(name)) is int for name in
                ('physical_owned_bytes', 'pgdata_bytes', 'wal_bytes', 'free_bytes', 'quota_bytes', 'reserved_bytes'))
            and all(value.get(name) == 1 for name in ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes'))) else 1
    if args.scenario == 'multi-source':
        return 0 if (value.get('ok') is True and value.get('execution_mode') == 'frozen'
                     and value.get('scenario') == 'multi-source' and value.get('backend') == 'postgresql'
                     and value.get('factory') == 'open_service_owner' and value.get('clean_stop') is True
                     and all(value.get(name) is True for name in ('metadata_before_embedding', 'search_during_model_block',
                         'catalog_preempts_embedding', 'scoped_cancel', 'partial_preserved', 'late_embedding_rejected',
                         'unavailable_source', 'honest_counters'))
                     and value.get('max_batch', 100) <= 16) else 1
    return 0 if (value.get('ok') is True and value.get('execution_mode') == 'frozen'
                 and value.get('scenario') == 'two-clients' and value.get('clean_stop') is True
                 and all(value.get(name) is True for name in ('two_clients', 'scoped_fetch', 'metadata_only', 'auth', 'attach', 'second_launch', 'token_absent'))
                 and value.get('second_launch_pid') != value.get('owner_pid')
                 and all(value.get(name) == 1 for name in ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes'))) else 1


if __name__ == '__main__':
    raise SystemExit(main())
