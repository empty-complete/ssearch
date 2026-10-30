"""Explicit offline learning provider; does not create a working service owner."""
from .contracts import CleanupResult, DemoSession, ReadonlySearchProvider
from .demo_files import close_demo_session, open_demo_session

__all__ = ('CleanupResult', 'DemoSession', 'ReadonlySearchProvider', 'open_demo_session', 'close_demo_session')
