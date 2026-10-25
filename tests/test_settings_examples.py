"""Bundled educational settings and owner candidates share one formal schema."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_settings_schema import api, repository


def test_settings_example_matches_single_formal_schema():
    sample = json.loads((Path(__file__).resolve().parents[1] /
                        'docs/contracts/agent-operations/settings.example.json').read_bytes())
    assert sample.pop('example') is True
    assert api('settings.schema').validate_settings(sample) == sample


def test_writer_rejects_example_marker(tmp_path):
    repo = repository(tmp_path)
    old = repo.load()
    before = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(Exception) as failure:
        repo.commit(dict(old.settings, example=True), old.revision)
    assert failure.value.code == 'settings_invalid'
    assert (tmp_path / 'settings.json').read_bytes() == before


def test_planned_closed_slots_round_trip_without_activating_features(tmp_path):
    repo = repository(tmp_path)
    old = repo.load()
    candidate = deepcopy(old.settings)
    candidate['ui']['close_action'] = 'quit'
    candidate['updates']['source'] = {
        'kind': 'unc', 'manifest_location': r'\\example\releases\manifest.json',
        'allowed_origins': [], 'credential_ref': None,
    }
    candidate['agent']['repair_policy']['allowed_actions'] = ['retry_source_probe']
    candidate['diagnostics']['outbox']['max_total_bytes'] = 50000000
    current = api('service.mutations').MutationCoordinator(repo).owner_apply(candidate, old.revision)
    again = repository(tmp_path).load()
    assert again == current
    assert again.settings['ui']['close_action'] == 'quit'
    assert again.settings['updates']['source'] == candidate['updates']['source']
    assert again.settings['agent']['repair_policy']['enabled'] is False
    assert not (tmp_path / 'engine/postgresql/18').exists()
