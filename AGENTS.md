# ssearch — development instructions

ssearch (Simple Search) is an independent local, offline/private search application
for large personal and corporate disks on Windows 10/11. Network/UNC/DFS folders
are optional sources. The core task is to find a document from its purpose or
project context and reveal it in Explorer. First-party code is MIT; preserve
third-party notices and the original application icon.

## Architecture and current scope

- Read README.md, CONTRIBUTING.md and the applicable docs/contracts before editing.
- Current implementation: Python/Qt with a separate shared search service and
  PostgreSQL/pgvector. The planned desktop is Electron main/preload in TypeScript
  with React/HeroUI/Vite. Preserve the service/API boundary; UI windows do not
  create another owner of the model, database or queue.
- Legacy `atom_search` imports, profile IDs, pipe names and installer identity
  are temporary compatibility names pending a controlled migration to ssearch.
  That separate gated migration must preserve user profiles, indexed data, IPC
  owner discovery and upgrades of existing installations. Coordinate changes
  across active branches rather than mass-renaming these identifiers.
- Future Electron renderers must be sandboxed and context-isolated, with only a
  typed allowlist bridge; no Node, credentials, filesystem, SQL or shell access.
- Settings use one versioned JSON contract, validation and atomic coordinator.
  Credentials and runtime state stay separate. New profiles select their own
  folders; never insert company shares or scan an arbitrary default folder.
- Document-body indexing, an optional integration for shared notes, extension
  loading and automatic update installation are future capabilities. Do not
  claim they work now.
  The configured GitHub update source does not establish a trusted update feed;
  signed update/recovery and native platform acceptance remain required.

## Work and verification

- Use focused codex/ branches and pull requests. One implementer edits shared code
  at a time; independent reading/review may run in parallel.
- All automated tests and builds run in GitHub Actions. Do not run local pytest,
  npm test/build, application packaging or foreground/native QA on the owner's PC.
  Local source inspection, text/link checks and syntax checks are sufficient to
  prepare the PR; they are not evidence of runtime success.
- Reuse existing coverage. Add a test for an uncovered public behavior, actual
  regression or meaningful safety risk, not a private implementation detail.
  Copy/layout/docs changes do not require artificial tests. Do not remove tests
  to satisfy a numerical target.
- Select checks by risk: Quick takes explicit affected targets; Full runs once at
  an acceptance/release boundary or for a documented broad risk. Real database,
  migration, ACL, deletion, cancellation, ownership and frozen-runtime checks
  cannot be replaced by mocks where those contracts are affected.
- An independent reviewer checks the diff and CI evidence. A different agent or
  maintainer merges only the reviewed commit after required checks pass. Do not
  repeat a successful build for the same source tree merely because it merged.
- Hosted Windows CI does not establish native/visual/Win10 or Stable acceptance.
  State remaining gates honestly. Never publish QA profiles, database contents,
  raw diagnostic logs, credentials or real company disk inventories.
- Keep documentation proportional: update the existing contract or README and
  concise PR description. Avoid a separate report for every change or review.

## Interface changes

Use frontend-design guidance for substantial UI work and the official HeroUI
React skill/component documentation for the chosen version's API. Impeccable's
Operate guidance and Hallmark's app-screen guidance are optional references
linked in CONTRIBUTING.md; do not install automatic hooks or repeat broad audits.
Keep tokens shared and check the affected keyboard flow, focus, long paths,
empty/loading/error states, narrow windows, both themes and reduced motion as
applicable. DOM checks alone do not establish visual/native acceptance.

Forks use their own product/profile identity, update source, signing keys and
support contacts, with an explicit migration strategy where needed. Preserve
upstream and third-party attribution.
