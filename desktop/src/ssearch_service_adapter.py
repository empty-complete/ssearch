"""Source-only entry shim; the reusable P2 host owns identity, auth and leases."""
import os
import sys

from atom_search.service.desktop_host import main


if __name__ == '__main__':
    code = main(sys.argv[1:])
    if code and sys.argv[1:2] == ['owner']:
        # Preserve the dedicated headless process's bounded terminal failure.
        os._exit(code)
    raise SystemExit(code)
