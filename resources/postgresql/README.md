# Prepared PostgreSQL runtime

This directory is an offline build input, not a download location used by Atom
Search on colleagues' computers. `manifest.json`, `provenance.json` and the wheel
hash lock are tracked; payload binaries remain ignored. Missing or changed assets
fail preparation. Nothing is downloaded by the build or runtime verifier.

The exact PostgreSQL 18.6 EDB ZIP, pgvector v0.8.7 source commit/archive, official
wheel URLs and SHA-256 values, compiler flags, archive selection, complete PE
imports and required dependency graph are recorded in `provenance.json`.
Preparation evidence is in the P0 Task 2 report. The original archives remain
intact under `.tools/task2/downloads` on the preparation machine.

Preserve the complete EDB `bin`, `lib`, `share`, `include`, `doc` trees and every
top-level notice. Only the separate pgAdmin 4 and StackBuilder application trees
are excluded. No required server import resolves into them. Optional procedural
language plugins remain untouched and are not enabled by Atom Search; their
Perl/Python/Tcl runtimes are not bundled. Copy the release x64 VC145 CRT DLLs from
the pinned VS redistribution directory to `bin`, retaining Microsoft notices.

Build unmodified pgvector with the x64 developer environment, a private writable
TEMP/TMP, `PGROOT` pointing here, and upstream `nmake /F Makefile.win` followed by
`nmake /F Makefile.win install`. Keep `OPTFLAGS` empty; no host-specific CPU flags.
Copy the original license sources to `licenses` before computing the manifest.
The manifest includes every shipped file except itself, including this README,
provenance, hash lock and license snapshots. Re-preparation must be independently
reviewed; never silently rewrite a manifest to accept damaged assets.

Install the four prepared driver wheels only with `--no-index --no-deps
--require-hashes --find-links <local-wheelhouse> -r
resources/postgresql/wheels.lock.txt`. The normal build checks exact installed
dependency versions, verifies every runtime hash, preserves the sibling Psycopg
DLL directory, and runs `tools/check_postgres_runtime.py` against its actual frozen
runtime. The proof owns a unique private profile and never uses an environment
DSN. Its JSON is separate from the model/search self-test. This is still the
SQLite prototype search backend; task 3 owns the production database lifecycle
and frozen PostgreSQL CLI, and task 4 owns clean Windows 10/11 validation.
