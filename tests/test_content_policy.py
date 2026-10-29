"""Content authority seam only: no ambient files, credentials, worker or PG."""
from dataclasses import replace
import importlib

import pytest

from atom_search.service.contracts import AccessContext, ServiceError


def surface(module, *names):
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        assert exc.name == module or module.startswith(exc.name + '.')
        loaded = None
    assert loaded is not None, f'required content boundary is absent: {module}'
    for name in names:
        assert callable(getattr(loaded, name, None)), f'required content surface: {name}'
    return loaded


@pytest.fixture
def api():
    class Boundary:
        def __iter__(self):
            # Resolve inside the test body: absent surfaces are assertion FAILs,
            # never missing-import collection/setup errors.
            return iter((surface('atom_search.content_indexing.contracts', 'ContentAccessPolicy',
                                 'ContentFileRef', 'FileReadResult'),
                         surface('atom_search.content_indexing.access', 'ContentAccessGate',
                                 'ContentCheckBudget')))
    return Boundary()


def rig(api, *, status='readable', policy=None, policy_present=True, outcome_change=None, check_delay=0):
    c, a = api
    clock = [100.0]
    events = []
    ref = c.ContentFileRef(source_id='source-a', result_id='result-a', file_id=7,
                           file_revision=4, relative_path='private/document.pdf')
    if policy is None and policy_present:
        policy = c.ContentAccessPolicy()

    def policy_for(source_id):
        events.append(('policy', source_id))
        return policy

    def resolve_file(source_id, result_id):
        events.append(('resolve', source_id, result_id))
        return ref

    class Checker:
        # This trusted host seam must enforce its deadline outside this module.
        # The fake is deterministic; it does not claim Windows ACL enforcement.
        def check(self, file, *, deadline):
            events.append(('check', file, deadline))
            clock[0] += check_delay
            result = c.FileReadResult(source_id=file.source_id, result_id=file.result_id,
                                      file_revision=file.file_revision, status=status)
            return replace(result, **(outcome_change or {}))

    budget = a.ContentCheckBudget(clock=lambda: clock[0])
    gate = a.ContentAccessGate(policy_for=policy_for, resolve_file=resolve_file,
                               checker=Checker(), budget=budget)
    caller = AccessContext('owner', frozenset({'content_read_v1', 'content_search_v1',
                                              'content_extract_v1'}), ('source-a',))
    return gate, caller, events, clock, ref


def denied(call, code):
    with pytest.raises(ServiceError) as caught:
        call()
    assert caught.value.code == code


@pytest.mark.parametrize('operation', ['extract', 'search_content', 'fetch_content'])
def test_default_current_file_policy_requires_exact_file_read_without_extra_consent(api, operation):
    gate, caller, events, _, ref = rig(api)
    grant = gate.authorize('source-a', operation=operation, access=caller, result_id='result-a')
    assert (grant.source_id, grant.result_id, grant.file_revision, grant.policy_revision,
            grant.operation, grant.cached_allowed) == ('source-a', 'result-a', 4, 1, operation, False)
    assert grant.checked_at
    assert events[-1][0:2] == ('check', ref)
    assert events[-1][2] == 102.0


@pytest.mark.parametrize('caller', [AccessContext('reader', frozenset(), ('source-a',)),
    AccessContext('reader', frozenset({'content_read_v1'}), ('other',)),
    AccessContext('owner', frozenset({'content_read_v1'}), ())])
def test_scope_and_capability_precede_policy_result_lookup_and_read(api, caller):
    gate, _, events, _, _ = rig(api)
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_scope_denied')
    assert events == []


@pytest.mark.parametrize('policy', [None, {'mode': 'future_policy'},
    {'mode': 'require_current_file_read', 'allow_cached_unreachable': False},
    {'mode': 'require_current_file_read', 'allow_cached_unreachable': 'false', 'policy_revision': 1}])
def test_unknown_or_malformed_persisted_policy_fails_closed(api, policy):
    gate, caller, events, _, _ = rig(api, policy=policy, policy_present=False)
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_policy_invalid')
    assert [e[0] for e in events] == ['policy']


@pytest.mark.parametrize('status', ['denied', 'revoked', 'timeout', 'offline', 'unreachable', 'unknown'])
def test_default_policy_hides_content_when_current_file_read_is_not_proven(api, status):
    gate, caller, _, _, _ = rig(api, status=status)
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_access_denied')


@pytest.mark.parametrize('status', ['offline', 'unreachable'])
@pytest.mark.parametrize('operation', ['search_content', 'fetch_content'])
def test_offline_cache_requires_explicit_owner_policy(api, status, operation):
    c, _ = api
    policy = c.ContentAccessPolicy('cached_owner_grant', True, 9)
    gate, caller, _, _, _ = rig(api, status=status, policy=policy)
    grant = gate.authorize('source-a', operation=operation, access=caller, result_id='result-a')
    assert grant.cached_allowed is True
    assert grant.policy_revision == 9


@pytest.mark.parametrize('status,operation', [('denied', 'fetch_content'), ('revoked', 'search_content'),
    ('timeout', 'fetch_content'), ('offline', 'extract')])
def test_cached_owner_policy_never_overrides_denial_revocation_timeout_or_extraction(api, status, operation):
    c, _ = api
    gate, caller, _, _, _ = rig(api, status=status,
                               policy=c.ContentAccessPolicy('cached_owner_grant', True, 9))
    denied(lambda: gate.authorize('source-a', operation=operation, access=caller,
                                  result_id='result-a'), 'content_access_denied')


@pytest.mark.parametrize('policy', [('require_current_file_read', True, 1),
                                  ('cached_owner_grant', False, 1), ('deny', False, 1)])
def test_offline_permission_is_not_inferred_from_one_policy_flag(api, policy):
    c, _ = api
    gate, caller, _, _, _ = rig(api, status='offline', policy=c.ContentAccessPolicy(*policy))
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_access_denied')


@pytest.mark.parametrize('change', [{'source_id': 'other'}, {'result_id': 'other'}, {'file_revision': 3}])
def test_current_file_authority_cannot_be_reused_for_another_result_or_revision(api, change):
    gate, caller, _, _, _ = rig(api, outcome_change=change)
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_access_denied')


def test_request_budget_caps_checks_and_does_not_reset_for_each_authorization(api):
    gate, caller, events, _, _ = rig(api)
    for _ in range(16):
        gate.authorize('source-a', operation='fetch_content', access=caller, result_id='result-a')
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_check_budget_exhausted')
    assert sum(e[0] == 'check' for e in events) == 16


def test_request_deadline_is_monotonic_and_closed_before_further_io(api):
    gate, caller, events, clock, _ = rig(api)
    clock[0] = 102.0
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_check_budget_exhausted')
    assert events == []


def test_late_checker_success_is_not_authority_after_request_deadline(api):
    gate, caller, events, _, _ = rig(api, check_delay=2.01)
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='result-a'), 'content_check_budget_exhausted')
    assert sum(e[0] == 'check' for e in events) == 1


def test_lookup_binding_and_unknown_operations_fail_before_file_read(api):
    gate, caller, events, _, _ = rig(api)
    denied(lambda: gate.authorize('source-a', operation='fetch_content', access=caller,
                                  result_id='other'), 'content_access_denied')
    assert all(e[0] != 'check' for e in events)
    events.clear()
    denied(lambda: gate.authorize('source-a', operation='root_read', access=caller,
                                  result_id='result-a'), 'content_operation_invalid')
    assert events == []
