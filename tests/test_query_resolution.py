"""Scoped interpretation preserves the literal query and public UUID boundary."""
from dataclasses import replace
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from atom_search_core.models import ProjectAlias, ProjectBinding, ProjectDefinition, SearchScope

PM = '11111111-1111-4111-8111-111111111111'
SUPPLY = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'
ALIAS = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
SECOND = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
SOURCE_A = '44444444-4444-4444-8444-444444444444'
SOURCE_B = '55555555-5555-4555-8555-555555555555'


def resolver():
    name = 'atom_search_core.query_resolution'
    assert importlib.util.find_spec(name) is not None, 'Pure scoped query resolver is required'
    return importlib.import_module(name).resolve_project_query


def definitions():
    projects = (
        ProjectDefinition(PM, 'Пермские моторы', (1, 2), (
            ProjectBinding(PM, 1, 'Зенитные фонари ПМ', True),
            ProjectBinding(PM, 2, '', True))),
        ProjectDefinition(SUPPLY, 'Поставка материалов', (2,), ()),
        ProjectDefinition(OTHER, 'Недоступный объект', (3,), ()))
    aliases = (ProjectAlias(ALIAS, PM, (1, 2), ('ПМ',), True, 'Owner confirmed'),
               ProjectAlias(SECOND, SUPPLY, (2,), ('ПМ',), True, 'Owner confirmed'))
    return projects, aliases


def test_full_canonical_name_resolves_confirmed_pm_branch_without_preselect():
    projects, aliases = definitions()
    query = '  Пермские моторы зенитные фонари  '
    result = resolver()(query, scope=SearchScope((1, 2)), projects=projects, aliases=aliases)
    assert result.original_query.encode() == query.encode()
    assert result.normalized_query == '  пермские моторы зенитные фонари  '
    assert result.selected_project_id is None and result.preferred_project_ids == (PM,)
    assert result.residual_query == 'зенитные фонари'
    assert result.expanded_terms == ('Пермские моторы',)
    assert result.applied_aliases == result.ambiguities == ()
    # Resolution identifies the confirmed project, not a textual PM substitution.
    from atom_search_core.project_context import project_for_path
    assert project_for_path(1, 'Зенитные фонари ПМ/a.dwg', projects).project_id == PM
    assert project_for_path(2, 'Спецификация.pdf', projects).project_id == PM


def test_ambiguous_pm_returns_both_options_and_literal_query():
    projects, aliases = definitions()
    query = 'ПМ спецификация'
    expected = resolver()(query, scope=SearchScope((2,)), projects=projects, aliases=aliases)
    assert expected.original_query == query and expected.residual_query == 'пм спецификация'
    assert expected.selected_project_id is None and expected.preferred_project_ids == ()
    assert expected.applied_aliases == ()
    assert [(item.term, item.project_ids) for item in expected.ambiguities] == [('ПМ', (PM, SUPPLY))]
    assert resolver()(query, scope=SearchScope((2,)), projects=projects[::-1],
                      aliases=aliases[::-1]) == expected


@pytest.mark.parametrize('term', ['ПМ', 'PM', 'PМ', 'ПM'])
def test_unknown_pm_stays_literal_and_pm_latin_requires_explicit_variant(term):
    projects, aliases = definitions()
    query = term + ' спецификация'
    result = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=())
    assert result.expanded_terms == result.preferred_project_ids == result.applied_aliases == ()
    assert result.original_query == query
    result = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=aliases)
    assert result.preferred_project_ids == ((PM,) if term == 'ПМ' else ())
    alias = replace(aliases[0], variants=(term,))
    explicit = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=(alias,))
    assert explicit.preferred_project_ids == (PM,)
    assert explicit.applied_aliases[0].matched_text == term


@pytest.mark.parametrize('query,matched', [
    ('ПМЗ', False), ('ПМ2', False), ('XПМ', False), ('ПМ_чертеж', True),
    ('(ПМ), спецификация', True), ('пм спецификация', True)])
def test_alias_matching_respects_word_boundaries(query, matched):
    projects, aliases = definitions()
    result = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=aliases)
    assert bool(result.applied_aliases) is matched
    assert result.original_query == query


def test_normalization_preserves_exact_original_fragments_and_residual_terms():
    projects, aliases = definitions()
    alias = replace(aliases[0], variants=('ffi', 'PM', 'ЁЖ', 'STRASSE'))
    query = '  ﬃ; ＰＭ; е\u0308ж; Straße; чертёж  '
    result = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=(alias,))
    assert result.original_query.encode() == query.encode()
    assert result.normalized_query == '  ffi; pm; еж; strasse; чертеж  '
    assert tuple(item.matched_text for item in result.applied_aliases) == ('ﬃ', 'ＰＭ', 'е\u0308ж', 'Straße')
    assert result.preferred_project_ids == (PM,) and result.expanded_terms == ('Пермские моторы',)
    assert 'чертеж' in result.residual_query and 'strasse' not in result.residual_query


def test_phrase_and_overlapping_variants_use_the_whole_configured_match():
    projects, aliases = definitions()
    alias = replace(aliases[0], variants=('ПМ', 'ПМ-2', 'Большой объект'))
    query = 'ПМ-2 Большой\tобъект спецификация'
    result = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=(alias,))
    assert tuple(item.matched_text for item in result.applied_aliases) == ('ПМ-2', 'Большой\tобъект')
    assert result.residual_query == 'спецификация'
    literal = resolver()('Большой другой объект', scope=SearchScope((1,)), projects=projects, aliases=(alias,))
    assert literal.applied_aliases == ()


@pytest.mark.parametrize('query,variant', [('… чертеж', '.'), ('İ чертеж', 'I')])
def test_partial_compatibility_expansion_or_combining_word_is_not_an_alias(query, variant):
    projects, aliases = definitions()
    result = resolver()(query, scope=SearchScope((1,)), projects=projects,
                        aliases=(replace(aliases[0], variants=(variant,)),))
    assert result.applied_aliases == result.preferred_project_ids == ()
    assert result.original_query == query


def test_same_canonical_name_ambiguity_uses_uuid_tie_break_and_public_scope():
    from atom_search.service.projects import project_resolution_dtos
    projects, _ = definitions()
    projects = (replace(projects[1], canonical_name='ОБЪЕКТ'),
                replace(projects[0], canonical_name='объект'))
    result = resolver()('Объект спецификация', scope=SearchScope((2,)), projects=projects, aliases=())
    assert result.preferred_project_ids == () and result.ambiguities[0].project_ids == (PM, SUPPLY)
    assert result.residual_query == 'объект спецификация'
    _, _, ambiguity = project_resolution_dtos(result, scope=SearchScope((2,)), projects=projects,
                                              source_ids={SOURCE_B: 2})
    assert [option.project_id for option in ambiguity[0].options] == [PM, SUPPLY]
    assert all(option.source_ids == (SOURCE_B,) for option in ambiguity[0].options)


def test_outer_whitespace_is_preserved_and_invalid_query_uses_existing_bound():
    projects, aliases = definitions()
    query = ' ' * 2048 + 'ПМ' + '\t' * 2048
    result = resolver()(query, scope=SearchScope((1,)), projects=projects, aliases=aliases)
    assert result.original_query == query and result.normalized_query == query.replace('ПМ', 'пм')
    assert result.applied_aliases[0].matched_text == 'ПМ' and result.residual_query == ''
    for invalid in ('', ' ', 'x' * 513, None):
        with pytest.raises(ValueError) as failure:
            resolver()(invalid, scope=SearchScope((1,)), projects=projects, aliases=aliases)
        assert failure.value.args == ('invalid_search_request',)


def test_aliases_outside_allowed_scope_never_expand_or_appear(monkeypatch):
    projects, aliases = definitions()
    alias = replace(aliases[0], source_ids=(2,))
    query = 'ПМ Недоступный объект'
    call = resolver()
    def forbidden(*args, **kwargs):
        pytest.fail('pure resolver attempted I/O or inference')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', forbidden)
        patch.setattr('os.stat', forbidden)
        result = call(query, scope=SearchScope((1,)), projects=projects, aliases=(alias,))
        empty = call(query, scope=SearchScope(()), projects=projects, aliases=aliases)
    assert result.expanded_terms == result.preferred_project_ids == result.applied_aliases == ()
    assert result.ambiguities == empty.ambiguities == ()
    assert empty.preferred_project_ids == empty.expanded_terms == empty.applied_aliases == ()
    assert empty.original_query == query and empty.residual_query == 'пм недоступный объект'
    assert alias.source_ids == (2,) and aliases[0].source_ids == (1, 2)
    unconfirmed = replace(aliases[0], confirmed=False)
    assert call('ПМ', scope=SearchScope((1,)), projects=projects, aliases=(unconfirmed,)).applied_aliases == ()


@pytest.mark.parametrize('selected', [OTHER, '99999999-9999-4999-8999-999999999999', 'invalid', ''])
def test_explicit_project_selection_rejects_foreign_or_unknown_without_disclosure(selected):
    projects, aliases = definitions()
    with pytest.raises(ValueError) as failure:
        resolver()('ПМ спецификация', scope=SearchScope((1,)), projects=projects,
                   aliases=aliases, project_id=selected)
    assert failure.value.args == ('project_scope_denied',)


def test_explicit_project_selection_keeps_query_and_resolves_ambiguity():
    projects, aliases = definitions()
    query = 'ПМ спецификация'
    result = resolver()(query, scope=SearchScope((2,)), projects=projects, aliases=aliases, project_id=SUPPLY)
    assert result.selected_project_id == SUPPLY and result.preferred_project_ids == (SUPPLY,)
    assert result.original_query == query and result.residual_query == 'спецификация'
    assert result.ambiguities == () and {item.project_id for item in result.applied_aliases} == {SUPPLY}
    with pytest.raises(ValueError) as failure:
        resolver()(query, scope=SearchScope(()), projects=projects, aliases=aliases, project_id=PM)
    assert failure.value.args == ('project_scope_denied',)


def test_transport_mapping_contains_only_permitted_public_source_ids():
    from atom_search.http.serialization import decode, encode, restore
    from atom_search.service.contracts import SearchResponse, ServiceError
    api = importlib.import_module('atom_search.service.projects')
    assert hasattr(api, 'project_resolution_dtos'), 'P2 DTO mapping is required'
    projects, aliases = definitions()
    resolution = resolver()('ПМ', scope=SearchScope((2,)), projects=projects, aliases=aliases)
    interpretation, applied, ambiguities = api.project_resolution_dtos(resolution,
        scope=SearchScope((2,)), projects=projects, source_ids={SOURCE_B: 2, SOURCE_A: 1})
    assert [(item.project_id, item.source_ids) for item in ambiguities[0].options] == [(PM, (SOURCE_B,)), (SUPPLY, (SOURCE_B,))]
    response = SearchResponse((), interpretation, applied, ambiguities, 7, ())
    assert restore(SearchResponse, decode(encode(response))) == response
    unique = resolver()('ПМ', scope=SearchScope((1,)), projects=projects, aliases=aliases)
    _, applied, _ = api.project_resolution_dtos(unique, scope=SearchScope((1,)),
        projects=projects, source_ids={SOURCE_A: 1})
    assert applied[0].source_id == SOURCE_A and applied[0].matched_text == 'ПМ'
    for resolution, scope, mapping in ((unique, SearchScope((2,)), {SOURCE_B: 2}),
                                      (unique, SearchScope((1,)), {}),
                                      (unique, SearchScope((1,)), {'1': 1}),
                                      (unique, SearchScope((1,)), {SOURCE_A: True}),
                                      (unique, SearchScope((1,)), {SOURCE_A: 1, SOURCE_B: 1})):
        with pytest.raises(ServiceError) as failure:
            api.project_resolution_dtos(resolution, scope=scope, projects=projects, source_ids=mapping)
        assert failure.value.code == 'project_scope_denied' and failure.value.details == {}


def test_public_project_search_stays_unavailable_before_sql_without_touching_owner():
    from atom_search.service.application import AtomSearchService
    from atom_search.service.contracts import AccessContext, SearchRequest, ServiceError
    subject = AtomSearchService(SimpleNamespace(), AccessContext('reader', frozenset({'search'}), (SOURCE_A,)))
    with pytest.raises(ServiceError) as failure:
        subject.search(SearchRequest('ПМ', project_id=PM))
    assert failure.value.code == 'capability_unavailable'
