import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
from atom_search import diagnostics

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("replacement", ["source_subtree", "settings_reparse", "settings_acl"])
def test_retained_verify_refuses_untrusted_paths_before_reading_settings(tmp_path, monkeypatch, replacement):
    from atom_search._postgres_proof import _Proof
    from atom_search.runtime import _windows
    from atom_search.runtime.postgres import PostgresRuntimeError
    proof = _Proof(tmp_path / "owned", tmp_path / "runtime", {}, "a" * 32)
    proof.prepare("seed")
    for name in ("source-profile", "restore-profile"):
        profile = proof.root / name
        cluster = profile / "postgresql/18"
        _windows.private_directory(cluster)
        identity = "11111111-1111-4111-8111-111111111111"
        _windows.atomic_json(cluster / "owner.json", {
            "installation_id": identity, "profile": str(profile),
            "data": str(cluster / "data"), "server": None, "launch_pending": False})
        proof.installations[name] = {
            "installation_id": identity, "cluster_identity": list(_windows._file_identity(cluster))}
    proof.seed_complete = True
    proof.save()
    proof.stop()
    settings = proof.source_dir / "settings.json"
    if replacement == "source_subtree":
        proof.source_dir.rename(proof.root / "retained-original")
        _windows.private_directory(proof.source_dir)
        settings.write_text("replacement content must not be read")
    original_reparse, original_private = _windows.reject_reparse, _windows.assert_private
    def reparse(path):
        if Path(path) == settings and replacement == "settings_reparse":
            raise _windows.WindowsError("reparse_path")
        return original_reparse(path)
    def private(path):
        if Path(path) == settings and replacement == "settings_acl":
            raise _windows.WindowsError("profile_acl_not_private")
        return original_private(path)
    monkeypatch.setattr(_windows, "reject_reparse", reparse)
    monkeypatch.setattr(_windows, "assert_private", private)
    original_open = Path.open
    reads = []
    def observe_open(path, *args, **kwargs):
        if path == settings:
            reads.append(str(path))
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observe_open)
    resumed = _Proof(proof.root, proof.runtime, {}, proof.token)
    try:
        with pytest.raises(PostgresRuntimeError, match="qa_ownership_unconfirmed"):
            resumed.prepare("verify")
        assert reads == [], "Untrusted settings were opened before ownership validation"
    finally:
        resumed.stop()


def test_proof_failure_keeps_safe_original_type_location_without_secret_text(tmp_path, monkeypatch):
    from atom_search import _postgres_proof
    from atom_search.runtime.postgres import PostgresRuntimeError
    from test_postgres_runtime_manifest import make_runtime
    runtime = tmp_path / "runtime"
    make_runtime(runtime)
    secret = "private-password-and-SQL-must-not-appear"
    def failed_start(*args):
        try:
            raise ValueError(secret)
        except ValueError:
            raise PostgresRuntimeError("runtime_start_failed") from None
    monkeypatch.setattr(_postgres_proof._Proof, "start", failed_start)
    report = tmp_path / "proof.json"
    assert diagnostics.run_postgres_self_test(
        report, data_dir=tmp_path / "profile", runtime_dir=runtime) == 1
    text = report.read_text()
    evidence = json.loads(text)
    detail = evidence.get("failure_detail", {})
    assert detail.get("type") == "ValueError"
    assert detail.get("locations") == [{
        "file": Path(__file__).name, "line": failed_start.__code__.co_firstlineno + 2}]
    assert secret not in text


def test_self_test_requires_fresh_explicit_disposable_profile(tmp_path):
    profile = tmp_path / "existing"
    profile.mkdir()
    (profile / "valuable").write_text("keep")
    report = tmp_path / "report.json"
    assert diagnostics.run_postgres_self_test(report, data_dir=profile) == 1
    assert (profile / "valuable").read_text() == "keep"
    value = json.loads(report.read_text())
    assert value["ok"] is False
    assert value["error_code"] == "disposable_profile_must_be_new"


def test_launcher_routes_postgres_test_without_desktop_import(tmp_path):
    spec = importlib.util.spec_from_file_location("test_pg_launcher", ROOT / "packaging/launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    report, profile = tmp_path / "report.json", tmp_path / "private"
    with patch.object(sys, "argv", ["AtomSearch", "--postgres-self-test", str(report),
                                   "--data-dir", str(profile)]), \
            patch.object(diagnostics, "run_postgres_self_test", return_value=7) as proof, \
            patch("atom_search.runtime.postgres.sanitize_host_environment"), \
            patch("atom_search.desktop.app.main", side_effect=AssertionError("desktop imported")):
        assert launcher.launch() == 7
    proof.assert_called_once_with(report, data_dir=profile)


def test_launcher_missing_explicit_profile_returns_failure(tmp_path):
    spec = importlib.util.spec_from_file_location("test_pg_launcher_missing", ROOT / "packaging/launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    with patch.object(sys, "argv", ["AtomSearch", "--postgres-self-test", str(tmp_path / "report")]):
        assert launcher.launch() != 0


def test_lifecycle_verify_rejects_unowned_existing_profile_without_touching_it(tmp_path):
    profile = tmp_path / "ordinary-user-profile"
    profile.mkdir()
    marker = profile / "valuable"
    marker.write_text("keep")
    report = tmp_path / "report.json"
    parameters = __import__("inspect").signature(diagnostics.run_postgres_self_test).parameters
    assert "lifecycle" in parameters, "Missing bounded lifecycle proof"
    assert diagnostics.run_postgres_self_test(
        report, data_dir=profile, lifecycle="verify", run_token="a" * 32) == 1
    assert marker.read_text() == "keep"
    assert json.loads(report.read_text())["error_code"] == "qa_ownership_unconfirmed"


def test_lifecycle_seed_requires_explicit_valid_run_identity_before_writes(tmp_path):
    report = tmp_path / "report.json"
    profile = tmp_path / "new"
    parameters = __import__("inspect").signature(diagnostics.run_postgres_self_test).parameters
    assert "lifecycle" in parameters, "Missing bounded lifecycle proof"
    for index, token in enumerate((None, "", "../foreign", "not-a-run-token")):
        report = tmp_path / f"report-{index}.json"
        assert diagnostics.run_postgres_self_test(
            report, data_dir=profile, lifecycle="seed", run_token=token) == 1
        assert not profile.exists()
        assert json.loads(report.read_text())["error_code"] == "qa_run_token_required"


@pytest.mark.parametrize("existing", [False, True])
def test_report_inside_profile_is_rejected_without_creating_or_overwriting_files(tmp_path, existing):
    profile = tmp_path / "protected"
    report = profile / "report.json"
    if existing:
        profile.mkdir()
        report.write_bytes(b"valuable user file")
    assert diagnostics.run_postgres_self_test(report, data_dir=profile) == 1
    if existing:
        assert report.read_bytes() == b"valuable user file"
    else:
        assert not profile.exists()


def test_existing_report_is_never_overwritten(tmp_path):
    report = tmp_path / "valuable.json"
    report.write_bytes(b"keep this")
    profile = tmp_path / "already-existing-profile"
    profile.mkdir()
    assert diagnostics.run_postgres_self_test(report, data_dir=profile) == 1
    assert report.read_bytes() == b"keep this"


def test_proof_memory_measurement_uses_verified_process_identity():
    import os
    from atom_search import _postgres_proof
    from atom_search.runtime import _windows
    measure = getattr(_postgres_proof, "_process_memory", None)
    assert callable(measure), "Actual process memory evidence is unavailable"
    identity = _windows.process_identity(os.getpid())
    value = measure(identity)
    assert value["working_set_bytes"] > 0
    assert value["peak_working_set_bytes"] >= value["working_set_bytes"]
    from dataclasses import replace
    with pytest.raises(_windows.WindowsError):
        measure(replace(identity, created=identity.created + 1))


def test_proof_rejects_corrupt_runtime_before_creating_outer_owner(tmp_path):
    from test_postgres_runtime_manifest import make_runtime
    runtime, profile, report = tmp_path / "runtime", tmp_path / "new", tmp_path / "report.json"
    make_runtime(runtime)
    (runtime / "bin/pg_dump.exe").write_bytes(b"untrusted")
    assert diagnostics.run_postgres_self_test(report, data_dir=profile, runtime_dir=runtime) == 1
    assert not profile.exists()


@pytest.mark.parametrize("replace_file", [False, True])
def test_backup_passfile_is_private_secret_free_in_conninfo_and_cleanup_checks_identity(
        tmp_path, monkeypatch, replace_file):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from atom_search._postgres_proof import _Proof
    from atom_search.runtime import _windows
    from atom_search.runtime.postgres import PostgresRuntimeError
    from psycopg.conninfo import conninfo_to_dict
    proof = _Proof(tmp_path / "owned", tmp_path / "unused-runtime", {}, "a" * 32)
    proof.prepare("seed")
    monkeypatch.setattr(proof, "runtime_guard", lambda instance: proof.guard())
    secret = "synthetic:password\\with_escaping"
    instance = SimpleNamespace(open_connection=lambda **kwargs: nullcontext(SimpleNamespace(
        info=SimpleNamespace(password=secret, host="127.0.0.1", user="atom_bootstrap",
                             port=45678, dbname="postgres"))))
    passfile = None
    try:
        def exercise():
            nonlocal passfile
            with proof.conninfo(instance) as value:
                assert secret not in value
                parameters = conninfo_to_dict(value)
                assert "password" not in parameters
                assert parameters["hostaddr"] == "127.0.0.1"
                passfile = Path(parameters["passfile"])
                _windows.assert_private(passfile)
                assert passfile.read_text() == (
                    "127.0.0.1:45678:postgres:atom_bootstrap:synthetic\\:password\\\\with_escaping\n")
                if replace_file:
                    passfile.rename(passfile.with_suffix(".retained-original"))
                    passfile.write_text("unrelated replacement")
        if replace_file:
            with pytest.raises(PostgresRuntimeError, match="temporary_ownership_changed"):
                exercise()
            assert passfile.read_text() == "unrelated replacement"
        else:
            exercise()
            assert not passfile.exists()
    finally:
        proof.stop()
