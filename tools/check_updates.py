"""Explicit isolated QA staging proof. No settings discovery, feed, activation or test keys."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

from atom_search.runtime import _windows
from atom_search.runtime.storage import StorageBudget, StorageBudgetError
from atom_search.updates.contracts import UpdateError, UpdateVersions
from atom_search.updates.manifest import ManifestVerifier
from atom_search.updates.staging import stage_release
from atom_search.updates.transports import UNCTransport, read_verified_manifest
from atom_search.updates.trust import ReplayLedger, TrustStore, strict_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport', required=True, choices=('fixture', 'unc'))
    parser.add_argument('--release-root', required=True, type=Path)
    parser.add_argument('--qa-profile', required=True, type=Path)
    parser.add_argument('--staging-dir', required=True, type=Path)
    parser.add_argument('--trust-store', required=True, type=Path)
    parser.add_argument('--installed-versions', required=True, type=Path)
    parser.add_argument('--channel', required=True, choices=('stable', 'nightly'))
    parser.add_argument('--quota-bytes', required=True, type=int)
    parser.add_argument('--headroom-bytes', type=int, default=0)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    outcome = dict(ok=False, mode='source', transport=args.transport,
                   actual_unc_qualified=False, activation=False, frozen_proof=False)
    report = None
    try:
        profile = _windows.reject_reparse(args.qa_profile).resolve()
        source = _windows.reject_reparse(args.release_root).resolve()
        if profile.is_relative_to(source) or source.is_relative_to(profile):
            raise UpdateError('update_profile')
        selected_report = _windows.reject_reparse(args.report).resolve()
        if selected_report == profile or not selected_report.is_relative_to(profile):
            raise UpdateError('update_profile')
        marker = profile / 'update-source-qa.json'
        identity = dict(format_version=1, purpose='ssearch-update-source-qa')
        if profile.exists() and any(profile.iterdir()):
            _windows.reject_reparse(marker)
            with marker.open('rb') as stream:
                marker_bytes = stream.read(4097)
            if len(marker_bytes) > 4096 or strict_json(marker_bytes) != identity:
                raise UpdateError('update_profile')
        _windows.private_directory(profile)
        report = selected_report
        _windows.atomic_json(marker, identity)
        engine = _windows.private_directory(profile / 'engine')
        with _windows.reject_reparse(args.installed_versions).open('rb') as stream:
            installed_raw = stream.read(4097)
        if len(installed_raw) > 4096:
            raise UpdateError('update_limit')
        installed = UpdateVersions(**strict_json(installed_raw))
        verifier = ManifestVerifier(TrustStore.from_file(args.trust_store),
                                    ReplayLedger(engine / 'runtime/update-replay.json'))
        transport = UNCTransport(source, fixture_mode=args.transport == 'fixture')
        manifest = read_verified_manifest(transport, verifier, channel=args.channel,
                                           installed=installed, now=datetime.now(timezone.utc))
        budget = StorageBudget((engine,), quota_bytes=args.quota_bytes, min_free_bytes=0)
        result = stage_release(manifest, transport, staging_dir=args.staging_dir,
                                budget=budget, headroom_bytes=args.headroom_bytes)
        outcome.update(ok=True, release_id=manifest.release_id, payload_sha256=manifest.payload_sha256,
                       staged_files=len(result.files))
    except (UpdateError, StorageBudgetError, _windows.WindowsError, OSError, ValueError, TypeError) as error:
        outcome['code'] = (error.code if isinstance(error, UpdateError) else
                           'update_storage' if isinstance(error, StorageBudgetError) else 'update_profile')
    if report is not None and report.parent.exists():
        try:
            _windows.assert_private(report.parent)
            _windows.reject_reparse(report)
            _windows.atomic_json(report, outcome)
        except (OSError, _windows.WindowsError):
            return 1
    return 0 if outcome['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
