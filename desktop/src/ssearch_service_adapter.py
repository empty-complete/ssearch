"""Source-only entry shim; the reusable P2 host owns identity, auth and leases."""
import os
import sys

from atom_search.service.desktop_host import main


if __name__ == '__main__':
    roles = {'profile': ['--profile-info'], 'broker': ['--desktop-client'],
             'owner': ['--serve', '--parent-lease']}
    role = sys.argv[1] if len(sys.argv) > 1 else None
    if role not in roles:
        raise SystemExit(2)
    # Source call sites keep their accepted roles; canonical validation belongs to the host.
    arguments = [*roles[role], *('--data-dir' if value == '--profile' else value for value in sys.argv[2:])]
    code = main(arguments)
    if code and role == 'owner':
        # Preserve the dedicated headless process's bounded terminal failure.
        os._exit(code)
    raise SystemExit(code)
