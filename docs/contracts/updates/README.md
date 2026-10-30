# Signed update manifest v1

This is the source admission and isolated staging contract for ssearch. Installation,
rollback and frozen update diagnostics are not implemented by this boundary.
It does not enable automatic updates or establish native Windows
acceptance. The production trust store is deliberately empty: every update is
rejected until the owner pins approved public signing keys. Test keys are generated
ephemerally by Windows CNG inside tests and never enter client resources.

`ManifestVerifier(TrustStore, ReplayLedger).verify(envelope, channel=...,
installed=UpdateVersions(...), now=aware_datetime)` returns an immutable
`VerifiedManifest` only after its replay acceptance has been flushed and committed.
There is no optional or memory-only replay mode. Callers must obtain this value
before requesting any artifact; this module has no network or artifact fetch API.
The source staging boundary below checks transport ordering and archive membership.
Actual UNC qualification and packaging/frozen integration remain separate gates.

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
checked before archive fetch/extraction by the staging boundary below. A reference
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
included. Manifest rejection never returns `VerifiedManifest`, commits rejected policy to
high-water, downloads artifacts, extracts files or starts another lifecycle owner.

## Bounded source staging

`UNCTransport(root, fixture_mode=False)` accepts only an explicit owner UNC root;
it reads `update-manifest.json` and signed relative inventory/artifact paths. Local
roots require explicit `fixture_mode=True`, and never qualify an actual UNC share.
No settings, provider endpoints, credentials, Git commands or production feeds are
discovered. `read_verified_manifest(transport, verifier, ...)` reads only the bounded
manifest and obtains its signed/replay-admitted DTO before any artifact access.
Inventory/artifact methods require `VerifiedManifest` and a listed artifact ID.

Each source read retains native directory handles, checks the leaf's actual final
path/type/link count, rejects reparse points and hardlinks, and denies deletion or
replacement of pinned directories/files. Source file handles also deny writes.
Initial authority is traversed from its explicit drive/share anchor through held
ancestors; a newly opened final target is never promoted to an unchecked root.
These are source checks on disposable local fixtures; real SMB interruption,
permissions, offline behavior and permitted-share qualification remain unverified.

`verify_inventory(reference, raw)` admits exactly the signed byte size/SHA-256 and
the strict UTF-8 document `{"format_version":1,"entries":[{"path":"...",
"size":123,"sha256":"..."}]}`. The signed reference supplies the exact entry count
and unpacked sum. Entries obey the same Windows path and integer bounds as the
manifest; duplicate, case-colliding and file/directory prefix paths are rejected.
Only regular files are listed. Directory paths are derived from their parents.

`stage_release(manifest, transport, staging_dir=..., budget=...,
headroom_bytes=0, cancelled=None)` verifies **all** inventories, the combined
destination namespace and both app executable membership/digests before opening
the first archive. Both executables must belong to the app artifact's own inventory.
A `file` artifact is copied as a plain file at its signed relative path; it is never
interpreted as an archive. Zip entries use the signed installation paths. Collisions
across plain files and archives are denied before artifact reads.

The existing caller-owned `StorageBudget` is mandatory; staging does not construct
another domain, owner, model, queue or database. The staging directory must be a
strict descendant of an existing budget root and must not overlap the source.
Before any staging write, the shared reservation covers retained inventory bytes,
compressed archives and all output files, rounded separately to the actual volume
allocation unit. It includes 65536 bytes plus one allocation unit per derived/new
directory as conservative staging metadata headroom. `headroom_bytes` adds explicit
future backup/WAL/temp needs; no PostgreSQL measurement is invented. Existing install
and PGDATA are already physical owned storage and are not reserved a second time.
`ensure_remaining` rechecks the real shared domain before each further write and
after flush. Credits for already allocated file ranges reduce the remaining peak;
the reservation is released on success, cancellation or error. Space/allocation
metadata may cause a real denial before logical byte limits are reached.

Staging creates one unpredictable, exclusively created owner-private subdirectory,
opens output files with create-only semantics, retains directory handles during
writes and flushes each file before admission. Source bytes are unchanged. A
successful `StagedRelease` contains the verified manifest, private payload directory
and exact staged file tuple. It does not activate, install, launch, import, associate
or execute any payload. A cancellation callback is checked before further writes.
Cleanup only removes recorded identity-matching own files and empty directories;
it does not recursively remove the caller's staging root or follow replacements.
Unknown/foreign entries and unsafe replacements are preserved.
Creation/writer native volume/file IDs are checked under retained deletion handles,
including parent/root identity and actual handle ACLs. Cleanup disposes that checked
handle, rather than checking an identity and then deleting a replaceable pathname.

## Supported archive profile

The reader intentionally supports a **strict classic ZIP subset**, not arbitrary
Forge/installer ZIP output: one disk; stored or raw DEFLATE; normal regular files;
optional zero-content stored directory records that are parents of signed files;
CP437 or explicitly flagged UTF-8 names. ZIP64, data descriptors, encryption,
extra fields, comments, self-extracting prefixes, trailing bytes, special devices,
links, reparse attributes and unknown flags/formats are rejected. No real release
producer has been appointed. Its output must be checked against this profile before
integration; general ZIP/ZIP64 compatibility is not claimed. The policy's 4 GiB
artifact and 32 GiB total unpacked bounds remain safety ceilings, not a promise that
every single ZIP64 file near those bounds can be decoded.

A bounded record-by-record central-directory preflight checks counts, lengths,
local/central agreement, duplicate names/offsets and exact non-overlapping contiguous
compressed spans before decompression. It does not eagerly allocate an archive's
declared central-directory size. Stored/DEFLATE streams then enforce signed size,
SHA-256, CRC, actual EOF and exact compressed consumption. Each DEFLATE call has a
bounded output length; no unbounded flush is used. This narrow `struct`/`zlib` reader
also detects extra decoded bytes hidden behind a smaller central-directory size,
which a general ZIP reader can truncate to its declared size. Decompression never
writes beyond the signed inventory size and requires the exact signed file set.

Additional fixed staging codes are `update_inventory`, `update_artifact`,
`update_archive`, `update_storage`, `update_path`, `update_cancelled`, `update_source`,
`update_read` and QA-only `update_profile`; errors do not contain untrusted paths.

`tools/check_updates.py` is an isolated QA/source entrypoint with mandatory explicit
transport, release root, QA profile, staging directory, trust store, installed
versions, channel, quota and report arguments. It refuses a nonempty profile without
its QA marker and never reads live settings or provides test-key fallback. Its
receipt always states source-only, no activation, no frozen proof and no actual UNC
qualification. A local fixture receipt does not close packaging, actual-share,
installation/recovery, native Windows, Stable or later lifecycle gates.
