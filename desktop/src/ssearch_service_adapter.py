"""Source-only private MAIN adapter. All authentication/identity remains in P2.

No endpoint, token, credential reference or arbitrary method is accepted from MAIN.
The initial frame is adapter correlation only, not service authority. stdout is
bounded protocol, stderr is intentionally unused. Frozen launch belongs to packaging.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import threading


def serve_with_parent_lease(profile, model_dir, parent):
    """Only the exact retained MAIN object and our private EOF can stop this source entry."""
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

        watcher = threading.Thread(target=watch_parent, name='ssearch-source-parent', daemon=True)
        watcher.start()  # Retained identity watch is installed before P2 owner composition.
        from atom_search.service.headless import _wait_control_eof, serve

        def watch_control():
            try:
                _wait_control_eof(sys.stdin, stop)
            except BaseException:
                failed.set()
            finally:
                stop.set()

        control = threading.Thread(target=watch_control, name='ssearch-source-control', daemon=True)
        control.start()
        try:
            result = 0 if stop.is_set() else serve(profile, model_dir=model_dir, stop=stop)
        finally:
            stop.set()
            watcher.join(1)
            control.join(1)
        return 1 if failed.is_set() or watcher.is_alive() or control.is_alive() else result


def main():
    parser = argparse.ArgumentParser(description='ssearch private source adapter')
    parser.add_argument('mode', choices=('profile', 'broker', 'owner'))
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--model-dir', type=Path)
    args = parser.parse_args()
    from atom_search import VERSION
    from atom_search.paths import data_path
    from atom_search.runtime import _windows
    from atom_search.service.desktop_client import _bounded, _read_frame, _write_frame, run_desktop_client
    from atom_search.http.security import public_uuid
    from atom_search.service.contracts import ServiceError
    context = None
    try:
        incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
        first = _bounded(lambda: incoming.read(1), 10)
        if not first:
            return 1
        context = _bounded(lambda: _read_frame(incoming, first), 10)
        expected = {'schema_version', 'session_id', 'challenge'}
        if args.mode == 'profile':
            expected |= {'parent_pid', 'parent_executable'}
        elif args.mode == 'owner':
            expected.add('parent')
        if (type(context) is not dict or set(context) != expected
                or type(context['schema_version']) is not int or context['schema_version'] != 1):
            return 1
        public_uuid(context['session_id']); public_uuid(context['challenge'])
        profile = _windows.reject_reparse((args.profile or data_path()).absolute()).resolve()
        _windows.volume_identity(profile)
        user = _windows.user_sid()
        key = hashlib.sha256((user + '|' + str(profile).casefold()).encode('utf-8')).hexdigest()
        reply = dict(schema_version=1, session_id=context['session_id'], challenge=context['challenge'])
        if args.mode == 'profile':
            if (type(context['parent_pid']) is not int or context['parent_pid'] <= 0
                    or type(context['parent_executable']) is not str):
                return 1
            parent = _windows.process_identity(context['parent_pid'])
            if (parent is None or parent.executable != os.path.normcase(
                    str(Path(context['parent_executable']).resolve()))):
                return 1
            reply.update(kind='profile', profile=str(profile), key=key, service_version=VERSION,
                         python_base=str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                         python_venv=str(Path(sys.executable).absolute()),
                         parent=dict(pid=parent.pid, executable=parent.executable, created=str(parent.created)))
            _bounded(lambda: _write_frame(outgoing, reply), 10)
            return 0
        if args.mode == 'owner':
            if args.profile is None or args.model_dir is None:
                return 1
            return serve_with_parent_lease(profile, args.model_dir, context['parent'])
        from atom_search.service.client import ServiceClient, read_owner_state
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
            reply.update(kind='ready', profile=str(profile), key=key,
                api_version='1', service_version=status.service_version, app_version=status.app_version,
                generation=client.descriptor['generation'], installation_id=client.descriptor['installation_id'],
                owner=owner, capabilities=['settings_validate', 'settings_apply', 'start_catalog', 'cancel_job'] if owner else [],
                process=dict(pid=identity.pid, executable=identity.executable, created=str(identity.created)),
                broker=dict(pid=broker.pid, executable=broker.executable, created=str(broker.created)))
            _bounded(lambda: _write_frame(outgoing, reply), 10)
        finally:
            client.close()
        return run_desktop_client(incoming, outgoing, profile=profile)
    except BaseException:
        if context is not None:
            try:
                _write_frame(sys.stdout.buffer, dict(schema_version=1, kind='unavailable',
                    session_id=context['session_id'], challenge=context['challenge'], code='service_unavailable'))
            except BaseException:
                pass
        return 1


if __name__ == '__main__':
    code = main()
    if code and sys.argv[1:2] == ['owner']:
        # Preserve headless's terminal bounded failure, including outstanding native workers.
        os._exit(code)
    raise SystemExit(code)
