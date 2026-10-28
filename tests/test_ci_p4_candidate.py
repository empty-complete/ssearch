"""Exact-head candidate integration contracts; no native execution or publication."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = 'e4e93eebfa51c53ea1703d9a39a34e7c667828ba'
PAYLOAD = '99c53a141c4864072e8a357d2a0cc5423c1c810b'
HEAD = 'a' * 40
REPOSITORY = 'empty-complete/ssearch'
WORKFLOW = '.github/workflows/windows-build.yml'
INPUT = 'resources/ci/installer-proof-input.json'
CONFIG = '.github/p4-candidate.json'
CANDIDATE = 'frontend/compatibility/candidate-versions.json'
LOCKS = ('frontend/package-lock.json', 'desktop/package-lock.json')
CHECKS = ('fresh_install_verified', 'same_version_reinstall_verified', 'owned_uninstall_verified',
          'payload_inventory_verified', 'shortcut_ownership_verified', 'application_bytes_preserved',
          'application_index_preserved', 'postgres_bytes_preserved', 'postgres_restart_verified',
          'installed_proofs_passed', 'runtime_cleanup_confirmed', 'process_completion_confirmed',
          'registered_installation_removed', 'owned_shortcuts_removed', 'standard_user_profile_verified',
          'parent_job_drained')
OPEN = ['qt_upgrade', 'native_windows_11', 'native_visual_accessibility', 'windows_10_final',
        'full_p4_acceptance', 'stable', 'long_qa_paths']


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker():
    if not (ROOT / 'tools/ci_p4_candidate.py').is_file():
        # Keep tests collectable before implementation. Missing behavior must
        # fail assertions, never satisfy an expected identity-error refusal.
        class MissingContract:
            class CandidateError(RuntimeError):
                pass

            def __getattr__(self, name):
                def missing(*args, **kwargs):
                    pytest.fail('candidate_integration_contract_not_implemented', pytrace=False)
                return missing
        return MissingContract()
    return load_tool('ci_p4_candidate')


class PublicApi:
    def __init__(self, root):
        self.root = root
        self.downloaded = []
        original = json.loads((ROOT / INPUT).read_text(encoding='utf-8'))
        # These are the immutable original installer pins, not new decision data.
        keys = ('repository', 'source_commit', 'run_id', 'artifact_id', 'workflow_id', 'workflow_path',
                'head_branch', 'artifact_name', 'archive_size', 'archive_sha256', 'installer_name',
                'installer_size', 'installer_sha256')
        self.original = {key: original[key] for key in keys}
        self.pins = copy.deepcopy(self.original)
        self.policy = {}
        self.policy['ui_compatibility_decision'] = dict(status='candidate',
            candidateDigest=hashlib.sha256((ROOT / CANDIDATE).read_bytes()).hexdigest(),
            reports=[f'https://github.com/{REPOSITORY}/actions/runs/40409747788',
                     f'https://github.com/{REPOSITORY}/actions/runs/40741860014'], openGates=list(OPEN))
        self.pr = dict(number=3, changed_files=2,
            head=dict(sha=HEAD, ref='codex/p4-shared-package', repo=dict(full_name=REPOSITORY)),
            base=dict(sha='b' * 40, ref='main', repo=dict(full_name=REPOSITORY)))
        self.comparison = dict(status='ahead', base_commit=dict(sha=ANCHOR),
            merge_base_commit=dict(sha=ANCHOR), files=[dict(filename=CONFIG),
                dict(filename='tools/ci_p4_candidate.py')])
        self.receipt = dict(schema_version=1, ok=True, payload_source_commit=PAYLOAD,
            harness_source_commit=ANCHOR, installer_sha256=self.original['installer_sha256'],
            installer_size=self.original['installer_size'], artifact_run_id=self.original['run_id'],
            artifact_id=self.original['artifact_id'], payload_inventory_sha256='c' * 64,
            runner_platform='Windows Server 2025 x64', windows_validation=[], full_suite_passed=False,
            checks=dict.fromkeys(CHECKS, True),
            open_gates=['qt_upgrade', 'native_windows_11', 'windows_10', 'full_p4_acceptance'])
        self.archive = root / 'receipt.zip'
        self.write_archive()
        self.policy['installation_proof'] = dict(source_commit=ANCHOR, run_id=40741860014,
            artifact_id=12841186601, artifact_name='electron-service-installer-proof-' + ANCHOR,
            archive_size=self.archive.stat().st_size,
            archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            receipt_sha256=hashlib.sha256(self.receipt_bytes()).hexdigest(),
            inventory_sha256='c' * 64)
        self.quick_run = self.run(self.original['run_id'], PAYLOAD)
        self.quick_artifact = self.artifact(self.original, PAYLOAD)
        self.install_run = self.run(40741860014, ANCHOR)
        self.install_artifact = self.artifact(self.policy['installation_proof'], ANCHOR)
        self.jobs = {number: [dict(name=name, status='completed', conclusion='success')
                             for name in ('changes', 'build')]
                     for number in (40409747788, 40741860014)}
        for relative in (INPUT, CANDIDATE, *LOCKS):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / relative).read_bytes())
        self.save_pins()

    def receipt_bytes(self):
        return (json.dumps(self.receipt, indent=2) + '\n').encode('utf-8')

    def write_archive(self, extra=None):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('installation-receipt.json', self.receipt_bytes())
            if extra is not None:
                archive.writestr(extra, 'PRIVATE_SENTINEL')

    def save_pins(self):
        (self.root / INPUT).write_text(json.dumps(self.pins), encoding='utf-8')
        (self.root / CONFIG).parent.mkdir(parents=True, exist_ok=True)
        (self.root / CONFIG).write_text(json.dumps(self.policy), encoding='utf-8')

    def run(self, number, sha):
        return dict(id=number, workflow_id=395933832, path=WORKFLOW, event='workflow_dispatch',
                    status='completed', conclusion='success', head_sha=sha,
                    head_branch='codex/p4-shared-package', head_repository=dict(full_name=REPOSITORY))

    def artifact(self, pins, sha):
        return dict(id=pins['artifact_id'], name=pins['artifact_name'], expired=False,
                    size_in_bytes=pins['archive_size'], digest='sha256:' + pins['archive_sha256'],
                    workflow_run=dict(id=pins['run_id'], head_sha=sha))

    def get(self, endpoint, **kwargs):
        values = {'pulls/3': self.pr, f'compare/{ANCHOR}...{HEAD}': self.comparison,
                  'actions/runs/40409747788': self.quick_run,
                  'actions/artifacts/12693783261': self.quick_artifact,
                  'actions/runs/40741860014': self.install_run,
                  'actions/artifacts/12841186601': self.install_artifact}
        return copy.deepcopy(values[endpoint])

    def pages(self, endpoint, key=None):
        for number, jobs in self.jobs.items():
            if endpoint == f'actions/runs/{number}/jobs?filter=latest':
                assert key == 'jobs', 'unexpected_public_job_inventory'
                return copy.deepcopy(jobs)
        assert endpoint == 'pulls/3/files', 'unexpected_public_api_inventory'
        return [dict(filename='packaging/ssearch-service.spec'), dict(filename=WORKFLOW)]

    def file(self, path, ref):
        if path == INPUT and ref == ANCHOR:
            return json.dumps(self.original)
        if path == WORKFLOW:
            return 'name: Windows build\non: push\njobs:\n  changes:\n    value: true\n  build:\n    value: true\n  publish:\n    value: true\n'
        return (ROOT / path).read_text(encoding='utf-8')

    def command(self, arguments, *, output=None, **kwargs):
        assert arguments[-1] == f'repos/{REPOSITORY}/actions/artifacts/12841186601/zip'
        self.downloaded.append(12841186601)
        output.write(self.archive.read_bytes())


@pytest.fixture
def public_api(tmp_path):
    return PublicApi(tmp_path)


def test_existing_classifier_selects_real_source_route_instead_of_legacy_qt_full(public_api, monkeypatch, capsys):
    # Meaningful RED against the current classifier, independent of new-file
    # availability: it currently returns True, causing mode=full/Qt on this PR.
    publisher = load_tool('ci_publish')
    assert publisher.classify(public_api, 3, HEAD) == 'candidate-source'
    monkeypatch.setattr(publisher, 'GitHub', lambda: public_api)
    monkeypatch.setattr('sys.argv', ['ci_publish.py', 'classify', '--pr-number', '3', '--head-sha', HEAD])
    assert publisher.main() == 0
    assert capsys.readouterr().out == 'candidate-source\n'


def test_candidate_route_requires_exact_reviewed_ancestor_and_closed_diff(checker, public_api):
    assert checker.candidate_route(public_api, public_api.pr, HEAD) is True


@pytest.mark.parametrize('fault', ['head', 'head-repository', 'base-repository', 'base-branch'])
def test_candidate_identity_failures_are_explicit_refusals(checker, public_api, fault):
    if fault == 'head':
        public_api.pr['head']['sha'] = 'd' * 40
    elif fault == 'head-repository':
        public_api.pr['head']['repo']['full_name'] = 'foreign/repository'
    elif fault == 'base-repository':
        public_api.pr['base']['repo']['full_name'] = 'foreign/repository'
    else:
        public_api.pr['base']['ref'] = 'other'
    with pytest.raises(checker.CandidateError):
        checker.candidate_route(public_api, public_api.pr, HEAD)


@pytest.mark.parametrize('fault', ['ancestry', 'unknown', 'rename', 'truncated', 'code', 'lock', 'resource'])
def test_unknown_or_changed_packaged_inputs_cannot_enter_candidate_route(checker, public_api, fault):
    if fault == 'ancestry':
        public_api.comparison['merge_base_commit']['sha'] = 'd' * 40
    elif fault == 'truncated':
        public_api.comparison['files'] *= 150
    else:
        path = {'unknown': 'arbitrary.txt', 'rename': CONFIG, 'code': 'desktop/src/main.ts',
                'lock': LOCKS[0], 'resource': INPUT}[fault]
        public_api.comparison['files'].append(dict(filename=path,
            **({'previous_filename': 'src/private.py'} if fault == 'rename' else {})))
    assert checker.candidate_route(public_api, public_api.pr, HEAD) is False


def test_authenticated_prior_proofs_are_reused_without_claiming_head_native_or_full(checker, public_api):
    result = checker.verify_candidate_inputs(public_api, public_api.root, HEAD)
    assert result == dict(route='candidate-source', source_commit=HEAD, payload_source_commit=PAYLOAD,
                         installation_harness_commit=ANCHOR, full_suite_passed=False,
                         windows_validation=[])
    assert public_api.downloaded == [12841186601]  # Tiny receipt only, no new payload download/build.


@pytest.mark.parametrize('number', [40409747788, 40741860014])
@pytest.mark.parametrize('fault', ['skipped', 'failed', 'missing', 'changes-failed'])
def test_prior_overall_success_does_not_replace_actual_changes_and_build_success(checker, public_api, number, fault):
    if fault == 'missing':
        public_api.jobs[number].pop()
    else:
        job = public_api.jobs[number][0 if fault == 'changes-failed' else 1]
        job['conclusion'] = 'skipped' if fault == 'skipped' else 'failure'
    with pytest.raises(checker.CandidateError):
        checker.verify_candidate_inputs(public_api, public_api.root, HEAD)


@pytest.mark.parametrize('fault', ['quick-head', 'quick-digest', 'installation-head', 'installation-digest',
                                  'expired', 'receipt-hash', 'native-claim', 'full-claim', 'cleanup',
                                  'drain', 'inventory', 'extra-entry', 'candidate', 'lock', 'input-pin', 'input-format'])
def test_proof_identity_integrity_and_scoped_acceptance_fail_closed(checker, public_api, fault):
    if fault == 'quick-head':
        public_api.quick_run['head_sha'] = HEAD
    elif fault == 'quick-digest':
        public_api.quick_artifact['digest'] = 'sha256:' + '0' * 64
    elif fault == 'installation-head':
        public_api.install_run['head_sha'] = HEAD
    elif fault == 'installation-digest':
        public_api.install_artifact['digest'] = 'sha256:' + '0' * 64
    elif fault == 'expired':
        public_api.install_artifact['expired'] = True
    elif fault == 'receipt-hash':
        public_api.policy['installation_proof']['receipt_sha256'] = '0' * 64
    elif fault in ('native-claim', 'full-claim', 'cleanup', 'drain', 'inventory', 'extra-entry'):
        if fault == 'native-claim':
            public_api.receipt['windows_validation'] = ['Windows 11']
        elif fault == 'full-claim':
            public_api.receipt['full_suite_passed'] = True
        elif fault == 'cleanup':
            public_api.receipt['checks']['runtime_cleanup_confirmed'] = False
        elif fault == 'drain':
            public_api.receipt['checks']['parent_job_drained'] = False
        elif fault == 'inventory':
            public_api.receipt['payload_inventory_sha256'] = '0' * 64
        public_api.write_archive(extra='unexpected.json' if fault == 'extra-entry' else None)
        proof = public_api.policy['installation_proof']
        proof.update(archive_size=public_api.archive.stat().st_size,
                     archive_sha256=hashlib.sha256(public_api.archive.read_bytes()).hexdigest(),
                     receipt_sha256=hashlib.sha256(public_api.receipt_bytes()).hexdigest())
        public_api.install_artifact = public_api.artifact(proof, ANCHOR)
    elif fault in ('candidate', 'lock'):
        (public_api.root / (CANDIDATE if fault == 'candidate' else LOCKS[0])).write_text('{}', encoding='utf-8')
    elif fault == 'input-pin':
        public_api.pins['installer_sha256'] = '0' * 64
    public_api.save_pins()
    if fault == 'input-format':
        path = public_api.root / INPUT
        path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(checker.CandidateError):
        checker.verify_candidate_inputs(public_api, public_api.root, HEAD)


@pytest.mark.parametrize('fault', ['symlink', 'reparse', 'duplicate', 'encrypted', 'oversize'])
def test_installation_receipt_archive_rejects_unsafe_entries_before_acceptance(checker, public_api, fault):
    content = public_api.receipt_bytes() + (b' ' * 65536 if fault == 'oversize' else b'')
    info = zipfile.ZipInfo('installation-receipt.json')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16 if fault == 'symlink' else (stat.S_IFREG | 0o600) << 16
    if fault == 'reparse':
        info.external_attr |= 0x400
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(public_api.archive, 'w') as archive:
        archive.writestr(info, content)
        if fault == 'duplicate':
            with pytest.warns(UserWarning, match='Duplicate name'):
                archive.writestr(info, content)
    if fault == 'encrypted':
        data = bytearray(public_api.archive.read_bytes())
        central = data.index(b'PK\x01\x02')
        data[6] |= 1
        data[central + 8] |= 1
        public_api.archive.write_bytes(data)
    proof = public_api.policy['installation_proof']
    proof.update(archive_size=public_api.archive.stat().st_size,
                 archive_sha256=hashlib.sha256(public_api.archive.read_bytes()).hexdigest(),
                 receipt_sha256=hashlib.sha256(content).hexdigest())
    public_api.install_artifact = public_api.artifact(proof, ANCHOR)
    public_api.save_pins()
    with pytest.raises(checker.CandidateError):
        checker.verify_candidate_inputs(public_api, public_api.root, HEAD)


@pytest.mark.parametrize('fault', [None, 'provisional', 'accepted', 'digest', 'reports', 'open', 'extra'])
def test_candidate_decision_preserves_factual_server_scope_and_open_platform_gates(checker, public_api, fault):
    decision = copy.deepcopy(public_api.policy['ui_compatibility_decision'])
    if fault in ('provisional', 'accepted'):
        decision['status'] = 'win11_provisional' if fault == 'provisional' else 'accepted'
    elif fault == 'digest':
        decision['candidateDigest'] = '0' * 64
    elif fault == 'reports':
        decision['reports'] = ['https://foreign.example/PRIVATE_SENTINEL']
    elif fault == 'open':
        decision['openGates'].remove('native_windows_11')
    elif fault == 'extra':
        decision['private_path'] = 'PRIVATE_SENTINEL'
    if fault is None:
        assert checker.validate_decision(decision, (ROOT / CANDIDATE).read_bytes(), pins=public_api.pins, proof=public_api.policy['installation_proof']) == decision
    else:
        with pytest.raises(checker.CandidateError):
            checker.validate_decision(decision, (ROOT / CANDIDATE).read_bytes(), pins=public_api.pins, proof=public_api.policy['installation_proof'])
