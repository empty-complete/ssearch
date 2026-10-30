"""Verified local staging only. This module never activates or executes payloads."""
from __future__ import annotations

from bisect import bisect_left
from contextlib import ExitStack
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import struct
import uuid
import zlib

from atom_search.runtime import _windows
from atom_search.runtime.storage import StorageBudget, StorageBudgetError

from .contracts import ArchiveEntry, StagedRelease, UpdateError, VerifiedManifest
from .inventory import verify_inventory
from .manifest import POLICY, _path
from .transports.unc import _PinnedTree, _assert_private_handle, _call, _dispose, _file_id, _kernel

CHUNK = 65536


@dataclass(frozen=True)
class _Member:
    entry: ArchiveEntry | None
    offset: int
    end: int
    data_offset: int
    compressed_size: int
    method: int
    crc: int


def _exact(stream, count):
    parts, remaining = [], count
    while remaining:
        raw = stream.read(remaining)
        if not raw:
            raise UpdateError('update_archive')
        parts.append(raw)
        remaining -= len(raw)
    return b''.join(parts)


def _zip_members(stream, inventory, size):
    """Read bounded classic ZIP records without an eager central-directory load.

    Supported profile: one disk, stored/deflate, no ZIP64, SFX, descriptors,
    extras, comments or encryption. Names/local records/spans are checked before
    any decompression. The signed whitelist bounds members, not archive metadata.
    """
    if size < 22:
        raise UpdateError('update_archive')
    stream.seek(size - 22)
    marker, disk, cd_disk, on_disk, count, cd_size, cd_offset, comment = struct.unpack('<4s4H2IH', _exact(stream, 22))
    allowed = {entry.path: entry for entry in inventory.entries}
    names = sorted(allowed)
    maximum_members = min(65534, len(names) + sum(name.count('/') for name in names))
    if (marker != b'PK\x05\x06' or disk or cd_disk or on_disk != count or comment
            or count > maximum_members or cd_size > count * (46 + 2048)
            or cd_offset + cd_size != size - 22):
        raise UpdateError('update_archive')
    stream.seek(cd_offset)
    members, seen, found = [], set(), set()
    for _ in range(count):
        if stream.tell() + 46 > cd_offset + cd_size:
            raise UpdateError('update_archive')
        record = struct.unpack('<4s6H3I5H2I', _exact(stream, 46))
        (signature, made, needed, flags, method, mtime, mdate, crc, compressed, unpacked,
         name_length, extra, entry_comment, start_disk, internal, external, offset) = record
        if (signature != b'PK\x01\x02' or made >> 8 not in (0, 3) or needed > 20
                or flags & ~0x800 or method not in (0, 8) or extra or entry_comment or start_disk
                or not 1 <= name_length <= 2048 or stream.tell() + name_length > cd_offset + cd_size
                or max(compressed, unpacked, offset) == 0xffffffff):
            raise UpdateError('update_archive')
        encoded_name = _exact(stream, name_length)
        try:
            name = encoded_name.decode('utf-8' if flags & 0x800 else 'cp437')
        except UnicodeError:
            raise UpdateError('update_archive') from None
        directory = name.endswith('/')
        try:
            canonical = _path(name[:-1] if directory else name)
        except UpdateError:
            raise UpdateError('update_archive') from None
        folded = canonical.casefold()
        mode = stat.S_IFMT(external >> 16)
        if folded in seen or external & (0x400 | 0x40 | 0x8):
            raise UpdateError('update_archive')
        seen.add(folded)
        if directory:
            index = bisect_left(names, canonical + '/')
            if (mode not in (0, stat.S_IFDIR) or compressed or unpacked or crc or method
                    or index == len(names) or not names[index].startswith(canonical + '/')):
                raise UpdateError('update_archive')
            entry = None
        else:
            entry = allowed.get(name)
            if (entry is None or mode not in (0, stat.S_IFREG) or external & 0x10
                    or unpacked != entry.size or (method == 0 and compressed != unpacked)):
                raise UpdateError('update_archive')
            found.add(name)
        central_position = stream.tell()
        if offset + 30 > cd_offset:
            raise UpdateError('update_archive')
        stream.seek(offset)
        local = struct.unpack('<4s5H3I2H', _exact(stream, 30))
        if (local != (b'PK\x03\x04', needed, flags, method, mtime, mdate, crc,
                      compressed, unpacked, name_length, 0)
                or _exact(stream, name_length) != encoded_name):
            raise UpdateError('update_archive')
        data_offset = stream.tell()
        end = data_offset + compressed
        if end > cd_offset:
            raise UpdateError('update_archive')
        members.append(_Member(entry, offset, end, data_offset, compressed, method, crc))
        stream.seek(central_position)
    if stream.tell() != cd_offset + cd_size or found != set(allowed):
        raise UpdateError('update_archive')
    position = 0
    for member in sorted(members, key=lambda item: item.offset):
        if member.offset != position:
            raise UpdateError('update_archive')
        position = member.end
    if position != cd_offset:
        raise UpdateError('update_archive')
    return tuple(member for member in members if member.entry is not None)


def _unpack_member(archive, member, emit):
    archive.seek(member.data_offset)
    remaining, produced, crc = member.compressed_size, 0, 0
    digest = hashlib.sha256()
    decoder = zlib.decompressobj(-15) if member.method == 8 else None

    def output(raw):
        nonlocal produced, crc
        if produced + len(raw) > member.entry.size:
            raise UpdateError('update_archive')
        if raw:
            emit(raw)
            produced += len(raw)
            crc = zlib.crc32(raw, crc)
            digest.update(raw)

    while remaining:
        raw = _exact(archive, min(CHUNK, remaining))
        remaining -= len(raw)
        if decoder is None:
            output(raw)
            continue
        pending = raw
        while pending:
            decoded = decoder.decompress(pending, min(CHUNK, member.entry.size - produced + 1))
            tail = decoder.unconsumed_tail
            if decoder.unused_data or (tail == pending and not decoded):
                raise UpdateError('update_archive')
            output(decoded)
            pending = tail
    if decoder is not None:
        # Do not call unbounded flush(). Drain with the same bounded API and
        # require an actual stream end, including exact compressed consumption.
        while not decoder.eof:
            decoded = decoder.decompress(b'', min(CHUNK, member.entry.size - produced + 1))
            if not decoded:
                break
            output(decoded)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise UpdateError('update_archive')
    if produced != member.entry.size or crc != member.crc or digest.hexdigest() != member.entry.sha256:
        raise UpdateError('update_archive')


def _cleanup(root, files, directories):
    """Delete only retained identity handles under the retained expected root.

    A pathname may locate an object, but never authorizes its deletion. The
    creation-time native volume/file ID and handle ACL must agree while deletion
    and all ancestor replacements remain denied. Foreign replacements survive.
    """
    try:
        with _PinnedTree(root, delete_root=True) as boundary:
            if _file_id(boundary._handle) != directories.get(root):
                return
            _assert_private_handle(boundary._handle)

            def remove(path, identity, directory):
                if path == root or not path.is_relative_to(root):
                    return
                try:
                    with boundary.retained_delete(path.relative_to(root).as_posix(), directory=directory,
                                                  expected_parents=directories) as handle:
                        if _file_id(handle) != identity:
                            return
                        _assert_private_handle(handle)
                        _dispose(handle)
                except (UpdateError, OSError, _windows.WindowsError):
                    pass  # Do not act on missing, occupied or replaced objects.

            for path, identity in reversed(list(files.items())):
                remove(path, identity, False)
            for path, identity in sorted(directories.items(), key=lambda item: len(item[0].parts), reverse=True):
                remove(path, identity, True)
            # Nonempty/foreign entries make disposition fail rather than recurse.
            _dispose(boundary._handle)
    except (UpdateError, OSError, _windows.WindowsError):
        pass


def _cluster_bytes(root):
    sectors, per_sector, free, total = (w.DWORD() for _ in range(4))
    if not _call(_kernel(), 'GetDiskFreeSpaceW', [w.LPCWSTR, *[c.POINTER(w.DWORD)] * 4])(
            str(root.anchor), c.byref(sectors), c.byref(per_sector), c.byref(free), c.byref(total)):
        raise UpdateError('update_storage')
    value = sectors.value * per_sector.value
    if not value:
        raise UpdateError('update_storage')
    return value


def _rounded(size, cluster):
    return ((size + cluster - 1) // cluster) * cluster


class _Allocation:
    def __init__(self, reservation, remaining, cluster, files, cancelled):
        self.reservation, self.remaining, self.cluster = reservation, remaining, cluster
        self.files, self.cancelled = files, cancelled

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise UpdateError('update_cancelled')
        self.reservation.ensure_remaining(self.remaining)

    def write_file(self, tree, relative, producer):
        self.check()
        with tree.open_file(relative, write=True) as output:
            import msvcrt
            path = tree.directory / relative
            self.files[path] = _file_id(msvcrt.get_osfhandle(output.fileno()))
            consumed = 0

            def emit(raw):
                nonlocal consumed
                self.check()
                if os.fstat(output.fileno()).st_nlink != 1:
                    raise UpdateError('update_path')
                try:
                    if output.write(raw) != len(raw):
                        raise OSError()
                except OSError:
                    raise UpdateError('update_storage') from None
                next_size = consumed + len(raw)
                self.remaining -= _rounded(next_size, self.cluster) - _rounded(consumed, self.cluster)
                consumed = next_size

            producer(emit)
            try:
                output.flush()
                os.fsync(output.fileno())
            except OSError:
                raise UpdateError('update_storage') from None
            self.check()
            if os.fstat(output.fileno()).st_nlink != 1:
                raise UpdateError('update_path')
            _windows.assert_private(path)
            return path


def _copy_verified(source, size, digest, emit):
    remaining, actual = size, hashlib.sha256()
    while True:
        raw = source.read(min(CHUNK, remaining + 1))
        if not raw:
            break
        if len(raw) > remaining:
            raise UpdateError('update_artifact')
        emit(raw)
        actual.update(raw)
        remaining -= len(raw)
    if remaining or actual.hexdigest() != digest:
        raise UpdateError('update_artifact')


def _inventories(manifest, transport):
    inventories, raw_values, destinations, app = {}, {}, [], None
    for artifact in manifest.artifacts:
        if artifact.format == 'zip':
            if artifact.inventory is None:
                raise UpdateError('update_inventory')
            raw = transport.read_inventory(manifest, artifact.id, max_bytes=artifact.inventory.size)
            value = verify_inventory(artifact.inventory, raw)
            inventories[artifact.id], raw_values[artifact.id] = value, raw
            destinations.extend(value.entries)
            if artifact.kind == 'app':
                app = value
        elif artifact.format == 'file' and artifact.inventory is None:
            destinations.append(ArchiveEntry(artifact.relative_path, artifact.size, artifact.sha256))
        else:
            raise UpdateError('update_manifest')
    folded = {entry.path.casefold() for entry in destinations}
    if len(folded) != len(destinations):
        raise UpdateError('update_inventory')
    for path in folded:
        parts = path.split('/')
        if any('/'.join(parts[:index]) in folded for index in range(1, len(parts))):
            raise UpdateError('update_inventory')
    if sum(entry.size for entry in destinations) > POLICY.unpacked_release_bytes:
        raise UpdateError('update_limit')
    if app is not None:
        entries = {entry.path: entry for entry in app.entries}
        for component in (manifest.executables.desktop, manifest.executables.service):
            entry = entries.get(component.path)
            if entry is None or entry.sha256 != component.sha256:
                raise UpdateError('update_inventory')
    return inventories, raw_values, destinations


def stage_release(manifest, transport, *, staging_dir: Path, budget: StorageBudget,
                  headroom_bytes: int = 0, cancelled=None) -> StagedRelease:
    if not isinstance(manifest, VerifiedManifest) or not isinstance(budget, StorageBudget):
        raise UpdateError('update_manifest')
    if type(headroom_bytes) is not int or not 0 <= headroom_bytes <= 2**63 - 1:
        raise UpdateError('update_limit')
    if cancelled is not None and not callable(cancelled):
        raise UpdateError('update_cancelled')
    files, directories, operation = {}, {}, None
    try:
        requested = _windows.reject_reparse(Path(staging_dir)).resolve()
        owned = next((root for root in budget.roots if requested != root and requested.is_relative_to(root)), None)
        if owned is None or not owned.is_dir():
            raise UpdateError('update_path')
        source_root = getattr(transport, 'source_root', None)
        if source_root is not None:
            source_root = _windows.reject_reparse(source_root).resolve()
            if requested.is_relative_to(source_root) or source_root.is_relative_to(requested):
                raise UpdateError('update_path')
        if cancelled is not None and cancelled():
            raise UpdateError('update_cancelled')
        inventories, raw_values, destinations = _inventories(manifest, transport)
        cluster = _cluster_bytes(owned)
        # Physical file allocation is rounded per file, not per release. Already
        # existing installation/PGDATA remain solely in the shared P0 measurement.
        future = sum(_rounded(entry.size, cluster) for entry in destinations)
        future += sum(_rounded(item.size, cluster) + _rounded(item.inventory.size, cluster)
                      for item in manifest.artifacts if item.format == 'zip')
        parent_count, previous = 0, []
        for name in sorted(entry.path.casefold() for entry in destinations):
            parents = name.split('/')[:-1]
            common = 0
            while common < min(len(previous), len(parents)) and previous[common] == parents[common]:
                common += 1
            parent_count += len(parents) - common
            previous = parents
        metadata_headroom = 65536 + cluster * (parent_count + 4 + len(requested.relative_to(owned).parts))
        peak = future + metadata_headroom + headroom_bytes
        operation = requested / ('ssearch-stage-' + uuid.uuid4().hex)

        def created(path, identity):
            if path == operation or path.is_relative_to(operation):
                directories[path] = identity

        with budget.reserve(peak, 'ssearch-update-stage') as reservation:
            allocation = _Allocation(reservation, peak, cluster, files, cancelled)
            allocation.check()
            with ExitStack() as stack:
                boundary = stack.enter_context(_PinnedTree(owned, on_create=created))
                staging = stack.enter_context(boundary.subtree(requested.relative_to(owned).as_posix()))
                work = stack.enter_context(staging.subtree(operation.name, exclusive=True))
                incoming = stack.enter_context(work.subtree('incoming', exclusive=True))
                payload = stack.enter_context(work.subtree('payload', exclusive=True))
                allocation.check()
                result_files = []
                # All inventories and executable/global membership have passed.
                for artifact in manifest.artifacts:
                    allocation.check()
                    if artifact.format == 'zip':
                        raw = raw_values[artifact.id]
                        allocation.write_file(incoming, artifact.id + '.inventory', lambda emit, raw=raw: emit(raw))
                        with transport.open_artifact(manifest, artifact.id) as source:
                            archive_path = allocation.write_file(incoming, artifact.id + '.zip',
                                lambda emit: _copy_verified(source, artifact.size, artifact.sha256, emit))
                        with incoming.open_file(archive_path.name) as archive:
                            members = _zip_members(archive, inventories[artifact.id], artifact.size)
                            for member in members:
                                allocation.check()
                                result_files.append(allocation.write_file(payload, member.entry.path,
                                    lambda emit, member=member: _unpack_member(archive, member, emit)))
                    else:
                        with transport.open_artifact(manifest, artifact.id) as source:
                            result_files.append(allocation.write_file(payload, artifact.relative_path,
                                lambda emit: _copy_verified(source, artifact.size, artifact.sha256, emit)))
                allocation.check()
                result = StagedRelease(manifest, payload.directory, tuple(result_files))
            cache_files = {path: identity for path, identity in files.items() if path.parent == operation / 'incoming'}
            # Include the expected operation-root ID as cleanup authority, but
            # successful payload files keep that root nonempty and undeletable.
            cache_dirs = {path: identity for path, identity in directories.items()
                          if path in (operation, operation / 'incoming')}
            _cleanup(operation, cache_files, cache_dirs)
            return result
    except BaseException as error:
        if operation is not None:
            _cleanup(operation, files, directories)
        if isinstance(error, UpdateError):
            raise
        if isinstance(error, StorageBudgetError):
            raise UpdateError('update_storage') from None
        if isinstance(error, _windows.WindowsError):
            raise UpdateError('update_path') from None
        if isinstance(error, zlib.error):
            raise UpdateError('update_archive') from None
        if isinstance(error, OSError):
            raise UpdateError('update_read') from None
        raise
