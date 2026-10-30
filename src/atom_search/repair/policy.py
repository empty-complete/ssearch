"""Pure, immutable owner restrictions; existing P2 authentication stays sole.

Only settings_validate is supported through the existing owner validation port.
This module neither grants agent settings mutation nor adds a service capability.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from atom_search.service.contracts import SettingsSnapshot

from .catalog import POLICY_ACTIONS
from .contracts import (AgentPrincipal, AgentSettingsValidation, NumericSettingLimit,
    SettingsValidatePort, repair_error, valid_integer, valid_uuid)


@dataclass(frozen=True)
class RepairPolicy:
    revision: int = 0
    enabled: bool = False
    allowed_actions: frozenset[str] = frozenset()
    setting_limits: tuple[NumericSettingLimit, ...] = ()

    def __post_init__(self):
        if (not valid_integer(self.revision) or type(self.enabled) is not bool
                or type(self.allowed_actions) is not frozenset
                or not self.allowed_actions <= POLICY_ACTIONS
                or type(self.setting_limits) is not tuple):
            raise repair_error('invalid_argument')
        paths = set()
        for limit in self.setting_limits:
            # The active schema3 interval is the only supported numeric field.
            # Neither policy construction nor an agent can widen schema limits.
            if (type(limit) is not NumericSettingLimit
                    or limit.path != 'indexing.interval_seconds' or limit.path in paths
                    or not valid_integer(limit.minimum) or not valid_integer(limit.maximum)
                    or not 86400 <= limit.minimum <= limit.maximum <= 31536000):
                raise repair_error('invalid_argument')
            paths.add(limit.path)

    def authorize(self, principal: AgentPrincipal, *, action: str,
                  source_id: str | None) -> None:
        if type(principal) is not AgentPrincipal or type(action) is not str:
            raise repair_error('invalid_argument')
        if source_id is not None and not valid_uuid(source_id):
            raise repair_error('invalid_argument')
        if not self.enabled or principal.policy_revision != self.revision:
            raise repair_error('access_denied', 403)
        if action == 'settings_validate':
            permitted = 'owner' in principal.access.capabilities and bool(self.setting_limits)
        else:
            # owner is not a wildcard for future agent repair authority.
            permitted = ('agent_repair' in principal.access.capabilities
                and action in self.allowed_actions and source_id is not None)
        if (not permitted or (source_id is not None
                and source_id not in principal.access.source_ids)):
            raise repair_error('access_denied', 403)

    def settings_validate(self, patch: dict, *, expected_revision: int,
                          principal: AgentPrincipal, current: SettingsSnapshot,
                          validator: SettingsValidatePort) -> AgentSettingsValidation:
        """Validate a bounded patch with P2; return only safe numeric changes.

        current and validator are trusted owner ports, never transport arguments.
        A global scheduler change requires explicit coverage of every enabled
        source even when the existing access contains owner authority.
        """
        if not valid_integer(expected_revision, 1) or type(current) is not SettingsSnapshot:
            raise repair_error('invalid_argument')
        self.authorize(principal, action='settings_validate', source_id=None)
        if current.revision != expected_revision:
            raise repair_error('revision_conflict', 409)
        enabled = {item['id'] for item in current.settings['sources'] if item['enabled']}
        if not principal.access.source_ids or not enabled <= set(principal.access.source_ids):
            raise repair_error('access_denied', 403)
        if (type(patch) is not dict or set(patch) != {'indexing'}
                or type(patch['indexing']) is not dict
                or set(patch['indexing']) != {'interval_seconds'}):
            raise repair_error('invalid_argument')
        value = patch['indexing']['interval_seconds']
        limit = next((item for item in self.setting_limits
                      if item.path == 'indexing.interval_seconds'), None)
        if (limit is None or not valid_integer(value)
                or not limit.minimum <= value <= limit.maximum):
            raise repair_error('invalid_argument')
        candidate = deepcopy(current.settings)
        candidate['indexing']['interval_seconds'] = value
        result = validator(candidate, expected_revision)
        if (not result.valid or result.normalized is None
                or any(change.path != 'indexing.interval_seconds' for change in result.diff)):
            raise repair_error('invalid_argument')
        # Do not expose normalized owner settings, credential refs or other
        # sources, even though the existing port validates the complete config.
        normalized = {'indexing': {'interval_seconds': result.normalized['indexing']['interval_seconds']}}
        return AgentSettingsValidation(True, expected_revision, normalized,
            tuple(deepcopy(result.diff)), (), ())
