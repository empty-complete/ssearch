"""Source-only private MAIN adapter. All authentication/identity remains in P2.

No endpoint, token, credential reference or arbitrary method is accepted from MAIN.
The initial frame is adapter correlation only, not service authority. stdout is
bounded protocol, stderr is intentionally unused. Frozen launch belongs to packaging.
"""
import argparse
import hashlib
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description='ssearch private source adapter')
    parser.add_argument('mode', choices=('profile', 'broker'))
    parser.add_argument('--profile', type=Path)
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
        if (type(context) is not dict or set(context) != {'schema_version', 'session_id', 'challenge'}
                or type(context['schema_version']) is not int or context['schema_version'] != 1):
            return 1
        public_uuid(context['session_id']); public_uuid(context['challenge'])
        profile = _windows.reject_reparse((args.profile or data_path()).absolute()).resolve()
        _windows.volume_identity(profile)
        user = _windows.user_sid()
        key = hashlib.sha256((user + '|' + str(profile).casefold()).encode('utf-8')).hexdigest()
        reply = dict(schema_version=1, session_id=context['session_id'], challenge=context['challenge'])
        if args.mode == 'profile':
            reply.update(kind='profile', profile=str(profile), key=key, service_version=VERSION,
                         python_base=str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                         python_venv=str(Path(sys.executable).absolute()))
            _bounded(lambda: _write_frame(outgoing, reply), 10)
            return 0
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
    raise SystemExit(main())
