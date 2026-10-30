"""Disposable signed release bytes. Nothing here is an executable or real share."""
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import zipfile

from atom_search.runtime import _windows
from atom_search.runtime.storage import StorageBudget
from atom_search.updates.contracts import UpdateVersions
from atom_search.updates.manifest import ManifestVerifier
from atom_search.updates.trust import ReplayLedger, TrustStore
from test_update_manifest import NOW, encode, payload, versions


def api(name):
    target = 'atom_search.updates.' + name
    try:
        present = importlib.util.find_spec(target)
    except ModuleNotFoundError:
        present = None
    assert present is not None, f'bounded update {name} is not implemented'
    return importlib.import_module(target)


class FixtureRelease:
    def __init__(self, tmp_path, key):
        self.key = key
        self.base = tmp_path
        self.source = tmp_path / 'fixture-source'
        self.source.mkdir()
        self.owned = _windows.private_directory(tmp_path / 'private')
        self.data = _windows.private_directory(self.owned / 'data')
        self.staging = self.data / 'staging'
        self.budget = StorageBudget((self.data,), quota_bytes=128 * 1024 * 1024, min_free_bytes=0)
        self.files = {'desktop/ssearch.exe': b'fictional desktop bytes',
                      'service/ssearch-service.exe': b'fictional service bytes'}
        self.document = deepcopy(payload())
        self.inventory = b''
        self.archive = b''
        self.rebuild()
        trusted = tmp_path / 'fixture-trust.json'
        trusted.write_bytes(encode(key.trust_document()))
        self.checker = ManifestVerifier(TrustStore.from_file(trusted), ReplayLedger(self.owned / 'replay.json'))

    def rebuild(self, *, inventory_entries=None, zip_entries=None):
        entries = inventory_entries if inventory_entries is not None else [
            dict(path=path, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            for path, raw in self.files.items()]
        self.inventory = encode(dict(format_version=1, entries=entries))
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path, raw in (zip_entries if zip_entries is not None else list(self.files.items())):
                entry = zipfile.ZipInfo(path)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | 0o600) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, raw)
        self.archive = output.getvalue()
        artifact = self.document['artifacts'][0]
        artifact.update(size=len(self.archive), sha256=hashlib.sha256(self.archive).hexdigest())
        artifact['inventory'].update(size=len(self.inventory), sha256=hashlib.sha256(self.inventory).hexdigest(),
                                     entry_count=len(entries), unpacked_bytes=sum(entry['size'] for entry in entries))
        for component in ('desktop', 'service'):
            value = self.document['executables'][component]
            value['sha256'] = hashlib.sha256(self.files[value['path']]).hexdigest()
        self.write()

    def write(self):
        artifact = self.document['artifacts'][0]
        for path, raw in ((artifact['relative_path'], self.archive),
                          (artifact['inventory']['relative_path'], self.inventory),
                          ('update-manifest.json', self.key.envelope(encode(self.document)))):
            destination = self.source / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)

    def verified(self):
        return self.checker.verify(self.key.envelope(encode(self.document)), channel='stable',
                                   installed=UpdateVersions(**versions()), now=NOW)

    def transport(self):
        return api('transports.unc').UNCTransport(self.source, fixture_mode=True)

    def source_bytes(self):
        return {str(path.relative_to(self.source)): path.read_bytes()
                for path in self.source.rglob('*') if path.is_file()}


def load_helper():
    return Path(__file__)
