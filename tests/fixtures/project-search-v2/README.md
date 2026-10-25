# Fictional metadata corpus v2

These twelve UTF-8 TXT files are synthetic acceptance inputs, not colleagues'
documents or genuine CAD/PDF files. Their bodies are not searched. Manifest
schema1 supplies public source/project UUIDs and confirmed/proposed bindings.
Query expectations distinguish literal, resolved, ambiguous, pending, revoked
and offline phases, including ordinary unassigned orders/reports/instructions.
Ambiguous ПМ retains literal candidates and both scoped options; it does not
expand to either canonical project until the caller makes an explicit choice.
Its exact result expectations distinguish pending canonical fallback from the
current indexed alias field. The QA scenario observes the actual persisted
projection state and records it with each phase; neither state accepts a set
chosen after seeing the returned results.

File `id` is a **logical fixture identifier**, not a preassigned result UUID.
The shared runner maps `(source UUID, relative path)` to it and verifies the
actual profile's canonical public result UUID through fetch and repeated search.

All JSON/TXT input bytes use LF. The corpus digest hashes sorted input names,
a NUL separator and each file's SHA256 digest, including raw manifest/query
bytes and all manifest-referenced TXT bytes. README is not a runtime input.
PyInstaller explicitly copies this one directory to `resources/project-search-v2`;
the external driver and frozen diagnostic compare this same digest.

P6 may reuse definitions/expectations with its own read-only demo provider.
This QA profile is never the application's working catalogue or tutorial UI.
