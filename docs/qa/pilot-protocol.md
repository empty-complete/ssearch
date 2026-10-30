# ssearch pilot evidence protocol

This document defines evidence collection; it records no completed pilot or
platform acceptance. Until participants, machines and receipts exist, use
`not_run`, or `blocked` with the actual missing prerequisite. Unknown measurements
are `null`. No source test, DOM assertion or screenshot establishes native Windows
acceptance or observed usability. Development may continue independently.

## Before any pilot or load run

The operator must explicitly record the approved hardware profile (OS/build, x64,
CPU, RAM, storage), number of pilot PCs, disposable profile/corpus location and
permitted load window. These inputs are currently **not supplied**. Do not infer
them from the developer PC, operate on a production index or begin a large run
without them. Windows 10 is tested only at final platform acceptance.

Bind evidence to one clean source SHA, desktop executable SHA256, frozen service
SHA256, complete payload inventory SHA256 and owner generation. Copy the measured
Electron, embedded Node, Chromium, frontend, service, desktop API, service API,
schema, model, PostgreSQL and vector versions exactly. Metadata-only candidates
still identify their model mode explicitly (for example `metadata-only`). Source
and frozen serve must have separate receipts; one executable's receipt does not
prove the other executable or their compatibility.

Keep receipts/reports locally in ignored artifacts. Publish only reviewed,
anonymous aggregates. Never include document contents, paths, queries, credentials
or participant names. No telemetry, upload or publication is performed by the
evaluator.

## Machine-readable contract

[release-evidence.schema.json](../../schemas/release-evidence.schema.json) is the
structural v1 contract. Evaluate it with
`python tools/qa/release_matrix.py evidence.json`; repeat `--require gate-id` to
add gates. Exit 0 means the declared candidate's evidence satisfies this policy;
exit 1 means blocked or unreadable evidence. Neither outcome promotes a stage,
authorizes publication, or claims Stable. No application or installer is launched.

Top-level fields are `schema_version: 1`, `candidate`, `capabilities`,
`declared_capabilities`, `environments` and `gates`. Candidate bindings contain
`source_sha`, `desktop_sha256`, `service_sha256`, `payload_inventory_sha256`,
`owner_generation`, and `versions`. Each receipt repeats this entire binding and
references an environment by `environment_id`. Environment entries require `os`,
`os_build`, `architecture`, `cpu`, `ram_bytes`; unmeasured fields are null and
cannot support eligibility. Exact binding mismatches block even when a receipt
claims pass. API compatibility currently accepts only desktop `1` / service `1`;
future pairs require a reviewed policy change.

Every gate has `gate_id`, `status` (`pass`, `fail`, `not_run`, `blocked`),
`artifact_sha256` (the candidate desktop hash), `environment_id`, `report_path`,
`reason`, `binding` and `evidence_kind` (`automated`, `browser`, `native`, `pilot`).
A pass needs a report reference. Other statuses need a reason. Duplicate gate IDs
are rejected; a later pass cannot erase a failure. Closed objects reject unknown
fields; the CLI rejects duplicate JSON keys. Report references are not opened or
authenticated by this evaluator: a trusted collector/reviewer must verify their
contents and provenance before submitting evidence. Test fixtures are synthetic
and are never actual release evidence.

Mandatory policy is combined with caller gates, never replaced:

| Scope | Required gates |
| --- | --- |
| Every candidate | `runtime.source`, `runtime.frozen`, `backend.integrity`, `security.access`, `offline.first-run`, `upgrade.compatibility`, `recovery.restore`, `platform.windows-11` |
| metadata (required) | `quality.metadata` |
| ui | `ui.browser`, `ux.primary-flow` |
| deep | `security.cached-content-policy`, `security.deep-access`, `quality.deep` |
| repair | `recovery.repair` |

Capabilities are explicit booleans. `declared_capabilities` retains previously
promised features; disabling a declared feature blocks until the product scope
decision is made explicitly. Disabled capabilities cannot carry pass receipts.
An existing fail blocks even outside the mandatory set. Missing gates and any
mandatory `not_run`/`blocked`/`fail` leave eligibility false. Final platform release
also requires `platform.windows-10` and the wider release/signing/Full acceptance
policy; this initial evaluator alone cannot approve Stable.

`ui.browser` requires browser evidence; `platform.*` requires native evidence.
Win11 receipts require a Windows 11 x64 environment, and final Win10 receipts
require Windows 10 x64. `ux.primary-flow` requires actual pilot evidence on Windows
11 x64. Browser work may confirm its own checks while these gates remain open.

## Independent primary-flow observation

Use 3–5 distinct first-time colleagues, without developer hints. Each participant
independently performs all six scenarios on the exact candidate:

| scenario_id | Task |
| --- | --- |
| `add-source` | Add an approved disposable source |
| `find-duplicate` | Recall an object/purpose and choose the correct duplicate using project/name/path context without hover |
| `reveal-in-explorer` | Show the selected file in Explorer; observe recovery for a moved/deleted file |
| `open-progress` | Find indexing/task progress and return to search |
| `restore-from-tray` | Restore the window from the tray |
| `skip-replay-onboarding` | Skip onboarding and replay it later |

The pilot receipt's `observation` contains `independent`,
`unresolved_failures`, and `participants`. Each participant has a pseudonymous
`participant_id`, `first_time`, and six `scenarios`. Each scenario records
`scenario_id`, `success`, `assistance_requested`, `errors`, `duration_seconds`
(null when unmeasured), and `recovery` (use `none` when none was needed). Record
observed assistance/errors/recovery honestly. Failed scenarios, requested help,
errors, unknown time or required recovery block this conservative primary-flow
policy until the difficulty is addressed and the affected observation repeated.
Do not erase failed receipts or replace them with a screenshot. Document the
resolution and retain the prior report locally.

Also retain keyboard-only navigation/focus, readable duplicate context, both
themes, narrow-window behavior, 100/150/200% DPI, reduced motion, empty/error and
offline states in the report. Record success, assistance, mistakes, elapsed time
and recovery for each, with actual environment and candidate bindings. A small
sample describes observations and provides no statistical usability guarantee.

## Remaining actual acceptance

Keep clean install/reinstall/upgrade/uninstall with profile preservation, owned
versus attached lifecycle, two clients, renderer/broker/service crashes,
offline-first launch without developer runtimes, real UNC/DFS/denied/revoked
access, path/IME/DPI/accessibility and restore/migration checks explicit in their
native/runtime/security reports. They remain unperformed until actual receipts
exist. Deep cached-content policy, repair and update/signature scenarios apply
only when those capabilities are shipped. Measure scale/quality on approved
resources and independent judgments, separately from demo fixtures. Do not lower
thresholds after observing a failure.

Public blocker codes include `schema_invalid`, `required_gates_invalid`,
`artifact_mismatch`, `api_incompatible`, `versions_unpinned`, `metadata_required`,
`capability_downgrade`, `disabled_capability_evidence`, `duplicate_gate`,
`environment_missing`, `environment_incomplete`, `environment_mismatch`,
`gate_missing`, `gate_fail`/`gate_failed`, `gate_not_run`, `gate_blocked`,
`reason_missing`, `report_missing`, `evidence_kind_mismatch`,
`ux_observation_incomplete`, and CLI `evidence_unreadable`. Validation errors
identify structural field locations. `eligible` never means that external report
claims have been independently verified by this program.
