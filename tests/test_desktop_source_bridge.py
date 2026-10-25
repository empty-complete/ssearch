"""Mandatory live P4 source gate, explicitly targeted before shared packaging.

Before the standard-user launcher, copy setup-node's pinned Node 24.22.1 to
.tools/ci-node/node.exe; run desktop npm ci/compile with its pinned Electron;
and prepare Python 3.12.10/runtime inputs with tools/prepare_ci.py --source.
Select tests/test_desktop_source_bridge.py in the source test targets. Full
discovery skips these unpackaged inputs. This proves neither Full, frozen nor
native acceptance; future shared P4 packaging/build wiring remains required.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.environ.get('BUILD_MODE') != 'source',
                                reason='explicit live P4 source gate before shared packaging')


def test_desktop_live_source_bridge_and_electron_broker_proof():
    deadline = time.monotonic() + 900
    root = Path(__file__).resolve().parents[1]
    source_sha = os.environ.get('SOURCE_SHA', '')
    if re.fullmatch('[0-9a-f]{40}', source_sha) is None:
        pytest.fail('desktop_source_sha_required', pytrace=False)
    if sys.version_info[:3] != (3, 12, 10):
        pytest.fail('desktop_source_python_version_mismatch', pytrace=False)
    node = root / '.tools/ci-node/node.exe'
    tsx = root / 'desktop/node_modules/tsx/dist/cli.mjs'
    live = root / 'desktop/tests/service-source.live.ts'
    electron = root / 'desktop/node_modules/electron/dist/electron.exe'
    smoke = root / 'desktop/tests/electron-source.cjs'
    if not all(path.is_file() for path in (node, tsx, live, electron, smoke)):
        pytest.fail('desktop_source_inputs_missing', pytrace=False)
    receipts = (root / 'desktop/artifacts/source-lifecycle.json',
                root / 'desktop/artifacts/electron-source.json')
    if any(path.exists() or path.is_symlink() for path in receipts):
        pytest.fail('desktop_source_receipts_must_be_fresh', pytrace=False)

    def run(arguments, runner, *, version=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail('desktop_source_deadline_exceeded', pytrace=False)
        failure = None
        try:
            result = subprocess.run(arguments, cwd=root, shell=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if version else subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=remaining)
        except subprocess.TimeoutExpired:
            failure = 'desktop_source_' + runner + '_timeout'
        except OSError:
            failure = 'desktop_source_' + runner + '_runner_failed'
        if failure is not None:
            pytest.fail(failure, pytrace=False)
        if result.returncode != 0:
            pytest.fail(f'desktop_source_{runner}_exit_failed:{result.returncode}', pytrace=False)
        return result

    if run([node, '--version'], 'node_version', version=True).stdout.strip() != b'v24.22.1':
        pytest.fail('desktop_source_node_version_mismatch', pytrace=False)
    run([node, tsx, '--test', live], 'service_bridge')
    run([electron, smoke], 'electron_broker')
    for path in receipts:
        failure = None
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                failure = 'desktop_source_receipt_invalid'
            else:
                value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            failure = 'desktop_source_receipt_unavailable'
        if failure is not None:
            pytest.fail(failure, pytrace=False)
        if not isinstance(value, dict) or value.get('source_commit') != source_sha or value.get('ok') is not True:
            pytest.fail('desktop_source_receipt_invalid', pytrace=False)
