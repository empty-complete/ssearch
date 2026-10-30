"""Closed descriptions only. No dispatch, process control or network probes.

Global/runtime repairs remain unsupported until the owner supplies verified
ownership, complete scope, storage and recovery contracts in the later slice.
"""
from dataclasses import dataclass
from types import MappingProxyType

from .contracts import RepairAction, repair_error


# Existing schema3 identifiers; saving one does not enable an implementation.
POLICY_ACTIONS = frozenset({
    'retry_source_probe', 'pause_source_job', 'use_lexical_fallback',
    'restore_local_model', 'retry_database', 'reselect_owned_port',
    'rebuild_derived_index', 'restore_valid_config', 'cancel_stalled_job',
    'cleanup_reproducible_cache',
})


@dataclass(frozen=True)
class SourceRepair:
    code: str
    state: str
    error_code: str
    action: RepairAction


_SOURCE_REPAIRS = MappingProxyType({
    'SOURCE_UNREACHABLE': SourceRepair('SOURCE_UNREACHABLE', 'unreachable',
        'source_unavailable', RepairAction('retry_source_probe', 'retry_source_probe', 5, 1, False)),
    'SOURCE_ACCESS_DENIED': SourceRepair('SOURCE_ACCESS_DENIED', 'access_denied',
        'source_access_denied', RepairAction('pause_source_job', 'pause_source_job', 5, 1, False)),
})


def source_repair(code: str) -> SourceRepair:
    if type(code) is not str or not 1 <= len(code) <= 64:
        raise repair_error('invalid_argument')
    repair = _SOURCE_REPAIRS.get(code)
    if repair is None:
        raise repair_error('unsupported_repair')
    return repair
