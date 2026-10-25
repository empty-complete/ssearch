# Contributing to ssearch

ssearch is an independent Windows search app. Contributions should help someone find a document and reach it in Explorer, while keeping indexing and search local. English and Russian are welcome in issues and pull requests.

## Start with a small change

Check [existing issues](https://github.com/empty-complete/ssearch/issues) before opening a new one. A reproducible bug, clearer wording or a small fix can go straight to a pull request. Discuss a new dependency, public API change or large redesign in an issue first so its scope is clear.

For a search problem, provide a tiny synthetic folder tree, a query, the expected result and what happened instead. Use invented file names and document text. Keep company documents, real disk inventories, database dumps, credentials, tokens and private paths out of issues, commits, logs and screenshots. Report a security issue privately to [filipp.beloborodoff@yandex.ru](mailto:filipp.beloborodoff@yandex.ru).

## Fork and open a pull request

1. Fork [empty-complete/ssearch](https://github.com/empty-complete/ssearch) on GitHub and clone your fork.
2. Create a focused branch from the current `main`, for example `fix/search-empty-state`.
3. Make the change, keeping unrelated formatting and refactoring out of the diff. Follow applicable repository instructions and the existing public contracts.
4. Commit and push the branch to your fork, then open a pull request against upstream `main`. Draft pull requests are welcome.
5. Explain the user-visible problem, the change and the relevant checks. Link the issue and CI run; identify anything still unverified.

Keep generated installers, build output, local environments and personal settings out of the pull request. A concise description and an update to the affected documentation are enough; a separate report is not required.

## Checks run in GitHub Actions

Automated tests and application builds run in [Windows build](https://github.com/empty-complete/ssearch/actions/workflows/windows-build.yml). Do not run local `pytest`, npm test/build or installer builds for the contribution workflow, and do not duplicate a completed CI run locally. Read the run's results and fix the affected code before requesting another run. First-time fork contributions may need a maintainer to approve the workflow run.

Use existing test coverage first. Add a focused regression test for a real bug, a new public behavior or a meaningful risk. Simple copy, layout and documentation changes do not need new tests that merely repeat the implementation. Do not remove tests to meet a count target.

The maintainer selects verification scope according to the change:

- **Documentation only:** check text and links; a new installer is unnecessary when application inputs are unchanged.
- **Source:** a manually selected, nonempty list of existing Python test files or node IDs, run under the same verified standard-user/profile/DPAPI/mutex preflight. It requires the exact clean source SHA and produces only a source test receipt; no distributable/release wheel, frozen proof, installer or publication. Preparation installs the local core offline through PEP 660 editable installation with the pinned backend and without fetching dependencies. Each test (setup through teardown) prints its node ID immediately, dumps stacks without locals after 120 seconds and exits nonzero after 900 seconds. The owned process job also caps the whole source run at 3600 seconds and cleans descendants. Source success does not satisfy Quick, Full or native acceptance.
- **Quick:** a targeted development build with an explicit, nonempty list of affected test files or node IDs. Include any relevant database, migration, permissions, deletion, cancellation or ownership checks.
- **Full:** the complete suite at a stage/release boundary or for an identified broad risk. Run it once as part of the build, not again before and after it.

Full and Quick also print each test node immediately and dump stacks without locals after 120 seconds. Their per-test limit is 3600 seconds; Source keeps its 900-second limit. The existing overall build deadlines and packaging gates still apply.

Quick does not replace full release acceptance. A green hosted Windows runner does not establish Windows 10/11 native, visual, installation or offline acceptance. Record any missing scenario as unverified; do not turn a missing environment into a passing result.

An independent reviewer checks the change and relevant evidence. Merge only after the required checks pass and review findings are resolved. A reviewer or maintainer handles the merge; authors should not merge their own unreviewed work. Publication of an installer is separate from a claim that the release is Stable.

## Architecture and UI

The current desktop uses Qt; the planned desktop uses Electron with a React/HeroUI/TypeScript frontend. The Python/PostgreSQL search service owns the index, model and jobs. Keep UI changes behind its public API instead of duplicating search or database ownership in a window. Legacy `atom_search` imports and related identities are temporary compatibility names pending a controlled migration to ssearch. That separate gated migration must preserve user profiles, indexed data, IPC owner discovery and upgrades of existing installations.

For UI changes, preserve the product's existing tokens and behavior. Use the [official HeroUI React skill and documentation](https://github.com/heroui-inc/heroui/blob/v3/skills/heroui-react/SKILL.md) for component APIs. [Impeccable's Operate guidance](https://github.com/pbakaus/impeccable/blob/main/.agents/skills/impeccable/reference/operate.md) and [Hallmark's app-screen guidance](https://github.com/Nutlope/hallmark/blob/main/skills/hallmark/references/flow.md#app-screens) can help with clarity and restraint; they are optional guidance, not required installations or automatic hooks.

Check the affected keyboard flow, long file names and paths, empty/error/loading states, narrow windows, both themes and reduced motion as applicable. Attach relevant screenshots when available and name the environment used. Batch this review at a meaningful UI boundary rather than after every styling edit.

## Licensing and forks

Contribute work you have the right to share under the project's [MIT license](LICENSE.txt). Retain third-party licenses and attribution when adding or modifying external code or assets; the MIT license does not replace their terms. No separate CLA or DCO is required.

A redistributed fork should use its own application identity, update source, signing keys and support contact. Preserve compatibility or provide an explicit migration for existing user data. Do not point a fork's update channel at upstream artifacts that belong to another product identity.
