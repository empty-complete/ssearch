"""Resource and per-user paths, independent of the desktop toolkit."""
import os
from pathlib import Path
import sys


def _bundle_root() -> Path:
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def resource_path(*parts: str) -> Path:
    return _bundle_root().joinpath('resources', *parts)


def model_path() -> Path:
    return _bundle_root() / 'models'


def data_path() -> Path:
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share')))
    return base / 'AtomSearch'
