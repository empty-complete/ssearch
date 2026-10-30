"""Admit a bounded signed inventory before opening its archive."""
from __future__ import annotations

import hashlib

from .contracts import ArchiveEntry, ArchiveInventoryRef, UpdateError, VerifiedArchiveInventory
from .manifest import POLICY, _digest, _path
from .trust import exact_object, integer, strict_json


def verify_inventory(reference: ArchiveInventoryRef, raw: bytes) -> VerifiedArchiveInventory:
    if not isinstance(reference, ArchiveInventoryRef) or type(raw) is not bytes:
        raise UpdateError('update_inventory')
    try:
        integer(reference.size, maximum=POLICY.inventory_bytes)
        integer(reference.entry_count, maximum=POLICY.inventory_entries)
        integer(reference.unpacked_bytes, maximum=POLICY.unpacked_release_bytes)
        _digest(reference.sha256)
        if len(raw) != reference.size or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise UpdateError('update_inventory')
        value = exact_object(strict_json(raw), ('format_version', 'entries'))
        if type(value['format_version']) is not int or value['format_version'] != 1:
            raise UpdateError('update_inventory')
        items = value['entries']
        if type(items) is not list or len(items) != reference.entry_count:
            raise UpdateError('update_inventory')
        entries, total = [], 0
        for item in items:
            exact_object(item, ('path', 'size', 'sha256'))
            name = _path(item['path'])
            size = integer(item['size'], maximum=POLICY.unpacked_release_bytes)
            total += size
            if total > POLICY.unpacked_release_bytes:
                raise UpdateError('update_limit')
            entries.append(ArchiveEntry(name, size, _digest(item['sha256'])))
        # O(n log n), rather than pairwise comparison of up to 20000 paths.
        folded = {entry.path.casefold() for entry in entries}
        if len(folded) != len(entries):
            raise UpdateError('update_inventory')
        for path in folded:
            parts = path.split('/')
            if any('/'.join(parts[:index]) in folded for index in range(1, len(parts))):
                raise UpdateError('update_inventory')
        if total != reference.unpacked_bytes:
            raise UpdateError('update_inventory')
        return VerifiedArchiveInventory(tuple(entries), total, reference.sha256)
    except UpdateError as error:
        if error.code == 'update_limit':
            raise
        raise UpdateError('update_inventory') from None
