# Authorized developer workspaces

`tools.agent_contributions` is development tooling, separate from the installed
search service. It creates a new clone and incident branch only; it cannot run
validation recipes, commit, push, publish proposals, merge, repair or update an
installation. The separate evidence API below records verified receipts; it does
not execute validation recipes. This source capability does not establish
packaged/native acceptance.

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

## Evidence and readiness

`ContributionRun` records regression RED/GREEN, affected checks, independent review,
the actual commit and an applicable clean-SHA build. It never commits, dispatches
workflows or executes tests/builds. The owner supplies a `ValidationRecipe` with
named command IDs, exact targets and their Python/frontend/desktop groups, required
scopes and the package boundary. Unknown command IDs and scope/target substitutions
are refused. Source-only tooling can require targeted checks without a build; a
package boundary requires Quick or Full plus explicitly named frozen checks. Full
is required once at a stage/release boundary or for an identified broad risk,
not for every fix. Source success cannot satisfy Quick, Full, frozen or native gates.

`ReceiptStore` starts empty. A trusted developer host must authenticate the exact
GitHub repository, workflow revision/recipe, run, job, source SHA, verified clean
checkout, working-tree fingerprint, check targets, job outcome and artifact bytes
before calling `add_verified(ReceiptPin(...))`. This is a privileged host API, like
the owner grant, **not an authentication implementation**. This slice ships no
authenticated GitHub importer, registry JSON loader or evidence-recording CLI.
An issue, caller-created JSON report, hash, run URL or self-asserted pin is not
proof and must never be admitted by the host. An empty store cannot make a run ready.

The parser verifies actual `run_ci_build.ps1` Source receipts (including the final
owned-job exit code and source verification), current Quick/Full build-manifest
scope/targets, and explicitly required flat fields in frozen runtime receipts.
RED must be the approved single regression test failing with test exit code 1;
collection/configuration/no-test and cleanup-only failures cannot be RED. GREEN
must pass that same test. Suite/browser/native PASS adapters are not implemented
here: required unsupported checks stay pending rather than accepting generic
`passed: true` JSON. A host can supply a future reviewed adapter separately.
Existing Source wrapper tests which actually run delegated TypeScript checks
(such as `tests/test_desktop_source_bridge.py`) can be explicit approved targets;
their source receipts still establish neither package nor native acceptance.

Review evidence is another trusted-host input: a genuinely independent reviewer
must review the exact source tree and have no blocking findings. Merely spelling
a different reviewer name does not authenticate a reviewer. Git fingerprints use
SHA-256 over paths, modes and actual tracked/untracked nonignored working bytes;
the fingerprint is not the Git SHA-1 tree object ID. Only fixed read-only Git
queries execute in the evidence API, with hooks/fsmonitor disabled. A later source
or HEAD change, dirty commit/build, altered report, mismatched SHA/tree/manifest,
missing required scope or frozen proof invalidates readiness. Reports and pins
are rechecked when `ready()` is queried, not just when recorded.

Unavailable Windows/model/compiler/UNC checks use `not_run`, null exit/hash and a
concrete limitation. A required pending gate blocks readiness; an optional honest
limitation does not. Hosted CI never establishes native Windows/Win10, visual or
Stable acceptance. Evidence readiness alone does not publish or accept a proposal.

`tools/check_agent_contributions.py --scenario evidence --fixture --workspace-root
<new-directory-under-home> --report <sibling-report.json>` is a fixed disposable
scenario available only with `GITHUB_ACTIONS=true`. It executes a known synthetic
failing unittest, fixes it, reruns the same test, captures a simulated review,
then creates the actual fixture commit and hashes a synthetic artifact. Its
explicit fixture recipe uses a distinct synthetic report kind refused by normal
recipes. Output is `synthetic: true`, `fixture_ready`, `published: false`, with
Full/frozen/native acceptance unclaimed. It proves fixture ordering only, not
real reviewer independence, authenticated receipts or an application build.
