"""Pure settings validation against bundled, closed versioned schemas.

This is deliberately a small validator for the keywords used by our schema,
not a general JSON Schema implementation. Unsupported keywords fail at load.
No candidate can cause file/network probes, credential reads or mutations.
"""
from __future__ import annotations

from copy import deepcopy
import json
import ntpath
from uuid import UUID

from atom_search.content import message
from atom_search.paths import resource_path
from atom_search.service.contracts import FieldError, ServiceError

MAX_SETTINGS_BYTES = 65536
CURRENT_SETTINGS_SCHEMA = 3
_KEYWORDS = frozenset({'$schema', 'title', 'description', 'type', 'const', 'enum',
                      'properties', 'required', 'additionalProperties', 'items',
                      'minItems', 'maxItems', 'minimum', 'maximum', 'minLength',
                      'maxLength', 'format', 'anyOf', 'uniqueItems'})


def _check_schema(schema):
    if set(schema) - _KEYWORDS:
        raise RuntimeError('unsupported_settings_schema_keyword')
    if schema.get('format', 'uuid') != 'uuid':
        raise RuntimeError('unsupported_settings_schema_format')
    if schema.get('type') == 'object' and schema.get('additionalProperties') is not False:
        raise RuntimeError('open_settings_schema_object')
    for child in schema.get('properties', {}).values():
        _check_schema(child)
    if 'items' in schema:
        _check_schema(schema['items'])
    for child in schema.get('anyOf', []):
        _check_schema(child)


_SCHEMAS = {version: json.loads(resource_path('schemas', f'settings-v{version}.schema.json').read_bytes())
            for version in (1, 2, 3)}
for _schema in _SCHEMAS.values():
    _check_schema(_schema)


def settings_error(code, *, details=None, status_code=400):
    return ServiceError(code, message('settings.' + code), code == 'settings_busy', details, status_code=status_code)


class SettingsValidationError(ServiceError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__('settings_invalid', message('settings.settings_invalid'),
                         details={'fields': [error.path for error in self.errors]})


def _error(path, code):
    return FieldError(path, code, message('settings.invalid_field', field=path or 'settings'))


def _walk(value, schema, path, errors):
    def fail(code):
        errors.append(_error(path, code))
    if 'anyOf' in schema:
        if not any(not _errors(value, option, path) for option in schema['anyOf']):
            fail('invalid_type')
        return
    expected = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int,
             'boolean': bool, 'null': type(None)}
    if expected is not None and type(value) is not types[expected]:
        fail('invalid_type')
        return
    if 'const' in schema and (value != schema['const'] or type(value) is not type(schema['const'])):
        fail('invalid_value')
    if 'enum' in schema and value not in schema['enum']:
        fail('invalid_value')
    if expected == 'object':
        properties = schema.get('properties', {})
        for name in schema.get('required', []):
            if name not in value:
                errors.append(_error(f'{path}.{name}'.strip('.'), 'required'))
        for name, item in value.items():
            child = f'{path}.{name}'.strip('.')
            if name not in properties:
                if schema.get('additionalProperties') is False:
                    errors.append(_error(child, 'unknown_field'))
            else:
                _walk(item, properties[name], child, errors)
    elif expected == 'array':
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', len(value)):
            fail('invalid_length')
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            fail('duplicate_value')
        for index, item in enumerate(value):
            _walk(item, schema.get('items', {}), f'{path}.{index}', errors)
    elif expected == 'string':
        if len(value.strip()) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', len(value)) or '\0' in value:
            fail('invalid_length')
        if schema.get('format') == 'uuid':
            try:
                if str(UUID(value)) != value:
                    raise ValueError()
            except ValueError:
                fail('invalid_uuid')
    elif expected == 'integer':
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            fail('out_of_range')


def _errors(value, schema, path=''):
    errors = []
    _walk(value, schema, path, errors)
    return errors


def _project_errors(candidate):
    from atom_search_core.project_context import path_segments
    errors, projects, aliases, confirmed = [], {}, set(), {}
    sources = {source['id'] for source in candidate['sources']}
    for index, project in enumerate(candidate['projects']):
        path = f'projects.{index}'
        if project['id'] in projects:
            errors.append(_error(path + '.id', 'duplicate_id'))
        projects[project['id']] = project
        for position, source in enumerate(project['source_ids']):
            if source not in sources:
                errors.append(_error(f'{path}.source_ids.{position}', 'unknown_source'))
        for position, binding in enumerate(project['bindings']):
            field = f'{path}.bindings.{position}'
            if binding['source_id'] not in project['source_ids']:
                errors.append(_error(field + '.source_id', 'source_outside_project'))
            try:
                prefix = path_segments(binding['path_prefix'])
            except ValueError:
                errors.append(_error(field + '.path_prefix', 'invalid_relative_path'))
                continue
            if binding['confirmed']:
                prior = confirmed.setdefault(binding['source_id'], [])
                if any(identity != project['id'] and
                       (prefix[:len(other)] == other or other[:len(prefix)] == prefix)
                       for other, identity in prior):
                    errors.append(_error(field + '.path_prefix', 'project_binding_conflict'))
                prior.append((prefix, project['id']))
    for index, alias in enumerate(candidate['search']['project_aliases']):
        path = f'search.project_aliases.{index}'
        if alias['id'] in aliases:
            errors.append(_error(path + '.id', 'duplicate_id'))
        aliases.add(alias['id'])
        project = projects.get(alias['project_id'])
        if project is None:
            errors.append(_error(path + '.project_id', 'unknown_project'))
        for position, source in enumerate(alias['source_ids']):
            if project is None or source not in project['source_ids']:
                errors.append(_error(f'{path}.source_ids.{position}', 'source_outside_project'))
    return errors


def validate_settings(candidate: dict, *, allow_legacy=False) -> dict:
    # Only physical old documents/recovery opt into v1/v2. Owner candidates cannot
    # downgrade a migrated profile, while journals retain their original values.
    version = candidate.get('schema_version') if type(candidate) is dict else None
    schema = _SCHEMAS[version] if allow_legacy and type(version) is int and version in (1, 2) else _SCHEMAS[CURRENT_SETTINGS_SCHEMA]
    errors = _errors(candidate, schema)
    if type(candidate) is dict and type(candidate.get('sources')) is list:
        roots, identities = set(), set()
        for index, source in enumerate(candidate['sources']):
            if type(source) is not dict:
                continue
            root, identity = source.get('path'), source.get('id')
            if type(root) is str:
                canonical = ntpath.normcase(ntpath.normpath(root.strip()))
                if ntpath.isabs(canonical):
                    canonical = canonical.rstrip('\\')
                if canonical in roots:
                    errors.append(_error(f'sources.{index}.path', 'duplicate_root'))
                roots.add(canonical)
            if type(identity) is str:
                if identity in identities:
                    errors.append(_error(f'sources.{index}.id', 'duplicate_id'))
                identities.add(identity)
    if errors:
        raise SettingsValidationError(errors)
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise SettingsValidationError((_error('', 'invalid_json'),)) from None
    if len(encoded) > MAX_SETTINGS_BYTES:
        raise settings_error('settings_too_large')
    if version in (2, 3):
        errors = _project_errors(candidate)
        if errors:
            raise SettingsValidationError(errors)
    return deepcopy(candidate)
