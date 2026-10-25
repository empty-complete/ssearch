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
fallback in the renderer. Task 2 owns the typed preload and service connection.
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
The workflow may download dependencies and browser tools during its online
preparation step; application runtime and the offline rebuild do not do so.

Forge is package-only, with no makers, publishers or updater. The shell has no
preload or service lifecycle in Task 1. Its temporary file loader resolves existing
assets under the selected root, blocks remote requests, permissions, subframes,
navigation, windows and downloads. Full atom protocol/IPC proof belongs to Task 2.
No package/installer/native acceptance is claimed by TypeScript or DOM checks.

Open gates: remote RED/GREEN observations (tests were authored first, no local
execution); candidate CI; independent combined visual/code review; Task 2 bridge
and ownership/security; Task 3 frozen service, package/installer/offline runtime;
actual Win11 IME, keyboard, Narrator, high contrast, DPI/Snap and crash recovery;
final Win10 1809+ test only at the end. Generic documented Windows 10 support
does not establish the exact 1809 native gate. No Stable claim.
