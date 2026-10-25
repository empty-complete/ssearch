"""An isolated per-user PostgreSQL supervisor, independent of the desktop UI.

The owning host must sanitize PG* environment once before starting workers.
The library never changes process environment and refuses unsafe ambient values.
Timeout is an unresolved process state, never evidence that a server exited.
"""
from __future__ import annotations
import asyncio
from dataclasses import asdict, dataclass
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import socket
import subprocess
import tempfile
import threading
import time
from typing import Literal
import uuid
import weakref
from . import _windows
from .credentials import CredentialError, CredentialStore
from .manifest import RuntimeValidationError, verify_runtime
from .storage import StorageBudget, StorageBudgetError


class PostgresRuntimeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RuntimeEndpoint:
    host: str
    port: int
    database: str
    installation_id: str
    postgres_version: str
    vector_version: str
    credential_ref: str


@dataclass(frozen=True)
class RuntimeState:
    state: Literal["stopped", "starting", "ready", "degraded", "blocked"]
    endpoint: RuntimeEndpoint | None
    error_code: str | None


class _HealthCursor:
    def __init__(self, owner, cursor):
        self.owner, self.cursor = owner, cursor

    def fetchone(self):
        return self.owner._run(self.cursor.fetchone())

    def fetchall(self):
        return self.owner._run(self.cursor.fetchall())


class _HealthConnection:
    """Public psycopg 3.3.6 async API inside the existing fixed probe lane.

    The local SelectorEventLoop supports Windows without changing global loop
    policy or starting an executor. hostaddr is fixed, so connect performs no
    DNS lookup. The common deadline bounds connect, queries and fetches.
    """
    def __init__(self, parameters, deadline, *, maintenance=False):
        import psycopg
        self.deadline, self.connection, self._pgconn = deadline, None, None
        self._runner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)
        try:
            parameters = dict(parameters, options='-c default_transaction_read_only=on -c statement_timeout=2000 -c lock_timeout=2000')
            # Own the public libpq handle from connect_start, including before
            # authentication. Cancelling AsyncConnection.connect() in 3.3.6
            # does not reliably close that not-yet-returned handle immediately.
            self._pgconn = self._connect(parameters)
            self.connection = psycopg.AsyncConnection(self._pgconn)
            self._run(self.connection.set_autocommit(maintenance))
            if not maintenance:
                self._run(self.connection.set_read_only(True))
                self._run(self.connection.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ))
        except BaseException:
            self.close()
            raise

    def _connect(self, parameters):
        from psycopg.conninfo import make_conninfo
        from psycopg.pq import PGconn, PollingStatus
        # Public PQconnectStart/PQconnectPoll ignore integer connect_timeout;
        # the selector uses the exact remaining monotonic deadline instead.
        # Fixed hostaddr prevents the only blocking DNS step documented by pq.
        connection = PGconn.connect_start(make_conninfo(**parameters).encode('utf-8'))
        try:
            with selectors.DefaultSelector() as selector:
                while time.monotonic() < self.deadline:
                    status = connection.connect_poll()
                    if status == PollingStatus.OK:
                        connection.nonblocking = 1
                        return connection
                    if status not in (PollingStatus.READING, PollingStatus.WRITING):
                        raise PostgresRuntimeError('health_probe_unavailable')
                    event = selectors.EVENT_READ if status == PollingStatus.READING else selectors.EVENT_WRITE
                    descriptor = connection.socket
                    selector.register(descriptor, event)
                    try:
                        if not selector.select(max(0, self.deadline - time.monotonic())):
                            raise PostgresRuntimeError('health_probe_timeout')
                    finally:
                        selector.unregister(descriptor)
                raise PostgresRuntimeError('health_probe_timeout')
        except BaseException:
            connection.finish()
            raise

    def _run(self, operation):
        if time.monotonic() >= self.deadline:
            operation.close()
            raise PostgresRuntimeError('health_probe_timeout')
        async def bounded():
            task = asyncio.create_task(operation)
            done, _ = await asyncio.wait({task}, timeout=max(0, self.deadline - time.monotonic()))
            if task in done:
                return task.result()
            # Public PGconn.finish() closes locally before cancellation, so
            # the pinned psycopg 3.3.6 cannot add
            # its normal 5+5s active-query cancellation/recovery waits.
            self._finish()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise PostgresRuntimeError('health_probe_timeout')
        return self._runner.run(bounded())

    def _finish(self):
        # Deadline cleanup and context cleanup share one owned handle. Detach
        # before releasing it so a repeated close never inspects a finished pq.
        connection, self._pgconn = self._pgconn, None
        if connection is not None:
            descriptor = connection.socket
            try:
                if descriptor >= 0:
                    # psycopg's pending wait still owns loop registrations.
                    # Remove them before closing the socket: Windows select()
                    # rejects a closed descriptor before cancellation can run
                    # wait_async()'s own (idempotent) removal in its finally.
                    loop = self._runner.get_loop()
                    loop.remove_reader(descriptor)
                    loop.remove_writer(descriptor)
            finally:
                connection.finish()

    def execute(self, query, parameters=None):
        milliseconds = int((self.deadline - time.monotonic()) * 1000)
        if milliseconds <= 0:
            raise PostgresRuntimeError('health_probe_timeout')
        self._run(self.connection.execute("SELECT set_config('statement_timeout',%s,false)", (str(milliseconds),)))
        return _HealthCursor(self, self._run(self.connection.execute(query, parameters)))

    def close(self):
        if self._runner is not None:
            try:
                # Closing a read-only observation needs no commit/rollback
                # round trip and remains possible after its deadline.
                self._finish()
            finally:
                self._runner.close()
                self._runner = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _safe_environment():
    if any(key.upper().startswith("PG") for key in os.environ):
        raise PostgresRuntimeError("unsafe_postgres_environment")


def sanitize_host_environment():
    """Owned executable startup only, on its main thread before any workers."""
    if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
        raise PostgresRuntimeError("host_environment_requires_single_thread")
    for key in list(os.environ):
        if key.upper().startswith("PG"):
            del os.environ[key]


def _storage_policy(profile_dir):
    try:
        settings_path = Path(profile_dir) / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        storage = settings.get("storage", {})
        if not isinstance(storage, dict):
            raise ValueError()
        quota = storage.get("quota_bytes", 5368709120)
        free = storage.get("min_free_bytes", 2147483648)
        if type(quota) is not int or quota <= 0 or type(free) is not int or free < 0:
            raise ValueError()
        return quota, free
    except (OSError, ValueError, AttributeError):
        raise PostgresRuntimeError("storage_policy_invalid") from None


def _run_logged(program, arguments, directory, environment, *, timeout):
    """Own file output: an inherited server handle never holds a PIPE open."""
    descriptor, name = tempfile.mkstemp(prefix=Path(program).stem + "-", suffix=".out", dir=directory)
    with os.fdopen(descriptor, "wb") as output:
        completed = subprocess.run([str(program), *map(str, arguments)], cwd=directory,
                                   env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    return completed.returncode, Path(name)


class PostgresRuntime:
    def __init__(self, runtime_dir: Path, profile_dir: Path, *, timeout_seconds: int = 30,
                 accounting_roots: tuple[Path, ...] | None = None,
                 read_only_files: tuple[Path, ...] | None = None):
        if type(timeout_seconds) is not int or timeout_seconds < 1 or timeout_seconds > 300:
            raise PostgresRuntimeError("invalid_timeout")
        self.runtime_dir = Path(runtime_dir).absolute()
        self.profile_dir = Path(profile_dir).absolute()
        self.timeout_seconds = timeout_seconds
        self.cluster_dir = self.profile_dir / "postgresql/18"
        self.data_dir = self.cluster_dir / "data"
        self._intent_path = self.cluster_dir / "owner.json"
        self._state = RuntimeState("stopped", None, None)
        self._token = uuid.uuid4().hex
        self._record = None
        self._manifest = None
        self._credentials = None
        self._connections = weakref.WeakSet()
        self._thread_lock = threading.RLock()
        self._draining = False
        self._accounting_roots = accounting_roots
        self._read_only_files = read_only_files
        self.storage_budget = None

    def _budget(self):
        """Called under the profile mutex, after strict private subtree setup.

        The canonical descriptor cannot move a ledger or discard live entries.
        Omitted selection means load the protected persisted domain, not empty.
        """
        quota, free = _storage_policy(self.profile_dir)
        path = self.cluster_dir.parent / 'storage-domain.json'
        requested = self._accounting_roots is not None or self._read_only_files is not None
        domain = None
        if path.exists():
            _windows.assert_private(path)
            try:
                domain = json.loads(path.read_text(encoding='utf-8'))
                if (set(domain) != {'version', 'roots', 'read_only_files', 'policy'}
                        or type(domain['version']) is not int or domain['version'] != 1
                        or domain['roots'] != [str(self.profile_dir)]
                        or not isinstance(domain['read_only_files'], list)
                        or any(not isinstance(item, str) or not Path(item).is_absolute()
                               for item in domain['read_only_files'])
                        or domain['policy'] != [quota, free]):
                    raise ValueError()
            except (ValueError, TypeError, OSError):
                self._fail('storage_domain_invalid')
        if requested:
            roots = tuple(_windows.reject_reparse(p).resolve()
                          for p in (self._accounting_roots or (self.profile_dir,)))
            if roots != (self.profile_dir,):
                self._fail('storage_domain_invalid')
            files = tuple(sorted((_windows.reject_reparse(p).resolve()
                                  for p in (self._read_only_files or ())), key=str))
            proposed = {'version': 1, 'roots': [str(p) for p in roots],
                        'read_only_files': [str(p) for p in files], 'policy': [quota, free]}
            if domain is not None and proposed != domain:
                self._fail('storage_domain_conflict')
            domain = proposed
        if domain is None:
            budget = StorageBudget((self.cluster_dir,), quota_bytes=quota, min_free_bytes=free)
        else:
            budget = StorageBudget((self.profile_dir,), quota_bytes=quota, min_free_bytes=free,
                                   ledger_dir=self.cluster_dir.parent,
                                   read_only_files=tuple(Path(p) for p in domain['read_only_files']))
        # Ready reuse must also inspect the original ledger and current domain.
        # No storage mutex is retained while starting or executing SQL.
        with budget.reserve(64 * 1024 if domain is not None else 0, 'runtime_domain'):
            if domain is not None and not path.exists():
                _windows.atomic_json(path, domain)
        self.storage_budget = budget
        return budget

    def _fail(self, code, state="blocked"):
        self._state = RuntimeState(state, None, code)
        raise PostgresRuntimeError(code) from None

    def _read_record(self):
        if not self._intent_path.exists():
            return None
        _windows.assert_private(self._intent_path)
        try:
            record = json.loads(self._intent_path.read_text(encoding="utf-8"))
            if (record["version"] != 1 or record["profile"] != str(self.profile_dir.resolve())
                    or record["data"] != str(self.data_dir.resolve())
                    or str(uuid.UUID(record["installation_id"])) != record["installation_id"]
                    or set(record["credentials"]) != {"application", "maintenance"}
                    or record["credentials"]["application"] == record["credentials"]["maintenance"]):
                raise ValueError()
            return record
        except (OSError, ValueError, KeyError, TypeError):
            self._fail("ownership_record_invalid")

    def _write_record(self):
        if self._intent_path.exists():
            self._assert_owner()
        _windows.atomic_json(self._intent_path, self._record)

    def _assert_owner(self):
        _windows.assert_private(self.profile_dir)
        _windows.assert_private(self.cluster_dir)
        actual = self._read_record()
        if (not actual or not self._record
                or actual["installation_id"] != self._record["installation_id"]
                or actual["credentials"] != self._record["credentials"]):
            self._fail("profile_ownership_changed")

    @contextmanager
    def _start_reservation(self, budget):
        reservation = budget.reserve(128 * 1024 * 1024, "postgres_start")
        try:
            yield reservation
        finally:
            # Releasing also writes the ledger. On ownership loss retain it,
            # exactly as we retain any other uncertain private artifact.
            self._assert_owner()
            reservation.__exit__(None, None, None)

    def _setup(self):
        _safe_environment()
        _windows.reject_reparse(self.runtime_dir)
        _windows.reject_reparse(self.profile_dir)
        runtime, profile = self.runtime_dir.resolve(), self.profile_dir.resolve()
        if profile == runtime or profile.is_relative_to(runtime) or runtime.is_relative_to(profile):
            self._fail("profile_runtime_overlap")
        self._manifest = verify_runtime(runtime)
        self.runtime_dir, self.profile_dir = runtime, profile
        _windows.private_directory(profile)
        _windows.private_directory(self.cluster_dir)
        record = self._read_record()
        if record is None and self.data_dir.exists() and any(self.data_dir.iterdir()):
            self._fail("unknown_pgdata")
        self._record = record
        if record:
            self._credentials = CredentialStore(self.cluster_dir, record["installation_id"])
            for reference in record["credentials"].values():
                self._credentials.resolve(reference)
            leftover = record.get("bootstrap_pwfile")
            if leftover is not None:
                if not isinstance(leftover, str) or not re.fullmatch(r"bootstrap-[A-Za-z0-9_-]+\.pw", leftover):
                    self._fail("bootstrap_password_reference_invalid")
                self._assert_owner()
                path = self.cluster_dir / leftover
                if path.exists():
                    _windows.assert_private(path)
                    path.unlink()
                self._record["bootstrap_pwfile"] = None
                self._write_record()
        else:
            identity = str(uuid.uuid4())
            self._credentials = CredentialStore(self.cluster_dir, identity)
            # Both DPAPI records are flushed and decrypted before intent/initdb.
            references = {role: self._credentials.create(role) for role in ("maintenance", "application")}
            self._record = {"version": 1, "profile": str(profile),
                            "data": str(self.data_dir.resolve()), "installation_id": identity,
                            "credentials": references, "bootstrapped": False,
                            "server": None, "supervisor": None, "owner_token": None,
                            "launch_pending": False, "port": None, "bootstrap_pwfile": None}
            self._write_record()

    def _run(self, executable, arguments, *, timeout=None, expected_process=None):
        verify_runtime(self.runtime_dir)
        self._assert_owner()
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("PG")}
        environment.update(TEMP=str(self.cluster_dir), TMP=str(self.cluster_dir), LC_ALL="C")
        try:
            with (_windows.verified_process(expected_process) if expected_process else nullcontext()):
                if expected_process and self._claimed_process(arguments=False) != expected_process:
                    self._fail("process_changed")
                code, _ = _run_logged(self.runtime_dir / "bin" / executable, arguments,
                                      self.cluster_dir, environment,
                                      timeout=timeout or self.timeout_seconds + 5)
        except subprocess.TimeoutExpired:
            self._fail("process_timeout", "degraded")
        self._assert_owner()
        return code

    def _initialize(self, reservation):
        if self.data_dir.exists() and any(self.data_dir.iterdir()):
            # A complete cluster resumes SQL setup. Any other nonempty directory
            # is retained, including an interrupted initdb; never run initdb again.
            if ((self.data_dir / "PG_VERSION").read_text(encoding="ascii").strip() != "18"
                    if (self.data_dir / "PG_VERSION").is_file() else True):
                self._fail("incomplete_pgdata")
            if not (self.data_dir / "global/pg_control").is_file():
                self._fail("incomplete_pgdata")
            return
        descriptor, name = tempfile.mkstemp(prefix="bootstrap-", suffix=".pw", dir=self.cluster_dir)
        password_file = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                self._record["bootstrap_pwfile"] = password_file.name
                self._write_record()
                output.write(self._credentials.resolve(self._record["credentials"]["maintenance"]) + "\n")
                output.flush()
                os.fsync(output.fileno())
            _windows.assert_private(password_file)
            # Relative ASCII filename in the verified private cwd avoids
            # initdb's native Unicode password-filename conversion.
            result = self._run("initdb.exe", ["-D", self.data_dir, "--encoding=UTF8", "--locale=C",
                                             "--username=atom_bootstrap", "--auth=scram-sha-256",
                                             "--pwfile", password_file.name], timeout=120)
            if result:
                self._fail("initdb_failed")
        finally:
            self._assert_owner()
            _windows.assert_private(password_file)
            password_file.unlink(missing_ok=True)
            self._record["bootstrap_pwfile"] = None
            self._write_record()
        reservation.ensure_remaining(64 * 1024 * 1024)

    def _choose_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def _configure(self, port):
        self._assert_owner()
        _windows.assert_private(self.data_dir)
        # No ambient options, external data_directory, or includes are accepted.
        (self.data_dir / "postgresql.conf").write_text(
            "listen_addresses = '127.0.0.1'\n"
            f"port = {port}\n"
            f"cluster_name = '{self._record['installation_id']}'\n"
            "password_encryption = 'scram-sha-256'\n"
            "unix_socket_directories = ''\n"
            "logging_collector = off\n"
            "log_statement = 'none'\n"
            "log_min_error_statement = 'panic'\n"
            "log_min_duration_statement = -1\n"
            "log_min_duration_sample = -1\n"
            "log_transaction_sample_rate = 0\n"
            "log_duration = off\n"
            "log_parameter_max_length = 0\n"
            "log_parameter_max_length_on_error = 0\n",
            encoding="utf-8")
        (self.data_dir / "pg_hba.conf").write_text(
            "host all all 127.0.0.1/32 scram-sha-256\n", encoding="ascii")
        # ALTER SYSTEM is never used by Atom; reject overrides instead of wiping.
        auto = self.data_dir / "postgresql.auto.conf"
        if auto.exists() and any(line.strip() and not line.lstrip().startswith("#")
                                 for line in auto.read_text(encoding="utf-8").splitlines()):
            self._fail("postgres_overrides_not_supported")

    def _claimed_process(self, *, arguments=True):
        pidfile = self.data_dir / "postmaster.pid"
        saved = self._record.get("server")

        def no_live_claim(code):
            # A missing/exited PID-file claim cannot erase a separately recorded
            # server. Unknown observation raises; PID reuse is compared in full.
            if saved and _windows.process_identity(saved["pid"]) == _windows.ProcessIdentity(**saved):
                self._fail(code)
            return None

        if not pidfile.exists():
            return no_live_claim("live_server_missing_pidfile")
        _windows.assert_private(pidfile)
        try:
            lines = pidfile.read_text(encoding="utf-8").splitlines()
            pid, start, port = int(lines[0]), int(lines[2]), int(lines[3])
            if Path(lines[1]).resolve() != self.data_dir.resolve() or not 1 <= port <= 65535:
                self._fail("foreign_postgres")
            identity = _windows.process_identity(pid)
            if identity is None:
                return no_live_claim("live_server_stale_pidfile")
            expected = os.path.normcase(str(self.runtime_dir / "bin/postgres.exe"))
            created_seconds = identity.created / 10000000 - 11644473600
            if identity.executable != expected or abs(created_seconds - start) > 3:
                self._fail("foreign_postgres")
            if saved and identity != _windows.ProcessIdentity(**saved):
                self._fail("process_identity_changed")
            if arguments:
                argv = _windows.process_arguments(identity, self.cluster_dir, guard=self._assert_owner)
                locations = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-D"]
                if len(locations) != 1 or Path(locations[0]).resolve() != self.data_dir.resolve():
                    self._fail("foreign_postgres")
            if self._record["port"] != port:
                self._fail("port_identity_changed")
            return identity
        except (ValueError, IndexError, OSError):
            self._fail("process_identity_unavailable")

    def _connection_parameters(self, role):
        _safe_environment()
        return dict(
            host="127.0.0.1", hostaddr="127.0.0.1", port=self._record["port"], dbname="postgres",
            user="atom_app" if role == "application" else "atom_bootstrap",
            password=self._credentials.resolve(self._record["credentials"][role]),
            connect_timeout=min(5, self.timeout_seconds), sslmode="disable", gssencmode="disable",
            passfile=str(self.cluster_dir / "unused-passfile"), options='', application_name="AtomSearch")

    def _connect(self, role):
        import psycopg
        return psycopg.connect(**self._connection_parameters(role), autocommit=True)

    def _verify_sql(self, connection, *, require_marker=True):
        actual = connection.execute("SHOW data_directory").fetchone()[0]
        if Path(actual).resolve() != self.data_dir.resolve():
            self._fail("sql_data_directory_mismatch")
        if connection.execute("SHOW server_version").fetchone()[0] != self._manifest.postgres_version:
            self._fail("sql_version_mismatch")
        if connection.execute("SHOW listen_addresses").fetchone()[0] != "127.0.0.1":
            self._fail("sql_listen_mismatch")
        if connection.execute("SELECT auth_method FROM pg_hba_file_rules").fetchall() != [("scram-sha-256",)]:
            self._fail("sql_auth_mismatch")
        if connection.execute("SHOW cluster_name").fetchone()[0] != self._record["installation_id"]:
            self._fail("sql_installation_mismatch")
        if require_marker:
            exists = connection.execute("SELECT to_regclass('atom_runtime.identity')").fetchone()[0]
            if not exists or connection.execute(
                    "SELECT installation_id FROM atom_runtime.identity").fetchall() != [
                        (self._record["installation_id"],)]:
                self._fail("sql_installation_mismatch")
            if connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone() != (
                    self._manifest.vector_version,):
                self._fail("sql_extension_mismatch")

    def _bootstrap_sql(self, connection):
        from psycopg import sql
        self._verify_sql(connection, require_marker=False)
        if connection.execute("SELECT to_regclass('atom_runtime.identity')").fetchone()[0]:
            if connection.execute("SELECT installation_id FROM atom_runtime.identity").fetchall() != [
                    (self._record["installation_id"],)]:
                self._fail("sql_installation_mismatch")
        else:
            with connection.transaction():
                connection.execute("CREATE SCHEMA atom_runtime AUTHORIZATION atom_bootstrap")
                connection.execute("CREATE TABLE atom_runtime.identity (installation_id text PRIMARY KEY)")
                connection.execute("INSERT INTO atom_runtime.identity VALUES (%s)",
                                   (self._record["installation_id"],))
                connection.execute("REVOKE ALL ON SCHEMA atom_runtime FROM PUBLIC")
        # Verifier computed by libpq in client memory. Neither cleartext nor
        # verifier is serialized to diagnostics. Suppress all statement channels
        # in this session before any password-bearing DDL, including errors.
        for statement in (
                "SET log_statement = 'none'", "SET log_min_error_statement = 'panic'",
                "SET log_min_duration_statement = -1", "SET log_min_duration_sample = -1",
                "SET log_transaction_sample_rate = 0", "SET log_duration = off",
                "SET log_parameter_max_length = 0", "SET log_parameter_max_length_on_error = 0"):
            connection.execute(statement)
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname='atom_app'").fetchone():
            password = self._credentials.resolve(self._record["credentials"]["application"])
            verifier = connection.pgconn.encrypt_password(password.encode("ascii"), b"atom_app",
                                                         b"scram-sha-256").decode("ascii")
            connection.execute(sql.SQL(
                "CREATE ROLE atom_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Literal(verifier)))
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self._verify_application_role(connection)

    def _verify_application_role(self, connection):
        flags = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname='atom_app'").fetchone()
        memberships = connection.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles "
            "WHERE rolname='atom_app')").fetchone()
        if flags != (False,) * 5 or memberships != (0,):
            self._fail("application_role_unsafe")

    def _ready(self, identity):
        with self._connect("maintenance") as connection:
            self._verify_sql(connection)
            self._verify_application_role(connection)
        if _windows.process_identity(identity.pid) != identity:
            self._fail("process_changed", "degraded")
        with self._connect("application") as connection:
            if connection.execute("SELECT current_user").fetchone() != ("atom_app",):
                self._fail("application_role_mismatch")
        endpoint = RuntimeEndpoint("127.0.0.1", self._record["port"], "postgres",
                                   self._record["installation_id"], self._manifest.postgres_version,
                                   self._manifest.vector_version, self._record["credentials"]["application"])
        self._state = RuntimeState("ready", endpoint, None)
        return endpoint

    def _resume_owned_start(self, identity, budget):
        """Resume durable launch intent only after proving creator and SQL identity."""
        supervisor = self._record.get("supervisor")
        if not supervisor:
            self._fail("bootstrap_owner_unknown")
        previous = _windows.ProcessIdentity(**supervisor)
        observed = _windows.process_identity(previous.pid)
        current = _windows.process_identity(os.getpid())
        if current is None:
            self._fail("process_identity_unavailable")
        if observed == previous and not (
                previous == current and self._record.get("owner_token") == self._token):
            self._fail("bootstrap_owner_active")
        # A reused PID is not the recorded creator. Never signal that process;
        # only the separately verified PostgreSQL process can be claimed.
        with self._start_reservation(budget):
            with _windows.verified_process(identity):
                with self._connect("maintenance") as connection:
                    self._verify_sql(connection, require_marker=self._record["bootstrapped"])
                    if self._claimed_process() != identity:
                        self._fail("process_changed")
                    self._record.update(server=asdict(identity), supervisor=asdict(current),
                                        owner_token=self._token, launch_pending=False)
                    self._write_record()
                    if not self._record["bootstrapped"]:
                        self._bootstrap_sql(connection)
                        self._record["bootstrapped"] = True
                        self._write_record()
                return self._ready(identity)

    def start(self) -> RuntimeEndpoint:
        with self._thread_lock:
            self._state = RuntimeState("starting", None, None)
            try:
                _safe_environment()
                with _windows.mutex("postgres:" + str(self.profile_dir.resolve()),
                                    timeout_seconds=self.timeout_seconds):
                    self._setup()
                    # Version root accounts data/WAL, tool TEMP, logs and secrets
                    # once. Only parent settings/ledger and immutable installation
                    # assets are outside this owned runtime allocation.
                    budget = self._budget()
                    identity = self._claimed_process()
                    if identity:
                        unrecorded = self._record.get("server") is None
                        if unrecorded and not self._record.get("launch_pending"):
                            self._fail("unrecorded_live_server")
                        if unrecorded or not self._record["bootstrapped"]:
                            return self._resume_owned_start(identity, budget)
                        endpoint = self._ready(identity)
                        supervisor = self._record.get("supervisor")
                        if (supervisor and _windows.process_identity(supervisor["pid"])
                                != _windows.ProcessIdentity(**supervisor)):
                            self._record.update(supervisor=asdict(_windows.process_identity(os.getpid())),
                                                owner_token=self._token, launch_pending=False)
                            self._write_record()
                        return endpoint
                    if self._record.get("launch_pending"):
                        self._fail("startup_unresolved", "degraded")
                    self._record.update(server=None, supervisor=asdict(_windows.process_identity(os.getpid())),
                                        owner_token=self._token)
                    self._write_record()
                    with self._start_reservation(budget) as reservation:
                        self._initialize(reservation)
                        for attempt in range(3):
                            port = self._choose_port()
                            self._configure(port)
                            self._record.update(port=port, launch_pending=True)
                            self._write_record()
                            log = self.cluster_dir / ("server-" + uuid.uuid4().hex + ".log")
                            result = self._run("pg_ctl.exe", ["-D", self.data_dir, "-l", log,
                                                              "-w", "-t", str(self.timeout_seconds), "start"])
                            identity = self._claimed_process()
                            if identity:
                                self._record.update(server=asdict(identity), launch_pending=False)
                                self._write_record()
                                if result:
                                    self._fail("startup_not_ready", "degraded")
                                break
                            # Only a completed launcher + explicit bind failure
                            # + absence of PID permits bounded port reselection.
                            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
                            if result and "could not bind" in text and "could not create any TCP/IP sockets" in text:
                                self._record["launch_pending"] = False
                                self._write_record()
                                if attempt < 2:
                                    continue
                                self._fail("port_conflict")
                            self._fail("startup_unresolved", "degraded")
                        with self._connect("maintenance") as connection:
                            self._bootstrap_sql(connection)
                        self._record["bootstrapped"] = True
                        self._write_record()
                        return self._ready(identity)
            except PostgresRuntimeError as error:
                if self._state.state == "starting":
                    self._state = RuntimeState("blocked", None, error.code)
                raise
            except (CredentialError, StorageBudgetError, RuntimeValidationError, _windows.WindowsError) as error:
                self._fail("runtime_validation_failed" if isinstance(error, RuntimeValidationError)
                           else str(error))
            except Exception:
                self._fail("runtime_start_failed", "degraded")

    def status(self, *, deadline=None) -> RuntimeState:
        acquired = (self._thread_lock.acquire() if deadline is None else
                    self._thread_lock.acquire(timeout=max(0, deadline - time.monotonic())))
        if not acquired:
            raise PostgresRuntimeError('health_probe_timeout')
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise PostgresRuntimeError('health_probe_timeout')
            if self._state.state == "ready" and self._record:
                saved = self._record.get("server")
                try:
                    if not saved or _windows.process_identity(saved["pid"]) != _windows.ProcessIdentity(**saved):
                        self._state = RuntimeState("degraded", None, "server_exited")
                except _windows.WindowsError:
                    self._state = RuntimeState("blocked", None, "process_identity_unavailable")
            if deadline is not None and time.monotonic() >= deadline:
                raise PostgresRuntimeError('health_probe_timeout')
            return self._state
        finally:
            self._thread_lock.release()

    def open_connection(self, *, role: Literal["application", "maintenance"] = "application"):
        with self._thread_lock:
            if role not in ("application", "maintenance"):
                raise PostgresRuntimeError("invalid_role")
            _safe_environment()
            if self._draining or self.status().state != "ready":
                raise PostgresRuntimeError("not_ready")
            try:
                with self._connect("maintenance") as maintenance:
                    self._verify_sql(maintenance)
                connection = self._connect(role)
                connection.autocommit = False
                self._connections.add(connection)
                return connection
            except PostgresRuntimeError:
                raise
            except Exception:
                self._fail("connection_failed", "degraded")

    def open_health_connection(self, *, deadline):
        """Bounded read-only observation; same identity checks as other clients.

        Health timeout is not evidence of corruption and does not mutate the
        runtime's state. The connection remains owned/drained by this runtime.
        """
        if not self._thread_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise PostgresRuntimeError('health_probe_timeout')
        try:
            if self._draining or self.status(deadline=deadline).state != 'ready':
                raise PostgresRuntimeError('not_ready')
            with _HealthConnection(self._connection_parameters('maintenance'), deadline, maintenance=True) as maintenance:
                self._verify_sql(maintenance)
            connection = _HealthConnection(self._connection_parameters('application'), deadline)
            self._connections.add(connection.connection)
            return connection
        finally:
            self._thread_lock.release()

    def stop(self) -> None:
        with self._thread_lock:
            if not self._record or self._record.get("owner_token") != self._token:
                self._state = RuntimeState("stopped", None, None)
                return
            self._draining = True
            try:
                deadline = time.monotonic() + self.timeout_seconds
                while any(not connection.closed for connection in self._connections):
                    if time.monotonic() >= deadline:
                        self._fail("operations_not_drained", "degraded")
                    time.sleep(0.05)
                with _windows.mutex("postgres:" + str(self.profile_dir.resolve()),
                                    timeout_seconds=self.timeout_seconds):
                    self._record = self._read_record()
                    if self._record.get("owner_token") != self._token:
                        self._fail("supervisor_changed")
                    identity = self._claimed_process()
                    if identity is None:
                        if self._record.get("launch_pending"):
                            self._fail("startup_unresolved", "degraded")
                        self._record.update(server=None, owner_token=None, supervisor=None)
                        self._write_record()
                        self._state = RuntimeState("stopped", None, None)
                        return
                    with self._connect("maintenance") as connection:
                        self._verify_sql(connection, require_marker=self._record["bootstrapped"])
                    if self._claimed_process() != identity:
                        self._fail("process_changed")
                    # PID metadata can disappear before kernel exit is signalled.
                    # Retain this verified object through the tool and wait only
                    # within the existing stop deadline for positive exit proof.
                    with _windows.verified_process(identity) as process:
                        result = self._run("pg_ctl.exe", ["-D", self.data_dir, "-w", "-t",
                                                         str(self.timeout_seconds), "-m", "fast", "stop"],
                                           expected_process=identity)
                        if result or not _windows.wait_process_exit(
                                process, timeout_seconds=max(0, deadline - time.monotonic())):
                            self._fail("shutdown_unresolved", "degraded")
                    self._record.update(server=None, owner_token=None, supervisor=None, launch_pending=False)
                    self._write_record()
                    self._state = RuntimeState("stopped", None, None)
            except PostgresRuntimeError:
                raise
            except Exception:
                self._fail("shutdown_identity_failed")
            finally:
                self._draining = False
