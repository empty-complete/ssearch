"""Experimental ssearch PostgreSQL UTF-8 manifests, on private copies only.

Resource writes use documented BeginUpdateResourceW/UpdateResourceW/
EndUpdateResourceW; pefile reads PE data without loading executable modules.
https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-updateresourcew
The activeCodePage feature starts at Windows 10 1903; 1809 fallback stays open.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
from xml.dom import Node, minidom
import zipfile

import pefile

from atom_search.runtime import _windows
from atom_search.runtime.manifest import sha256, verify_runtime


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / 'resources/postgresql'
EXECUTABLES = ('initdb.exe', 'pg_ctl.exe', 'postgres.exe', 'pg_dump.exe', 'pg_restore.exe')
ASM1 = 'urn:schemas-microsoft-com:asm.v1'
ASM3 = 'urn:schemas-microsoft-com:asm.v3'
SETTINGS = 'http://schemas.microsoft.com/SMI/2019/WindowsSettings'
PROOF_FLAGS = frozenset(('absolute_initdb_arguments', 'manifest_idempotent', 'runtime', 'source_restart',
                         'restore', 'scram', 'wrong_password_rejected', 'dpapi_verified',
                         'private_acl_verified', 'application_privileges_verified', 'clean_stop',
                         'profile_removed', 'rows_equal', 'restore_restart', 'target_identity_preserved',
                         'distinct_installations', 'dimension', 'cosine_rows', 'logical_backup',
                         'runtime_marker_excluded', 'source_preserved'))


class ProbeError(RuntimeError):
    """Only fixed public codes; never native errors or private paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise ProbeError(code)


def _xml_signature(node):
    """Compare all existing XML semantics except the selected code-page value."""
    if node.nodeType == Node.ELEMENT_NODE:
        if node.namespaceURI == SETTINGS and node.localName == 'activeCodePage':
            return None
        attributes = tuple(sorted((item.namespaceURI or '', item.localName or item.name, item.value)
                                  for item in node.attributes.values()
                                  if item.namespaceURI != 'http://www.w3.org/2000/xmlns/'))
        children = tuple(value for child in node.childNodes
                         if (value := _xml_signature(child)) is not None)
        if (node.namespaceURI == ASM3 and node.localName in ('application', 'windowsSettings')
                and not attributes and not children):
            return None
        return node.namespaceURI, node.localName, attributes, children
    if node.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
        return ('text', node.data) if node.data.strip() else None
    if node.nodeType == Node.COMMENT_NODE:
        return 'comment', node.data
    return None


def _child(document, parent, namespace, name):
    matches = [node for node in parent.childNodes if node.nodeType == Node.ELEMENT_NODE
               and node.namespaceURI == namespace and node.localName == name]
    _require(len(matches) <= 1, 'ambiguous_manifest_settings')
    if matches:
        return matches[0]
    node = document.createElementNS(namespace, name)
    node.setAttributeNS('http://www.w3.org/2000/xmlns/', 'xmlns', namespace)
    parent.appendChild(node)
    return node


def _utf8_xml(raw, name):
    if raw is None:
        document = minidom.parseString(
            f'<assembly xmlns="{ASM1}" manifestVersion="1.0">'
            f'<assemblyIdentity type="win32" name="ssearch.pg-utf8.{Path(name).stem}" '
            'version="1.0.0.0" processorArchitecture="amd64"/></assembly>')
    else:
        text = raw.replace(b'\0', b'').upper()
        _require(b'<!DOCTYPE' not in text and b'<!ENTITY' not in text, 'manifest_dtd_forbidden')
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            xml = raw.decode('utf-16').rstrip('\0')
        elif raw.startswith((b'<\0', b'\0<')):
            xml = raw.decode('utf-16-le' if raw.startswith(b'<\0') else 'utf-16-be').rstrip('\0')
        else:
            xml = raw.rstrip(b'\0')
        document = minidom.parseString(xml)
    root = document.documentElement
    _require(root.namespaceURI == ASM1 and root.localName == 'assembly', 'manifest_root_invalid')
    before = _xml_signature(root)
    application = _child(document, root, ASM3, 'application')
    settings = _child(document, application, ASM3, 'windowsSettings')
    code_page = _child(document, settings, SETTINGS, 'activeCodePage')
    _require(len(root.getElementsByTagNameNS(SETTINGS, 'activeCodePage')) == 1,
             'ambiguous_active_codepage')
    if raw is not None and ''.join(node.data for node in code_page.childNodes
                                   if node.nodeType == Node.TEXT_NODE).strip() == 'UTF-8':
        return raw
    for node in list(code_page.childNodes):
        code_page.removeChild(node)
    code_page.appendChild(document.createTextNode('UTF-8'))
    _require(_xml_signature(root) == before, 'manifest_settings_changed')
    return document.toxml(encoding='utf-8', standalone=document.standalone)


def _pe_state(path):
    with pefile.PE(str(path)) as pe:
        security = pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']]
        _require(not security.VirtualAddress and not security.Size, 'signed_pe_forbidden')
        resource = pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']]
        resource_section = pe.get_section_by_rva(resource.VirtualAddress) if resource.VirtualAddress else None
        if resource_section is not None:
            _require(not resource_section.Characteristics & (0x20 | 0x20000000), 'resource_contains_code')
        sections = [(section.Name.hex(), section.Characteristics, section.SizeOfRawData,
                     hashlib.sha256(section.get_data()).hexdigest())
                    for section in pe.sections if section is not resource_section]
        code = [entry for entry in sections if entry[1] & (0x20 | 0x20000000)
                or bytes.fromhex(entry[0]).rstrip(b'\0') == b'.text']
        _require(bool(code), 'pe_code_sections_missing')
        manifests, other_resources = {}, {}
        for kind in getattr(getattr(pe, 'DIRECTORY_ENTRY_RESOURCE', None), 'entries', []):
            for name in kind.directory.entries:
                for language in name.directory.entries:
                    data = language.data.struct
                    raw = pe.get_data(data.OffsetToData, data.Size)
                    key = (kind.id if kind.id is not None else str(kind.name),
                           name.id if name.id is not None else str(name.name), language.id)
                    if kind.id == 24 and name.id == 1:
                        manifests[language.id] = raw
                    else:
                        other_resources[key] = hashlib.sha256(raw).hexdigest()
        identity = (pe.FILE_HEADER.Machine, pe.FILE_HEADER.Characteristics, pe.FILE_HEADER.TimeDateStamp,
                    pe.OPTIONAL_HEADER.AddressOfEntryPoint, pe.OPTIONAL_HEADER.ImageBase,
                    pe.OPTIONAL_HEADER.Subsystem, pe.OPTIONAL_HEADER.DllCharacteristics)
        return dict(sections=sections, code=code, resources=other_resources,
                    manifests=manifests, pe_identity=identity)


def _write_manifests(path, manifests):
    kernel = c.WinDLL('kernel32', use_last_error=True)
    begin = kernel.BeginUpdateResourceW
    begin.argtypes, begin.restype = [w.LPCWSTR, w.BOOL], w.HANDLE
    update = kernel.UpdateResourceW
    update.argtypes, update.restype = [w.HANDLE, c.c_void_p, c.c_void_p, w.WORD, c.c_void_p, w.DWORD], w.BOOL
    end = kernel.EndUpdateResourceW
    end.argtypes, end.restype = [w.HANDLE, w.BOOL], w.BOOL
    handle = begin(str(path), False)  # Preserve all resources not explicitly replaced.
    _require(bool(handle), 'resource_update_begin_failed')
    discard = True
    try:
        for language, raw in manifests.items():
            buffer = c.create_string_buffer(raw)
            _require(update(handle, c.c_void_p(24), c.c_void_p(1), language,
                            c.cast(buffer, c.c_void_p), len(raw)), 'resource_update_failed')
        discard = False
    finally:
        _require(end(handle, discard), 'resource_update_commit_failed')


def patch_private_executable(path):
    path = Path(path).absolute()
    _require(not path.resolve().is_relative_to(ORIGINAL.resolve()), 'canonical_runtime_write_forbidden')
    _require(path.name in EXECUTABLES, 'candidate_executable_not_allowed')
    _windows.assert_private(path)
    original_hash = sha256(path)
    before = _pe_state(path)
    manifests = {language: _utf8_xml(raw, path.name)
                 for language, raw in (before['manifests'] or {0: None}).items()}
    if manifests != before['manifests']:
        _write_manifests(path, manifests)
    after = _pe_state(path)
    _require(before['sections'] == after['sections'] and before['code'] == after['code'], 'pe_sections_changed')
    _require(before['resources'] == after['resources'], 'unrelated_resources_changed')
    _require(before['pe_identity'] == after['pe_identity'], 'pe_identity_changed')
    _require(after['manifests'] == manifests, 'manifest_resource_mismatch')
    return dict(original_sha256=original_hash, new_sha256=sha256(path),
                original_manifest_present=bool(before['manifests']),
                code_sections_unchanged=True, other_sections_unchanged=True,
                pe_identity_preserved=True, assembly_identity_preserved=True, trust_info_preserved=True,
                other_xml_settings_preserved=True,
                original_xml_sha256={str(language): hashlib.sha256(raw).hexdigest()
                                     for language, raw in before['manifests'].items()},
                xml_sha256={str(language): hashlib.sha256(raw).hexdigest() for language, raw in manifests.items()})


def prepare_candidate(target):
    _require(os.name == 'nt', 'windows_required')
    _require(importlib.metadata.version('pefile') == '2023.2.7', 'pefile_version_mismatch')
    original = verify_runtime(ORIGINAL)
    # Preflight every selected original before creating or mutating a copy.
    for name in EXECUTABLES:
        _pe_state(ORIGINAL / 'bin' / name)
    target = Path(target).absolute()
    _require(not target.exists() and not target.resolve().is_relative_to(ORIGINAL.resolve()), 'candidate_must_be_new')
    _windows.private_directory(target)
    for asset in original.files:
        destination = target / asset.path
        _windows.private_directory(destination.parent)
        shutil.copyfile(ORIGINAL / asset.path, destination)
        _windows.assert_private(destination)
    manifest = json.loads((ORIGINAL / 'manifest.json').read_text(encoding='utf-8'))
    audit = {}
    for name in EXECUTABLES:
        relative = 'bin/' + name
        audit[relative] = patch_private_executable(target / relative)
        for record in manifest['files']:
            if record['path'] == relative:
                record.update(size=(target / relative).stat().st_size, sha256=sha256(target / relative))
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    verify_runtime(target)
    verify_runtime(ORIGINAL)
    return audit


def archive_candidate(candidate, audit, proof_flags):
    """After actual proof PASS, export only exact runtime inventory + receipt."""
    source_sha = os.environ.get('SOURCE_SHA', '')
    _require(re.fullmatch('[0-9a-f]{40}', source_sha) is not None, 'source_sha_required')
    _require(set(proof_flags) == PROOF_FLAGS and all(value is True for value in proof_flags.values()),
             'proof_flags_required')
    manifest = verify_runtime(candidate)
    verify_runtime(ORIGINAL)
    output = ROOT / 'artifacts/ci-native-utf8' / source_sha
    _require(not output.exists(), 'candidate_artifacts_already_exist')
    output.mkdir(parents=True)
    archive_path = output / 'ssearch-pg-utf8-candidate.zip'
    names = sorted([asset.path for asset in manifest.files] + ['manifest.json'])
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system, info.external_attr = 3, 0o100644 << 16
            with (candidate / name).open('rb') as source, archive.open(info, 'w') as destination:
                shutil.copyfileobj(source, destination)
    build = sys.getwindowsversion().build
    receipt = dict(experimental=True, release=False, full_suite_passed=False, source_commit=source_sha,
                   artifact_kind='experimental_native_runtime_candidate',
                   original_manifest_sha256=sha256(ORIGINAL / 'manifest.json'),
                   candidate_manifest_sha256=sha256(candidate / 'manifest.json'), executables=audit,
                   proof_flags=proof_flags, archive_sha256=sha256(archive_path),
                   archive_inventory=names, active_code_page='UTF-8', feature_minimum='Windows 10 1903',
                   feature_minimum_build=18362, feature_supported=build >= 18362, host_build=build,
                   windows_validation=[], win10_1809_legacy_fallback='OPEN')
    receipt_path = output / 'candidate-receipt.json'
    with receipt_path.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, indent=2)
    return archive_path, receipt_path
