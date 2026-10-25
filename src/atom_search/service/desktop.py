"""Temporary native-shell adapter over the same owner, not another runtime."""
from dataclasses import asdict
import threading

from atom_search_core.models import SearchResult
from atom_search_core.source import canonical_root
from atom_search.http.security import error
from .client import ServiceClient


def compatible_operation(owner, method, arguments, *, progress=None, cancel=None):
    root = arguments.get('root')
    if method == 'desktop_status':
        return owner.health.desktop_snapshot(canonical_root(root) if root is not None else None)
    if method in ('desktop_search', 'desktop_scan') and not any(
            item['enabled'] for item in owner.repository.snapshot().settings['sources']):
        if method == 'desktop_search':
            return []
        raise error('source_scope_denied', 403)
    if root is not None:
        configured = owner.repository.snapshot().settings['sources']
        source = next((item for item in configured if item['enabled'] and canonical_root(item['path']) == canonical_root(root)), None)
        if source is None:
            raise error('source_scope_denied', 403)
        owner.registry.resolve((source['id'],), allowed=(source['id'],))
    if method == 'desktop_search':
        return owner.core.search(arguments['query'], limit=arguments.get('limit', 100), root=root)
    if method == 'desktop_scan':
        # Manual refresh forms one wave for all enabled work sources. The
        # current shell still receives its selected source's metadata report.
        sources = owner.repository.snapshot().settings['sources']
        identities = tuple(item['id'] for item in sources if item['enabled'])
        jobs = owner.scheduler.start_catalog(identities)
        target = next(job for identity, job in zip(identities, jobs) if identity == source['id'])
        report = owner.scheduler.wait_catalog(target, progress=progress, cancel=cancel)
        if report['status'] != 'cancelled':
            for identity in jobs:
                if identity != target:
                    neighbor = owner.scheduler.wait_catalog(identity, cancel=cancel, cancel_job=False)
                    if neighbor['status'] == 'cancelled' and cancel is not None and cancel.is_set():
                        return dict(report, status='cancelled')
        return report
    if method == 'desktop_embed':
        return owner.scheduler.wait_embeddings(progress=progress, cancel=cancel)
    if method == 'desktop_select_source':
        return owner.registry.select_source_id(arguments['path'])
    if method == 'desktop_runtime_state':
        return owner.repository.runtime_state()
    if method == 'desktop_save_runtime_state':
        return owner.repository.save_runtime_state(**arguments)
    raise error('route_not_found', 404)


class DesktopView:
    def __init__(self, owner):
        self.owner = owner

    def search(self, query, limit=100, *, root=None):
        with self.owner.operation():
            return compatible_operation(self.owner, 'desktop_search', dict(query=query, limit=limit, root=root))

    def status(self, *, root=None):
        with self.owner.operation():
            return compatible_operation(self.owner, 'desktop_status', dict(root=root))

    def scan(self, root, progress=None, cancel=None):
        cancel = cancel if cancel is not None else threading.Event()
        with self.owner.operation(cancel):
            return compatible_operation(self.owner, 'desktop_scan', dict(root=root), progress=progress, cancel=cancel)

    def index_embeddings(self, *, progress=None, cancel=None):
        cancel = cancel if cancel is not None else threading.Event()
        with self.owner.operation(cancel):
            return compatible_operation(self.owner, 'desktop_embed', {}, progress=progress, cancel=cancel)

    def close(self):
        pass


class RemoteDesktopView:
    def __init__(self, client):
        self.client = client

    def search(self, query, limit=100, *, root=None):
        return [SearchResult(**item) for item in self.client._ipc('desktop_search', dict(query=query, limit=limit, root=root))]

    def status(self, *, root=None):
        return self.client._ipc('desktop_status', dict(root=root))

    def scan(self, root, progress=None, cancel=None):
        from .ipc import stream_request
        self.client._identity()
        return stream_request(self.client.descriptor, self.client._secret(), 'desktop_scan', dict(root=root), progress, cancel)

    def index_embeddings(self, *, progress=None, cancel=None):
        from .ipc import stream_request
        self.client._identity()
        return stream_request(self.client.descriptor, self.client._secret(), 'desktop_embed', {}, progress, cancel)

    def close(self):
        pass


class RemoteRepository:
    def __init__(self, repository, client):
        self._local, self.client = repository, client

    def __getattr__(self, name):
        return getattr(self._local, name)

    def snapshot(self):
        return self.client.settings_get()

    def runtime_state(self):
        return self.client._ipc('desktop_runtime_state', {})

    def save_runtime_state(self, **arguments):
        return self.client._ipc('desktop_save_runtime_state', arguments)


class RemoteCoordinator:
    def __init__(self, client):
        self.client = client

    def validate(self, candidate, expected_revision):
        return self.client.settings_validate(candidate, expected_revision)

    def owner_apply(self, candidate, expected_revision):
        return self.client.settings_apply(candidate, expected_revision)


class RemoteRegistry:
    def __init__(self, client):
        self.client = client

    def activate(self):
        pass

    def select_source_id(self, root):
        return self.client._ipc('desktop_select_source', {'path': root})
