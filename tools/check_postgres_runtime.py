"""One-shot source checker using the same Runtime API as the frozen executable.

Only this owned process sanitizes PG* at startup, before any worker or libpq use.
--work-dir is a parent for a new private disposable profile, never existing PGDATA.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))
from atom_search.diagnostics import run_postgres_self_test
from atom_search.runtime.manifest import verify_runtime
from atom_search.runtime.postgres import sanitize_host_environment


def check_runtime(runtime_dir: Path, work_dir: Path) -> dict:
    # Integrity precedes creating even a disposable profile/report parent.
    verify_runtime(runtime_dir)
    work_dir = Path(work_dir).absolute()
    work_dir.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    profile = work_dir / ('atom-pg-proof-' + identity)
    report = work_dir / ('proof-' + identity + '.json')
    code = run_postgres_self_test(report, data_dir=profile, runtime_dir=runtime_dir)
    value = json.loads(report.read_text(encoding='utf-8'))
    if code:
        raise RuntimeError(value.get('error_code', 'postgres_proof_failed'))
    report.unlink()
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', type=Path, default=ROOT / 'resources/postgresql')
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        return 1
    try:
        sanitize_host_environment()
        report = check_runtime(args.runtime_dir, args.work_dir)
    except Exception:
        # No raw SQL, driver or secret-bearing exceptions in terminal/report.
        report = {'ok': False, 'error_code': 'postgres_checker_failed'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as output:
        output.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
