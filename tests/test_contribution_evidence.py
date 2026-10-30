"""Evidence safety contracts. Disposable Git/process fixtures execute only in CI."""
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "fictional/project"
TARGET = "tests/test_synthetic.py::test_defect"


@pytest.fixture
def api():
    sys.path.insert(0, str(ROOT))
    try:
        contracts = importlib.import_module("tools.agent_contributions.contracts")
        try:
            validation = importlib.import_module("tools.agent_contributions.validation")
            evidence = importlib.import_module("tools.agent_contributions.evidence")
        except ModuleNotFoundError:
            pytest.fail("Task2 evidence/validation contracts are not implemented", pytrace=False)
        return SimpleNamespace(**{name: getattr(contracts, name) for name in (
            "ContributionError", "CheckEvidence", "ReviewEvidence", "BuildEvidence")},
            **{name: getattr(validation, name) for name in (
                "CheckSpec", "ValidationRecipe", "ReceiptPin", "ReceiptStore", "snapshot")},
            ContributionRun=evidence.ContributionRun)
    finally:
        sys.path.remove(str(ROOT))


def git(path, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=path,
                            capture_output=True, text=True, shell=False, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def scenario(api):
    # Default CI TEMP can be inside the checkout: never use it as a developer root.
    with tempfile.TemporaryDirectory(prefix="ssearch-evidence-fixture-", dir=Path.home()) as value:
        root = Path(value)
        repo = root / "developer"
        repo.mkdir()
        git(repo, "init", "--initial-branch=main")
        git(repo, "config", "user.name", "Fictional implementer")
        git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "defect.py").write_text("def answer(): return 0\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "synthetic defect")
        commands = (
            api.CheckSpec("regression", "regression", (TARGET,), "source"),
            api.CheckSpec("python", "python", ("tests/test_synthetic.py",), "source"),
            api.CheckSpec("browser", "browser", ("tests/search.test.tsx",), "suite"),
            api.CheckSpec("frozen", "frozen", (), "frozen", ("model_ready", "clean_stop",
                "source_clean", "public_identity_preserved")),
            api.CheckSpec("native", "native", (), "native"),
        )
        recipe = api.ValidationRecipe(REPOSITORY, commands, ("python",), None, ())
        pins = []
        store = api.ReceiptStore(repository_id=REPOSITORY, pins=pins)
        run = api.ContributionRun(workspace=repo, implementer_id="implementer", recipe=recipe,
                                  receipts=store)
        state = SimpleNamespace(api=api, root=root, repo=repo, pins=pins, recipe=recipe,
                                store=store, run=run, serial=0)
        yield state


def report(s, command="python", *, exit_code=0, status=None, payload=None, trusted=True):
    current = s.api.snapshot(s.repo)
    spec = next(item for item in s.recipe.commands if item.command_id == command)
    s.serial += 1
    path = s.root / (str(s.serial) + ".json")
    if payload is None:
        payload = dict(mode="source", source_commit=current.source_sha,
            test_targets=list(spec.targets), full_suite_passed=False, test_exit_code=exit_code,
            exit_code=exit_code, source_verified_after_tests=True)
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if trusted:
        pin = s.api.ReceiptPin(path, digest, REPOSITORY, "41000000001", "52000000001",
            current.source_sha, current.tree_sha256, command, spec.report_kind)
        # The owner host imports authenticated GitHub metadata, never issue JSON.
        s.store.add_verified(pin)
    return s.api.CheckEvidence(command, command, current.source_sha, exit_code, digest,
        status or ("passed" if exit_code == 0 else "failed"), None)


def regression(s):
    red = report(s, "regression", exit_code=1)
    (s.repo / "defect.py").write_text("def answer(): return 42\n", encoding="utf-8")
    git(s.repo, "commit", "-am", "synthetic fix")
    green = report(s, "regression")
    s.run.record_regression(red, green)
    return red, green


def reviewed(s):
    regression(s)
    s.run.record_checks("python", report(s))
    tree = s.api.snapshot(s.repo).tree_sha256
    s.run.record_review(s.api.ReviewEvidence("independent-reviewer", tree, 0))
    return git(s.repo, "rev-parse", "HEAD")


def test_ready_requires_regression_required_scope_independent_review_and_commit(scenario):
    s = scenario
    assert s.run.ready() is False
    regression(s)
    assert s.run.ready() is False
    s.run.record_checks("python", report(s))
    assert s.run.ready() is False
    tree = s.api.snapshot(s.repo).tree_sha256
    for review in (s.api.ReviewEvidence("implementer", tree, 0),
                   s.api.ReviewEvidence("reviewer", "0" * 64, 0),
                   s.api.ReviewEvidence("reviewer", tree, 1)):
        with pytest.raises(s.api.ContributionError):
            s.run.record_review(review)
    s.run.record_review(s.api.ReviewEvidence("reviewer", tree, 0))
    assert s.run.ready() is False
    s.run.record_commit(git(s.repo, "rev-parse", "HEAD"))
    assert s.run.ready() is True


@pytest.mark.parametrize("kind", ["passed_red", "failed_green", "different_id", "different_targets"])
def test_regression_requires_same_approved_test_failed_red_then_passed_green(scenario, kind):
    s = scenario
    red = report(s, "regression", exit_code=0 if kind == "passed_red" else 1)
    green = report(s, "regression", exit_code=1 if kind == "failed_green" else 0)
    if kind == "different_id":
        green = report(s)
    elif kind == "different_targets":
        current = s.api.snapshot(s.repo)
        green = report(s, "regression", payload=dict(mode="source", source_commit=current.source_sha,
            test_targets=["tests/other.py"], full_suite_passed=False, test_exit_code=0,
            exit_code=0, source_verified_after_tests=True))
    with pytest.raises(s.api.ContributionError):
        s.run.record_regression(red, green)
    assert s.run.ready() is False


@pytest.mark.parametrize("kind", ["untrusted", "hash", "source", "status", "unknown_command",
                                  "duplicate_json", "source_changed_during_tests"])
def test_receipt_provenance_and_contents_are_bound_not_self_asserted(scenario, kind):
    s = scenario
    check = report(s, trusted=kind != "untrusted")
    if kind in {"hash", "source", "status", "unknown_command"}:
        field, value = {"hash": ("report_sha256", "f" * 64), "source": ("source_sha", "f" * 40),
            "status": ("status", "failed"), "unknown_command": ("command_id", "git reset --hard")}[kind]
        check = replace(check, **{field: value})
    elif kind in {"duplicate_json", "source_changed_during_tests"}:
        pin = s.store.pins[-1]
        raw = pin.report_path.read_bytes()
        raw = (b'{"mode":"source",' + raw[1:] if kind == "duplicate_json" else
               raw.replace(b'"source_verified_after_tests": true', b'"source_verified_after_tests": false'))
        pin.report_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        s.store.add_verified(replace(pin, report_sha256=digest))
        check = replace(check, report_sha256=digest)
    with pytest.raises(s.api.ContributionError):
        s.run.record_checks("python", check)
    assert s.run.ready() is False


def test_required_safety_scope_pending_and_optional_unsupported_are_honest(scenario):
    s = scenario
    s.recipe = replace(s.recipe, required_scopes=("python", "native"))
    s.run = s.api.ContributionRun(workspace=s.repo, implementer_id="implementer", recipe=s.recipe,
                                 receipts=s.store)
    target = reviewed(s)
    pending = s.api.CheckEvidence("Windows/model/compiler/UNC", "native", target, None, None,
                                 "not_run", "No authorized native environment")
    s.run.record_checks("native", pending)
    s.run.record_commit(target)
    assert s.run.ready() is False
    with pytest.raises(s.api.ContributionError):
        s.run.record_checks("native", replace(pending, status="passed", exit_code=0))
    with pytest.raises(s.api.ContributionError):
        s.run.record_checks("native", replace(pending, limitation=None))


def test_optional_unsupported_does_not_require_full_for_every_fix(scenario):
    s = scenario
    target = reviewed(s)
    pending = s.api.CheckEvidence("native", "native", target, None, None,
                                 "not_run", "No Windows 10 environment")
    s.run.record_checks("native", pending)
    s.run.record_commit(target)
    assert s.run.ready() is True


@pytest.mark.parametrize("change", ["tracked", "untracked", "head"])
def test_source_change_invalidates_review_commit_and_ready(scenario, change):
    s = scenario
    target = reviewed(s)
    s.run.record_commit(target)
    assert s.run.ready()
    if change == "tracked":
        (s.repo / "defect.py").write_text("def answer(): return 7\n", encoding="utf-8")
    elif change == "untracked":
        (s.repo / "extra.py").write_text("unreviewed = True\n", encoding="utf-8")
    else:
        git(s.repo, "commit", "--allow-empty", "-m", "changed commit identity")
    assert s.run.ready() is False
    with pytest.raises(s.api.ContributionError):
        s.run.record_commit(target)


def build(s, *, mode="quick"):
    current = s.api.snapshot(s.repo)
    # Current build-manifest fields; provenance/tree/clean binding comes from the
    # authenticated workflow pin, not invented manifest fields.
    raw = json.dumps(dict(source_commit=current.source_sha, app_version="0.2.0", core_version="0.2.0",
        test_scope="targeted" if mode == "quick" else "full",
        test_targets=["tests/test_synthetic.py"] if mode == "quick" else [],
        frontend_test_targets=[], desktop_test_targets=[], full_suite_passed=mode == "full",
        windows_validation=[], catalog=dict(backend="postgresql", schema_version=5),
        capabilities=dict(postgresql="application_verified"))).encode()
    path = s.root / "build-manifest.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    s.store.add_verified(s.api.ReceiptPin(path, digest, REPOSITORY, "41000000002", "52000000002",
        current.source_sha, current.tree_sha256, "build", "build"))
    return s.api.BuildEvidence(current.source_sha, True, path, digest, ())


@pytest.mark.parametrize("fault", ["before_commit", "dirty", "sha", "tree", "hash", "manifest",
                                  "source_only", "missing_frozen"])
def test_build_requires_commit_clean_exact_source_manifest_and_frozen(scenario, fault):
    s = scenario
    s.recipe = replace(s.recipe, build_mode="quick", required_frozen=("frozen",))
    s.run = s.api.ContributionRun(workspace=s.repo, implementer_id="implementer", recipe=s.recipe,
                                 receipts=s.store)
    target = reviewed(s)
    proof = build(s)
    if fault != "before_commit":
        s.run.record_commit(target)
    if fault == "dirty":
        (s.repo / "defect.py").write_text("dirty", encoding="utf-8")
    elif fault == "sha":
        proof = replace(proof, source_sha="0" * 40)
    elif fault == "tree":
        pin = s.store.pins[-1]
        s.store.add_verified(replace(pin, tree_sha256="0" * 64))
    elif fault == "hash":
        proof = replace(proof, manifest_sha256="0" * 64)
    elif fault == "manifest":
        proof.manifest_path.write_text("{}", encoding="utf-8")
    elif fault == "source_only":
        proof = replace(proof, manifest_path=s.store.pins[0].report_path,
                        manifest_sha256=s.store.pins[0].report_sha256)
    with pytest.raises(s.api.ContributionError):
        s.run.record_build(proof)
    assert s.run.ready() is False


def test_quick_build_and_frozen_proof_are_separate_from_full_and_native(scenario):
    s = scenario
    s.recipe = replace(s.recipe, build_mode="quick", required_frozen=("frozen",))
    s.run = s.api.ContributionRun(workspace=s.repo, implementer_id="implementer", recipe=s.recipe,
                                 receipts=s.store)
    target = reviewed(s)
    s.run.record_commit(target)
    assert s.run.ready() is False
    frozen = report(s, "frozen", payload=dict(source_commit=target, execution_mode="frozen",
        ok=True, model_ready=True, clean_stop=True, source_clean=True, public_identity_preserved=True))
    s.run.record_build(replace(build(s), frozen_checks=(frozen,)))
    assert s.run.ready() is True
    full_run = s.api.ContributionRun(workspace=s.repo, implementer_id="implementer",
        recipe=replace(s.recipe, build_mode="full"), receipts=s.store)
    full_run.record_regression(*regression_pair_from_store(s))
    full_run.record_checks("python", report(s))
    full_run.record_review(s.api.ReviewEvidence("reviewer", s.api.snapshot(s.repo).tree_sha256, 0))
    full_run.record_commit(target)
    with pytest.raises(s.api.ContributionError):
        full_run.record_build(replace(build(s), frozen_checks=(frozen,)))
    assert full_run.ready() is False


def regression_pair_from_store(s):
    values = [pin for pin in s.store.pins if pin.command_id == "regression"]
    return tuple(s.api.CheckEvidence("regression", "regression", pin.source_sha, code,
        pin.report_sha256, "failed" if code else "passed", None)
        for pin, code in zip(values[:2], (1, 0)))


def test_issue_command_is_data_and_validation_never_dispatches_it(scenario, monkeypatch):
    s = scenario
    def forbidden(*args, **kwargs):
        pytest.fail("recording data must never execute validation or issue commands")
    # ready uses a fixed read-only snapshot, not an issue-supplied command runner.
    check = report(s)
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(s.api.ContributionError):
        s.store.verify(replace(check, command_id="powershell -Command evil"), s.recipe)


def test_disposable_synthetic_defect_test_fix_review_commit_artifact_order(scenario, monkeypatch):
    s = scenario
    try:
        checker = importlib.import_module("tools.check_agent_contributions")
    except ModuleNotFoundError:
        pytest.fail("Task2 fixed synthetic CI scenario is not implemented", pytrace=False)
    observations = []
    root = s.root / "synthetic-scenario"
    output = s.root / "synthetic-report.json"
    # The helper additionally requires explicit fixture=True. It has no user
    # command/remote input and must never present synthetic output as production.
    assert __import__("os").environ.get("GITHUB_ACTIONS") == "true"
    checker.run_synthetic_scenario(workspace_root=root, report=output, fixture=True,
                                   observer=observations.append)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["synthetic"] is True
    assert result["published"] is False
    assert result["ready"] is True
    assert result["full_suite_passed"] is False
    assert result["frozen_status"] == result["native_status"] == "not_run"
    assert [entry["step"] for entry in observations] == [
        "red", "green", "review", "commit", "synthetic_artifact"]
    red, green, review, commit, artifact = observations
    assert red["exit_code"] != 0 and green["exit_code"] == 0
    assert red["test_id"] == green["test_id"]
    assert review["head_sha"] == green["head_sha"] == red["head_sha"]
    assert review["tree_sha256"] == commit["tree_sha256"]
    assert commit["head_sha"] != review["head_sha"]
    assert git(root / "repository", "rev-parse", "HEAD") == commit["head_sha"]
    assert result["target_sha"] == artifact["source_sha"] == commit["head_sha"]
    artifact_path = Path(artifact["path"])
    assert artifact_path.is_file()
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == artifact["sha256"]
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(s.api.ContributionError):
        checker.run_synthetic_scenario(workspace_root=s.root / "forbidden", report=output,
                                       fixture=True)
    assert not (s.root / "forbidden").exists()
