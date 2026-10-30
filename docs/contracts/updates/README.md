# Signed update manifest v1

This is the source admission contract for ssearch. Transport, archive inspection,
staging, installation, rollback and frozen update diagnostics are not implemented
by this boundary. It does not enable automatic updates or establish native Windows
acceptance. The production trust store is deliberately empty: every update is
rejected until the owner pins approved public signing keys. Test keys are generated
ephemerally by Windows CNG inside tests and never enter client resources.

`ManifestVerifier(TrustStore, ReplayLedger).verify(envelope, channel=...,
installed=UpdateVersions(...), now=aware_datetime)` returns an immutable
`VerifiedManifest` only after its replay acceptance has been flushed and committed.
There is no optional or memory-only replay mode. Callers must obtain this value
before requesting any artifact; this module has no network or artifact fetch API.
Actual transport ordering and archive membership are separate future safety gates.

The envelope is a UTF-8 JSON object containing exactly `key_id`, `payload_b64` and
`signature_b64`. Base64 must be canonical standard Base64, without whitespace.
The decoded signature is exactly 64 bytes, raw big-endian `r || s`, not DER.
Windows CNG verifies ECDSA P-256/SHA-256 over the **exact decoded payload bytes**
before the payload is parsed. Reformatting or re-encoding JSON changes those bytes.
No alternate crypto implementation, unpinned key lookup, or key supplied by the
manifest is accepted. CNG uses `BCryptImportKeyPair` with `ECCPUBLICBLOB` and
`BCryptVerifySignature`; see [Microsoft's CNG documentation](https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptverifysignature).

Both JSON layers reject duplicate keys, floating point numbers, NaN/Infinity,
invalid UTF-8, BOMs and unknown fields. Integers never accept booleans. The
[schema](manifest.schema.json) describes the wire structure; runtime checks also
enforce byte counts, sums, path collisions, compatibility and replay ordering.

The signed payload contains `format_version: 1`, a bounded release identity,
lowercase 40-hex `source_commit`, positive 63-bit `sequence`, explicit `stable`
or `nightly` channel, UTC second-resolution timestamps, `versions`, `artifacts`,
`compatibility` and `executables`. `versions.app` is the whole release version;
`desktop`, `service` and `service_api` describe its exact component pair.
`schema` is the PostgreSQL catalogue version; `settings_schema` is the JSON
settings version, independent of settings revision. `postgres` and `extension`
describe their separate lifecycles. Component versions use two to four numeric
dot-separated segments with no leading zeroes; prerelease strings are unsupported.
Nightly admission is explicit through the channel and its independent sequence.

The signed `compatibility` object must repeat exactly the target `desktop`,
`service` and `service_api`, and provides inclusive `schema_min`/`schema_max` and
`settings_schema_min`/`settings_schema_max` for the installed profile. This version
of admission requires an unchanged service API identity. All target app, desktop,
service, PostgreSQL, extension, catalogue and settings versions must be at least
their installed versions. This admission does not authorize a PostgreSQL major
transition or perform any schema migration.

Both `executables.desktop` and `executables.service` contain signed relative `.exe`
paths and lowercase SHA-256 digests. An `app` artifact must be a zip containing the
complete paired delivery, not an opaque installer or standalone executable.
Every zip has a signed inventory reference (`relative_path`, `size`, `sha256`,
`entry_count`, `unpacked_bytes`); a plain file has `inventory: null`. Inventory
contents, the two executable entries and their actual membership/digests must be
checked before archive fetch/extraction/activation by future staging. A reference
alone does not prove those files exist or that an archive is safe.

Paths use `/`, are relative and contain at most 512 Unicode code points. Empty,
dot/traversal segments, backslashes, absolute/UNC/drive paths, ADS/Windows-invalid
characters, controls, surrogate code points, trailing spaces/dots and device
names (including superscript COM/LPT aliases) are rejected. All artifact and
inventory paths share one case-insensitive namespace; duplicates and file/directory
prefix collisions are rejected. Executable paths have a separate installation
namespace and must be distinct.

Admission limits are 262144 envelope bytes, 16 artifacts, 4294967296 bytes per
artifact and 8589934592 total artifact bytes. Each inventory is at most 4194304
bytes and 20000 entries. Total declared unpacked bytes (zip references plus plain
files) are at most 34359738368. Issued time may lead `now` by at most 300 seconds;
expired releases are always denied. These are security bounds, not space reserves.

Pinned trust JSON has `format_version: 1` and at most 64 `keys`, each containing
exactly `key_id`, `algorithm: "ECDSA_P256_SHA256"`, `public_key_b64`, `not_before`
and `not_after`. The public key is the 72-byte CNG P-256 `ECCPUBLICBLOB`, with the
`ECS1` header and 32-byte X/Y coordinates. Keys must be unique and valid at the
verification time; validity intervals are half-open. Trust JSON is bounded to
262144 bytes. No production key or remote endpoint is assigned by this contract.

The replay file is owner-private runtime state, separate from user settings:
`{"format_version":1,"channels":{"stable":{"sequence":10,"payload_sha256":"..."}}}`.
A per-owner OS mutex keyed by canonical absolute path serializes read, comparison
and atomic replacement across processes. It uses the existing runtime private
directory/ACL/reparse checks and fsync/replace writer. A higher sequence advances
high-water; an equal sequence permits only the same SHA-256 of the exact signed
payload, allowing retry after a crash. Lower sequences, differing equal-sequence
payloads, corrupt state, unsafe storage or failed commits reject admission.
The initial absent ledger starts empty; this is not protection against its owner
deliberately deleting or restoring the whole profile. Hostile local owner actions
are outside this signed-release boundary.

Public denial codes are `update_manifest`, `update_limit`, `update_signature`,
`update_key`, `update_channel`, `update_time`, `update_expired`,
`update_compatibility`, `update_downgrade`, `update_replay` and `update_platform`.
Errors contain only fixed codes; payloads, credentials and untrusted paths are not
included. Rejection never returns `VerifiedManifest`, commits rejected policy to
high-water, downloads artifacts, extracts files or starts another lifecycle owner.
