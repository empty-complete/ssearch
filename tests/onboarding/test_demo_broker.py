"""Named broker-facing demo calls cannot select host paths, caps or work mode."""
import importlib
import importlib.util
from pathlib import Path
import uuid

import pytest

from atom_search.service.contracts import SearchRequest, SearchResponse, ServiceError


MANIFEST = Path(__file__).resolve().parents[2] / 'examples/onboarding-demo/manifest.json'


def api():
    name = 'atom_search.onboarding.demo_broker'
    assert importlib.util.find_spec(name) is not None, 'named host-only demo broker adapter is required'
    return importlib.import_module(name)


def adapter(tmp_path, *, capabilities=()):
    broker = api()
    manager_name = 'atom_search.onboarding.session_manager'
    assert importlib.util.find_spec(manager_name) is not None, 'host-owned demo session manager is required'
    manager = importlib.import_module(manager_name).DemoSessionManager(manifest_path=MANIFEST,
        profile_dir=tmp_path / 'profile', build_capabilities=capabilities,
        register_owned_exclusion=lambda path: None)
    return broker.DemoBrokerAdapter(manager)


def assert_error(code, operation):
    with pytest.raises(ServiceError) as failure:
        operation()
    assert failure.value.code == code and failure.value.details == {}


def test_named_demo_calls_use_actual_owned_txt_and_public_search_fetch_dtos(tmp_path):
    subject = adapter(tmp_path, capabilities=('project_aliases',))
    session = subject.open_demo()
    try:
        assert session.mode == 'demo' and session.capabilities == ('project_aliases',)
        assert not hasattr(session, 'provider') and not hasattr(session, 'root')
        response = subject.search_demo(session.session_id, SearchRequest('Пермские моторы зенитные фонари'))
        assert isinstance(response, SearchResponse)
        assert any('Чёто до' in row.relative_path for row in response.results[:4])
        selected = response.results[0]
        fetched = subject.fetch_demo(session.session_id, selected.result_id)
        assert fetched.result_id == selected.result_id and fetched.score is None
        assert Path(fetched.full_path).read_text(encoding='utf-8').startswith('Учебные данные.')
        assert subject.search_demo(session.session_id, SearchRequest('ПМ', source_ids=())).results == ()
        assert_error('result_not_found', lambda: subject.fetch_demo(session.session_id,
                     selected.result_id, source_ids=()))
    finally:
        subject.close_demo(session.session_id)
    assert not Path(fetched.full_path).exists()


@pytest.mark.parametrize('payload', [{'manifest_path': 'C:/foreign/manifest.json'},
                                    {'owned_base': 'C:/foreign'}, {'profile_dir': 'C:/foreign'}, {'capabilities': ('signed_updates',)},
                                    {'mode': 'work'}, {'owner_id': 'forged'}, {'token': 'forged'}])
def test_caller_cannot_override_host_composition_through_open(tmp_path, payload):
    subject = adapter(tmp_path)
    with pytest.raises(TypeError):
        subject.open_demo(**payload)
    assert not (tmp_path / 'profile/runtime/onboarding-demo').exists()


def test_adapter_has_no_generic_invoke_path_or_host_shutdown_control(tmp_path):
    subject = adapter(tmp_path)
    for method in ('invoke', 'dispatch', 'execute', 'open_work', 'begin_close'):
        with pytest.raises(AttributeError):
            getattr(subject, method)
    with pytest.raises(TypeError):
        subject.open_demo({'mode': 'demo', 'owned_base': 'C:/caller'})
    assert not (tmp_path / 'profile/runtime/onboarding-demo').exists()


def test_dictionary_requests_and_work_mode_do_not_expand_the_typed_allowlist(tmp_path):
    subject = adapter(tmp_path)
    session = subject.open_demo()
    try:
        for search_request in ({'query': 'ПМ', 'path': 'C:/foreign'}, {'method': 'settings_apply'},
                               SearchRequest('ПМ', mode='work')):
            assert_error('invalid_request', lambda: subject.search_demo(session.session_id, search_request))
        with pytest.raises(TypeError):
            subject.search_demo(session.session_id, SearchRequest('ПМ'), capabilities=('signed_updates',))
        assert_error('source_scope_denied', lambda: subject.search_demo(session.session_id,
            SearchRequest('ПМ', source_ids=(str(uuid.uuid4()),))))
    finally:
        subject.close_demo(session.session_id)


def test_broker_never_resolves_ids_owned_by_another_manager_or_closed_session(tmp_path):
    first, other = adapter(tmp_path / 'first'), adapter(tmp_path / 'other')
    session = first.open_demo()
    selected = first.search_demo(session.session_id, SearchRequest('спецификация')).results[0]
    try:
        for operation in (lambda: other.search_demo(session.session_id, SearchRequest('спецификация')),
                          lambda: other.fetch_demo(session.session_id, selected.result_id),
                          lambda: other.close_demo(session.session_id)):
            assert_error('demo_session_not_found', operation)
        assert Path(selected.full_path).is_file()
    finally:
        first.close_demo(session.session_id)
    assert_error('demo_session_not_found', lambda: first.search_demo(session.session_id, SearchRequest('ПМ')))


def test_close_info_redacts_paths_and_reports_preserved_files_truthfully(tmp_path):
    from dataclasses import FrozenInstanceError

    subject = adapter(tmp_path)
    session = subject.open_demo()
    row = subject.search_demo(session.session_id, SearchRequest('спецификация')).results[0]
    modified = Path(row.full_path)
    modified.write_text('user changed this tutorial file', encoding='utf-8')
    result = subject.close_demo(session.session_id)
    assert result.session_id == session.session_id
    assert result.cleanup_pending is False and result.deferred_count == 0
    assert result.removed_count == 7 and result.preserved_count == 1
    assert modified.read_text(encoding='utf-8') == 'user changed this tutorial file'
    assert not hasattr(result, 'root') and not hasattr(result, 'removed') and not hasattr(result, 'preserved')
    assert all(type(value) in (str, bool, int) for value in vars(result).values())
    with pytest.raises(FrozenInstanceError):
        result.cleanup_pending = True
