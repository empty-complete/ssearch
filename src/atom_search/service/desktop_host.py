"""Private ssearch desktop host shared by source and frozen launchers.

Only inherited pipes carry correlation/parent identity. No endpoint, credential
reference or arbitrary method is accepted. P2 retains service authentication,
owner composition and recovery. Importing this module does not compose an owner.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import threading


def serve_with_parent_lease(profile, model_dir, parent):
    """Retain the verified MAIN process object; parent exit or private EOF stops us."""
    from atom_search.runtime import _windows
    if (type(parent) is not dict or set(parent) != {'pid', 'executable', 'created'}
            or type(parent['pid']) is not int or parent['pid'] <= 0
            or type(parent['executable']) is not str or not Path(parent['executable']).is_absolute()
            or type(parent['created']) is not str or re.fullmatch('[1-9][0-9]{0,19}', parent['created']) is None):
        return 1
    identity = _windows.ProcessIdentity(parent['pid'],
        os.path.normcase(str(Path(parent['executable']).resolve())), int(parent['created']))
    stop, failed = threading.Event(), threading.Event()
    with _windows.verified_process(identity) as handle:
        if _windows.wait_process_exit(handle, timeout_seconds=0):
            return 1

        def watch_parent():
            try:
                while not stop.is_set():
                    if _windows.wait_process_exit(handle, timeout_seconds=.1):
                        stop.set()
                        return
            except BaseException:
                failed.set()
                stop.set()

        watcher = threading.Thread(target=watch_parent, name='ssearch-host-parent', daemon=True)
        watcher.start()  # Install the retained identity watch before P2 composition.
        from .headless import _wait_control_eof, serve

        def watch_control():
            try:
                _wait_control_eof(sys.stdin, stop)
            except BaseException:
                failed.set()
            finally:
                stop.set()

        control = threading.Thread(target=watch_control, name='ssearch-host-control', daemon=True)
        control.start()
        try:
            result = 0 if stop.is_set() else serve(profile, model_dir=model_dir, stop=stop)
        finally:
            stop.set()
            watcher.join(1)
            control.join(1)
        return 1 if failed.is_set() or watcher.is_alive() or control.is_alive() else result


def main(argv=None) -> int:
    """Run profile discovery, a broker, or headless serve; return 0/1 (usage: 2).

    --profile-info and --desktop-client use the existing adapter handshake.
    --serve --parent-lease uses its owner handshake and requires explicit profile
    and model paths. --serve alone delegates to the ordinary headless entry,
    including its signals, optional control EOF and bounded shutdown semantics.
    A nonzero serve result is terminal for the dedicated launcher process.
    """
    parser = argparse.ArgumentParser(description='ssearch private desktop service host', allow_abbrev=False)
    role = parser.add_mutually_exclusive_group(required=True)
    role.add_argument('--profile-info', action='store_true')
    role.add_argument('--desktop-client', action='store_true')
    role.add_argument('--serve', action='store_true')
    parser.add_argument('--parent-lease', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--control-stdin', action='store_true')
    args = parser.parse_args(argv)
    if args.parent_lease and not args.serve:
        parser.error('--parent-lease requires --serve')
    if args.control_stdin and (not args.serve or args.parent_lease):
        parser.error('--control-stdin requires ordinary --serve')
    if args.serve and args.data_dir is None:
        parser.error('--serve requires --data-dir')
    if args.parent_lease and args.model_dir is None:
        parser.error('--parent-lease requires --model-dir')
    if not args.serve and args.model_dir is not None:
        parser.error('--model-dir requires --serve')
    if args.serve and not args.parent_lease:
        from .headless import main as headless_main
        arguments = ['--data-dir', str(args.data_dir)]
        if args.model_dir is not None:
            arguments += ['--model-dir', str(args.model_dir)]
        if args.control_stdin:
            arguments.append('--control-stdin')
        return headless_main(arguments)

    correlation = None
    reply_started = False
    try:
        from atom_search import VERSION
        from atom_search.paths import data_path
        from atom_search.runtime import _windows
        from .desktop_client import _bounded, _read_frame, _write_frame, run_desktop_client
        from atom_search.http.security import public_uuid
        from .contracts import ServiceError

        incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
        first = _bounded(lambda: incoming.read(1), 10)
        if not first:
            return 1
        context = _bounded(lambda: _read_frame(incoming, first), 10)
        expected = {'schema_version', 'session_id', 'challenge'}
        if args.profile_info:
            expected |= {'parent_pid', 'parent_executable'}
        elif args.parent_lease:
            expected.add('parent')
        if (type(context) is not dict or set(context) != expected
                or type(context['schema_version']) is not int or context['schema_version'] != 1):
            return 1
        public_uuid(context['session_id'])
        public_uuid(context['challenge'])
        # Only validated canonical correlation can appear in a failure reply.
        correlation = dict(schema_version=1, session_id=context['session_id'], challenge=context['challenge'])
        profile = _windows.reject_reparse((args.data_dir or data_path()).absolute()).resolve()
        _windows.volume_identity(profile)
        user = _windows.user_sid()
        key = hashlib.sha256((user + '|' + str(profile).casefold()).encode('utf-8')).hexdigest()
        reply = dict(correlation)
        if args.profile_info:
            if (type(context['parent_pid']) is not int or context['parent_pid'] <= 0
                    or type(context['parent_executable']) is not str
                    or not Path(context['parent_executable']).is_absolute()):
                return 1
            parent = _windows.process_identity(context['parent_pid'])
            if (parent is None or parent.executable != os.path.normcase(
                    str(Path(context['parent_executable']).resolve()))):
                return 1
            executable = Path(sys.executable)
            base = executable if getattr(sys, 'frozen', False) else Path(
                getattr(sys, '_base_executable', None) or sys.executable)
            reply.update(kind='profile', profile=str(profile), key=key, service_version=VERSION,
                         python_base=str(base.resolve()), python_venv=str(executable.absolute()),
                         parent=dict(pid=parent.pid, executable=parent.executable, created=str(parent.created)))
            reply_started = True
            _bounded(lambda: _write_frame(outgoing, reply), 10)
            return 0
        if args.parent_lease:
            return serve_with_parent_lease(profile, args.model_dir, context['parent'])

        from .client import ServiceClient, read_owner_state
        endpoint = profile / 'engine' / 'runtime' / 'endpoint.json'
        client = ServiceClient(endpoint, None)
        try:
            status = client.status()  # Existing ACL/PID/DPAPI and nonce proof.
            if status.api_version != '1' or status.service_version != VERSION or status.app_version != VERSION:
                raise ValueError('unsupported_service_version')
            owner = False
            try:
                # OwnerIPC._require('owner'), read-only cache; no mutation or grant.
                client._ipc('desktop_status', {})
                owner = True
            except ServiceError as failure:
                if failure.code != 'access_denied':
                    raise
            client._identity()
            state, identity = read_owner_state(endpoint)
            if (state['generation'] != client.descriptor['generation']
                    or state['installation_id'] != client.descriptor['installation_id']
                    or identity.pid != client.descriptor['pid']):
                raise ValueError('changed_owner')
            client._identity()
            broker = _windows.process_identity(os.getpid())
            if broker is None:
                raise ValueError('broker_identity_unavailable')
            reply.update(kind='ready', profile=str(profile), key=key,
                api_version='1', service_version=status.service_version, app_version=status.app_version,
                generation=client.descriptor['generation'], installation_id=client.descriptor['installation_id'],
                owner=owner, capabilities=['settings_validate', 'settings_apply', 'start_catalog', 'cancel_job'] if owner else [],
                process=dict(pid=identity.pid, executable=identity.executable, created=str(identity.created)),
                broker=dict(pid=broker.pid, executable=broker.executable, created=str(broker.created)))
            reply_started = True
            _bounded(lambda: _write_frame(outgoing, reply), 10)
        finally:
            client.close()
        return run_desktop_client(incoming, outgoing, profile=profile)
    except BaseException:
        # A timed-out writer may still hold the stream. Never start a second
        # frame after any reply write has begun; the launcher closes this host.
        if correlation is not None and not reply_started:
            try:
                _bounded(lambda: _write_frame(outgoing, dict(correlation, kind='unavailable',
                    code='service_unavailable')), 10)
            except BaseException:
                pass
        return 1


if __name__ == '__main__':
    code = main()
    if code and '--serve' in sys.argv[1:]:
        # Bound only this dedicated host, including uncertain native worker drain.
        os._exit(code)
    raise SystemExit(code)
