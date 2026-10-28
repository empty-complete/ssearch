"""Qt-free frozen service entry; authority stays in the existing service APIs."""
import argparse
from contextlib import contextmanager
import importlib.metadata
import os
from pathlib import Path
import sys
import warnings


SCENARIOS = {
    'two-clients': 'run_service_self_test',
    'multi-source': 'run_multi_source_service_test',
    'health-offline': 'run_health_offline_service_test',
    'project-settings': 'run_project_settings_service_test',
    'project-context': 'run_project_context_service_test',
    'project-retrieval': 'run_project_retrieval_service_test',
    'project-golden': 'run_project_search_service_test',
}


class _InvalidArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's ordinary usage errors can echo credentials or private argv.
        raise _InvalidArguments()


def _path(value):
    if not value or '\0' in value:
        raise argparse.ArgumentTypeError('invalid_path')
    return Path(value)


def _offline_environment():
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'


class _ExactUserWarning(type):
    def __subclasscheck__(cls, category):
        return category is UserWarning


class _OrtServerAdvisory(Warning, metaclass=_ExactUserWarning):
    """Filter marker: match UserWarning itself, never an unknown subclass."""


@contextmanager
def _packaged_ort_advisory():
    enabled = False
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        try:
            version = sys.getwindowsversion()
            enabled = (version.product_type in (2, 3)
                       and version.platform_version == (10, 0, 26100)
                       and importlib.metadata.version('onnxruntime') == '1.22.1')
        except Exception:
            pass  # Unknown dependency/OS context keeps ordinary warnings visible.
    if not enabled:
        yield
        return
    with warnings.catch_warnings():
        # ORT 1.22.1 misclassifies this Server build's release label. This
        # packaging policy grants no platform acceptance and imports no ORT.
        warnings.filterwarnings('ignore', category=_OrtServerAdvisory,
            message=(r'(?-i:\AUnsupported Windows version \(2025server\)\. '
                     r'ONNX Runtime supports Windows 10 and above, only\.\Z)'),
            module=r'\Aonnxruntime\.capi\.onnxruntime_validation\Z')
        yield


def _arguments(argv):
    parser = _Parser(add_help=False, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--profile-info', action='store_true')
    mode.add_argument('--desktop-client', action='store_true')
    mode.add_argument('--serve', action='store_true')
    mode.add_argument('--postgres-self-test', type=_path)
    mode.add_argument('--service-test', type=_path)
    mode.add_argument('--service-attach-test', type=_path)
    mode.add_argument('--self-test', type=_path)
    parser.add_argument('--data-dir', type=_path)
    parser.add_argument('--model-dir', type=_path)
    parser.add_argument('--parent-lease', action='store_true')
    parser.add_argument('--control-stdin', action='store_true')
    parser.add_argument('--service-test-scenario', choices=tuple(SCENARIOS))
    parser.add_argument('--postgres-qa-lifecycle', choices=('seed', 'verify'))
    parser.add_argument('--postgres-qa-run')
    args = parser.parse_args(argv)
    if ((args.parent_lease and not args.serve)
            or (args.control_stdin and (not args.serve or args.parent_lease))
            or (args.parent_lease and args.model_dir is None)
            or ((args.desktop_client or args.serve or args.postgres_self_test
                 or args.service_test or args.service_attach_test) and args.data_dir is None)
            or ((args.profile_info or args.desktop_client or args.postgres_self_test) and args.model_dir is not None)
            or (args.service_test_scenario is not None and not args.service_test)
            or ((args.postgres_qa_lifecycle is not None or args.postgres_qa_run is not None)
                and not args.postgres_self_test)
            or (args.postgres_qa_run is not None and args.postgres_qa_lifecycle is None)):
        raise _InvalidArguments()
    return args


def launch(argv=None) -> int:
    """Route fixed public modes without sanitizing a library caller's PG state."""
    _offline_environment()
    try:
        args = _arguments(argv)
        if args.profile_info or args.desktop_client or (args.serve and args.parent_lease):
            from atom_search.service.desktop_host import main
            forwarded = (['--profile-info'] if args.profile_info else ['--desktop-client']
                         if args.desktop_client else ['--serve', '--parent-lease'])
            if args.data_dir is not None:
                forwarded += ['--data-dir', str(args.data_dir)]
            if args.model_dir is not None:
                forwarded += ['--model-dir', str(args.model_dir)]
            return main(forwarded)
        if args.serve:
            from atom_search.service.headless import main
            forwarded = ['--data-dir', str(args.data_dir)]
            if args.model_dir is not None:
                forwarded += ['--model-dir', str(args.model_dir)]
            if args.control_stdin:
                forwarded.append('--control-stdin')
            return main(forwarded)
        from atom_search import diagnostics
        if args.postgres_self_test:
            options = dict(data_dir=args.data_dir)
            if args.postgres_qa_lifecycle is not None:
                options.update(lifecycle=args.postgres_qa_lifecycle, run_token=args.postgres_qa_run)
            return diagnostics.run_postgres_self_test(args.postgres_self_test, **options)
        options = dict(data_dir=args.data_dir, model_dir=args.model_dir)
        if args.service_test:
            operation = getattr(diagnostics, SCENARIOS[args.service_test_scenario or 'two-clients'])
            return operation(args.service_test, **options)
        if args.service_attach_test:
            return diagnostics.run_service_attach_test(args.service_attach_test, **options)
        return diagnostics.run_self_test(args.self_test, **options)
    except _InvalidArguments:
        return 2
    except BaseException:
        # stdout belongs to the broker/profile protocol. No raw diagnostics,
        # traceback, private file or GUI fallback is produced on either stream.
        return 1


def _entry() -> int:
    _offline_environment()
    try:
        _arguments(None)  # Reject private/invalid argv before importing the native boundary.
    except _InvalidArguments:
        return 2
    try:
        # Dedicated process startup only, before workers or native consumers.
        from atom_search.runtime.postgres import sanitize_host_environment
        sanitize_host_environment()
    except BaseException:
        return 1
    with _packaged_ort_advisory():
        return launch()


if __name__ == '__main__':
    code = _entry()
    if code and ('--serve' in sys.argv[1:] or '--desktop-client' in sys.argv[1:]):
        # As in the source entry, uncertain owned cleanup cannot keep this
        # dedicated process alive. Never terminate a PID obtained by discovery.
        os._exit(code)
    raise SystemExit(code)
