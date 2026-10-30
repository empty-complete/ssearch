"""Explicit CI-only disposable evidence fixture; never a production validator.

Only fixed synthetic unittest/Git operations below can execute, and only after
both fixture=True and GITHUB_ACTIONS=true. No issue, remote, or command input.
The actual contribution API records receipts without executing tests/builds.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agent_contributions.config import local_path, require
from tools.agent_contributions.contracts import CheckEvidence, ContributionError, ReviewEvidence
from tools.agent_contributions.evidence import ContributionRun
from tools.agent_contributions.validation import (CheckSpec, ReceiptPin, ReceiptStore,
                                                  ValidationRecipe, snapshot)


def run_synthetic_scenario(*, workspace_root: Path, report: Path, fixture: bool = False,
                           observer=None) -> None:
    require(fixture is True and os.environ.get("GITHUB_ACTIONS") == "true", "github_fixture_only")
    root = local_path(workspace_root)
    output = local_path(report)
    require(root.is_relative_to(Path.home().resolve()) and root.parent.is_dir()
            and not root.exists() and output.parent == root.parent and not output.exists(), "unsafe_fixture")
    for ancestor in (root.parent, *root.parent.parents):
        require(not (ancestor / ".git").exists()
                and not ((ancestor / "HEAD").is_file() and (ancestor / "objects").is_dir()), "unsafe_fixture")
    root.mkdir()
    repository = root / "repository"
    repository.mkdir()
    executable = shutil.which("git")
    require(executable is not None, "git_unavailable")
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")

    def git(operation):
        operations = {
            "init": ["init", "--initial-branch=main", "--template="],
            "add": ["add", "--", "defect.py", "test_synthetic.py", ".gitignore"],
            "base": ["-c", "user.name=Fictional implementer", "-c", "user.email=fixture@example.invalid",
                     "commit", "-m", "synthetic defect"],
            "fix": ["-c", "user.name=Fictional implementer", "-c", "user.email=fixture@example.invalid",
                    "commit", "-m", "synthetic fix after fixture review"],
        }
        try:
            result = subprocess.run([executable, "-c", "core.hooksPath=/dev/null",
                "-c", "core.autocrlf=false", *operations[operation]], cwd=repository, env=env,
                shell=False, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise ContributionError("fixture_failed") from None
        require(result.returncode == 0, "fixture_failed")

    def observe(step, **values):
        if observer is not None:
            observer(dict(step=step, **values))

    git("init")
    (repository / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repository / "defect.py").write_text("def answer(): return 0\n", encoding="utf-8")
    (repository / "test_synthetic.py").write_text(
        "import unittest\nfrom defect import answer\n"
        "class SyntheticDefect(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(answer(), 42)\n", encoding="utf-8")
    git("add")
    git("base")
    test_id = "test_synthetic.SyntheticDefect.test_answer"
    spec = CheckSpec("synthetic-regression", "regression", (test_id,), "synthetic")
    recipe = ValidationRecipe("synthetic/fixture", (spec,), (), None, (), fixture=True)
    store = ReceiptStore(repository_id=recipe.repository_id)
    run = ContributionRun(workspace=repository, implementer_id="fixture-implementer",
                          recipe=recipe, receipts=store)

    def check(step):
        # Exactly one known synthetic test; no shell or caller-controlled argv.
        try:
            result = subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover",
                "-s", ".", "-p", "test_synthetic.py"], cwd=repository, env=env,
                shell=False, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise ContributionError("fixture_failed") from None
        current = snapshot(repository)
        payload = dict(synthetic=True, source_commit=current.source_sha, test_targets=[test_id],
                       exit_code=result.returncode)
        raw = json.dumps(payload).encode()
        path = root / (step + ".json")
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        # These locally produced pins are admitted ONLY into the explicit
        # synthetic recipe; this is not an authenticated GitHub importer.
        store.add_verified(ReceiptPin(path, digest, recipe.repository_id, "1", "1",
            current.source_sha, current.tree_sha256, spec.command_id, "synthetic", current.clean))
        observe(step, exit_code=result.returncode, test_id=test_id, head_sha=current.source_sha,
                tree_sha256=current.tree_sha256)
        return CheckEvidence(step, spec.command_id, current.source_sha, result.returncode,
            digest, "passed" if result.returncode == 0 else "failed", None)

    red = check("red")
    require(red.exit_code == 1, "fixture_red_required")
    (repository / "defect.py").write_text("def answer(): return 42\n", encoding="utf-8")
    green = check("green")
    require(green.exit_code == 0, "fixture_green_required")
    run.record_regression(red, green)
    reviewed = snapshot(repository)
    # Simulate the external reviewer identity explicitly; no real review claimed.
    run.record_review(ReviewEvidence("fixture-independent-reviewer", reviewed.tree_sha256, 0))
    observe("review", head_sha=reviewed.source_sha, tree_sha256=reviewed.tree_sha256)
    git("add")
    git("fix")
    committed = snapshot(repository)
    run.record_commit(committed.source_sha)
    observe("commit", head_sha=committed.source_sha, tree_sha256=committed.tree_sha256)
    artifact = root / "synthetic-artifact.json"
    artifact.write_text(json.dumps(dict(synthetic=True, source_sha=committed.source_sha,
        reviewed_tree_sha256=reviewed.tree_sha256)), encoding="utf-8")
    observe("synthetic_artifact", source_sha=committed.source_sha, path=str(artifact),
            sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    output.write_text(json.dumps(dict(synthetic=True, fixture_ready=run.ready(), published=False,
        target_sha=committed.source_sha, full_suite_passed=False, frozen_status="not_run",
        native_status="not_run")), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed disposable synthetic evidence scenario in GitHub")
    parser.add_argument("--scenario", choices=("evidence",), required=True)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_synthetic_scenario(workspace_root=args.workspace_root, report=args.report, fixture=args.fixture)
        return 0
    except ContributionError as exc:
        print(json.dumps(dict(error_code=exc.code)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
