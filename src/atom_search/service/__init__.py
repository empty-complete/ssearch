"""Shared application contracts; importing them does not start any resources."""

__all__ = ('ServiceOwner', 'open_service_owner', 'AtomSearchService', 'ServiceClient')


def __getattr__(name):
    if name in ('ServiceOwner', 'open_service_owner'):
        from . import owner
        return getattr(owner, name)
    if name == 'AtomSearchService':
        from .application import AtomSearchService
        return AtomSearchService
    if name == 'ServiceClient':
        from .client import ServiceClient
        return ServiceClient
    raise AttributeError(name)
