# Authorized developer workspaces

`tools.agent_contributions` is development tooling, separate from the installed
search service. It creates a new clone and incident branch only; it cannot run
validation recipes, commit, push, publish proposals, merge, repair or update an
installation. This source capability does not establish packaged/native acceptance.

Configuration follows [config.schema.json](config.schema.json); the separate grant
follows its `$defs/grant`. Both are trusted owner inputs, protected by the owner
outside the issue channel. They are not signed capabilities or an identity provider:
the caller must obtain the owner's actual permission, not fabricate grant JSON from
incoming issue text. Never use an issue's remote, commands, workspace, credentials,
hooks or branch. Unknown JSON keys and duplicate keys are rejected.

Owner config and unexpired grant must agree on repository ID, exact HTTPS/UNC
remote, full lowercase 40-hex base SHA, developer credential reference and local
workspace root. The grant also identifies the authorized task, incident UUID and
grantor. The only source branch is `codex/agent-fix-<incident UUID>`; the destination
is `<workspace_root>/<incident UUID>`. Acceptance branches are separate future
delivery policy. All quota/retention limits are explicit positive integers, with
bundle bytes no greater than queue bytes; no production values are guessed.
Any missing nullable prerequisite disables clone capability.

The root must already exist as an owner-designated local developer directory,
separate from an installation, search profile, documents or another checkout.
UNC workspace roots, traversal, symlinks, junctions/reparse ancestors, Git worktree
or bare repository ancestors and existing destinations are refused. A new empty
destination is reserved exclusively. Failed
clones may leave their new directory for explicit inspection; retries never remove
it or overwrite another directory. The root must remain under the owner's control;
this CLI is not a defense against another local principal racing directory changes.

Git uses fixed argument lists, `shell=False`, a minimal environment, hooks disabled
with the nonempty `core.hooksPath=/dev/null`,
empty templates, no credential helpers or ambient Git config, no submodules and no
HTTP redirects. The Git executable/PATH and OS certificate store must be trusted by
the developer. HTTPS credentials require a separately injected developer resolver:
it accepts an opaque `developer:` reference and returns an HTTP Authorization value.
That value is passed only in the clone child's environment, never argv/config files
or output. Resolver failures and Git failures expose bounded codes, not raw output.
Search/runtime tokens are not accepted. UNC authentication uses existing OS access
rights after the same explicit grant checks.

`python -m tools.agent_contributions --config <owner.json> --grant <grant.json>
--parent <approved-local-root>` reads only these owner files. The CLI has no
credential resolver integration yet, so HTTPS clone stays disabled there; a trusted
embedding developer host can inject a resolver through the Python API. There is no
fixture CLI flag. `GitClient.for_local_fixture` explicitly maps one approved remote
to a disposable local bare repository solely for tests; production config never
accepts local/file/SSH/helper protocols. Tests and builds run only in GitHub Actions.
