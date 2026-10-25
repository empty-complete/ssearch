"""Real Windows binaries, private profiles only. No environment DSN is accepted."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import pytest
from atom_search.runtime.postgres import PostgresRuntime, PostgresRuntimeError

RUNTIME = Path(__file__).resolve().parents[2] / "resources/postgresql"


@pytest.fixture
def runtime(tmp_path):
    instance = PostgresRuntime(RUNTIME, tmp_path / "private")
    try:
        yield instance
    finally:
        instance.stop()


def test_real_cluster_vector_query_survives_restart(runtime):
    first = runtime.start()
    with runtime.open_connection(role="maintenance") as connection:
        connection.execute("CREATE TABLE public.proof (id integer PRIMARY KEY, embedding vector(3))")
        connection.execute("INSERT INTO public.proof VALUES (1, '[1,0,0]'), (2, '[0,1,0]')")
        connection.execute("GRANT SELECT ON public.proof TO atom_app")
    with runtime.open_connection() as connection:
        assert connection.execute(
            "SELECT id, embedding <=> '[1,0,0]'::vector FROM public.proof ORDER BY id"
        ).fetchall() == [(1, 0.0), (2, 1.0)]
    runtime.stop()
    assert runtime.status().state == "stopped"
    second = runtime.start()
    assert first.installation_id == second.installation_id
    with runtime.open_connection() as connection:
        assert connection.execute("SELECT count(*) FROM public.proof").fetchone() == (2,)


def test_stop_confirms_owned_process_exit_when_pid_metadata_disappears(runtime, monkeypatch):
    from atom_search.runtime import _windows
    runtime.start()
    record = json.loads((runtime.cluster_dir / 'owner.json').read_text(encoding='utf-8'))
    server = _windows.ProcessIdentity(**record['server'])
    assert _windows.process_identity(server.pid) == server
    observe = _windows.process_identity
    def unavailable_after_exit(pid):
        observed = observe(pid)
        if pid == server.pid and observed is None:
            # A real owned-child observer captured process_image_unavailable
            # during exit. A successful stop must use its retained identity,
            # rather than depend on a fresh PID metadata query after exit.
            raise _windows.WindowsError('process_image_unavailable')
        return observed
    with monkeypatch.context() as patch:
        patch.setattr(_windows, 'process_identity', unavailable_after_exit)
        runtime.stop()
    assert runtime.status().state == 'stopped'
    record = json.loads((runtime.cluster_dir / 'owner.json').read_text(encoding='utf-8'))
    assert record['server'] is None and record['owner_token'] is None
    assert record['supervisor'] is None and record['launch_pending'] is False


def test_real_runtime_application_role_cannot_create_role_or_database(runtime):
    import psycopg
    runtime.start()
    with runtime.open_connection() as connection:
        connection.autocommit = True
        assert connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname=current_user").fetchone() == (False,) * 5
        for statement in ("CREATE ROLE forbidden", "CREATE DATABASE forbidden",
                          "SET ROLE atom_bootstrap"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
                connection.execute(statement)
            assert caught.value.sqlstate == "42501"
        assert connection.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles "
            "WHERE rolname=current_user)").fetchone() == (0,)
    with runtime.open_connection(role="maintenance") as maintenance:
        maintenance.execute("ALTER ROLE atom_app CREATEDB")
    attached = PostgresRuntime(RUNTIME, runtime.profile_dir)
    try:
        with pytest.raises(PostgresRuntimeError, match="application_role_unsafe"):
            attached.start()
    finally:
        # Test owns this cluster; restore only the privilege deliberately changed.
        with runtime._connect("maintenance") as maintenance:
            maintenance.execute("ALTER ROLE atom_app NOCREATEDB")


def test_second_launch_reuses_only_matching_profile(runtime, tmp_path):
    endpoint = runtime.start()
    attached = PostgresRuntime(RUNTIME, runtime.profile_dir)
    assert attached.start() == endpoint
    attached.stop()
    with runtime.open_connection() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    other = PostgresRuntime(RUNTIME, tmp_path / "other")
    try:
        assert other.start().installation_id != endpoint.installation_id
    finally:
        other.stop()


def test_real_runtime_port_conflict_preserves_foreign_listener(runtime, monkeypatch):
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        occupied = foreign.getsockname()[1]
        real = runtime._choose_port
        ports = iter([occupied])
        monkeypatch.setattr(runtime, "_choose_port", lambda: next(ports, None) or real())
        endpoint = runtime.start()
        assert endpoint.port != occupied
        with socket.create_connection(("127.0.0.1", occupied), timeout=1):
            pass


@pytest.mark.parametrize("stop_first", [False, True])
def test_partial_initialization_can_resume_without_destroying_existing_data(runtime, monkeypatch, stop_first):
    original = runtime._bootstrap_sql
    observed = []
    def interrupt(connection):
        observed.append((connection.execute("SHOW cluster_name").fetchone()[0],
                         connection.execute("SELECT to_regclass('atom_runtime.identity')").fetchone()[0]))
        raise RuntimeError("simulated interruption before bootstrap")
    monkeypatch.setattr(runtime, "_bootstrap_sql", interrupt)
    with pytest.raises(RuntimeError):
        runtime.start()
    assert observed == [(runtime._record["installation_id"], None)]
    identity = runtime._claimed_process()
    # Exercise both same-creator continuation and prebootstrap own-stop.
    if stop_first:
        runtime.stop()
    version = (runtime.data_dir / "PG_VERSION").read_bytes()
    monkeypatch.setattr(runtime, "_bootstrap_sql", original)
    runtime.start()
    if not stop_first:
        assert runtime._claimed_process() == identity
    assert (runtime.data_dir / "PG_VERSION").read_bytes() == version
    with runtime.open_connection() as connection:
        assert connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone() == ("0.8.7",)


def test_runtime_proof_does_not_read_user_sources(tmp_path):
    root = Path(__file__).resolve().parents[2]
    report = tmp_path / "proof.json"
    profile = tmp_path / "private-host-profile"
    environment = dict(os.environ, PYTHONPATH=str(root / "src"),
                       PGSERVICE="not-an-atom-service", PGHOST="192.0.2.123", PGPORT="1",
                       PGPASSWORD="ambient-secret-sentinel", PGOPTIONS="-c search_path=foreign",
                       PGPASSFILE=str(tmp_path / "foreign-passfile"),
                       PGSERVICEFILE=str(tmp_path / "foreign-service"))
    forbidden = tmp_path / "unrelated-user-files"
    forbidden.mkdir()
    canary = forbidden / "source.docx"
    canary.write_text("private synthetic source must not be read", encoding="utf-8")
    (forbidden / "settings.json").write_text(
        json.dumps({"source": str(canary), "sources": [str(canary)]}), encoding="utf-8")
    environment.update(LOCALAPPDATA=str(forbidden), APPDATA=str(forbidden))
    script = r'''
import os, runpy, sys
from pathlib import Path
forbidden = Path(sys.argv[1]).resolve()
def guard(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path == forbidden or path.is_relative_to(forbidden):
            raise AssertionError("proof read an unrelated source")
    if event == "import" and args[0].split(".")[0] in ("atom_search_core", "onnxruntime", "tokenizers"):
        raise AssertionError("proof imported search/model")
sys.addaudithook(guard)
from atom_search.runtime.postgres import PostgresRuntime
original_fail = PostgresRuntime._fail
def observe_failure(instance, code, *args, **kwargs):
    error = sys.exception()
    if code == "runtime_start_failed" and error is not None:
        import json, traceback
        print(json.dumps({"type": type(error).__name__,
                          "locations": [(Path(frame.filename).name, frame.lineno)
                                        for frame in traceback.extract_tb(error.__traceback__)]}),
              flush=True)
    return original_fail(instance, code, *args, **kwargs)
PostgresRuntime._fail = observe_failure
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    with (tmp_path / "host-output.log").open("wb") as output:
        completed = subprocess.run([sys.executable, "-c", script, str(forbidden),
                                    str(root / "packaging/launcher.py"),
                                    "--postgres-self-test", str(report), "--data-dir", str(profile),
                                    "--postgres-qa-lifecycle", "seed", "--postgres-qa-run", "a" * 32],
                                   env=environment, stdout=output, stderr=subprocess.STDOUT, timeout=900)
    assert completed.returncode == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["execution_mode"] == "source"
    assert result["runtime_api"] == "PostgresRuntime"
    for key in ("clean_stop", "profile_retained", "restart_verified", "dpapi_verified",
                "private_acl_verified", "wrong_password_rejected", "application_privileges_verified"):
        assert result[key] is True
    assert result["cosine_rows"] == [[1, 0.0], [2, 1.0], [3, 2.0]]
    assert result["profile_removed"] is False
    assert profile.exists()
    assert "ambient-secret-sentinel" not in report.read_text()
    assert canary.read_text(encoding="utf-8") == "private synthetic source must not be read"
    assert result.get("restore", {}).get("restart_verified") is True
    owner = profile / "source-profile/postgresql/18/owner.json"
    settings = profile / "source-profile/settings.json"
    preserved = owner.read_bytes(), settings.read_bytes()
    for token, expected_code in (("b" * 32, 1), ("a" * 32, 0)):
        verification = tmp_path / f"verify-{token[0]}.json"
        with (tmp_path / f"verify-{token[0]}.log").open("wb") as output:
            verified = subprocess.run(
                [sys.executable, "-c", script, str(forbidden), str(root / "packaging/launcher.py"),
                 "--postgres-self-test", str(verification), "--data-dir", str(profile),
                 "--postgres-qa-lifecycle", "verify", "--postgres-qa-run", token],
                env=environment, stdout=output, stderr=subprocess.STDOUT, timeout=300)
        assert verified.returncode == expected_code
        evidence = json.loads(verification.read_text(encoding="utf-8"))
        if expected_code:
            assert evidence["error_code"] == "qa_ownership_unconfirmed"
            assert (owner.read_bytes(), settings.read_bytes()) == preserved
        else:
            assert evidence["persisted_rows_verified"] is True
            assert evidence["clean_stop"] is True
            assert evidence["settings_sha256"] == result["settings_sha256"]
            assert evidence["preservation"]["source_installation_id"] == result["preservation"]["source_installation_id"]
            assert evidence["profile_retained"] is True


@pytest.mark.parametrize("profile_name", ["AsciiName x Filenames", "Кириллица с пробелами"],
                         ids=["ascii", "unicode"])
def test_restored_cluster_keeps_rows_and_vector_search(tmp_path, monkeypatch, profile_name):
    from atom_search.diagnostics import run_postgres_self_test
    from atom_search.runtime import _windows, postgres as postgres_module
    report = tmp_path / "restore.json"
    profile = tmp_path / profile_name
    # Preserve only type/location of an unexpected native boundary failure.
    # Never include locals, SQL, driver text or credentials in test output.
    failures = []
    initdb_failures = []
    original_run_logged = postgres_module._run_logged
    def observe_initdb(program, arguments, directory, environment, *, timeout):
        result, log_path = original_run_logged(program, arguments, directory, environment, timeout=timeout)
        if result and Path(program).name.lower() == "initdb.exe":
            if log_path.parent != Path(directory) or log_path.suffix != ".out":
                pytest.fail("initdb_log_not_owned", pytrace=False)
            _windows.assert_private(log_path)
            content = log_path.read_bytes().lower()
            snippets = {
                "missing_path_or_file": (b"no such file or directory", b"cannot find the path", b"cannot find the file"),
                "permission_denied": (b"permission denied", b"access is denied"),
                "invalid_argument": (b"invalid argument", b"parameter is incorrect"),
                "permission_change_or_set": (b"could not change permissions", b"could not set permissions", b"could not chmod"),
                "create_or_access_directory": (b"could not create directory", b"could not access directory", b"could not open directory", b"could not create data directory"),
                "bootstrap_backend_or_child_process": (b"bootstrap", b"child process", b"could not execute command", b"backend process", b"could not start backend"),
                "path_too_long": (b"file name too long", b"filename too long", b"path too long", b"filename or extension is too long"),
                "invalid_encoding_or_conversion": (b"invalid byte sequence", b"invalid encoding", b"could not convert", b"encoding conversion", b"no equivalent in target encoding"),
                "locale": (b"invalid locale", b"could not create locale", b"locale is not supported", b"could not determine locale"),
                "bootstrap_password_file": (b"password file", b"--pwfile", b"bootstrap-"),
            }
            data_path = Path(arguments[arguments.index("-D") + 1])
            data_path = str((data_path if data_path.is_absolute() else Path(directory) / data_path).absolute())
            import ctypes
            get_acp = ctypes.WinDLL("kernel32").GetACP
            get_acp.argtypes = []
            get_acp.restype = ctypes.c_uint
            initdb_failures.append({"exit_code": result, "data_path_length": len(data_path),
                                    "data_path_ascii": data_path.isascii(), "windows_acp": get_acp(),
                                    "categories": {name: any(token in content for token in tokens)
                                                   for name, tokens in snippets.items()}})
        return result, log_path
    monkeypatch.setattr(postgres_module, "_run_logged", observe_initdb)
    original_fail = PostgresRuntime._fail
    def observe_failure(instance, code, *args, **kwargs):
        error = sys.exception()
        if code == "runtime_start_failed" and error is not None:
            import traceback
            failures.append({"type": type(error).__name__,
                             "locations": [(Path(frame.filename).name, frame.lineno)
                                           for frame in traceback.extract_tb(error.__traceback__)]})
        return original_fail(instance, code, *args, **kwargs)
    monkeypatch.setattr(PostgresRuntime, "_fail", observe_failure)
    result = run_postgres_self_test(report, data_dir=profile, runtime_dir=RUNTIME)
    try:
        value = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pytest.fail("proof_report_unavailable", pytrace=False)
    failure_detail = value.get("failure_detail", {})
    diagnostic = {"error_code": value.get("error_code"), "failures": failures,
                  "initdb_failures": initdb_failures,
                  "failure_detail": {"type": failure_detail.get("type"),
                                     "locations": failure_detail.get("locations", [])}}
    if result != 0:
        print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr, flush=True)
    assert result == 0, diagnostic
    assert value.get("backup", {}).get("method") == "logical-schema"
    assert value["backup"]["bytes"] > 0
    assert value["backup"]["runtime_marker_excluded"] is True
    assert value["restore"]["rows_equal"] is True
    assert value["restore"]["restart_verified"] is True
    assert value["restore"]["target_identity_preserved"] is True
    assert value["restore"]["dimension"] == 384
    assert value["restore"]["cosine_rows"] == [[1, 0.0], [2, 1.0], [3, 2.0]]
    assert value["restore"]["source_installation_id"] != value["restore"]["target_installation_id"]
    assert value["preservation"]["source_verified"] is True
    assert value["profile_removed"] is True


def test_restore_failure_keeps_original_profile(tmp_path, monkeypatch):
    from atom_search.diagnostics import run_postgres_self_test
    original = subprocess.run
    restore_attempts = []
    def corrupt_archive(arguments, *args, **kwargs):
        if Path(arguments[0]).name == "pg_restore.exe" and "--single-transaction" in arguments:
            archive = Path(arguments[-1])
            archive.write_bytes(b"invalid synthetic archive")
            restore_attempts.append(archive)
        return original(arguments, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", corrupt_archive)
    report = tmp_path / "failed-restore.json"
    profile = tmp_path / "retained-proof"
    assert run_postgres_self_test(report, data_dir=profile, runtime_dir=RUNTIME) == 1
    value = json.loads(report.read_text(encoding="utf-8"))
    assert len(restore_attempts) == 1
    assert value["error_code"] == "restore_tool_failed"
    assert value["preservation"]["source_verified"] is True
    assert value["profile_removed"] is False
    assert value["clean_stop"] is True
    source = PostgresRuntime(RUNTIME, Path(value["source_profile"]))
    try:
        assert source.start().installation_id == value["preservation"]["source_installation_id"]
        with source.open_connection() as connection:
            assert connection.execute(
                "SELECT id,payload,vector_dims(embedding) FROM atom_p0_restore.documents ORDER BY id"
            ).fetchall() == [(1, "contract", 384), (2, "drawing", 384), (3, "schedule", 384)]
    finally:
        source.stop()


@pytest.mark.parametrize("crash_point", ["pending_launch", "before_sql"])
def test_exited_creator_resumes_same_live_cluster(tmp_path, crash_point):
    """A real creator exits abruptly; only its verified orphan may be resumed."""
    from dataclasses import asdict
    from atom_search.runtime import _windows
    profile = tmp_path / "crashed-creator"
    evidence = tmp_path / "creator-evidence.json"
    script = r'''
import json, os, sys
from dataclasses import asdict
from pathlib import Path
from atom_search.runtime.postgres import PostgresRuntime
r = PostgresRuntime(Path(sys.argv[1]), Path(sys.argv[2]))
def crash(connection):
    identity = r._claimed_process()
    r._verify_sql(connection, require_marker=False)
    connection.execute("CREATE TABLE public.surviving_data (value text)")
    connection.execute("INSERT INTO public.surviving_data VALUES ('preserved before crash')")
    Path(sys.argv[3]).write_text(json.dumps({
        "server": asdict(identity), "installation": r._record["installation_id"],
        "creator": r._record["supervisor"]}), encoding="utf-8")
    os._exit(23)
if sys.argv[4] == "before_sql":
    r._bootstrap_sql = crash
else:
    original = r._run
    def launch(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "pg_ctl.exe" and args[1][-1] == "start" and result == 0:
            with r._connect("maintenance") as connection:
                crash(connection)
        return result
    r._run = launch
r.start()
'''
    root = Path(__file__).resolve().parents[2]
    instance = PostgresRuntime(RUNTIME, profile)
    observed = None
    try:
        with (tmp_path / "creator-output.log").open("wb") as output:
            child = subprocess.run(
                [sys.executable, "-c", script, str(RUNTIME), str(profile), str(evidence), crash_point],
                env=dict(os.environ, PYTHONPATH=str(root / "src")),
                stdout=output, stderr=subprocess.STDOUT, timeout=120)
        assert child.returncode == 23
        observed = json.loads(evidence.read_text(encoding="utf-8"))
        creator = _windows.ProcessIdentity(**observed["creator"])
        assert _windows.process_identity(creator.pid) != creator
        identity = _windows.ProcessIdentity(**observed["server"])
        before = (instance.data_dir / "PG_VERSION").read_bytes()
        endpoint = instance.start()
        assert endpoint.installation_id == observed["installation"]
        assert instance._claimed_process() == identity
        assert (instance.data_dir / "PG_VERSION").read_bytes() == before
        with instance.open_connection(role="maintenance") as connection:
            assert connection.execute("SELECT value FROM public.surviving_data").fetchall() == [
                ("preserved before crash",)]
    finally:
        # The controller owns this disposable child experiment. Even on RED,
        # verify the exact observed server/UUID and exited creator before claiming
        # a cleanup token; never signal a PID merely because it occupies a port.
        if evidence.exists():
            observed = observed or json.loads(evidence.read_text(encoding="utf-8"))
            instance._setup()
            identity = instance._claimed_process()
            if identity is not None:
                assert identity == _windows.ProcessIdentity(**observed["server"])
                creator = _windows.ProcessIdentity(**observed["creator"])
                assert _windows.process_identity(creator.pid) != creator
                with _windows.mutex("postgres:" + str(profile.resolve())):
                    with _windows.verified_process(identity):
                        with instance._connect("maintenance") as connection:
                            instance._verify_sql(connection, require_marker=False)
                        assert instance._record["installation_id"] == observed["installation"]
                        instance._record.update(server=asdict(identity),
                                                supervisor=asdict(_windows.process_identity(os.getpid())),
                                                owner_token=instance._token, launch_pending=False)
                        instance._write_record()
                instance.stop()
                assert _windows.process_identity(identity.pid) != identity
