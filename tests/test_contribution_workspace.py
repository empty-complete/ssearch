"""Developer workspace boundaries; all repositories and credentials are fictional."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://example.invalid/owner/project.git"
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
INCIDENT = str(uuid.UUID("a92c3e5e-2877-4918-9726-0718c20250b2"))
BRANCH = "codex/agent-fix-" + INCIDENT


@pytest.fixture
def api():
    sys.path.insert(0, str(ROOT))
    try:
        try:
            contracts = importlib.import_module("tools.agent_contributions.contracts")
        except ModuleNotFoundError as exc:
            pytest.fail("Authorized contribution workspace contracts are not implemented: " + str(exc))
        config = importlib.import_module("tools.agent_contributions.config")
        workspace = importlib.import_module("tools.agent_contributions.workspace")
        git = importlib.import_module("tools.agent_contributions.git_client")
        return SimpleNamespace(**{name: getattr(contracts, name) for name in (
            "ContributionAuthorization", "ContributionConfig", "ContributionPolicy", "ContributionError")},
            load_config=config.load_config, authorize=config.authorize,
            create_workspace=workspace.create_workspace, GitClient=git.GitClient)
    finally:
        sys.path.remove(str(ROOT))


def configured(api, tmp_path, *, base="1" * 40):
    parent = tmp_path / "authorized-developer-workspaces"
    parent.mkdir(exist_ok=True)
    policy = api.ContributionPolicy(10485760, 2, 16777216, 30, ("main",))
    config = api.ContributionConfig(repository_id="fictional-project", remote=REMOTE,
        base_sha=base, developer_credential_ref="developer:fixture", proposal_destination=None,
        policy=policy, workspace_root=parent)
    grant = api.ContributionAuthorization(task_id=str(uuid.uuid4()), incident_id=INCIDENT,
        repository_id=config.repository_id, granted_by="fixture-owner", expires_at=NOW + timedelta(hours=1),
        remote=REMOTE, base_sha=base, workspace_root=parent, branch=BRANCH,
        developer_credential_ref=config.developer_credential_ref)
    return config, grant, parent


def git_run(*args, cwd):
    result = subprocess.run(["git", "-c", "core.hooksPath=", *args], cwd=cwd,
        shell=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def bare_repository(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git_run("init", "--initial-branch=main", cwd=source)
    git_run("config", "user.name", "Fixture", cwd=source)
    git_run("config", "user.email", "fixture@example.invalid", cwd=source)
    (source / "README.md").write_text("fictional base", encoding="utf-8")
    git_run("add", "README.md", cwd=source)
    git_run("commit", "-m", "fixture base", cwd=source)
    base = git_run("rev-parse", "HEAD", cwd=source)
    (source / "README.md").write_text("later default branch", encoding="utf-8")
    git_run("commit", "-am", "later fixture", cwd=source)
    bare = tmp_path / "upstream.git"
    git_run("clone", "--bare", str(source), str(bare), cwd=tmp_path)
    return bare, base


def test_authorized_clone_uses_exact_base_and_incident_branch(api, tmp_path, bare_repository):
    bare, base = bare_repository
    config, grant, parent = configured(api, tmp_path, base=base)
    protected = tmp_path / "installed-profile-foreign"
    protected.mkdir()
    sentinel = protected / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    client = api.GitClient.for_local_fixture(approved_remote=REMOTE, repository=bare)
    result = api.create_workspace(config, grant, parent=parent, git_client=client, now=NOW)
    assert result.path == parent / INCIDENT
    assert result.base_sha == base
    assert result.branch == BRANCH
    assert git_run("rev-parse", "HEAD", cwd=result.path) == base
    assert git_run("branch", "--show-current", cwd=result.path) == BRANCH
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert git_run("branch", "--list", BRANCH, cwd=bare) == ""


@pytest.mark.parametrize("kind", ["missing", "expired", "naive", "wrong_repository", "wrong_remote",
    "wrong_base", "wrong_branch", "wrong_workspace", "wrong_credential", "search", "runtime", "disabled"])
def test_missing_expired_or_mismatched_grant_cannot_start_git(api, tmp_path, monkeypatch, kind):
    config, grant, parent = configured(api, tmp_path)
    if kind == "missing":
        grant = None
    elif kind == "expired":
        grant = replace(grant, expires_at=NOW)
    elif kind == "naive":
        grant = replace(grant, expires_at=NOW.replace(tzinfo=None))
    elif kind.startswith("wrong_"):
        field, value = {"wrong_repository": ("repository_id", "foreign"),
            "wrong_remote": ("remote", "https://example.invalid/other.git"),
            "wrong_base": ("base_sha", "2" * 40), "wrong_branch": ("branch", "main"),
            "wrong_workspace": ("workspace_root", tmp_path / "installed"),
            "wrong_credential": ("developer_credential_ref", "developer:other")}[kind]
        grant = replace(grant, **{field: value})
    elif kind in {"search", "runtime"}:
        config = replace(config, developer_credential_ref=kind + ":token")
        grant = replace(grant, developer_credential_ref=config.developer_credential_ref)
    else:
        config = replace(config, remote=None)
    def forbidden(*args, **kwargs):
        pytest.fail("Git must not start before authorization")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(api.ContributionError):
        api.create_workspace(config, grant, parent=parent, now=NOW)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("change", ["policy", "base_sha", "developer_credential_ref", "workspace_root"])
def test_incomplete_owner_configuration_is_disabled(api, tmp_path, change):
    config, grant, _ = configured(api, tmp_path)
    with pytest.raises(api.ContributionError):
        api.authorize(replace(config, **{change: None}), grant, now=NOW)


@pytest.mark.parametrize("remote", ["--upload-pack=evil", "ext::evil", "file:///tmp/repo", "/tmp/repo",
    "git@example.invalid:repo", "ssh://example.invalid/repo", "http://example.invalid/repo",
    "https://user:secret@example.invalid/repo", "https://example.invalid/repo?token=secret",
    "https://example.invalid/repo#fragment", "https://example.invalid/repo\n--config=evil",
    "https://example.invalid/../repo", "https://example.invalid/%2e%2e/repo"])
def test_production_remote_rejects_options_helpers_embedded_credentials_and_unsafe_urls(api, tmp_path, remote):
    config, grant, _ = configured(api, tmp_path)
    with pytest.raises(api.ContributionError):
        api.authorize(replace(config, remote=remote), replace(grant, remote=remote), now=NOW)


def test_explicit_unc_repository_is_allowed_but_unc_workspace_is_rejected(api, tmp_path):
    config, grant, parent = configured(api, tmp_path)
    remote = r"\\fixture-server\approved-share\project.git"
    api.authorize(replace(config, remote=remote), replace(grant, remote=remote), now=NOW)
    unc = Path(r"\\fixture-server\share\workspaces")
    with pytest.raises(api.ContributionError):
        api.create_workspace(replace(config, workspace_root=unc), replace(grant, workspace_root=unc), parent=unc, now=NOW)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("kind", ["existing", "foreign_parent", "traversal", "symlink"])
def test_workspace_rejects_existing_installed_or_escaping_paths(api, tmp_path, monkeypatch, kind):
    config, grant, parent = configured(api, tmp_path)
    target = parent / INCIDENT
    if kind == "existing":
        target.mkdir()
        (target / "dirty.txt").write_text("keep", encoding="utf-8")
    elif kind == "foreign_parent":
        parent = tmp_path / "installed-profile"
        parent.mkdir()
    elif kind == "traversal":
        parent = parent / ".." / parent.name
    else:
        linked = tmp_path / "linked"
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(parent)], capture_output=True)
            assert made.returncode == 0, made.stderr
        else:
            linked.symlink_to(parent, target_is_directory=True)
        parent = linked
        config = replace(config, workspace_root=linked)
        grant = replace(grant, workspace_root=linked)
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected path must not start Git")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(api.ContributionError):
        api.create_workspace(config, grant, parent=parent, now=NOW)
    if kind == "existing":
        assert (target / "dirty.txt").read_text(encoding="utf-8") == "keep"


def test_unknown_base_never_falls_back_to_default_branch(api, tmp_path, bare_repository):
    bare, _ = bare_repository
    config, grant, parent = configured(api, tmp_path, base="f" * 40)
    client = api.GitClient.for_local_fixture(approved_remote=REMOTE, repository=bare)
    with pytest.raises(api.ContributionError):
        api.create_workspace(config, grant, parent=parent, git_client=client, now=NOW)
    assert git_run("branch", "--list", BRANCH, cwd=bare) == ""
    target = parent / INCIDENT
    if target.exists():
        assert git_run("branch", "--show-current", cwd=target) != BRANCH


def test_fixture_adapter_cannot_substitute_another_owner_remote(api, tmp_path, bare_repository):
    bare, base = bare_repository
    config, grant, parent = configured(api, tmp_path, base=base)
    client = api.GitClient.for_local_fixture(approved_remote="https://example.invalid/foreign.git", repository=bare)
    with pytest.raises(api.ContributionError):
        api.create_workspace(config, grant, parent=parent, git_client=client, now=NOW)


def config_payload(parent):
    return dict(schema_version=1, repository_id="fictional-project", remote=REMOTE, base_sha="1" * 40,
        developer_credential_ref="developer:fixture", proposal_destination=None, workspace_root=str(parent),
        policy=dict(max_bundle_bytes=10485760, max_queued_proposals=2, max_queued_bytes=16777216,
            sent_retention_days=30, allowed_target_branches=["main"]))


@pytest.mark.parametrize("field,value", [("max_bundle_bytes", 0), ("max_queued_proposals", -1),
    ("max_queued_bytes", True), ("sent_retention_days", 1.5), ("max_bundle_bytes", 16777217),
    ("allowed_target_branches", ["--upload-pack=evil"]), ("allowed_target_branches", [])])
def test_config_requires_explicit_positive_consistent_limits(api, tmp_path, field, value):
    payload = config_payload(tmp_path)
    payload["policy"][field] = value
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(api.ContributionError):
        api.load_config(path)


def test_owner_config_rejects_issue_commands_and_unknown_fields(api, tmp_path):
    payload = config_payload(tmp_path)
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    config = api.load_config(path)
    assert config.remote == REMOTE
    assert config.proposal_destination is None
    for key in ("issue", "command", "branch", "git_config", "credential"):
        path.write_text(json.dumps({**payload, key: "git reset --hard; hidden remote"}), encoding="utf-8")
        with pytest.raises(api.ContributionError):
            api.load_config(path)


def test_git_uses_fixed_arguments_controlled_environment_and_disables_hooks(api, tmp_path, monkeypatch):
    calls = []
    secret = "Bearer fictional-developer-secret"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.ext::evil.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "evil-hooks"))
    monkeypatch.setenv("SEARCH_TOKEN", "search-secret")
    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        # Emulate only successful fixed Git operations, not a general shell.
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, "1" * 40 + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", record)
    refs = []
    def resolve(reference):
        refs.append(reference)
        return secret
    client = api.GitClient(credential_resolver=resolve)
    client.clone_at(REMOTE, destination=tmp_path / "new", base_sha="1" * 40, branch=BRANCH,
        credential_ref="developer:fixture")
    assert refs == ["developer:fixture"]
    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, list)
        assert kwargs["shell"] is False
        assert secret not in repr(argv)
        env = kwargs["env"]
        assert "GIT_SSH_COMMAND" not in env and "GIT_TEMPLATE_DIR" not in env and "SEARCH_TOKEN" not in env
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert "url.ext::evil.insteadOf" not in repr(env)
        assert "core.hooksPath=" in argv
        assert "protocol.ext.allow=never" in argv
        assert "http.followRedirects=false" in argv
    assert any("clone" in argv and "--no-checkout" in argv for argv, _ in calls)


def test_cli_and_search_have_no_mutual_runtime_dependency(api):
    probe = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'atom_search', 'atom_search_core', 'PySide6', 'psycopg'}:
            raise AssertionError('Developer tooling imported search runtime: '+fullname)
sys.meta_path.insert(0, Block())
import tools.agent_contributions.__main__
"""
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    for directory in (ROOT / "src", ROOT / "packages" / "atom-search-core" / "src"):
        assert not any("agent_contributions" in path.read_text(encoding="utf-8") for path in directory.rglob("*.py"))
