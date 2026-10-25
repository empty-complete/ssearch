"""Confirmed scoped mappings and atomic owner settings format migration."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from test_settings_schema import api, repository


SOURCE = '7c631704-a655-4466-9a8f-176386b6e353'
PROJECT = 'e3649197-0b1b-46f8-bc6a-f52c314e14b3'
OTHER = '159ab1be-cd3a-49cc-b679-23b701111b62'


def selected_settings():
    value = api('settings.migration').default_settings()
    value['sources'] = [dict(id=SOURCE, path=r'C:\Documents', label='', enabled=True)]
    return value


def candidate():
    value = selected_settings()
    value['projects'] = [dict(id=PROJECT, canonical_name='Пермские моторы', source_ids=[SOURCE],
        bindings=[dict(source_id=SOURCE, path_prefix='Зенитные фонари ПМ',
                       confirmed=True, basis='Подтверждено владельцем проекта')])]
    value['search']['project_aliases'] = [dict(id=str(uuid.uuid4()), project_id=PROJECT,
        source_ids=[SOURCE], variants=['ПМ'], confirmed=True, basis='Словарь владельца')]
    return value


def test_typed_project_binding_matches_segments_without_filesystem(tmp_path, monkeypatch):
    value = candidate()
    projects_api = api('service.projects')
    from atom_search_core.project_context import project_for_path
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('mapping read a file'))
    projects, aliases = projects_api.parse_project_settings(value, {SOURCE: 42})
    assert projects[0].source_ids == (42,)
    assert projects[0].canonical_name == 'Пермские моторы'
    assert project_for_path(42, 'Зенитные фонари ПМ/a.dwg', projects) == projects[0]
    assert project_for_path(42, 'Зенитные фонари ПМ-2/a.dwg', projects) is None
    assert project_for_path(99, 'Зенитные фонари ПМ/a.dwg', projects) is None
    assert aliases[0].variants == ('ПМ',)  # PM is never invented.
    assert aliases[0].basis == 'Словарь владельца'


def test_direct_source_root_and_unconfirmed_bindings():
    value = candidate()
    binding = value['projects'][0]['bindings'][0]
    binding.update(path_prefix='', confirmed=False)
    projects_api = api('service.projects')
    from atom_search_core.project_context import project_for_path
    projects, _ = projects_api.parse_project_settings(value, {SOURCE: 7})
    assert project_for_path(7, 'Спецификация.pdf', projects) is None
    binding['confirmed'] = True
    projects, _ = projects_api.parse_project_settings(value, {SOURCE: 7})
    assert project_for_path(7, 'Спецификация.pdf', projects) == projects[0]
    assert project_for_path(7, 'Ветка/Спецификация.pdf', projects) == projects[0]


@pytest.mark.parametrize('prefix', ['../ПМ', '/ПМ', r'\\server\share', r'C:\ПМ', 'C:ПМ', 'a/../ПМ'])
def test_project_schema_rejects_escaping_and_rooted_bindings(prefix):
    value = candidate()
    value['projects'][0]['bindings'][0]['path_prefix'] = prefix
    with pytest.raises(api('settings.schema').SettingsValidationError) as failure:
        api('settings.schema').validate_settings(value)
    assert 'projects.0.bindings.0.path_prefix' in {e.path for e in failure.value.errors}


def test_project_scope_references_and_confirmed_conflicts_are_checked():
    value = candidate()
    unknown = str(uuid.uuid4())
    value['projects'][0]['source_ids'] = [unknown]
    with pytest.raises(api('settings.schema').SettingsValidationError) as failure:
        api('settings.schema').validate_settings(value)
    assert 'projects.0.source_ids.0' in {e.path for e in failure.value.errors}
    value = candidate()
    second = deepcopy(value['projects'][0])
    second.update(id=OTHER, canonical_name='Другой объект')
    second['bindings'][0]['path_prefix'] += '/Вложенная'
    value['projects'].append(second)
    with pytest.raises(api('settings.schema').SettingsValidationError) as failure:
        api('settings.schema').validate_settings(value)
    assert 'project_binding_conflict' in {e.code for e in failure.value.errors}


def test_duplicate_alias_is_scoped_data_and_mapping_must_be_persisted():
    value = candidate()
    second = deepcopy(value['projects'][0])
    second.update(id=OTHER, canonical_name='Поставка материалов', bindings=[])
    value['projects'].append(second)
    alias = deepcopy(value['search']['project_aliases'][0])
    alias.update(id=str(uuid.uuid4()), project_id=OTHER, confirmed=False, basis='Предложение')
    value['search']['project_aliases'].append(alias)
    assert api('settings.schema').validate_settings(value) == value
    projects_api = api('service.projects')
    projects, aliases = projects_api.parse_project_settings(value, {SOURCE: 12})
    assert len(projects) == len(aliases) == 2
    assert aliases[0].variants == aliases[1].variants == ('ПМ',)
    assert aliases[1].confirmed is False and aliases[1].basis == 'Предложение'
    with pytest.raises(api('settings.schema').SettingsValidationError):
        projects_api.parse_project_settings(value, {})


@pytest.mark.parametrize('change, field', [
    ('variants', 'search.project_aliases.0.variants'),
    ('variant_length', 'search.project_aliases.0.variants.0'),
    ('bindings', 'projects.0.bindings'),
    ('blank_basis', 'projects.0.bindings.0.basis'),
    ('alias_scope', 'search.project_aliases.0.source_ids.0'),
])
def test_project_limits_and_alias_scope_are_field_errors(change, field):
    value = candidate()
    if change == 'variants':
        value['search']['project_aliases'][0]['variants'] = [str(i) for i in range(33)]
    elif change == 'variant_length':
        value['search']['project_aliases'][0]['variants'] = ['я' * 129]
    elif change == 'bindings':
        value['projects'][0]['bindings'] *= 129
    elif change == 'blank_basis':
        value['projects'][0]['bindings'][0]['basis'] = ' '
    else:
        value['search']['project_aliases'][0]['source_ids'] = [str(uuid.uuid4())]
    with pytest.raises(api('settings.schema').SettingsValidationError) as failure:
        api('settings.schema').validate_settings(value)
    assert field in {e.path for e in failure.value.errors}


def test_pure_schema1_migration_preserves_values_and_revision():
    original = selected_settings()
    original.update(schema_version=1, revision=19)
    original['sources'][0].update(path=r'\\сервер\Выбор владельца', label='Архив')
    original['indexing']['interval_seconds'] = 604800
    before = deepcopy(original)
    migrated = api('settings.migration').migrate_settings_v1(original)
    assert original == before
    assert migrated == dict(before, schema_version=3)


def test_schema1_load_publishes_once_and_preserves_original_bytes(tmp_path):
    original = selected_settings()
    original.update(schema_version=1, revision=9)
    original['sources'][0]['path'] = r'D:\Выбранный каталог'
    original['indexing']['interval_seconds'] = 604800
    raw = json.dumps(original, ensure_ascii=False, indent=3).encode('utf-8')
    (tmp_path / 'settings.json').write_bytes(raw)
    loaded = repository(tmp_path).load()
    assert loaded.schema_version == 3 and loaded.revision == 10
    assert loaded.settings == dict(original, schema_version=3, revision=10)
    assert (tmp_path / 'engine/settings-backups/schema1.json').read_bytes() == raw
    assert repository(tmp_path).load() == loaded
    journal = json.loads((tmp_path / 'engine/runtime/settings-operation.json').read_bytes())
    assert journal['schema_version'] == 1 and journal['state'] == 'applied'
    assert journal['before'] == original and journal['candidate'] == loaded.settings
    with pytest.raises(api('settings.schema').SettingsValidationError):
        api('service.mutations').MutationCoordinator(repository_loaded(tmp_path)).owner_apply(
            dict(loaded.settings, schema_version=1), loaded.revision)
    assert repository(tmp_path).load() == loaded


def repository_loaded(path):
    repo = repository(path)
    repo.load()
    return repo


def test_denied_migration_replace_preserves_schema1_bytes_and_can_retry(tmp_path, monkeypatch):
    value = selected_settings()
    value.update(schema_version=1, revision=8)
    raw = json.dumps(value, indent=3).encode()
    path = tmp_path / 'settings.json'
    path.write_bytes(raw)
    module = api('settings.repository')
    original_write = module.atomic_write_json
    def denied(target, document):
        if target == path and document['schema_version'] == 3:
            raise PermissionError('controlled migration replace denial')
        return original_write(target, document)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'atomic_write_json', denied)
        with pytest.raises(PermissionError):
            repository(tmp_path).load()
    assert path.read_bytes() == raw
    current = repository(tmp_path).load()
    assert current.schema_version == 3 and current.revision == 9
    assert (tmp_path / 'engine/settings-backups/schema1.json').read_bytes() == raw
    assert repository(tmp_path).load() == current


def test_partial_schema1_backup_write_does_not_poison_migration_retry(tmp_path, monkeypatch):
    import os
    value = selected_settings()
    value.update(schema_version=1, revision=8)
    raw = json.dumps(value, ensure_ascii=False, indent=3).encode('utf-8')
    path = tmp_path / 'settings.json'
    path.write_bytes(raw)
    backup_dir = tmp_path / 'engine/settings-backups'
    real_sync = os.fsync
    damaged = []
    def partial_write(descriptor):
        if backup_dir.exists() and not damaged:
            identity = os.fstat(descriptor).st_ino
            for candidate in backup_dir.iterdir():
                if candidate.is_file() and candidate.stat().st_ino == identity:
                    # A real partially persisted write, not an exception after
                    # leaving a complete valid backup behind.
                    os.ftruncate(descriptor, 13)
                    real_sync(descriptor)
                    damaged.append(candidate.name)
                    raise OSError('controlled partial backup write')
        return real_sync(descriptor)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', partial_write)
        with pytest.raises(OSError, match='controlled partial backup write'):
            repository(tmp_path).load()
    assert damaged and path.read_bytes() == raw
    current = repository(tmp_path).load()
    assert current.schema_version == 3 and current.revision == 9
    assert (backup_dir / 'schema1.json').read_bytes() == raw
    assert repository(tmp_path).load() == current


def test_crashed_schema_migration_recovers_original_before_retry(tmp_path, monkeypatch):
    value = selected_settings()
    value.update(schema_version=1, revision=9)
    value['ui']['theme'] = 'dark'
    raw = json.dumps(value, indent=3).encode()
    (tmp_path / 'settings.json').write_bytes(raw)
    module = api('settings.repository')
    original_write = module.atomic_write_json
    def crash(target, document):
        if target.name == 'settings-operation.json' and document['state'] == 'committed':
            raise SystemExit('controlled post-replace crash')
        return original_write(target, document)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'atomic_write_json', crash)
        with pytest.raises(SystemExit):
            repository(tmp_path).load()
    current = repository(tmp_path).load()
    assert current.schema_version == 3 and current.revision == 12
    assert current.settings == dict(value, schema_version=3, revision=12)
    assert (tmp_path / 'engine/settings-backups/schema1.json').read_bytes() == raw
    assert repository(tmp_path).load() == current


@pytest.mark.parametrize('state', ['prepared', 'committed', 'applied', 'rolled_back'])
def test_old_schema1_journal_is_recovered_before_new_format_publication(tmp_path, state):
    prepared = repository(tmp_path).load().settings
    before = dict(prepared, schema_version=1, revision=20)
    before['ui']['theme'] = 'dark'
    proposed = deepcopy(before)
    proposed['revision'] = 21
    proposed['ui']['theme'] = 'light'
    if state in ('prepared', 'committed', 'applied'):
        physical = proposed
    else:
        physical = dict(before, revision=22)
    (tmp_path / 'settings.json').write_text(json.dumps(physical), encoding='utf-8')
    journal = tmp_path / 'engine/runtime/settings-operation.json'
    journal.write_text(json.dumps(dict(schema_version=1, operation_id=str(uuid.uuid4()),
        state=state, before=before, candidate=proposed)), encoding='utf-8')
    current = repository(tmp_path).load()
    expected = proposed if state == 'applied' else before
    assert current.schema_version == 3
    assert current.revision == (22 if state == 'applied' else 23)
    assert current.settings == dict(expected, schema_version=3, revision=current.revision)
    assert repository(tmp_path).load() == current


def test_starting_owner_migrates_through_one_existing_coordinator_without_pg(tmp_path, monkeypatch):
    from atom_search.service.owner import open_service_owner
    from atom_search.service.mutations import MutationCoordinator
    value = selected_settings()
    value.update(schema_version=1, revision=7)
    value['sources'][0]['path'] = r'D:\Owner files'
    (tmp_path / 'settings.json').write_text(json.dumps(value), encoding='utf-8')
    seen = []
    original = MutationCoordinator.owner_apply
    def apply(coordinator, candidate, revision):
        seen.append((id(coordinator), candidate['schema_version'], revision))
        return original(coordinator, candidate, revision)
    monkeypatch.setattr(MutationCoordinator, 'owner_apply', apply)
    owner = open_service_owner(tmp_path, tmp_path / 'missing')
    try:
        current = owner.facade(owner.owner_access).settings_get()
        assert current.schema_version == 3 and current.revision == 8
        assert seen == [(id(owner.coordinator), 3, 7)]
        assert owner.core is None
    finally:
        owner.close()
