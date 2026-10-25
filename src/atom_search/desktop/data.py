"""Prepare the settings container; the PG owner admits all legacy snapshots."""
from pathlib import Path

from atom_search.content import message
from atom_search.paths import data_path


class DataMigrationError(RuntimeError):
    """Profile preparation failed without changing legacy data."""


def initialize_data_directory(explicit: Path | None = None, *, model_dir: Path | None = None) -> Path:
    directory = Path(explicit) if explicit is not None else data_path()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Owner JSON migration/validation follows in the window before the
        # asynchronous PG factory. Keep this legacy discovery non-destructive.
        # Only ordinary startup may adopt old settings, through the same owner.
        if (explicit is None and (directory.parent / 'KaizenSearch/settings.json').exists()
                and all(p.name == 'engine' for p in directory.iterdir())):
            from atom_search.search_runtime import _prepare_profile
            from atom_search.paths import model_path
            with _prepare_profile(directory, model_dir or model_path(), discover_legacy=True):
                pass
    except Exception as error:
        raise DataMigrationError(message("migration.failure", legacy=directory.parent / "KaizenSearch",
                                         target=directory, error=error)) from error
    return directory
