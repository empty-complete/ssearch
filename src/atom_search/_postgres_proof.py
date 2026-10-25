"""Private synthetic QA through PostgresRuntime, never a user backup API.

The outer owner accounts sibling source/target/archive roots. Its small marker
and reservation ledger are metadata outside those roots. Each Runtime retains
its independent installation identity, credential namespace and local budget.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from atom_search.runtime import _windows
from atom_search.runtime.credentials import CredentialStore
from atom_search.runtime.manifest import verify_runtime
from atom_search.runtime.postgres import PostgresRuntime, PostgresRuntimeError
from atom_search.runtime.storage import StorageBudget

SCHEMA = "atom_p0_restore"
BASIS = "[" + ",".join(["1"] + ["0"] * 383) + "]"
FIXTURE = [(1, "contract", BASIS),
           (2, "drawing", "[" + ",".join(["0", "1"] + ["0"] * 382) + "]"),
           (3, "schedule", "[" + ",".join(["-1"] + ["0"] * 383) + "]")]
COSINE = [[1, 0.0], [2, 1.0], [3, 2.0]]


def _fail(code):
    raise PostgresRuntimeError(code)


def _failure_detail(error):
    """Type/locations only: no message, arguments, SQL, locals or source reads."""
    observed = set()
    for _ in range(8):
        observed.add(id(error))
        context = error.__context__
        if context is None or id(context) in observed:
            break
        error = context
    locations = []
    trace = error.__traceback__
    while trace is not None and len(locations) < 12:
        locations.append({"file": Path(trace.tb_frame.f_code.co_filename).name,
                          "line": trace.tb_lineno})
        trace = trace.tb_next
    return {"type": type(error).__name__, "locations": locations}


def _digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _identity():
    if getattr(sys, "frozen", False):
        from _atom_build_identity import IDENTITY
        if (IDENTITY.get("source_clean") is not True
                or not re.fullmatch("[0-9a-f]{40}", IDENTITY.get("source_commit", ""))):
            _fail("build_identity_unavailable")
        return dict(IDENTITY)
    from atom_search import VERSION
    return {"source_commit": None, "source_clean": None, "app_version": VERSION,
            "core_version": importlib.metadata.version("ssearch-core")}


def _process_memory(identity):
    """Actual postmaster sample, not whole application/backend/GUI memory."""
    import ctypes as c
    from ctypes import wintypes as w
    class Counters(c.Structure):
        _fields_ = [("cb", w.DWORD), ("faults", w.DWORD),
                    ("peak", c.c_size_t), ("working", c.c_size_t),
                    ("peak_paged", c.c_size_t), ("paged", c.c_size_t),
                    ("peak_nonpaged", c.c_size_t), ("nonpaged", c.c_size_t),
                    ("pagefile", c.c_size_t), ("peak_pagefile", c.c_size_t)]
    with _windows.verified_process(identity), _windows.process_handle(identity.pid) as handle:
        if not handle or _windows._identity(handle, identity.pid) != identity:
            raise _windows.WindowsError("process_changed")
        counters = Counters()
        counters.cb = c.sizeof(counters)
        _windows._check(_windows._api(
            _windows._dll("psapi"), "GetProcessMemoryInfo",
            [w.HANDLE, c.POINTER(Counters), w.DWORD])(handle, c.byref(counters), counters.cb),
            "process_memory_unavailable")
        return {"pid": identity.pid, "created_filetime": identity.created,
                "working_set_bytes": counters.working, "peak_working_set_bytes": counters.peak}


def _snapshot(instance):
    with instance.open_connection() as connection:
        rows = connection.execute(
            f"SELECT id,payload,embedding::text FROM {SCHEMA}.documents ORDER BY id").fetchall()
        dimensions = connection.execute(
            f"SELECT min(vector_dims(embedding)),max(vector_dims(embedding)) FROM {SCHEMA}.documents"
        ).fetchone()
        cosine = connection.execute(
            f"SELECT id,embedding <=> %s::vector AS distance FROM {SCHEMA}.documents "
            "ORDER BY distance,id", (BASIS,)).fetchall()
    if (rows != FIXTURE or dimensions != (384, 384)
            or any(a[0] != b[0] or abs(a[1] - b[1]) > 1e-9 for a, b in zip(cosine, COSINE))
            or len(cosine) != 3):
        _fail("proof_vector_query_failed")
    return {"rows": [list(row) for row in rows], "cosine_rows": [list(row) for row in cosine],
            "dimension": 384}


class _Proof:
    def __init__(self, root, runtime, report, token):
        self.root, self.runtime, self.report, self.token = root, runtime, report, token
        self.marker = root / "qa-owner.json"
        self.source_dir, self.target_dir, self.work = (
            root / "source-profile", root / "restore-profile", root / "backup")
        self.instances = []
        self.passwords = []
        self.record = None
        self.root_identity = None
        self.reservation = None

    def guard(self):
        _windows.assert_private(self.root)
        _windows.assert_private(self.marker)
        if (_windows._file_identity(self.root) != self.root_identity
                or json.loads(self.marker.read_text(encoding="utf-8")) != self.record):
            _fail("qa_ownership_unconfirmed")

    def save(self):
        if self.record is not None:
            self.guard()
        record = {"version": 1, "run_token": self.token, "root": str(self.root),
                  "root_identity": list(self.root_identity),
                  "seed_complete": getattr(self, "seed_complete", False),
                  "installations": json.loads(json.dumps(getattr(self, "installations", {})))}
        if hasattr(self, "settings_hash"):
            record["settings_sha256"] = self.settings_hash
        if self.record is None:
            with self.marker.open("x", encoding="utf-8") as output:
                json.dump(record, output)
                output.flush()
                os.fsync(output.fileno())
            _windows.assert_private(self.marker)
        else:
            _windows.atomic_json(self.marker, record)
        self.record = record

    def prepare(self, lifecycle):
        if lifecycle == "verify":
            try:
                _windows.assert_private(self.root)
                _windows.assert_private(self.marker)
                record = json.loads(self.marker.read_text(encoding="utf-8"))
                if (record["version"] != 1 or record["run_token"] != self.token
                        or record["seed_complete"] is not True
                        or record["root"] != str(self.root)
                        or record["root_identity"] != list(_windows._file_identity(self.root))
                        or set(record["installations"]) != {"source-profile", "restore-profile"}):
                    raise ValueError()
                self.root_identity = tuple(record["root_identity"])
                self.record, self.installations = record, dict(record["installations"])
                self.settings_hash = record["settings_sha256"]
                self.seed_complete = True
                self.guard()
                source = PostgresRuntime(self.runtime, self.source_dir)
                self.runtime_guard(source)
                settings = self.source_dir / "settings.json"
                _windows.assert_private(self.source_dir)
                _windows.assert_private(settings)  # also rejects every reparse ancestor
                if _digest(settings) != self.settings_hash:
                    _fail("qa_settings_changed")
            except Exception:
                _fail("qa_ownership_unconfirmed")
        else:
            if self.root.exists():
                _fail("disposable_profile_must_be_new")
            _windows.private_directory(self.root)
            self.root_identity = _windows._file_identity(self.root)
            self.installations = {}
            self.save()
        self.guard()
        budget = StorageBudget((self.source_dir, self.target_dir, self.work),
                               quota_bytes=5 * 1024**3, min_free_bytes=2 * 1024**3)
        self.reservation = budget.reserve(384 * 1024**2, "synthetic_restore_proof")
        if lifecycle != "verify":
            for path in (self.source_dir, self.work):
                self.guard()
                _windows.private_directory(path)
            settings = self.source_dir / "settings.json"
            _windows.atomic_json(settings, {"qa": {"run_id": self.token}, "theme": "dark",
                                            "sources": []})
            self.settings_hash = _digest(settings)
            self.save()

    def runtime_guard(self, instance):
        self.guard()
        _windows.assert_private(instance.profile_dir)
        _windows.assert_private(instance.cluster_dir)
        expected = self.installations[instance.profile_dir.name]
        if list(_windows._file_identity(instance.cluster_dir)) != expected["cluster_identity"]:
            _fail("qa_runtime_ownership_changed")
        owner = instance.cluster_dir / "owner.json"
        _windows.assert_private(owner)
        value = json.loads(owner.read_text(encoding="utf-8"))
        if (value["installation_id"] != expected["installation_id"]
                or value["profile"] != str(instance.profile_dir)
                or value["data"] != str(instance.data_dir)):
            _fail("qa_runtime_ownership_changed")
        return value

    def start(self, directory):
        self.guard()
        instance = PostgresRuntime(self.runtime, directory)
        if directory.name in self.installations:
            record = self.runtime_guard(instance)
            if record["server"] is not None or record.get("launch_pending"):
                _fail("qa_expected_stopped_runtime")
        elif directory.exists() and directory != self.source_dir:
            _fail("restore_target_must_be_new")
        self.instances.append(instance)
        started = time.monotonic()
        endpoint = instance.start()
        self.report.setdefault("timings", {})[directory.name + "_start_seconds"] = round(
            time.monotonic() - started, 3)
        existing = self.installations.get(directory.name)
        if existing and endpoint.installation_id != existing["installation_id"]:
            _fail("qa_runtime_ownership_changed")
        self.installations[directory.name] = {
            "installation_id": endpoint.installation_id,
            "cluster_identity": list(_windows._file_identity(instance.cluster_dir))}
        self.save()
        self.runtime_guard(instance)
        self.sql_identity(instance, endpoint.installation_id)
        self.memory(instance, "idle_sample")
        return instance, endpoint

    def memory(self, instance, phase):
        record = self.runtime_guard(instance)
        if record["server"] is None:
            return
        identity = _windows.ProcessIdentity(**record["server"])
        metrics = self.report.setdefault("memory", {}).setdefault(instance.profile_dir.name, {
            "method": "GetProcessMemoryInfo; owned postmaster only, excludes backends and GUI"})
        try:
            metrics[phase] = _process_memory(identity)
        except _windows.WindowsError:
            metrics[phase] = {"status": "unavailable"}

    def sql_identity(self, instance, identity):
        with instance.open_connection(role="maintenance") as connection:
            cluster = connection.execute("SHOW cluster_name").fetchone()[0]
            data = connection.execute("SHOW data_directory").fetchone()[0]
            marker = connection.execute("SELECT installation_id::text FROM atom_runtime.identity").fetchone()[0]
        if cluster != identity or marker != identity or Path(data).resolve() != instance.data_dir:
            _fail("proof_runtime_identity_changed")

    def tool(self, name, arguments, *, instance=None):
        self.guard()
        if instance:
            self.runtime_guard(instance)
        manifest = verify_runtime(self.runtime)
        if not any(asset.path == "bin/" + name for asset in manifest.files):
            _fail("backup_tool_unverified")
        _windows.assert_private(self.work)
        directory_identity = _windows._file_identity(self.work)
        descriptor, filename = tempfile.mkstemp(prefix=name + "-", suffix=".out", dir=self.work)
        output = Path(filename)
        file_identity = _windows._file_identity(output)
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("PG")}
        environment.update(TEMP=str(self.work), TMP=str(self.work), LC_ALL="C")
        timed_out = False
        with os.fdopen(descriptor, "wb") as stream:
            try:
                result = subprocess.run([str(self.runtime / "bin" / name), *map(str, arguments)],
                                        cwd=self.work, env=environment, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=60,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            except subprocess.TimeoutExpired:
                timed_out = True
        self.guard()
        if instance:
            self.runtime_guard(instance)
        _windows.assert_private(output)
        if (_windows._file_identity(self.work) != directory_identity
                or _windows._file_identity(output) != file_identity):
            _fail("temporary_ownership_changed")
        payload = output.read_bytes()
        if any(secret.encode("ascii") in payload for secret in self.passwords):
            output.unlink()
            _fail("proof_secret_leak")
        if timed_out:
            _fail("backup_tool_timeout")
        return result.returncode, payload.decode("utf-8", errors="replace")

    @contextmanager
    def conninfo(self, instance):
        from psycopg.conninfo import make_conninfo
        self.runtime_guard(instance)
        directory_identity = _windows._file_identity(self.work)
        with instance.open_connection(role="maintenance") as connection:
            info = connection.info
            password = info.password
            self.passwords.append(password)
            if (info.host != "127.0.0.1" or info.user != "atom_bootstrap"
                    or not password or "\n" in password or "\r" in password):
                _fail("proof_connection_invalid")
            descriptor, filename = tempfile.mkstemp(prefix="pgpass-", suffix=".conf", dir=self.work)
            passfile = Path(filename)
            file_identity = _windows._file_identity(passfile)
            try:
                with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as output:
                    escape = lambda value: str(value).replace("\\", "\\\\").replace(":", "\\:")
                    output.write(":".join(map(escape, (
                        "127.0.0.1", info.port, info.dbname, info.user, password))) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                _windows.assert_private(passfile)
                yield make_conninfo(host="127.0.0.1", hostaddr="127.0.0.1", port=info.port,
                                    dbname=info.dbname, user=info.user, passfile=str(passfile),
                                    connect_timeout=5, sslmode="disable", gssencmode="disable",
                                    options="", client_encoding="UTF8")
            finally:
                # Unknown/replaced directories/files are preserved, not removed.
                self.runtime_guard(instance)
                _windows.assert_private(passfile)
                if (_windows._file_identity(self.work) != directory_identity
                        or _windows._file_identity(passfile) != file_identity):
                    _fail("temporary_ownership_changed")
                passfile.unlink()

    def auth(self, instance, endpoint):
        import psycopg
        with instance.open_connection(role="maintenance") as connection:
            maintenance_password = connection.info.password
            self.passwords.append(maintenance_password)
            scram = connection.execute(
                "SELECT bool_and(rolpassword LIKE 'SCRAM-SHA-256$%') FROM pg_authid "
                "WHERE rolname IN ('atom_app','atom_bootstrap')").fetchone()[0]
        if scram is not True:
            _fail("proof_scram_failed")
        with instance.open_connection() as connection:
            password = connection.info.password
            self.passwords.append(password)
            store = CredentialStore(instance.cluster_dir, endpoint.installation_id)
            if (len(password) < 40 or password == maintenance_password
                    or store.resolve(endpoint.credential_ref) != password):
                _fail("proof_credentials_not_independent")
            connection.autocommit = True
            flags = connection.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user").fetchone()
            if flags != (False,) * 5:
                _fail("proof_application_privileges")
            for statement in ("CREATE ROLE atom_forbidden", "CREATE DATABASE atom_forbidden",
                              "SET ROLE atom_bootstrap"):
                try:
                    connection.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    _fail("proof_application_privileges")
            connection.execute("SELECT 1").fetchone()
        try:
            with psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=endpoint.port,
                                 dbname=endpoint.database, user="atom_app",
                                 password=secrets.token_urlsafe(48), connect_timeout=5,
                                 sslmode="disable", gssencmode="disable", options="",
                                 passfile=str(self.work / "unused-passfile")):
                _fail("proof_wrong_password_accepted")
        except psycopg.OperationalError as error:
            if "password authentication failed" not in str(error):
                _fail("proof_auth_failure_unconfirmed")
        with instance.open_connection() as connection:
            connection.execute("SELECT 1").fetchone()
        self.report.update(scram=True, dpapi_verified=True, private_acl_verified=True,
                           application_privileges_verified=True, wrong_password_rejected=True)

    def restore(self, source, source_endpoint, expected):
        versions = {}
        for name in ("pg_dump.exe", "pg_restore.exe"):
            code, version = self.tool(name, ["--version"])
            if code or not re.search(r"\(PostgreSQL\) 18\.6(?:\s|$)", version):
                _fail("backup_tool_version_mismatch")
            versions[name] = version.strip()
        archive = self.work / "fixture.dump"
        with self.conninfo(source) as conninfo:
            code, _ = self.tool("pg_dump.exe", [
                "--format=custom", "--schema=" + SCHEMA, "--strict-names",
                "--lock-wait-timeout=5s", "--no-password", "--file=" + str(archive),
                "--dbname=" + conninfo], instance=source)
        if code or not archive.is_file() or archive.stat().st_size == 0:
            _fail("dump_tool_failed")
        _windows.assert_private(archive)
        code, toc = self.tool("pg_restore.exe", ["--list", str(archive)])
        if code or SCHEMA not in toc or "atom_runtime" in toc:
            _fail("backup_scope_invalid")
        self.report["backup"] = {
            "method": "logical-schema", "scope": "synthetic-fixture-schema", "same_major": True,
            "tools": versions, "path": "backup/fixture.dump", "bytes": archive.stat().st_size,
            "sha256": _digest(archive), "runtime_marker_excluded": True, "dump_exit_code": 0,
            "requires_preinstalled_vector": True, "preserves_ownership_and_acl": False}
        self.reservation.ensure_remaining(256 * 1024**2)
        target, target_endpoint = self.start(self.target_dir)
        if target_endpoint.installation_id == source_endpoint.installation_id:
            _fail("restore_target_identity_not_distinct")
        self.report["restore"] = {
            "source_installation_id": source_endpoint.installation_id,
            "target_installation_id": target_endpoint.installation_id,
            "target_profile": str(self.target_dir), "rows_equal": False,
            "restart_verified": False, "float_tolerance": 1e-9}
        with self.conninfo(target) as conninfo:
            code, _ = self.tool("pg_restore.exe", [
                "--single-transaction", "--exit-on-error", "--no-owner", "--no-acl",
                "--no-tablespaces", "--no-password", "--dbname=" + conninfo, str(archive)],
                instance=target)
        self.report["restore"]["restore_exit_code"] = code
        if code:
            _fail("restore_tool_failed")
        with target.open_connection(role="maintenance") as connection:
            connection.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO atom_app")
            connection.execute(f"GRANT SELECT ON {SCHEMA}.documents TO atom_app")
        actual = _snapshot(target)
        if actual != expected:
            _fail("restore_rows_mismatch")
        self.sql_identity(target, target_endpoint.installation_id)
        target.stop()
        if target.start().installation_id != target_endpoint.installation_id:
            _fail("restore_restart_identity_changed")
        if _snapshot(target) != expected:
            _fail("restore_restart_rows_mismatch")
        self.sql_identity(target, target_endpoint.installation_id)
        self.report["restore"].update(
            rows_equal=True, target_identity_preserved=True, restart_verified=True,
            dimension=actual["dimension"], cosine_rows=actual["cosine_rows"], count=len(actual["rows"]))
        self.memory(target, "after_restore_restart")
        self.reservation.ensure_remaining(128 * 1024**2)

    def audit(self):
        self.guard()
        for instance in self.instances:
            self.runtime_guard(instance)
            for path in instance.cluster_dir.iterdir():
                if path.is_file():
                    data = path.read_bytes()
                    if (any(secret.encode("ascii") in data for secret in self.passwords)
                            or b"SCRAM-SHA-256$" in data):
                        _fail("proof_secret_leak")
        if any(secret in json.dumps(self.report) for secret in self.passwords):
            _fail("proof_secret_leak")
        if any(self.work.glob("pgpass-*")):
            _fail("proof_passfile_retained")

    def stop(self):
        stopped = True
        for instance in reversed(self.instances):
            try:
                instance.stop()
                record = self.runtime_guard(instance)
                if (instance.status().state != "stopped" or record["server"] is not None
                        or record.get("launch_pending")):
                    stopped = False
            except Exception:
                stopped = False
        self.report["clean_stop"] = stopped
        if not stopped:
            self.report["cleanup_error_code"] = "owned_server_stop_unconfirmed"
        if self.reservation is not None:
            try:
                self.guard()
                self.reservation.__exit__(None, None, None)
            except Exception:
                stopped = False
                self.report["cleanup_error_code"] = "qa_reservation_release_unconfirmed"
        return stopped

    def remove(self):
        self.guard()
        for instance in self.instances:
            record = self.runtime_guard(instance)
            if record["server"] is not None or record.get("launch_pending"):
                _fail("proof_cleanup_identity")
        for directory, directories, files in os.walk(self.root):
            for name in directories + files:
                _windows.reject_reparse(Path(directory) / name)
        self.guard()
        shutil.rmtree(self.root)
        self.report["profile_removed"] = not self.root.exists()


def _run(report_path, *, data_dir, runtime_dir, lifecycle="oneshot", run_token=None):
    """Fresh one-shot, or explicit retained QA seed/verify with the same owner."""
    started = time.monotonic()
    root, target = Path(data_dir).absolute(), Path(report_path).absolute()
    # No finally writer may create or replace any rejected profile/report path.
    try:
        _windows.reject_reparse(root)
        _windows.reject_reparse(target)
        if target.exists() or target.resolve().is_relative_to(root.resolve()):
            return 1
    except Exception:
        return 1
    report = {"ok": False, "runtime_api": "PostgresRuntime",
              "execution_mode": "frozen" if getattr(sys, "frozen", False) else "source",
              "profile": str(root), "clean_stop": False, "profile_removed": False,
              "lifecycle": lifecycle, "checks": {}, "failures": []}
    proof = source = endpoint = expected = None
    try:
        if lifecycle not in ("oneshot", "seed", "verify"):
            _fail("qa_lifecycle_invalid")
        if lifecycle != "oneshot" and not re.fullmatch("[0-9a-f]{32}", run_token or ""):
            _fail("qa_run_token_required")
        if lifecycle != "verify" and root.exists():
            _fail("disposable_profile_must_be_new")
        report.update(_identity())
        # Hash-first even for the outer owner, marker and reservation ledger.
        manifest = verify_runtime(runtime_dir)
        report["sizes"] = {"runtime_bytes": sum(asset.size for asset in manifest.files),
                           "accounting": "owned sibling roots; excludes outer marker/ledger metadata"}
        proof = _Proof(root, Path(runtime_dir).resolve(), report, run_token or uuid.uuid4().hex)
        proof.prepare(lifecycle)
        source, endpoint = proof.start(proof.source_dir)
        report.update(source_profile=str(proof.source_dir), server_version=endpoint.postgres_version,
                      extension_version=endpoint.vector_version)
        if lifecycle != "verify":
            with source.open_connection(role="maintenance") as connection:
                connection.execute(f"CREATE SCHEMA {SCHEMA}")
                connection.execute(
                    f"CREATE TABLE {SCHEMA}.documents (id integer PRIMARY KEY,payload text NOT NULL,"
                    "embedding vector(384) NOT NULL)")
                with connection.cursor() as cursor:
                    cursor.executemany(f"INSERT INTO {SCHEMA}.documents VALUES (%s,%s,%s::vector)", FIXTURE)
                connection.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO atom_app")
                connection.execute(f"GRANT SELECT ON {SCHEMA}.documents TO atom_app")
        expected = _snapshot(source)
        proof.auth(source, endpoint)
        report.update(dimension=384, cosine_rows=expected["cosine_rows"])
        if lifecycle != "verify":
            source.stop()
            if source.start().installation_id != endpoint.installation_id or _snapshot(source) != expected:
                _fail("proof_restart_identity")
            report["restart_verified"] = True
            proof.restore(source, endpoint, expected)
        else:
            report.update(restart_verified=True, persisted_rows_verified=True)
        proof.sql_identity(source, endpoint.installation_id)
        if _digest(proof.source_dir / "settings.json") != proof.settings_hash:
            _fail("qa_settings_changed")
        report["settings_sha256"] = proof.settings_hash
        report["settings_preserved"] = True
        proof.audit()
        proof.memory(source, "after_proof")
        report["checks"].update(runtime="pass", source_restart="pass",
                                restore="not-run" if lifecycle == "verify" else "pass")
        report["ok"] = True
    except Exception as error:
        report["error_code"] = error.code if isinstance(error, PostgresRuntimeError) else "postgres_proof_failed"
        report["failure_detail"] = _failure_detail(error)
        report["failures"].append(report["error_code"])
    finally:
        if proof and source and expected:
            try:
                proof.runtime_guard(source)
                proof.sql_identity(source, endpoint.installation_id)
                if _snapshot(source) != expected:
                    _fail("proof_source_changed")
                report["preservation"] = {"source_verified": True,
                                          "source_installation_id": endpoint.installation_id}
            except Exception:
                report["ok"] = False
                report["preservation"] = {"source_verified": False}
                report["failures"].append("source_preservation_unconfirmed")
        if proof:
            if not proof.stop():
                report["ok"] = False
                report["failures"].append(report["cleanup_error_code"])
            if report["ok"]:
                try:
                    proof.audit()
                    if lifecycle == "oneshot":
                        proof.remove()
                    else:
                        proof.seed_complete = True
                        proof.save()
                        report["profile_retained"] = True
                except Exception:
                    report["ok"] = False
                    report["failures"].append("proof_cleanup_unconfirmed")
        report["checks"]["cleanup"] = "pass" if report["clean_stop"] else "fail"
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        try:
            _windows.reject_reparse(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.write("\n")
        except Exception:
            return 1
    return 0 if report["ok"] else 1


def run(report_path, *, data_dir, runtime_dir, lifecycle="oneshot", run_token=None):
    try:
        with _windows.mutex("postgres-proof:" + str(Path(data_dir).resolve())):
            return _run(report_path, data_dir=data_dir, runtime_dir=runtime_dir,
                        lifecycle=lifecycle, run_token=run_token)
    except Exception:
        # No unsafe fallback file or raw driver/credential exception.
        return 1
