"""Real ordinary frozen --serve proof; profiles/logs stay in private ignored QA.

Caller source SHA is an expectation bound by a separate same-image frozen
application proof/common manifest. The server API does not expose a source SHA.
This controller never copies a bundle, changes profile ownership or kills a
discovered PID. Only retained Popen children are eligible for failure cleanup.
"""
import argparse
from copy import deepcopy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))
from atom_search.runtime import _windows
from atom_search.service.client import ServiceClient, read_owner_state
from atom_search.service.contracts import SearchRequest, ServiceError

STARTUP_SECONDS = 120
SHUTDOWN_SECONDS = 50
OUTPUT_LIMIT = 65536
OUTPUT_STREAMS = ('stdout', 'stderr')
OUTPUT_LABELS = frozenset(scenario + '-' + role for scenario in ('ascii', 'unicode')
                          for role in ('primary', 'attached', 'restart'))
FLAGS = frozenset(('two_clients', 'private_validate_apply', 'cas_conflict', 'unauthenticated_401',
                   'empty_sources', 'no_jobs_or_scan', 'metadata_search_empty', 'child_identity_verified',
                   'second_serve_same_owner', 'primary_eof_clean_stop', 'restart_preserved',
                   'model_ready', 'postgres_ready', 'no_output'))


class GateFailure(Exception):
    def __init__(self, code):
        self.code = code


def _require(condition, code):
    if not condition:
        raise GateFailure(code)


def _sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _verify_service_bundle(directory):
    spec = importlib.util.spec_from_file_location('ssearch_frozen_bundle_support', ROOT / 'packaging/build_support.py')
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    support.validate_bundle(directory, service_only=True)


def _validate(args):
    _require(os.name == 'nt', 'windows_required')
    _require(re.fullmatch('[0-9a-f]{40}', args.source_commit) is not None
             and re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', args.app_version) is not None,
             'invalid_arguments')
    executable, qa, report = args.service_executable, args.qa_directory, args.report
    _require(all(path.is_absolute() for path in (executable, qa, report)), 'absolute_paths_required')
    for path in (executable, qa, report):
        _windows.reject_reparse(path)
    _require(executable.name.lower() == 'ssearch-service.exe' and executable.is_file(), 'service_executable_required')
    _require(not qa.exists() and not qa.is_symlink() and not report.exists() and not report.is_symlink(),
             'fresh_qa_and_report_required')
    _require(qa != report, 'report_path_invalid')
    _require(not qa.resolve().is_relative_to(executable.parent.resolve())
             and not report.resolve().is_relative_to(executable.parent.resolve()),
             'qa_must_be_outside_bundle')
    _verify_service_bundle(executable.parent)


def _child_environment():
    environment = dict(os.environ)
    for name in list(environment):
        upper = name.upper()
        if (upper.startswith(('PYTHON', 'PYTEST', 'PYI_', '_PYI_', 'NODE', 'ELECTRON', 'NPM_', 'CONDA_', 'PG', 'SSEARCH_'))
                or upper in ('__PYVENV_LAUNCHER__', 'VIRTUAL_ENV', 'SOURCE_SHA', 'BUILD_MODE',
                             'PATH')):
            del environment[name]
    system = Path(environment.get('SystemRoot', environment.get('SYSTEMROOT', '')))
    _require(system.is_absolute() and (system / 'System32').is_dir(), 'system_environment_required')
    environment['PATH'] = str(system / 'System32')
    return environment


def _capture_identity(info):
    return (info.st_dev, info.st_ino)


def _capture_count(info):
    return dict(bytes=min(info.st_size, OUTPUT_LIMIT + 1), oversize=info.st_size > OUTPUT_LIMIT)


def _capture_categories(content):
    lower = content.lower()
    categories = dict(
        traceback=b'traceback (most recent call last):' in lower,
        thread=b'exception in thread ' in lower,
        unraisable=b'exception ignored in:' in lower,
        warning=re.search(rb'\b[a-z]*warning:', lower) is not None,
        onnxruntime=b'onnxruntime:' in lower,
        pyinstaller=re.search(rb'\[pyi-[0-9]+:', lower) is not None)
    categories['unknown'] = bool(content) and not any(categories.values())
    return categories


def _read_capture(child, index):
    """Inspect only the created private regular file after retained cleanup."""
    try:
        _require(child.closed and child.cleanup_confirmed, 'capture_unavailable')
        path = child.logs[index]
        _windows.assert_private(path.parent)
        _windows.reject_reparse(path)
        _windows.assert_private(path)
        expected = child.log_identities[index]
        before = path.lstat()
        _require(expected[1] != 0 and stat.S_ISREG(before.st_mode)
                 and _capture_identity(before) == expected, 'capture_unavailable')
        with path.open('rb', buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            _require(stat.S_ISREG(opened.st_mode) and _capture_identity(opened) == expected,
                     'capture_unavailable')
            _windows.reject_reparse(path)
            _require(_capture_identity(path.lstat()) == expected, 'capture_unavailable')
            content = stream.read(OUTPUT_LIMIT)
            after = os.fstat(stream.fileno())
            _require(_capture_identity(after) == expected and after.st_size == opened.st_size,
                     'capture_unavailable')
        return dict(state='read', **_capture_count(after), truncated=after.st_size > len(content),
                    categories=_capture_categories(content))
    except BaseException:
        return dict(state='unavailable')


def _emit_output_diagnostic(children):
    for child in children:
        if not getattr(child, 'output_leak', False):
            continue
        diagnostic = dict(diagnostic_only=True, code='child_output_leak',
            child=child.label if child.label in OUTPUT_LABELS else 'unknown',
            checkpoints=child.output_checkpoints,
            streams={name: _read_capture(child, index) for index, name in enumerate(OUTPUT_STREAMS)})
        print(json.dumps(diagnostic, ensure_ascii=True, separators=(',', ':')), file=sys.stderr)
        return  # One bounded diagnostic; the original failure remains authoritative.


class _Child:
    def __init__(self, executable, profile, qa, label, environment):
        self.identity = None
        self.label = label
        self.logs = [qa / (label + '.stdout'), qa / (label + '.stderr')]
        self.streams = []
        self.log_identities = []
        self.output_checkpoints = {}
        self.output_leak = False
        self.process = None
        self.closed = False
        self.cleanup_confirmed = False
        try:
            for path in self.logs:
                self.streams.append(path.open('xb', buffering=0))
                self.log_identities.append(_capture_identity(os.fstat(self.streams[-1].fileno())))
            self.process = subprocess.Popen(
                [str(executable), '--serve', '--data-dir', str(profile), '--model-dir',
                 str(executable.parent / '_internal/models'), '--control-stdin'],
                cwd=executable.parent, env=environment, stdin=subprocess.PIPE,
                stdout=self.streams[0], stderr=self.streams[1], shell=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except BaseException:
            for stream in self.streams:
                stream.close()
            raise GateFailure('child_launch_failed') from None

    def verify(self, executable):
        _require(self.process.poll() is None, 'child_exited_before_ready')
        self.identity = _windows.process_identity(self.process.pid)
        _require(self.identity is not None and self.identity.executable == os.path.normcase(str(executable.resolve())),
                 'retained_child_image_mismatch')
        with _windows.verified_process(self.identity):
            pass

    def stop(self):
        _require(self.process.poll() is None, 'child_exited_before_eof')
        self.output_checkpoints['beforeEOF'] = self._output_counts()
        with _windows.verified_process(self.identity):
            self.process.stdin.close()
            _require(self.process.wait(SHUTDOWN_SECONDS) == 0, 'child_nonzero_exit')
        self.output_checkpoints['afterExit'] = self._output_counts()
        self.output_leak = any(value.get('bytes') != 0
                               for value in self.output_checkpoints['afterExit'].values())
        _require(not self.output_leak, 'child_output_leak')

    def _output_counts(self):
        counts = {}
        for index, name in enumerate(OUTPUT_STREAMS):
            try:
                info = self.logs[index].lstat()
                _require(stat.S_ISREG(info.st_mode)
                         and _capture_identity(info) == self.log_identities[index], 'capture_unavailable')
                counts[name] = _capture_count(info)
            except BaseException:
                counts[name] = dict(state='unavailable')
        return counts

    def cleanup(self):
        if self.closed:
            return self.cleanup_confirmed
        clean = True
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except BaseException:
            clean = False
        try:
            try:
                code = self.process.wait(SHUTDOWN_SECONDS)
                clean = clean and code == 0
            except subprocess.TimeoutExpired:
                clean = False
                self.process.kill()  # Retained Popen object only, never a descriptor PID.
                self.process.wait(10)
        except BaseException:
            clean = False
        finally:
            for stream in self.streams:
                stream.close()
            # CPython's pinned Windows Popen keeps a kernel handle after wait().
            handle = getattr(self.process, '_handle', None)
            if handle is not None:
                handle.Close()
            self.closed = True
        self.cleanup_confirmed = clean
        return self.cleanup_confirmed


def _spawn(executable, profile, qa, label, environment, children):
    child = _Child(executable, profile, qa, label, environment)
    children.append(child)  # Retain before any identity/readiness check can fail.
    child.verify(executable)
    return child


def _owner(client, child, version):
    status = client.status()  # Reuse P2 handshake, DPAPI, nonce and ACL validation.
    state, identity = read_owner_state(client.endpoint_file)
    _require(child.process.poll() is None and client.descriptor['pid'] == child.process.pid
             and state['state'] == 'ready' and identity == child.identity, 'owner_not_retained_child')
    _require(status.app_version == status.service_version == version and status.api_version == '1',
             'service_version_mismatch')
    with _windows.verified_process(child.identity):
        pass
    return status


def _ready(endpoint, child, version):
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline and child.process.poll() is None:
        client = None
        try:
            client = ServiceClient(endpoint, None)
            status = _owner(client, child, version)
            if (status.components.model.state == 'ready' and status.components.database.runtime_state == 'ready'
                    and status.components.database.version == '18.6'
                    and status.components.database.extension_version == '0.8.7'):
                _require(time.monotonic() < deadline, 'startup_timeout')
                return client
        except ServiceError:
            pass  # The atomic endpoint may precede the first bounded health probe.
        except BaseException:
            if client is not None:
                client.close()
            raise
        if client is not None:
            client.close()
        time.sleep(.05)
    raise GateFailure('startup_timeout')


def _empty(client):
    status = client.status()
    _require(not status.components.sources and not status.components.jobs, 'implicit_sources_or_jobs')
    _require(client.settings_get().settings['sources'] == [], 'implicit_source_settings')
    _require(client.start_catalog([]) == (), 'empty_catalog_started_jobs')
    _require(client.search(SearchRequest('drawing', source_ids=(), mode='literal')).results == (), 'empty_search_results')


def _stopped(endpoint, child, generation):
    state, identity = read_owner_state(endpoint)
    _require(not endpoint.exists() and state['state'] == 'stopped' and identity == child.identity
             and state['generation'] == generation, 'clean_stop_state_unconfirmed')


def _scenario(executable, profile, qa, label, environment, version, children):
    endpoint = profile / 'engine/runtime/endpoint.json'
    primary = _spawn(executable, profile, qa, label + '-primary', environment, children)
    first = second = restarted_client = None
    try:
        first = _ready(endpoint, primary, version)
        second = ServiceClient(endpoint, None)
        _owner(second, primary, version)
        _require(first.descriptor == second.descriptor, 'two_client_identity_mismatch')
        address = urlsplit(first.descriptor['base_url'])
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            connection.request('GET', '/v1/status')
            _require(connection.getresponse().status == 401, 'unauthenticated_request_accepted')
        finally:
            connection.close()
        _empty(first)
        snapshot = second.settings_get()
        candidate = deepcopy(snapshot.settings)
        candidate['ui']['theme'] = 'dark'
        _require(first.settings_validate(candidate, snapshot.revision).valid, 'private_validation_failed')
        applied = first.settings_apply(candidate, snapshot.revision)
        _require(second.settings_get() == applied and applied.revision == snapshot.revision + 1, 'private_apply_failed')
        try:
            second.settings_apply(candidate, snapshot.revision)
        except ServiceError as failure:
            _require(failure.code == 'revision_conflict', 'cas_error_mismatch')
        else:
            raise GateFailure('cas_conflict_missing')
        attached = _spawn(executable, profile, qa, label + '-attached', environment, children)
        time.sleep(.5)
        _owner(second, primary, version)
        attached.stop()
        _owner(first, primary, version)
        _empty(first)
        primary.stop()
        _stopped(endpoint, primary, first.descriptor['generation'])
        restarted = _spawn(executable, profile, qa, label + '-restart', environment, children)
        restarted_client = _ready(endpoint, restarted, version)
        _require(restarted_client.settings_get() == applied
                 and restarted_client.descriptor['installation_id'] == first.descriptor['installation_id'],
                 'restart_settings_or_identity_changed')
        _empty(restarted_client)
        restarted.stop()
        _stopped(endpoint, restarted, restarted_client.descriptor['generation'])
        return dict.fromkeys(FLAGS, True)
    finally:
        for client in (first, second, restarted_client):
            if client is not None:
                client.close()


def run_gate(args):
    _validate(args)
    before = _sha256(args.service_executable)
    environment = _child_environment()
    _windows.private_directory(args.qa_directory)
    children, scenarios = [], {}
    failure = None
    cleanup_ok = True
    try:
        for label, name in (('ascii', 'serve profile ASCII'), ('unicode', 'serve profile Кириллица')):
            flags = _scenario(args.service_executable, args.qa_directory / name, args.qa_directory,
                              label, environment, args.app_version, children)
            _require(set(flags) == FLAGS and all(value is True for value in flags.values()), 'incomplete_scenario_proof')
            scenarios[label] = flags
    except BaseException as error:
        failure = error.code if isinstance(error, GateFailure) else 'frozen_service_gate_failed'
    finally:
        for child in reversed(children):
            try:
                if not child.cleanup():
                    cleanup_ok = False
            except BaseException:
                cleanup_ok = False
    if failure == 'child_output_leak':
        _emit_output_diagnostic(children)
    _require(cleanup_ok, 'owned_cleanup_unconfirmed')
    _require(failure is None, failure or 'frozen_service_gate_failed')
    after = _sha256(args.service_executable)
    _require(before == after, 'service_image_changed')
    path = str(args.service_executable)
    return dict(schema_version=1, ok=True, execution_mode='frozen', scenario='ordinary-serve',
                source_commit=args.source_commit, source_commit_observed=False,
                source_identity_binding='external-frozen-application-proof', app_version=args.app_version,
                service_sha256_before=before, service_sha256_after=after, scenarios=scenarios,
                executable_path_has_unicode=not path.isascii(), executable_path_has_spaces=' ' in path,
                startup_timeout_seconds=STARTUP_SECONDS, shutdown_timeout_seconds=SHUTDOWN_SECONDS,
                windows_validation=[])


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GateFailure('invalid_arguments')


def main(argv=None):
    failure = None
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument('--service-executable', type=Path, required=True)
        parser.add_argument('--qa-directory', type=Path, required=True)
        parser.add_argument('--source-commit', required=True)
        parser.add_argument('--app-version', required=True)
        parser.add_argument('--report', type=Path, required=True)
        args = parser.parse_args(argv)
        receipt = run_gate(args)
        _windows.reject_reparse(args.report)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, indent=2)
        return 0
    except BaseException as error:
        failure = error.code if isinstance(error, GateFailure) else 'frozen_service_gate_failed'
    print(failure, file=sys.stderr)  # Fixed codes only; never exception text or private paths.
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
