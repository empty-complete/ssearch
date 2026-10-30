"""Strict configuration and exact, issue-independent owner grant matching."""
from datetime import datetime
import json
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .contracts import (ContributionAuthorization, ContributionConfig, ContributionError,
                        ContributionPolicy)

SHA = re.compile(r"[0-9a-f]{40}\Z")
REFERENCE = re.compile(r"developer:[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ContributionError(code)


def uuid_text(value: str) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def branch_valid(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 200
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is not None
        and not any(part in value for part in ("..", "//", "@{"))
        and not value.endswith(("/", "."))
        and all(part and not part.startswith(".") and not part.endswith(".lock")
                for part in value.split("/")))


def validate_remote(remote: str) -> None:
    require(isinstance(remote, str) and 0 < len(remote) <= 2048, "invalid_remote")
    require(not any(ord(c) <= 32 or ord(c) == 127 for c in remote), "invalid_remote")
    if remote.startswith("\\\\"):
        require(not remote.startswith(("\\\\?\\", "\\\\.\\")), "invalid_remote")
        parts = remote[2:].split("\\")
        require(len(parts) >= 3 and all(re.fullmatch(r"[A-Za-z0-9_ .-]+", p)
                and p not in {".", ".."} and not p.endswith((" ", ".")) for p in parts), "invalid_remote")
        return
    try:
        url = urlsplit(remote)
        _ = url.port
    except ValueError:
        raise ContributionError("invalid_remote") from None
    require(url.scheme == "https" and bool(url.hostname) and url.username is None
            and url.password is None and not url.query and not url.fragment
            and "\\" not in remote and remote.startswith("https://"), "invalid_remote")
    require(bool(url.path) and url.path != "/" and not url.path.endswith("/"), "invalid_remote")
    # Decode once and reject residual escapes too, so encoded separators/dots
    # cannot change the owner-approved path when Git/cURL interprets the URL.
    decoded = unquote(url.path)
    require(not any(c in decoded for c in ("%", "\\", "?", "#"))
            and not any(ord(c) <= 32 or ord(c) == 127 for c in decoded)
            and not any(p in {"", ".", ".."} for p in decoded[1:].split("/")), "invalid_remote")


def local_path(value: Path) -> Path:
    require(isinstance(value, Path), "unsafe_workspace")
    text = str(value)
    require(value.is_absolute() and not text.startswith(("\\\\", "//"))
            and not PureWindowsPath(text).drive.startswith("\\\\")
            and ".." not in value.parts and not any(ord(c) < 32 for c in text), "unsafe_workspace")
    # Inspect the lexical chain before resolve() can hide a junction or symlink.
    for item in (value, *value.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ContributionError("unsafe_workspace") from None
        require(not item.is_symlink() and not (getattr(info, "st_file_attributes", 0) & 0x400),
                "unsafe_workspace")
    return value.resolve()


def validate_policy(policy: ContributionPolicy) -> None:
    require(isinstance(policy, ContributionPolicy), "disabled")
    for value in (policy.max_bundle_bytes, policy.max_queued_proposals,
                  policy.max_queued_bytes, policy.sent_retention_days):
        require(type(value) is int and value > 0, "invalid_policy")
    require(policy.max_bundle_bytes <= policy.max_queued_bytes, "invalid_policy")
    require(isinstance(policy.allowed_target_branches, tuple) and bool(policy.allowed_target_branches)
            and all(branch_valid(b) for b in policy.allowed_target_branches)
            and len(set(policy.allowed_target_branches)) == len(policy.allowed_target_branches), "invalid_policy")


def validate_config(config: ContributionConfig, *, enabled: bool = False) -> None:
    require(isinstance(config, ContributionConfig), "disabled")
    require(isinstance(config.repository_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
            config.repository_id) is not None, "invalid_config")
    for name in ("remote", "base_sha", "developer_credential_ref", "policy", "workspace_root"):
        if enabled:
            require(getattr(config, name) is not None, "disabled")
    if config.remote is not None:
        validate_remote(config.remote)
    if config.base_sha is not None:
        require(isinstance(config.base_sha, str) and SHA.fullmatch(config.base_sha) is not None, "invalid_base")
    if config.developer_credential_ref is not None:
        require(isinstance(config.developer_credential_ref, str)
                and REFERENCE.fullmatch(config.developer_credential_ref) is not None, "invalid_developer_credential")
    if config.policy is not None:
        validate_policy(config.policy)
    if config.workspace_root is not None:
        local_path(config.workspace_root)
    require(config.proposal_destination is None or isinstance(config.proposal_destination, str), "invalid_config")


def authorize(config: ContributionConfig, grant: ContributionAuthorization, *, now: datetime) -> None:
    validate_config(config, enabled=True)
    require(isinstance(grant, ContributionAuthorization), "missing_grant")
    require(isinstance(now, datetime) and now.utcoffset() is not None
            and isinstance(grant.expires_at, datetime) and grant.expires_at.utcoffset() is not None,
            "invalid_expiry")
    require(grant.expires_at > now, "expired_grant")
    require(uuid_text(grant.task_id) and uuid_text(grant.incident_id), "invalid_grant")
    require(isinstance(grant.granted_by, str) and 0 < len(grant.granted_by) <= 200
            and not any(ord(c) < 32 for c in grant.granted_by), "invalid_grant")
    for name in ("repository_id", "remote", "base_sha", "developer_credential_ref"):
        require(getattr(grant, name) == getattr(config, name), "grant_mismatch")
    require(grant.branch == "codex/agent-fix-" + grant.incident_id, "grant_mismatch")
    require(local_path(grant.workspace_root) == local_path(config.workspace_root), "grant_mismatch")


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "invalid_json")
        result[key] = value
    return result


def read_owner_json(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            data = source.read(65537)
        require(len(data) <= 65536, "invalid_json")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    except (OSError, ValueError, UnicodeError):
        raise ContributionError("invalid_json") from None
    require(type(value) is dict, "invalid_json")
    return value


def load_config(path: Path) -> ContributionConfig:
    value = read_owner_json(path)
    expected = {"schema_version", "repository_id", "remote", "base_sha", "developer_credential_ref",
                "proposal_destination", "policy", "workspace_root"}
    require(set(value) == expected and type(value["schema_version"]) is int
            and value["schema_version"] == 1, "invalid_config")
    value.pop("schema_version")
    policy = value["policy"]
    if policy is not None:
        require(type(policy) is dict and set(policy) == set(ContributionPolicy.__dataclass_fields__), "invalid_policy")
        require(type(policy["allowed_target_branches"]) is list, "invalid_policy")
        policy["allowed_target_branches"] = tuple(policy["allowed_target_branches"])
        value["policy"] = ContributionPolicy(**policy)
    if value["workspace_root"] is not None:
        require(isinstance(value["workspace_root"], str), "unsafe_workspace")
        value["workspace_root"] = Path(value["workspace_root"])
    result = ContributionConfig(**value)
    validate_config(result)
    return result
