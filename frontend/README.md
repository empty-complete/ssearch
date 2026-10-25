# ssearch frontend candidate

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
GitHub RED for the asset boundary: run 40206580329 (original protocol spelling),
then the exact `ssearch://` checkpoint c1565de. Targeted unit/browser and actual
hidden/offscreen Electron tests are authored; their current commit still needs
GitHub execution and one combined independent review. The Electron receipt records
actual versions and binary SHA256 in ignored test artifacts. It proves source
interception/CSP/sender behavior with synthetic ports, not real broker authority,
service lifecycle, package/installer/native acceptance or final Win10 support.

Open gates: remote RED/GREEN observations (tests were authored first, no local
execution); candidate CI; independent combined visual/code review; real broker
and ownership/security integration; Task 3 frozen service, package/installer/offline runtime;
actual Win11 IME, keyboard, Narrator, high contrast, DPI/Snap and crash recovery;
final Win10 1809+ test only at the end. Generic documented Windows 10 support
does not establish the exact 1809 native gate. No Stable claim.

License provenance exception: the inspected client-only 0.0.1 npm tarball declares
MIT but supplies no license text or copyright notice. Its browser entry is empty.
Only this exact version, tarball integrity and three original file hashes are
accepted; its original metadata and inspection evidence are preserved. The
collector still rejects every other missing license. No upstream copyright
holder is invented; packaging acceptance of this omission remains an open gate.
