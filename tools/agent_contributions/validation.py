"""Read-only source fingerprints and parsers for owner-pinned CI receipts.

No validation commands, CI dispatch, commit, or authentication are implemented
here. Receipt pins and recipes are protected inputs from a trusted host which
has independently authenticated the GitHub workflow/job/artifact provenance.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .config import SHA, local_path, require
from .contracts import BuildEvidence, CheckEvidence, ContributionError


DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")


@dataclass(frozen=True)
class SourceSnapshot:
    source_sha: str
    tree_sha256: str
    clean: bool


def _query(workspace: Path, operation: str) -> bytes:
    # This is an enumerated read-only interface, never an issue command runner.
    operations = {
        "root": ["rev-parse", "--show-toplevel"],
        "head": ["rev-parse", "--verify", "HEAD"],
        "index": ["ls-files", "--stage", "-z"],
        "untracked": ["ls-files", "--others", "--exclude-standard", "-z"],
        "status": ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    }
    executable = shutil.which("git")
    require(executable is not None and operation in operations, "source_unavailable")
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run([executable, "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
            *operations[operation]], cwd=local_path(workspace), env=env, shell=False,
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ContributionError("source_unavailable") from None
    require(result.returncode == 0 and len(result.stdout) <= 16 * 1024 * 1024, "source_unavailable")
    return result.stdout


def snapshot(workspace: Path) -> SourceSnapshot:
    """Hash exact tracked/untracked working bytes and modes; never write Git state.

    Fingerprints deliberately include untracked nonignored source. Ignored build
    output is not source; pins must originate from the same verified CI checkout.
    This is a working-tree SHA-256, not a Git SHA-1 tree object identifier.
    """
    workspace = local_path(workspace)
    try:
        root = Path(_query(workspace, "root").decode("utf-8").strip()).resolve()
        require(root == workspace, "source_root_mismatch")
        head = _query(workspace, "head").decode("ascii").strip()
        require(SHA.fullmatch(head) is not None, "source_unavailable")
        entries = {}
        for row in _query(workspace, "index").split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            mode, _oid, stage = metadata.decode("ascii").split()
            require(stage == "0" and mode in {"100644", "100755"}, "unsupported_source_entry")
            entries[name.decode("utf-8")] = mode
        for raw in _query(workspace, "untracked").split(b"\0"):
            if raw:
                entries[raw.decode("utf-8")] = "100644"
        rows = []
        for name, mode in sorted(entries.items()):
            relative = Path(name)
            require(not relative.is_absolute() and ".." not in relative.parts, "unsafe_source_entry")
            path = local_path(workspace / relative)
            require(path.is_relative_to(workspace), "unsafe_source_entry")
            if not path.exists():
                rows.append([name, mode, "missing"])
                continue
            require(path.is_file(), "unsupported_source_entry")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            rows.append([name, mode, digest.hexdigest()])
        clean = not _query(workspace, "status")
        # Reject a HEAD/index change during the fingerprint read.
        require(_query(workspace, "head").decode("ascii").strip() == head, "source_changed")
        tree = hashlib.sha256(json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        return SourceSnapshot(head, tree, clean)
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ContributionError):
            raise
        raise ContributionError("source_unavailable") from None


@dataclass(frozen=True)
class CheckSpec:
    command_id: str
    scope: str
    targets: tuple[str, ...]
    report_kind: str
    required_checks: tuple[str, ...] = ()
    target_group: str = "python"


@dataclass(frozen=True)
class ValidationRecipe:
    """Owner-approved named checks, required scopes and actual package boundary."""

    repository_id: str
    commands: tuple[CheckSpec, ...]
    required_scopes: tuple[str, ...]
    build_mode: str | None
    required_frozen: tuple[str, ...]
    fixture: bool = False

    def __post_init__(self):
        require(isinstance(self.repository_id, str) and bool(self.repository_id), "invalid_recipe")
        require(type(self.fixture) is bool and isinstance(self.commands, tuple) and bool(self.commands),
                "invalid_recipe")
        ids = set()
        scopes = set()
        for command in self.commands:
            require(isinstance(command, CheckSpec) and isinstance(command.command_id, str)
                    and IDENTIFIER.fullmatch(command.command_id) is not None
                    and command.command_id not in ids, "invalid_recipe")
            require(isinstance(command.scope, str) and IDENTIFIER.fullmatch(command.scope) is not None,
                    "invalid_recipe")
            require(isinstance(command.report_kind, str)
                    and command.report_kind in {"source", "suite", "native", "frozen", "synthetic"}
                    and (command.report_kind != "synthetic" or self.fixture), "invalid_recipe")
            require(isinstance(command.target_group, str)
                    and command.target_group in {"python", "frontend", "desktop"}, "invalid_recipe")
            require(isinstance(command.targets, tuple) and all(isinstance(item, str) and item
                    and len(item) <= 2048 and not any(ord(c) < 32 for c in item)
                    for item in command.targets), "invalid_recipe")
            require(isinstance(command.required_checks, tuple)
                    and all(isinstance(item, str) and IDENTIFIER.fullmatch(item) is not None
                            for item in command.required_checks), "invalid_recipe")
            if command.report_kind in {"source", "suite", "synthetic"}:
                require(bool(command.targets), "invalid_recipe")
            if command.report_kind == "frozen":
                require(bool(command.required_checks), "invalid_recipe")
            ids.add(command.command_id)
            scopes.add(command.scope)
        require(isinstance(self.required_scopes, tuple) and set(self.required_scopes) <= scopes
                and (self.build_mode is None or isinstance(self.build_mode, str)
                     and self.build_mode in {"quick", "full"})
                and isinstance(self.required_frozen, tuple) and set(self.required_frozen) <= ids,
                "invalid_recipe")
        require(not self.required_frozen or self.build_mode is not None, "invalid_recipe")
        require(self.build_mode is None or bool(self.required_frozen), "invalid_recipe")
        require(all(self.spec(item).report_kind == "frozen" for item in self.required_frozen), "invalid_recipe")

    def spec(self, command_id: str) -> CheckSpec:
        matches = [item for item in self.commands if item.command_id == command_id]
        require(len(matches) == 1, "unknown_command_id")
        return matches[0]


@dataclass(frozen=True)
class ReceiptPin:
    """Host-authenticated artifact identity. Constructor does NOT authenticate it."""

    report_path: Path
    report_sha256: str
    repository_id: str
    run_id: str
    job_id: str
    source_sha: str
    tree_sha256: str
    command_id: str
    report_kind: str
    clean: bool = True


def _json(path: Path, digest: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "invalid_report")
            result[key] = value
        return result
    try:
        path = local_path(path)
        require(path.is_file() and path.stat().st_size <= 1024 * 1024, "invalid_report")
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == digest, "report_hash_mismatch")
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContributionError("invalid_report")))
        require(isinstance(value, dict), "invalid_report")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ContributionError):
            raise
        raise ContributionError("invalid_report") from None


class ReceiptStore:
    """Empty until a trusted host admits independently verified GitHub pins.

    There is intentionally no issue/JSON/CLI importer or claim of authentication.
    Possession of a file and its caller-computed digest cannot admit a receipt.
    """

    def __init__(self, *, repository_id: str, pins=()):
        self.repository_id = repository_id
        self._pins = []
        for pin in pins:
            self.add_verified(pin)

    @property
    def pins(self) -> tuple[ReceiptPin, ...]:
        return tuple(self._pins)

    def add_verified(self, pin: ReceiptPin) -> None:
        """Trusted-host boundary; caller must authenticate provenance beforehand."""
        require(isinstance(pin, ReceiptPin) and pin.repository_id == self.repository_id, "invalid_pin")
        require(isinstance(pin.report_sha256, str) and DIGEST.fullmatch(pin.report_sha256) is not None
                and isinstance(pin.source_sha, str) and SHA.fullmatch(pin.source_sha) is not None
                and isinstance(pin.tree_sha256, str) and DIGEST.fullmatch(pin.tree_sha256) is not None,
                "invalid_pin")
        require(isinstance(pin.run_id, str) and re.fullmatch(r"[1-9][0-9]{0,19}", pin.run_id) is not None
                and isinstance(pin.job_id, str) and re.fullmatch(r"[1-9][0-9]{0,19}", pin.job_id) is not None
                and type(pin.clean) is bool and isinstance(pin.command_id, str)
                and IDENTIFIER.fullmatch(pin.command_id) is not None, "invalid_pin")
        local_path(pin.report_path)
        # Host corrections replace the same artifact anchor. Consumers reverify
        # pins/files at readiness; an old recorded assertion never bypasses it.
        self._pins = [item for item in self._pins if not (
            item.report_sha256 == pin.report_sha256 and item.command_id == pin.command_id)]
        self._pins.append(pin)

    def _pinned(self, digest: str, command: str, kind: str, source: str) -> ReceiptPin:
        matches = [item for item in self._pins if item.report_sha256 == digest
            and item.command_id == command and item.report_kind == kind and item.source_sha == source
            and item.repository_id == self.repository_id]
        require(len(matches) == 1, "untrusted_report")
        return matches[0]

    def verify(self, evidence: CheckEvidence, recipe: ValidationRecipe) -> ReceiptPin | None:
        require(isinstance(evidence, CheckEvidence) and recipe.repository_id == self.repository_id,
                "invalid_evidence")
        spec = recipe.spec(evidence.command_id)  # deny unknown IDs before any process/file access
        require(isinstance(evidence.name, str) and 0 < len(evidence.name) <= 512
                and isinstance(evidence.source_sha, str) and SHA.fullmatch(evidence.source_sha) is not None,
                "invalid_evidence")
        require(isinstance(evidence.status, str) and evidence.status in {"passed", "failed", "not_run"},
                "invalid_evidence")
        if evidence.status == "not_run":
            require(evidence.exit_code is None and evidence.report_sha256 is None
                    and isinstance(evidence.limitation, str) and 0 < len(evidence.limitation.strip()) <= 2048,
                    "missing_limitation")
            return None
        require(type(evidence.exit_code) is int and evidence.exit_code >= 0
                and isinstance(evidence.report_sha256, str)
                and DIGEST.fullmatch(evidence.report_sha256) is not None and evidence.limitation is None
                and (evidence.status == "passed") == (evidence.exit_code == 0), "invalid_evidence")
        pin = self._pinned(evidence.report_sha256, evidence.command_id, spec.report_kind, evidence.source_sha)
        value = _json(pin.report_path, pin.report_sha256)
        require(value.get("source_commit") == pin.source_sha, "report_source_mismatch")
        if spec.report_kind == "source":
            require(set(value) == {"mode", "source_commit", "test_targets", "full_suite_passed",
                "test_exit_code", "exit_code", "source_verified_after_tests"}
                and value["mode"] == "source" and value["test_targets"] == list(spec.targets)
                and value["full_suite_passed"] is False and value["source_verified_after_tests"] is True
                and type(value["test_exit_code"]) is int and type(value["exit_code"]) is int
                and value["test_exit_code"] == evidence.exit_code == value["exit_code"]
                and pin.clean, "invalid_source_receipt")
        elif spec.report_kind == "frozen":
            require(evidence.status == "passed" and value.get("ok") is True
                and value.get("execution_mode") == "frozen"
                and all(value.get(name) is True for name in spec.required_checks) and pin.clean,
                "invalid_frozen_receipt")
        elif spec.report_kind == "synthetic":
            require(recipe.fixture and value.get("synthetic") is True
                and value.get("test_targets") == list(spec.targets)
                and type(value.get("exit_code")) is int and value["exit_code"] == evidence.exit_code,
                "invalid_fixture_receipt")
        else:
            # No generic {passed:true} parser. A real TS/browser/native receipt
            # adapter must be defined and reviewed before those PASSes are enabled.
            raise ContributionError("unsupported_report_kind")
        return pin

    def verify_build(self, evidence: BuildEvidence, recipe: ValidationRecipe) -> ReceiptPin:
        require(recipe.build_mode in {"quick", "full"} and type(evidence.clean) is bool and evidence.clean,
                "invalid_build")
        require(isinstance(evidence.source_sha, str) and SHA.fullmatch(evidence.source_sha) is not None
                and isinstance(evidence.manifest_sha256, str)
                and DIGEST.fullmatch(evidence.manifest_sha256) is not None, "invalid_build")
        pin = self._pinned(evidence.manifest_sha256, "build", "build", evidence.source_sha)
        require(local_path(evidence.manifest_path) == local_path(pin.report_path) and pin.clean,
                "invalid_build")
        manifest = _json(evidence.manifest_path, evidence.manifest_sha256)
        quick = recipe.build_mode == "quick"
        require(manifest.get("source_commit") == evidence.source_sha
                and manifest.get("test_scope") == ("targeted" if quick else "full")
                and manifest.get("full_suite_passed") is (not quick)
                and isinstance(manifest.get("test_targets"), list), "build_scope_mismatch")
        groups = {"python": "test_targets", "frontend": "frontend_test_targets", "desktop": "desktop_test_targets"}
        selected_count = 0
        for group, key in groups.items():
            values = manifest.get(key, [])
            require(isinstance(values, list) and all(isinstance(item, str) and item for item in values)
                    and len(set(values)) == len(values), "build_targets_mismatch")
            selected_count += len(values)
            if quick:
                required = {target for spec in recipe.commands if spec.scope in recipe.required_scopes
                            and spec.target_group == group for target in spec.targets}
                require(required <= set(values), "build_targets_mismatch")
        if quick:
            require(selected_count > 0, "build_targets_mismatch")
        else:
            require(manifest["test_targets"] == []
                and manifest.get("frontend_test_targets", []) == []
                and manifest.get("desktop_test_targets", []) == [], "build_targets_mismatch")
        return pin
