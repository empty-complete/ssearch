# ssearch frontend candidate

P4 transport/lifecycle source slice: bounded TS broker frames and a private
MAIN-owned Python adapter reuse P2 identity/nonce and read-only owner IPC proof;
spawned/attached mode never grants mutation rights. One canonical user/profile
session owns the broker and any verified source-process control lease; windows
share it, close-to-tray differs from quit, and EOF covers parent crash/shutdown.
The next package boundary belongs to the CI/Task 3 owner, not this source slice.
Live inputs: Node 24.22.1/Electron 45, pinned CPython 3.12.10 venv and verified PG
payload/wheels under the existing loaded standard-user/DPAPI CI job; source
PYTHONPATH uses src and packages/atom-search-core/src, with a missing-model fixture.
Required cases cover scopes/revisions/errors, 64 pending/frame bounds, owner proof,
simultaneous views/launches, crash/reconnect/stale replies, attached-safe release
and source-parent orphan EOF. Packaging/frozen/native/Win10 remain separate gates.

P4 Task 1 palette-input slice: one canonical set of the existing 16 semantic
color roles generates static default CSS in the current stylesheet; noncolor
tokens and selector behavior stay in place. Next package boundary is Task 3.
Required checks cover exact default colors, closed serialization, prepared-mode
metadata, light/dark screenshots and query/selection during system media changes.
The existing Catppuccin default pair now has one typed semantic source. A small
build transform fills one placeholder in the existing stylesheet, preserving its
imports, selectors, color-scheme and static first-paint link. Fonts, motion and
spacing remain in CSS. Existing white surfaces and light warning color are
explicit ssearch adaptations; every default color stays unchanged. Nord and
Dracula are pinned dark-only prepared mappings, with future light composition
requiring an explicit choice and accessibility still unaccepted. Custom inputs
are a data-only RGB-byte type over the same finite roles; no ingestion, picker,
CSS/URL path, persistence or settings writer is introduced. Original license
texts for all three palettes accompany local dist notices.

Notice provenance slice: preserve the exact client-only 0.0.1 inputs and measure
its actual browser graph/output before accepting a source-bundle-only empty
marker disposition. Required cases include missing/incomplete graph evidence,
changed package bytes/integrity, server branch, emitted code/assets/imports and
every other missing-license package. Actual package acceptance remains separate.

P4 Task 2 builtin slice: trusted appearance metadata feeds the existing in-memory
theme preview; next package boundary remains Task 3. Required source scenarios:
canonical resolution, immutable output, duplicate/spoof/version/capability/namespace
refusal, and unchanged theme/search behavior through the existing selector.
`DesktopExtensionDescriptor` v1 and the pure builtin registry use the host's
static appearance catalog. Supplied metadata must match that catalog exactly;
resolution returns canonical frozen data, with no caller-selected permissions,
namespace, code or handlers. Its sole UI capability is theme preview. The bounded
`ssearch.appearance` namespace is metadata only; P2 remains the only settings writer.
The selector keeps the same modes, labels, order and in-memory behavior. External
loading/execution, service capabilities, P7 agent plugins and persisted settings
are outside this registry contract. Source evidence and review are linked in the PR.

Task 1 introduces React 19, HeroUI v3, Tailwind 4 and Vite independently of the
desktop host. This is source implementation awaiting GitHub checks, independent
review and native acceptance. It is not a connected replacement for the Qt app.

The screen serves one task: enter a remembered object or purpose, distinguish
same-name files by their full paths, then reveal a selected current result.
The top bar, large search field, quiet source context and wrapping result rows
follow the UI charter. Segoe UI uses the Windows system font; Catppuccin Latte
and Mocha provide neutral surfaces and a blue selection/focus accent. There are
no remote fonts, decorative cards or entrance cascades. Theme selection is an
in-memory preview, not another settings owner. Full paths remain visible without
hover, including distinguishing parent folders in narrow windows.

Applied design guidance: frontend-design; official [HeroUI v3 skill](https://github.com/heroui-inc/heroui/blob/31a6261590798d3c46f9cd0adf507653b3bc29ad/skills/heroui-react/SKILL.md)
and the exact candidate component sources; [Impeccable Operate](https://github.com/pbakaus/impeccable/blob/9fa16a06d8957fde5687984629fd028c9f2abf10/.agents/skills/impeccable/reference/operate.md),
craft-floor and new-work; [Hallmark app screens](https://github.com/Nutlope/hallmark/blob/7ec868225c6caa040558c321ee0fefa0e73f83a8/skills/hallmark/references/flow.md#app-screens)
and craft controls. Applied: familiar top bar, restrained tokens, compact controls,
instant focus outline, skeleton/empty/error/full states and readable paths as the
signature. The pinned charter and explicit implementation scope settle the design;
no launcher, hook, concept tournament or local rendering tool is installed.
The source snapshots and their licenses live in compatibility/upstream; they are
development references, excluded from the renderer. No separate audit report.

`SearchController` cancels and invalidates on every edit. Both abort state and
query generation reject late replies; reveal looks up only a result belonging
to the current completed generation. Composition Enter does not submit. HeroUI
ListBox owns keyboard selection and activation. Native OR media reduced motion
disables transitions; hidden windows stop loops. Tests exercise these contracts.

Production currently reports service unavailable and keeps the query for retry.
There is no HTTP client, credential, Node/Electron import, native reveal or mock
fallback in the renderer. The typed preload is a source boundary; the actual
verified broker connection remains a separate integration gate.
Only explicit development `VITE_DEV_MOCK=1` selects synthetic DTO fixtures; Vite
eliminates that import in production and bundle inspection rejects fixture IDs.
The preview reveal action explicitly explains that Explorer is not opened.
HMR alone receives a development CSP permitting its loopback websocket and React
refresh preamble. Production keeps a strict self-only script policy and disables
connections. A separate headless production scenario checks the unavailable
service path, retained query, local assets and an actual CSP-denied connection.

All tests/builds run on GitHub. The scoped workflow runs locked dependencies,
types, targeted behavior and desktop asset-boundary tests, an offline reinstall
and production build, local-asset/license inventory and headless screenshots.
Offline rebuild requires a prewarmed, integrity-verified npm cache; missing cache
fails before npm ci, and npm ci itself uses --offline --ignore-scripts.
`tools/build_frontend.ps1 -CachePath <prepared-cache>` is the CI entry point.
It also takes `-DesktopCachePath <prepared-desktop-cache>` and verifies/reinstalls
both dependency trees offline, with Electron binary download disabled. Missing
and empty cache scenarios must reject before npm ci; receipts join the existing
asset inventory with each lock's digest and verified package integrities.
The workflow may download dependencies and browser tools during its online
preparation step; application runtime and the offline rebuild do not do so.

Forge is package-only, with no makers, publishers or updater. The source shell now
loads only canonical `ssearch://ui/` assets from its isolated session; `atom://` and
file URLs have no alias. The sandboxed CommonJS preload publishes eight named
methods, consumed by `createSearchClient(api): SearchClient`. Exact sender/frame,
origin, session epoch, argument/body bounds and trusted main-side owner capability
checks precede the injected service port. Production has no verified port and
fails closed, without starting a service. Browser aborts remain local and send a
bounded cancellation ID; 64 pending calls, operation budgets and epoch checks
reject late completions. An uncertain mutation outcome requires an authoritative
state/revision read before any user retry; the client never retries mutations.

Task 2 source security uses the pinned [Electron 45 protocol](https://raw.githubusercontent.com/electron/electron/v45.0.0/docs/api/protocol.md),
[contextBridge](https://raw.githubusercontent.com/electron/electron/v45.0.0/docs/api/context-bridge.md)
and [security guidance](https://raw.githubusercontent.com/electron/electron/v45.0.0/docs/tutorial/security.md).
Assets/IPC source checks and independent review are recorded in [PR 4](https://github.com/empty-complete/ssearch/pull/4).
New source slices require their own GitHub checks and combined review.
The Electron receipt records
actual versions and binary SHA256 in ignored test artifacts. It proves source
interception/CSP/sender behavior with synthetic ports, not real broker authority,
service lifecycle, package/installer/native acceptance or final Win10 support.

Remaining owner migration gate: the build-time `tools/read-product.ts` adapter
reads the single PRODUCT catalog in `src/atom_search/content.py`; the workflow
also observes that catalog and the existing service DTO source path. These are
real dependencies awaiting the owner's namespace migration. This slice does not
claim a complete branding migration. Frontend/desktop icon inputs now use
`assets/ssearch.png` / `assets/ssearch.ico`, with the original icon bytes preserved.

Open gates: remote RED/GREEN observations (tests were authored first, no local
execution); candidate CI; independent combined visual/code review; real broker
and ownership/security integration; Task 3 frozen service, package/installer/offline runtime;
actual Win11 IME, keyboard, Narrator, high contrast, DPI/Snap and crash recovery;
final Win10 1809+ test only at the end. Generic documented Windows 10 support
does not establish the exact 1809 native gate. No Stable claim.

The exact client-only 0.0.1 tarball declares MIT, has a zero-byte default entry and
a separate 144-byte server error entry, and supplies no license text/copyright
notice or repository/gitHead. The npm publisher is not treated as a copyright
statement; the primary React RFC corroborates marker conventions, not a notice.
Original metadata, SRI, all three file hashes and primary-source observations are
preserved in compatibility/upstream and licenses/client-only. No replacement
license text or copyright holder is invented.

The source-bundle disposition now requires a build-only observer of the actual
Vite/Rolldown graph and output. It changes no resolution or module semantics.
`licenses/client-only/build-graph.json` distinguishes observed empty default entry
from absence in a supported complete browser graph. Server branch, rendered
implementation, external marker imports, package assets, unsupported telemetry
or stale/missing/added chunks fail closed. The collector binds lock/evidence and
all current script hashes; final bundle verification binds the actual source SHA.
Every other missing-license package is still rejected. The existing Forge inputs
copy frontend/dist into the desktop renderer and do not declare frontend
node_modules as an input. This source inspection does not prove a future frozen
package's actual contents; that acceptance remains open.
