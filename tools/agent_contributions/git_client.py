"""Fixed Git operations; no shell, issue commands, ambient auth or hooks."""
from collections.abc import Callable
import os
from pathlib import Path
import re
import shutil
import subprocess

from .config import REFERENCE, SHA, branch_valid, local_path, require, validate_remote
from .contracts import ContributionError


class GitClient:
    def __init__(self, *, credential_resolver: Callable[[str], str] | None = None):
        self._resolver = credential_resolver
        self._fixture: tuple[str, Path] | None = None

    @classmethod
    def for_local_fixture(cls, *, approved_remote: str, repository: Path) -> "GitClient":
        """Explicit disposable bare-repository adapter, never exposed by the CLI."""
        validate_remote(approved_remote)
        repository = local_path(repository)
        require(repository.is_dir() and (repository / "HEAD").is_file()
                and (repository / "objects").is_dir(), "invalid_fixture")
        client = cls()
        client._fixture = (approved_remote, repository)
        return client

    def clone_at(self, remote: str, *, destination: Path, base_sha: str,
                 branch: str, credential_ref: str | None) -> None:
        validate_remote(remote)
        require(isinstance(base_sha, str) and SHA.fullmatch(base_sha) is not None, "invalid_base")
        require(branch_valid(branch) and re.fullmatch(
            r"codex/agent-fix-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", branch)
            is not None, "invalid_branch")
        require(isinstance(credential_ref, str) and REFERENCE.fullmatch(credential_ref) is not None,
                "invalid_developer_credential")
        destination = local_path(destination)
        require(destination.parent.is_dir(), "unsafe_workspace")
        require(not destination.exists() or (destination.is_dir() and not any(destination.iterdir())),
                "workspace_exists")
        source = remote
        header = None
        file_transport = remote.startswith("\\\\")
        if self._fixture is not None:
            require(self._fixture[0] == remote, "fixture_remote_mismatch")
            source = str(local_path(self._fixture[1]))
            file_transport = True
        elif not file_transport:
            require(self._resolver is not None, "developer_credentials_unavailable")
            try:
                header = self._resolver(credential_ref)
            except Exception:
                raise ContributionError("developer_credentials_unavailable") from None
            require(isinstance(header, str) and 0 < len(header) <= 8192
                    and not any(ord(c) < 32 or ord(c) == 127 for c in header), "invalid_developer_credential")
        executable = shutil.which("git")
        require(executable is not None, "git_unavailable")
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_NO_REPLACE_OBJECTS="1")
        prefix = [executable, "-c", "core.hooksPath=", "-c", "credential.helper=",
                  "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
                  "-c", "protocol.allow=never", "-c", "protocol.ext.allow=never",
                  "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=" +
                  ("always" if file_transport else "never"), "-c", "submodule.recurse=false"]

        def run(arguments, *, cwd: Path, auth=False):
            # Recheck lexical ancestors immediately before every process launch.
            local_path(cwd)
            child_env = dict(env)
            if auth and header:
                child_env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http." + remote + ".extraHeader",
                                 GIT_CONFIG_VALUE_0="Authorization: " + header)
            try:
                result = subprocess.run(prefix + arguments, cwd=cwd, env=child_env,
                    shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            except (OSError, subprocess.SubprocessError):
                raise ContributionError("git_failed") from None
            if result.returncode != 0:
                # Git stderr can contain URLs/credential material. Do not propagate it.
                raise ContributionError("git_failed")
            return result.stdout.strip()

        run(["clone", "--no-checkout", "--no-local", "--template=", "--", source, str(destination)],
            cwd=destination.parent, auth=True)
        # No checkout of the remote's default branch, including on unknown SHA.
        actual = run(["rev-parse", "--verify", base_sha + "^{commit}"], cwd=destination)
        require(actual == base_sha, "base_mismatch")
        run(["checkout", "--no-guess", "-b", branch, base_sha, "--"], cwd=destination)
        require(run(["rev-parse", "--verify", "HEAD"], cwd=destination) == base_sha, "base_mismatch")
