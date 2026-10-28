"""Exact existing installer admission and private QA/public receipt boundary.

This controller never builds, publishes, or accepts a different run as fallback.
Native acceptance is supplied by the retained standard-user Windows controller.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHECKS = ('fresh_install_verified', 'same_version_reinstall_verified', 'owned_uninstall_verified',
    'payload_inventory_verified', 'shortcut_ownership_verified', 'application_bytes_preserved',
    'application_index_preserved', 'postgres_bytes_preserved', 'postgres_restart_verified',
    'installed_proofs_passed', 'runtime_cleanup_confirmed', 'process_completion_confirmed',
    'registered_installation_removed', 'owned_shortcuts_removed', 'standard_user_profile_verified', 'parent_job_drained')
OPEN_GATES = ['qt_upgrade', 'native_windows_11', 'windows_10', 'full_p4_acceptance']
INVENTORY_CODES = frozenset(('installer_inventory_boundary', 'installer_inno_inventory_invalid',
    'installer_non_file', 'installer_duplicate_file', 'installer_inno_identity_changed',
    'installer_inno_file_missing', 'installer_payload_image_mismatch', 'installer_inventory_mismatch',
    'installer_reparse_refused', 'installer_report_unavailable', 'installer_object_required'))


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def no_links(path):
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink() and not (getattr(item.lstat(), 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT), 'installer_reparse_refused')


def read_json(path, limit=2 * 1024 * 1024):
    no_links(path)
    require(path.is_file() and path.stat().st_size <= limit, 'installer_report_unavailable')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    require(type(value) is dict, 'installer_object_required')
    return value


def write_json(path, value):
    no_links(path)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_input_metadata(run, artifact, pins):
    require(run.get('id') == pins['run_id'] and run.get('workflow_id') == pins['workflow_id']
        and run.get('path') == pins['workflow_path'] and run.get('event') == 'workflow_dispatch'
        and run.get('status') == 'completed' and run.get('conclusion') == 'success'
        and run.get('head_sha') == pins['source_commit'] and run.get('head_branch') == pins['head_branch']
        and run.get('head_repository', {}).get('full_name') == pins['repository'], 'installer_run_mismatch')
    require(artifact.get('id') == pins['artifact_id'] and artifact.get('name') == pins['artifact_name']
        and artifact.get('expired') is False and artifact.get('size_in_bytes') == pins['archive_size']
        and artifact.get('digest') == 'sha256:' + pins['archive_sha256']
        and artifact.get('workflow_run', {}).get('id') == pins['run_id']
        and artifact.get('workflow_run', {}).get('head_sha') == pins['source_commit'], 'installer_artifact_mismatch')


def validate_application_preservation(first, restored):
    require(restored.get('prior_index_verified') is True and restored.get('prior_files') == 3
        and restored.get('prior_index_fingerprint') == first.get('index_fingerprint')
        and all(key in first and restored.get(key) == first[key] for key in
            ('public_source_id', 'public_file_ids', 'catalog_id', 'installation_id',
             'model_manifest_digest', 'index_fingerprint')), 'installer_application_not_preserved')


def verify_installed_payload(payload, manifest, *, owned_inno_files, inventory_output, diagnostic=False):
    no_links(payload)
    no_links(inventory_output)
    require(not inventory_output.exists() and payload.is_dir(), 'installer_inventory_boundary')
    require(set(owned_inno_files) == {'unins000.exe', 'unins000.dat'}
        or owned_inno_files == {}, 'installer_inno_inventory_invalid')
    files, folded, pending = {}, set(), [payload]
    while pending:
        item = pending.pop()
        no_links(item)
        if item.is_dir():
            pending.extend(item.iterdir())
            continue
        require(item.is_file() and stat.S_ISREG(item.stat().st_mode), 'installer_non_file')
        name = item.relative_to(payload).as_posix()
        require(name.casefold() not in folded, 'installer_duplicate_file')
        folded.add(name.casefold())
        record = dict(size=item.stat().st_size, sha256=digest(item))
        if name in owned_inno_files:
            require(record == owned_inno_files[name], 'installer_inno_identity_changed')
        else:
            files[name] = record
    require(all(name.casefold() in folded for name in owned_inno_files), 'installer_inno_file_missing')
    files = dict(sorted(files.items()))
    desktop_match = files.get('ssearch.exe') == {key: manifest['executable'][key] for key in ('size', 'sha256')}
    service_match = files.get('service/ssearch-service.exe') == {key: manifest['service_executable'][key] for key in ('size', 'sha256')}
    images_match = manifest.get('payload_kind') == 'electron-service' and desktop_match and service_match
    if diagnostic and not images_match:
        print(f'CI_INSTALLER_INVENTORY files={min(len(files), 999999)} desktop_match={str(desktop_match).lower()} '
              f'service_match={str(service_match).lower()} inventory_match=unavailable', flush=True)
    require(images_match, 'installer_payload_image_mismatch')
    # Preserve the original Windows json.dump text writer, including CRLF.
    write_json(inventory_output, dict(schema_version=1, source_commit=manifest['source_commit'],
        app_version=manifest['app_version'], core_version=manifest['core_version'], files=files, inventory_only=True))
    inventory_match = digest(inventory_output) == manifest['desktop']['inventory_sha256']
    if diagnostic:
        print(f'CI_INSTALLER_INVENTORY files={min(len(files), 999999)} desktop_match={str(desktop_match).lower()} '
              f'service_match={str(service_match).lower()} inventory_match={str(inventory_match).lower()}', flush=True)
    require(inventory_match, 'installer_inventory_mismatch')
    return files


def sanitize_installation_receipt(raw, *, pins, harness_commit, exit_code):
    observed = raw.get('checks', {})
    checks = {key: type(observed) is dict and observed.get(key) is True for key in CHECKS}
    inventory = raw.get('inventory_sha256', '')
    valid_hash = type(inventory) is str and re.fullmatch('[0-9a-f]{64}', inventory) is not None
    identity = (raw.get('source_commit') == pins['source_commit']
        and type(raw.get('installer_sha256')) is str and raw['installer_sha256'].lower() == pins['installer_sha256']
        and raw.get('installer_bytes') == pins['installer_size']
        and type(harness_commit) is str and re.fullmatch('[0-9a-f]{40}', harness_commit) is not None)
    server = raw.get('os_product_type') in (2, 3) and raw.get('os_build') == 26100
    return dict(schema_version=1, ok=raw.get('ok') is True and type(exit_code) is int and exit_code == 0
        and identity and valid_hash and all(checks.values()), payload_source_commit=pins['source_commit'],
        harness_source_commit=harness_commit, installer_sha256=pins['installer_sha256'],
        installer_size=pins['installer_size'], artifact_run_id=pins['run_id'], artifact_id=pins['artifact_id'],
        payload_inventory_sha256=inventory if valid_hash else None,
        runner_platform='Windows Server 2025 x64' if server else 'Windows runner unqualified',
        windows_validation=[], full_suite_passed=False, checks=checks, open_gates=OPEN_GATES)


def verify_harness(commit):
    require(type(commit) is str and re.fullmatch('[0-9a-f]{40}', commit), 'installer_harness_sha_required')
    def git(*args):
        result = subprocess.run(['git', *args], cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
        require(result.returncode == 0, 'installer_harness_git_failed')
        return result.stdout.decode('ascii').strip()
    require(git('rev-parse', 'HEAD') == commit and not git('status', '--porcelain', '--untracked-files=all'),
            'installer_harness_changed')


def validate_package(directory, pins):
    publisher = load_module('ssearch_installer_publisher', ROOT / 'tools/ci_publish.py')
    paths = list(directory.iterdir())
    require(publisher.public_names([path.name for path in paths]) == pins['installer_name'], 'installer_public_files')
    require(all(path.is_file() and not path.is_symlink() for path in paths), 'installer_public_file_invalid')
    sums = (directory / 'SHA256SUMS.txt').read_text(encoding='utf-8')
    actual = {}
    for line in sums.splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        require(match is not None and match[2] not in actual, 'installer_checksums_invalid')
        actual[match[2]] = match[1]
    require(actual == {path.name: digest(path) for path in paths if path.name != 'SHA256SUMS.txt'},
            'installer_checksums_mismatch')
    manifest = read_json(directory / 'build-manifest.json')
    source = pins['source_commit']
    require(manifest.get('source_commit') == source and manifest.get('payload_kind') == 'electron-service'
        and manifest.get('app_version') == manifest.get('core_version') == '0.2.0'
        and manifest.get('test_scope') == 'targeted' and manifest.get('full_suite_passed') is False
        and manifest.get('catalog') == dict(backend='postgresql', schema_version=5)
        and manifest.get('capabilities') == dict(postgresql='application_verified')
        and manifest.get('windows_validation') == [] and manifest.get('desktop', {}).get('required_frozen_proofs_passed') is True,
        'installer_quick_manifest_mismatch')
    installer = directory / pins['installer_name']
    require(installer.stat().st_size == pins['installer_size'] and digest(installer) == pins['installer_sha256']
        and manifest.get('installer') == dict(path='artifacts/' + pins['installer_name'],
            size=pins['installer_size'], sha256=pins['installer_sha256']), 'installer_identity_mismatch')
    receipt = read_json(directory / 'test-receipt.json')
    evidence = read_json(directory / 'ci-evidence.json')
    require(receipt.get('source_commit') == source and receipt.get('scope') == 'targeted'
        and receipt.get('full_suite_passed') is False and type(receipt.get('exit_code')) is int
        and receipt['exit_code'] == 0 and any(receipt.get(key) for key in
            ('test_targets', 'frontend_test_targets', 'desktop_test_targets')),
        'installer_test_receipt_mismatch')
    require(evidence.get('source_commit') == source and evidence.get('test_scope') == 'targeted'
        and evidence.get('full_suite_passed') is False and evidence.get('windows_validation') == [],
        'installer_evidence_mismatch')
    proofs = evidence.get('frozen_proofs', {})
    require(set(proofs) == set(publisher.PROOF_CHECKS), 'installer_frozen_proofs_missing')
    for name, flags in publisher.PROOF_CHECKS.items():
        proof = proofs[name]
        require(proof.get('passed') is True and proof.get('execution_mode') == 'frozen'
            and proof.get('checks') == dict.fromkeys(flags, True), 'installer_frozen_proof_invalid')
    desktop = evidence.get('desktop', {})
    require(desktop.get('payload_kind') == 'electron-service' and desktop.get('required_frozen_proofs_passed') is True
        and desktop.get('stage_accepted') is False
        and desktop.get('original_proof_index_sha256') == manifest['desktop'].get('proof_index_sha256')
        and desktop.get('scoped_backend_counts') == dict.fromkeys(
            ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes'), 1)
        and desktop.get('ui_scenarios') == dict.fromkeys(
            ('security', 'load', 'lifecycle', 'empty-profile'), 'packaged_hidden_offscreen'), 'installer_desktop_proofs_missing')
    return manifest


def fetch(pins):
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and os.environ.get('GITHUB_REPOSITORY') == pins['repository'], 'installer_manual_ci_required')
    base = ROOT / 'artifacts/development-quick' / pins['source_commit']
    no_links(base)
    require(not base.exists(), 'installer_ingress_exists')
    base.mkdir(parents=True)
    def api(endpoint, output):
        result = subprocess.run(['gh', 'api', '--method', 'GET', 'repos/' + pins['repository'] + '/' + endpoint],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=600)
        require(result.returncode == 0, 'installer_download_failed')
    for endpoint, name in ((f"actions/runs/{pins['run_id']}", 'run.json'),
                           (f"actions/artifacts/{pins['artifact_id']}", 'artifact.json')):
        with (base / name).open('xb') as stream:
            api(endpoint, stream)
    artifact = read_json(base / 'artifact.json')
    validate_input_metadata(read_json(base / 'run.json'), artifact, pins)
    archive = base / 'artifact.zip'
    with archive.open('xb') as stream:
        api(f"actions/artifacts/{pins['artifact_id']}/zip", stream)
    publisher = load_module('ssearch_installer_extract', ROOT / 'tools/ci_publish.py')
    package = base / 'download'
    publisher.extract_artifact(archive, artifact, package)
    validate_package(package, pins)
    (base / 'artifacts').mkdir()
    for path in package.iterdir():
        target = base / ('artifacts/' + path.name if path.name.endswith('.exe') else path.name)
        shutil.copyfile(path, target)


def proof_contracts():
    return load_module('ssearch_installer_build_support', ROOT / 'packaging/build_support.py')


def validate_ui(directory, manifest, report_path, scenario):
    support = proof_contracts()
    contracts = support.desktop_proof_contracts()
    versions = read_json(ROOT / 'frontend/compatibility/candidate-versions.json')['electron']
    expected = dict(source_commit=manifest['source_commit'], app_version=manifest['app_version'],
        core_version=manifest['core_version'], electron=versions['version'], node=versions['embedded_node'],
        chrome=versions['chromium'], hashes={key: digest(directory / name) for key, name in
            (('desktop_sha256', 'ssearch.exe'), ('service_sha256', 'service/ssearch-service.exe'),
             ('app_asar_sha256', 'resources/app.asar'), ('product_metadata_sha256', 'resources/product-metadata.json'))})
    contracts.validate_ui_report(read_json(report_path), scenario=scenario, expected=expected,
                                 policy=contracts.load_ui_policy(ROOT))


def sanitize_ui_summary(value):
    """Recheck child diagnostics at the parent boundary, using existing domains."""
    formatter = load_module('ssearch_installer_frozen_summary', ROOT / 'tools/summarize_frozen_report.py')
    require(type(value) is dict and value.get('diagnostic_only') is True
        and value.get('role') in tuple(role for role in formatter.ROLES if role.startswith('ui-'))
        and type(value.get('process_exit_code')) is int and value['process_exit_code'] in (0, 1)
        and value.get('report_state') in ('read', 'unavailable') and type(value.get('fields')) is dict,
        'installer_ui_summary_invalid')
    fields, safe = value['fields'], {}
    if value['report_state'] == 'read':
        if type(fields.get('ok')) is bool:
            safe['ok'] = fields['ok']
        _, codes, _ = formatter.public_symbols()
        if type(fields.get('reason_code')) is str and fields['reason_code'] in codes:
            safe['reason_code'] = fields['reason_code']
        required = fields.get('required_checks', [])
        required = required if type(required) is list else []
        safe['required_checks'] = formatter.ui_checks({}, value['role'])
        for check in safe['required_checks']:
            statuses = [item.get('status') for item in required if type(item) is dict and item.get('name') == check['name']]
            check['status'] = ('missing' if not statuses else statuses[0] if len(statuses) == 1
                and statuses[0] in ('pass', 'fail', 'not_run', 'missing', 'invalid') else 'invalid')
        if value['process_exit_code'] == 1 and fields.get('ok') is False:
            details = formatter.ui_failure_diagnostics(fields.get('failure_diagnostics'))
            if details:
                safe['failure_diagnostics'] = details
    return dict(diagnostic_only=True, role=value['role'], process_exit_code=value['process_exit_code'],
                report_state=value['report_state'], fields=safe)


def main(argv=None):
    try:
        class Parser(argparse.ArgumentParser):
            def error(self, message):
                raise ValueError('installer_arguments_invalid')
        parser = Parser(add_help=False, allow_abbrev=False)
        parser.add_argument('command', choices=('fetch', 'verify-harness', 'private', 'inventory', 'preservation',
            'ui-policy', 'ui', 'application', 'owner', 'serve', 'receipt', 'admit', 'candidate',
            'ui-summary', 'ui-summary-line'))
        parser.add_argument('--harness-commit')
        parser.add_argument('--directory', type=Path)
        parser.add_argument('--manifest', type=Path)
        parser.add_argument('--owned', type=Path)
        parser.add_argument('--output', type=Path)
        parser.add_argument('--first', type=Path)
        parser.add_argument('--report', type=Path)
        parser.add_argument('--scenario')
        parser.add_argument('--exit-code', type=int, default=1)
        parser.add_argument('--not-before-ms', type=int, default=0)
        parser.add_argument('--parent-drained', action='store_true')
        args = parser.parse_args(argv)
        pins = read_json(ROOT / 'resources/ci/installer-proof-input.json')
        if args.command == 'fetch':
            verify_harness(args.harness_commit)
            fetch(pins)
        elif args.command == 'verify-harness':
            verify_harness(args.harness_commit)
        elif args.command == 'admit':
            base = ROOT / 'artifacts/development-quick' / pins['source_commit']
            validate_package(base / 'download', pins)
            require(digest(base / 'build-manifest.json') == digest(base / 'download/build-manifest.json')
                and digest(base / 'artifacts' / pins['installer_name']) == pins['installer_sha256'],
                'installer_staged_inputs_changed')
        elif args.command == 'private':
            from atom_search.runtime import _windows
            require(not args.directory.exists(), 'installer_private_directory_exists')
            _windows.private_directory(args.directory)
        elif args.command == 'inventory':
            verify_installed_payload(args.directory, read_json(args.manifest),
                owned_inno_files=read_json(args.owned), inventory_output=args.output, diagnostic=True)
            # The published digest binds every file; also retain the existing
            # full desktop/service closure and offline resource checks.
            manifest = read_json(args.manifest)
            proof_contracts().validate_desktop_bundle(args.directory, source_commit=manifest['source_commit'],
                app_version=manifest['app_version'], core_version=manifest['core_version'])
        elif args.command == 'preservation':
            validate_application_preservation(read_json(args.first), read_json(args.report))
        elif args.command == 'ui-policy':
            print(json.dumps(proof_contracts().desktop_proof_contracts().load_ui_policy(ROOT)))
        elif args.command in ('ui-summary', 'ui-summary-line'):
            if args.command == 'ui-summary':
                formatter = load_module('ssearch_installer_frozen_report', ROOT / 'tools/summarize_frozen_report.py')
                value = formatter.summarize_report(args.report, 'ui-' + args.scenario, args.exit_code,
                                                   not_before_ms=args.not_before_ms)
            else:
                raw = sys.stdin.buffer.read(65537)
                require(len(raw) <= 65536, 'installer_ui_summary_oversized')
                value = json.loads(raw.decode('utf-8'))
            print('CI_INSTALLER_UI_SUMMARY ' + json.dumps(sanitize_ui_summary(value), ensure_ascii=True, sort_keys=True))
        elif args.command in ('ui', 'application', 'owner', 'serve'):
            manifest = read_json(args.manifest)
            report = read_json(args.report)
            if args.command == 'ui':
                validate_ui(args.directory, manifest, args.report, args.scenario)
            elif args.command == 'application':
                proof_contracts().validate_application_proof(report, source_commit=manifest['source_commit'],
                    app_version=manifest['app_version'], core_version=manifest['core_version'])
            elif args.command == 'owner':
                require(report.get('execution_mode') == 'frozen' and report.get('scenario') == 'two-clients'
                    and all(report.get(key) is True for key in ('ok', 'clean_stop', 'model_ready', 'two_clients',
                        'scoped_fetch', 'metadata_only', 'auth', 'attach', 'second_launch', 'token_absent'))
                    and report.get('access_checks') == ['result_not_found', 'source_scope_denied', 'access_denied']
                    and all(type(report.get(key)) is int and report[key] == 1 for key in
                        ('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes')), 'installer_owner_proof_invalid')
            else:
                from check_frozen_service import FLAGS
                require(type(report.get('schema_version')) is int and report['schema_version'] == 1
                    and report.get('ok') is True and report.get('execution_mode') == 'frozen'
                    and report.get('scenario') == 'ordinary-serve' and report.get('app_version') == manifest['app_version']
                    and report.get('source_commit') == manifest['source_commit'] and report.get('source_commit_observed') is False
                    and report.get('source_identity_binding') == 'external-frozen-application-proof'
                    and report.get('service_sha256_before') == report.get('service_sha256_after') == manifest['service_executable']['sha256']
                    and report.get('executable_path_has_unicode') is True and report.get('executable_path_has_spaces') is True
                    and report.get('windows_validation') == []
                    and type(report.get('scenarios')) is dict and set(report['scenarios']) == {'ascii', 'unicode'}
                    and all(type(flags) is dict and set(flags) == FLAGS and all(value is True for value in flags.values())
                        for flags in report['scenarios'].values()), 'installer_serve_proof_invalid')
        elif args.command == 'candidate':
            raw = read_json(args.report) if args.report.exists() else {}
            checks = raw.get('checks', {})
            value = {key: raw.get(key) for key in ('source_commit', 'installer_sha256', 'installer_bytes',
                'inventory_sha256', 'os_product_type', 'os_build')}
            require(all(value[key] == pins[pin] for key, pin in (
                ('source_commit', 'source_commit'), ('installer_sha256', 'installer_sha256'),
                ('installer_bytes', 'installer_size'))) or not raw, 'installer_candidate_identity')
            value['ok'] = raw.get('ok') is True
            value['checks'] = {key: type(checks) is dict and checks.get(key) is True
                               for key in CHECKS if key != 'parent_job_drained'}
            # Private reports/IDs never cross the disposable account boundary.
            require(value['inventory_sha256'] is None or (type(value['inventory_sha256']) is str
                and re.fullmatch('[0-9a-f]{64}', value['inventory_sha256'])), 'installer_candidate_hash_invalid')
            for key in ('os_product_type', 'os_build'):
                require(value[key] is None or (type(value[key]) is int and 0 <= value[key] <= 1000000),
                        'installer_candidate_os_invalid')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, value)
        else:
            verify_harness(args.harness_commit)
            raw = read_json(args.report) if args.report.exists() else {}
            # This flag is accepted only at the parent-only finalization call.
            # The unprivileged controller never writes this check itself.
            raw.setdefault('checks', {})['parent_job_drained'] = args.parent_drained
            receipt = sanitize_installation_receipt(raw, pins=pins, harness_commit=args.harness_commit,
                                                     exit_code=args.exit_code)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, receipt)
            return 0 if receipt['ok'] else 1
        return 0
    except BaseException as error:
        # Match only our own closed machine codes; unknown error arguments and
        # exception types/messages never cross the private-output boundary.
        code = error.args[0] if type(error) is ValueError and len(error.args) == 1 else None
        code = code if type(code) is str and code in INVENTORY_CODES else 'unavailable'
        print('CI_INSTALLER_HELPER code=' + code, flush=True)
        print('INSTALLER_PROOF_FAILED', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
