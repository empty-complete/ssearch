"""Runtime refusal paths must preserve the caller's data and report typed state."""
import dataclasses
import json
import os
import traceback
from types import SimpleNamespace
from pathlib import Path
import pytest
from atom_search.runtime.credentials import CredentialStore, CredentialError
from atom_search.runtime.postgres import PostgresRuntime, PostgresRuntimeError, _storage_policy
from atom_search.runtime import _windows

RUNTIME = Path(__file__).resolve().parents[1] / "resources/postgresql"


@pytest.fixture(scope="module")
def module_runtime_verifier():
    from atom_search.runtime.manifest import verify_runtime

    manifest = None

    def verified(directory):
        nonlocal manifest
        assert Path(directory).resolve() == RUNTIME.resolve(), "Unexpected runtime directory"
        if manifest is None:
            manifest = verify_runtime(directory)
        return manifest

    return verified


@pytest.fixture(autouse=True)
def reuse_verified_runtime(monkeypatch, module_runtime_verifier):
    # This module changes only profiles: verify the immutable payload once, lazily.
    # Integrity mutation tests and real runtime integration keep their own verifier.
    import atom_search.runtime.postgres as module
    monkeypatch.setattr(module, "verify_runtime", module_runtime_verifier)


def test_existing_pgdata_is_never_reinitialized(tmp_path):
    profile = tmp_path / "private"
    _windows.private_directory(profile)
    data = profile / "postgresql/18/data"
    data.mkdir(parents=True)
    marker = data / "user-data"
    marker.write_text("valuable")
    runtime = PostgresRuntime(RUNTIME, profile)
    with pytest.raises(PostgresRuntimeError, match="unknown_pgdata"):
        runtime.start()
    assert marker.read_text() == "valuable"
    assert runtime.status().state == "blocked"
    assert runtime.status().endpoint is None


def test_credentials_are_dpapi_bound_and_references_cannot_escape(tmp_path):
    store = CredentialStore(tmp_path / "private", "profile-a")
    reference = store.create("application")
    password = store.resolve(reference)
    assert len(password) >= 40
    assert password.encode() not in b"".join(p.read_bytes() for p in store.directory.glob("*"))
    assert CredentialStore(store.directory.parent, "profile-a").resolve(reference) == password
    with pytest.raises(CredentialError):
        CredentialStore(store.directory.parent, "profile-b").resolve(reference)
    for invalid in ("../outside", "/absolute", "c:\\secret", reference + ".bak"):
        with pytest.raises(CredentialError):
            store.resolve(invalid)
        with pytest.raises(CredentialError):
            store.remove(invalid)
    store.remove(reference)
    with pytest.raises(CredentialError):
        store.resolve(reference)


def test_corrupt_credentials_fail_closed(tmp_path):
    store = CredentialStore(tmp_path / "private", "profile-a")
    reference = store.create("maintenance")
    next(store.directory.glob("*.dpapi")).write_bytes(b"corrupt")
    with pytest.raises(CredentialError):
        store.resolve(reference)


def test_storage_policy_preserves_owner_values_and_rejects_invalid(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"other": 1, "storage": {
        "quota_bytes": 6000000000, "min_free_bytes": 1000000000}}))
    before = settings.read_bytes()
    assert _storage_policy(tmp_path) == (6000000000, 1000000000)
    assert settings.read_bytes() == before
    settings.write_text('{"storage":{"quota_bytes":true}}')
    with pytest.raises(PostgresRuntimeError, match="storage_policy_invalid"):
        _storage_policy(tmp_path)


def test_no_endpoint_before_start_and_invalid_role_is_refused(tmp_path):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    assert dataclasses.asdict(runtime.status()) == {
        "state": "stopped", "endpoint": None, "error_code": None}
    with pytest.raises(PostgresRuntimeError, match="not_ready"):
        runtime.open_connection()
    with pytest.raises(PostgresRuntimeError, match="invalid_role"):
        runtime.open_connection(role="superuser")


def test_poisoned_environment_refuses_library_without_profile_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("PGSERVICE", "secret-service-sentinel")
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    with pytest.raises(PostgresRuntimeError, match="unsafe_postgres_environment") as caught:
        runtime.start()
    assert "secret-service-sentinel" not in str(caught.value)
    assert not runtime.profile_dir.exists()
    assert os.environ["PGSERVICE"] == "secret-service-sentinel"


def test_start_timeout_checks_owned_process_before_retry(tmp_path, monkeypatch):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    calls = []
    def tool(program, arguments, **kwargs):
        calls.append(program)
        if program == "initdb.exe":
            runtime.data_dir.mkdir()
            (runtime.data_dir / "PG_VERSION").write_text("18")
            (runtime.data_dir / "global").mkdir()
            (runtime.data_dir / "global/pg_control").write_bytes(b"fixture")
            return 0
        runtime._fail("process_timeout", "degraded")
    monkeypatch.setattr(runtime, "_run", tool)
    with pytest.raises(PostgresRuntimeError, match="process_timeout"):
        runtime.start()
    assert runtime.status().state == "degraded"
    with pytest.raises(PostgresRuntimeError, match="startup_unresolved"):
        runtime.start()
    assert calls == ["initdb.exe", "pg_ctl.exe"]
    assert (runtime.data_dir / "global/pg_control").read_bytes() == b"fixture"


def test_start_does_not_adopt_foreign_postgres_or_kill_port_owner(tmp_path, monkeypatch):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    _windows.private_directory(runtime.data_dir)
    identity = _windows.process_identity(os.getpid())
    # A PID claim pointing to this Python process must never be stopped.
    (runtime.data_dir / "postmaster.pid").write_text(
        f"{os.getpid()}\n{runtime.data_dir}\n{int(identity.created / 10000000 - 11644473600)}\n54321\n")
    runtime._record["port"] = 54321
    runtime._write_record()
    calls = []
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(PostgresRuntimeError, match="foreign_postgres"):
        runtime.start()
    assert calls == []
    assert runtime.status().endpoint is None
    assert _windows.process_identity(os.getpid()) == identity


def test_owned_incomplete_pgdata_is_never_reinitialized(tmp_path):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    # Unknown nonempty incomplete files are not repaired or overwritten.
    runtime.data_dir.mkdir()
    marker = runtime.data_dir / "valuable"
    marker.write_text("untouched")
    with pytest.raises(PostgresRuntimeError, match="incomplete_pgdata"):
        runtime.start()
    assert marker.read_text() == "untouched"


def test_credentials_not_in_endpoint_json_argv_or_logs(tmp_path, monkeypatch):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    secret = "never-serialize-this-secret-sentinel"
    commands = []
    def fail(program, arguments, **kwargs):
        commands.append([program, *map(str, arguments)])
        raise RuntimeError(secret)
    monkeypatch.setattr(runtime, "_run", fail)
    with pytest.raises(PostgresRuntimeError) as caught:
        runtime.start()
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert secret not in json.dumps(commands)
    assert secret not in json.dumps(dataclasses.asdict(runtime.status()))
    assert all(secret.encode() not in path.read_bytes()
               for path in runtime.cluster_dir.rglob("*") if path.is_file())


def test_bootstrap_budget_accounts_for_owned_logs_and_temp(tmp_path, monkeypatch):
    import atom_search.runtime.postgres as module
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    (runtime.cluster_dir / "retained-temp").write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setattr(module, "_storage_policy", lambda profile: (129 * 1024 * 1024, 0))
    def forbidden(*args, **kwargs):
        raise AssertionError("quota must refuse before initdb")
    monkeypatch.setattr(runtime, "_run", forbidden)
    with pytest.raises(PostgresRuntimeError, match="quota_exceeded"):
        runtime.start()
    assert not runtime.data_dir.exists()


def test_interrupted_bootstrap_password_cleanup_uses_durable_reference(tmp_path):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    owned = runtime.cluster_dir / "bootstrap-interrupted.pw"
    unclaimed = runtime.cluster_dir / "bootstrap-unclaimed.pw"
    owned.write_text("owned")
    unclaimed.write_text("unclaimed")
    runtime._record["bootstrap_pwfile"] = owned.name
    runtime._write_record()
    runtime._setup()
    assert not owned.exists()
    assert unclaimed.read_text() == "unclaimed"
    assert runtime._record["bootstrap_pwfile"] is None


def test_stop_tool_refuses_changed_process_identity_before_launch(tmp_path, monkeypatch):
    import atom_search.runtime.postgres as module
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    identity = _windows.process_identity(os.getpid())
    stale = dataclasses.replace(identity, created=identity.created - 1)
    invoked = []
    monkeypatch.setattr(module, "_run_logged", lambda *args, **kwargs: invoked.append(args))
    with pytest.raises(RuntimeError, match="process_changed"):
        runtime._run("pg_ctl.exe", ["stop"], expected_process=stale)
    assert not invoked


def test_prebootstrap_sql_requires_installation_uuid_not_just_data_directory(tmp_path):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._manifest = SimpleNamespace(postgres_version="18.6")
    runtime._record = {"installation_id": "975c6e1d-3327-49e2-86d9-0467ad9276bc"}
    rows = {
        "SHOW data_directory": (str(runtime.data_dir),),
        "SHOW server_version": ("18.6",),
        "SHOW listen_addresses": ("127.0.0.1",),
        "SHOW cluster_name": ("different-installation",),
    }
    class Connection:
        def execute(self, query):
            return SimpleNamespace(fetchone=lambda: rows[query],
                                   fetchall=lambda: [("scram-sha-256",)])
    with pytest.raises(PostgresRuntimeError, match="sql_installation_mismatch"):
        runtime._verify_sql(Connection(), require_marker=False)


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_partial_bootstrap_does_not_claim_active_or_uncertain_supervisor(tmp_path, monkeypatch, uncertain, pending):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    identity = _windows.process_identity(os.getpid())
    runtime._record.update(server=None if pending else dataclasses.asdict(identity), launch_pending=pending,
                           supervisor=dataclasses.asdict(identity), owner_token="other-creator")
    runtime._write_record()
    before = runtime._intent_path.read_bytes()
    monkeypatch.setattr(runtime, "_claimed_process", lambda: identity)
    def forbidden(*args, **kwargs):
        raise AssertionError("no SQL or bootstrap before creator exit is proven")
    monkeypatch.setattr(runtime, "_connect", forbidden)
    if uncertain:
        def unknown(pid):
            raise _windows.WindowsError("process_identity_unavailable")
        monkeypatch.setattr(_windows, "process_identity", unknown)
    code = "process_identity_unavailable" if uncertain else "bootstrap_owner_active"
    with pytest.raises(PostgresRuntimeError, match=code):
        runtime.start()
    assert runtime._intent_path.read_bytes() == before
    assert runtime.status().endpoint is None


def test_unrecorded_live_server_without_pending_intent_is_not_claimed(tmp_path, monkeypatch):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    before = runtime._intent_path.read_bytes()
    monkeypatch.setattr(runtime, "_claimed_process", lambda: _windows.process_identity(os.getpid()))
    def forbidden(*args, **kwargs):
        raise AssertionError("unrecorded server has no durable launch intent")
    monkeypatch.setattr(runtime, "_connect", forbidden)
    with pytest.raises(PostgresRuntimeError, match="unrecorded_live_server"):
        runtime.start()
    assert runtime._intent_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["start", "stop"])
@pytest.mark.parametrize("saved_state", ["live", "unavailable"])
def test_stale_pidfile_cannot_hide_live_or_uncertain_saved_server(tmp_path, monkeypatch, operation, saved_state):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    runtime._setup()
    current = _windows.process_identity(os.getpid())
    saved = dataclasses.replace(current, pid=12345,
                                executable=os.path.normcase(str(RUNTIME / "bin/postgres.exe")))
    _windows.private_directory(runtime.data_dir / "global")
    (runtime.data_dir / "PG_VERSION").write_text("18")
    (runtime.data_dir / "global/pg_control").write_bytes(b"existing database")
    configuration = runtime.data_dir / "postgresql.conf"
    configuration.write_text("preserve the existing server configuration")
    pidfile = runtime.data_dir / "postmaster.pid"
    pidfile.write_text(f"42424\n{runtime.data_dir}\n1\n54321\n")
    runtime._record.update(server=dataclasses.asdict(saved), supervisor=dataclasses.asdict(current),
                           owner_token=runtime._token, port=54321, bootstrapped=True, launch_pending=False)
    runtime._write_record()
    files = [runtime._intent_path, configuration, pidfile, runtime.data_dir / "PG_VERSION",
             runtime.data_dir / "global/pg_control"]
    before = {path: path.read_bytes() for path in files}
    checked, invoked = [], []
    def observe(pid):
        checked.append(pid)
        if pid == 12345:
            if saved_state == "unavailable":
                raise _windows.WindowsError("process_identity_unavailable")
            return saved
        if pid == 42424:
            return None
        assert pid == current.pid
        return current
    def forbidden(*args, **kwargs):
        invoked.append(args)
        runtime._fail("unexpected_native_launch")
    monkeypatch.setattr(_windows, "process_identity", observe)
    # Native integrity/profile setup already ran above; this regression targets
    # the real lifecycle decision and its subsequent record/config/tool writes.
    monkeypatch.setattr(runtime, "_setup", lambda: None)
    monkeypatch.setattr(runtime, "_run", forbidden)
    with pytest.raises(PostgresRuntimeError):
        getattr(runtime, operation)()
    assert not invoked
    assert {path: path.read_bytes() for path in files} == before
    assert 12345 in checked and 42424 in checked
    assert runtime.status().state in ("blocked", "degraded")
    assert runtime.status().endpoint is None


@pytest.mark.parametrize("pidfile_present", [False, True])
@pytest.mark.parametrize("saved_state", ["exited", "pid_reused"])
def test_no_live_server_requires_positive_saved_identity_reconciliation(tmp_path, monkeypatch,
                                                                      pidfile_present, saved_state):
    runtime = PostgresRuntime(RUNTIME, tmp_path / "private")
    _windows.private_directory(runtime.data_dir)
    saved = _windows.ProcessIdentity(12345, str(RUNTIME / "bin/postgres.exe"), 100)
    runtime._record = {"server": dataclasses.asdict(saved), "port": 54321}
    if pidfile_present:
        (runtime.data_dir / "postmaster.pid").write_text(f"42424\n{runtime.data_dir}\n1\n54321\n")
    checked = []
    def observe(pid):
        checked.append(pid)
        if pid == 42424:
            return None
        assert pid == saved.pid
        return None if saved_state == "exited" else dataclasses.replace(saved, created=101)
    monkeypatch.setattr(_windows, "process_identity", observe)
    assert runtime._claimed_process() is None
    assert saved.pid in checked
