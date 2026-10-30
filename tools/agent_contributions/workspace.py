"""Create one new workspace after all exact owner-authority checks."""
from datetime import datetime, timezone
from pathlib import Path

from .config import authorize, local_path, require
from .contracts import (ContributionAuthorization, ContributionConfig, ContributionError,
                        ContributionWorkspace)
from .git_client import GitClient


def create_workspace(config: ContributionConfig, grant: ContributionAuthorization, *, parent: Path,
                     git_client: GitClient | None = None, now: datetime | None = None) -> ContributionWorkspace:
    authorize(config, grant, now=now if now is not None else datetime.now(timezone.utc))
    root = local_path(parent)
    require(root == local_path(config.workspace_root) and root.is_dir(), "unsafe_workspace")
    target = root / grant.incident_id
    local_path(target)
    require(not target.exists(), "workspace_exists")
    # Exclusive reservation never cleans or overwrites a previous user's directory.
    try:
        target.mkdir()
    except OSError:
        raise ContributionError("workspace_exists") from None
    client = git_client if git_client is not None else GitClient()
    client.clone_at(config.remote, destination=target, base_sha=config.base_sha,
                    branch=grant.branch, credential_ref=config.developer_credential_ref)
    return ContributionWorkspace(target, grant.incident_id, grant.branch, config.base_sha)
