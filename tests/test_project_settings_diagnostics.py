"""The frozen migration gate refuses an unseeded profile before composition."""
import json


def test_project_settings_gate_requires_real_schema1_seed_before_starting_owner(tmp_path, monkeypatch):
    import atom_search.diagnostics as diagnostics
    import atom_search.service.owner as owner
    def forbidden(*args, **kwargs):
        raise AssertionError('unseeded gate started an owner')
    monkeypatch.setattr(owner, 'open_service_owner', forbidden)
    operation = getattr(diagnostics, 'run_project_settings_service_test', None)
    assert callable(operation), 'project settings frozen scenario is missing'
    report = tmp_path / 'report.json'
    assert operation(report, data_dir=tmp_path / 'unseeded', model_dir=tmp_path / 'missing') == 1
    result = json.loads(report.read_bytes())
    assert result['ok'] is False
    assert result['error_code'] == 'project_settings_seed_required'
    assert not (tmp_path / 'unseeded').exists()


def test_project_context_gate_requires_real_v4_seed_before_starting_owner(tmp_path, monkeypatch):
    import atom_search.diagnostics as diagnostics
    import atom_search.service.owner as owner
    def forbidden(*args, **kwargs):
        raise AssertionError('unseeded context gate started an owner')
    monkeypatch.setattr(owner, 'open_service_owner', forbidden)
    operation = getattr(diagnostics, 'run_project_context_service_test', None)
    assert callable(operation), 'project context frozen scenario is missing'
    report = tmp_path / 'context.json'
    assert operation(report, data_dir=tmp_path / 'unseeded', model_dir=tmp_path / 'missing') == 1
    result = json.loads(report.read_bytes())
    assert result['ok'] is False and result['error_code'] == 'project_context_seed_required'
    assert not (tmp_path / 'unseeded').exists()


def test_project_retrieval_cli_refuses_existing_profile_before_owner_or_qt(tmp_path, monkeypatch):
    import atom_search.diagnostics as diagnostics
    import atom_search.service.owner as owner
    from atom_search.desktop.app import main
    def forbidden(*args, **kwargs):
        raise AssertionError('retrieval gate reused an existing profile')
    monkeypatch.setattr(owner, 'open_service_owner', forbidden)
    assert callable(getattr(diagnostics, 'run_project_retrieval_service_test', None))
    profile = tmp_path / 'already-present'
    profile.mkdir()
    report = tmp_path / 'retrieval.json'
    assert main(['--service-test', str(report), '--service-test-scenario', 'project-retrieval',
                 '--data-dir', str(profile), '--model-dir', str(tmp_path / 'missing')]) == 1
    result = json.loads(report.read_bytes())
    assert result['ok'] is result['clean_stop'] is False
    assert result['scenario'] == 'project-retrieval'
    assert result['error_code'] == 'disposable_profile_must_be_new'
    assert list(profile.iterdir()) == []
