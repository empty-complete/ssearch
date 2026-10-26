"""Print diagnostic facts only; this output never qualifies a frozen gate."""
import argparse
import ast
import builtins
import json
from pathlib import Path
import re
import stat


ROOT = Path(__file__).resolve().parents[1]
ROLES = ('service-owner', 'postgres', 'application', 'ui-load', 'ui-security', 'ui-lifecycle', 'ui-empty-profile')
BOOLEANS = frozenset(('ok', 'clean_stop', 'model_ready', 'source_clean', 'public_identity_preserved',
    'two_clients', 'scoped_fetch', 'metadata_only', 'auth', 'attach', 'second_launch', 'token_absent',
    'scram', 'wrong_password_rejected', 'profile_removed', 'restart_verified', 'dpapi_verified',
    'private_acl_verified', 'application_privileges_verified'))
COUNTS = frozenset(('model_constructions', 'runtime_starts', 'runtime_stops', 'core_closes',
                    'files', 'vector_count', 'dimension', 'source_count', 'max_batch'))


def public_symbols():
    """Names/literals from actual first-party source, without importing it."""
    modules, codes = set(), set()
    types = {name for name, value in vars(builtins).items()
             if isinstance(value, type) and issubclass(value, BaseException)}
    for source in (ROOT / 'src', *(ROOT / 'packages').glob('*/src')):
        for path in source.rglob('*.py'):
            relative = path.relative_to(source).with_suffix('')
            parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
            if parts:
                modules.add('.'.join(parts))
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.endswith(('Error', 'Exception')):
                    types.add(node.name)
                if isinstance(node, ast.Constant) and type(node.value) is str and re.fullmatch('[a-z][a-z0-9_]{0,79}', node.value):
                    codes.add(node.value)
    for path in (ROOT / 'desktop/src').glob('diagnostics*.ts'):
        codes.update(re.findall(r"'([a-z][a-z0-9_]{0,79})'", path.read_text(encoding='utf-8')))
    return modules, codes, types


def failure_detail(value, modules, types):
    errors, frames = [], 0
    if not isinstance(value, dict) or not isinstance(value.get('exceptions'), list):
        return {'exceptions': errors}
    for error in value['exceptions'][:8]:
        if not isinstance(error, dict) or error.get('type') not in types:
            continue
        locations = []
        for location in error.get('locations', [])[:12] if isinstance(error.get('locations'), list) else []:
            if (frames < 12 and isinstance(location, dict) and location.get('module') in modules
                    and type(location.get('line')) is int and 0 < location['line'] <= 1_000_000):
                locations.append({'module': location['module'], 'line': location['line']})
                frames += 1
        errors.append({'type': error['type'], 'locations': locations})
    return {'exceptions': errors}


def ui_checks(report, role):
    source = (ROOT / 'desktop/src/diagnosticsPolicy.ts').read_text(encoding='utf-8')
    block, = re.findall(r'export const requiredUiChecks = \{(.*?)\} as const;', source, re.S)
    required = {quoted or bare: ast.literal_eval(values) for quoted, bare, values in
                re.findall(r"(?:'([^']+)'|([a-z-]+))\s*:\s*(\[[^\]]*\])", block, re.S)}
    checks = report.get('checks', [])
    if not isinstance(checks, list):
        checks = []
    result = []
    for name in required[role.removeprefix('ui-')]:
        observed = [item.get('status') for item in checks if isinstance(item, dict) and item.get('name') == name]
        status = 'missing' if not observed else observed[0] if len(observed) == 1 and observed[0] in ('pass', 'fail', 'not_run') else 'invalid'
        result.append({'name': name, 'status': status})
    return result


def summarize_report(path, role, exit_code, *, not_before_ms):
    if role not in ROLES or type(exit_code) is not int or exit_code not in (0, 1) or type(not_before_ms) is not int or not_before_ms < 0:
        raise ValueError('invalid_summary_request')
    result = {'diagnostic_only': True, 'role': role, 'process_exit_code': exit_code,
              'report_state': 'unavailable', 'fields': {}}
    try:
        path = Path(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not 0 < info.st_size <= 65536 or info.st_mtime_ns // 1_000_000 < not_before_ms):
            return result
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return result
        report = json.loads(raw.decode('utf-8'))
        if not isinstance(report, dict):
            return result
        modules, codes, types = public_symbols()
        fields = {name: report[name] for name in BOOLEANS if type(report.get(name)) is bool}
        fields.update({name: report[name] for name in COUNTS
                       if type(report.get(name)) is int and 0 <= report[name] <= 1_000_000_000})
        for name in ('phase', 'error_code', 'cleanup_error', 'reason_code'):
            allowed = codes | types if name in ('error_code', 'cleanup_error') else codes
            if type(report.get(name)) is str and report[name] in allowed:
                fields[name] = report[name]
        for name in ('error_type', 'cleanup_error_type'):
            if type(report.get(name)) is str and report[name] in types:
                fields[name] = report[name]
        for name in ('failure_detail', 'cleanup_failure_detail'):
            if name in report:
                fields[name] = failure_detail(report[name], modules, types)
        if role.startswith('ui-'):
            fields['required_checks'] = ui_checks(report, role)
        result.update(report_state='read', fields=fields)
    except (OSError, ValueError, TypeError, KeyError, SyntaxError, AttributeError):
        pass
    return result


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'frozen_summary_invalid_arguments\n')


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--exit-code', type=int, choices=(0, 1), required=True)
    parser.add_argument('--not-before-ms', type=int, required=True)
    args = parser.parse_args()
    try:
        summary = summarize_report(args.report, args.role, args.exit_code, not_before_ms=args.not_before_ms)
    except Exception:
        parser.exit(2, 'frozen_summary_unavailable\n')
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
