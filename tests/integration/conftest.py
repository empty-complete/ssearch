"""Owned real runtime for catalog tests; never connect to ambient PostgreSQL."""
from pathlib import Path

import pytest

from atom_search.runtime.postgres import PostgresRuntime


@pytest.fixture(scope="module")
def pg_runtime(tmp_path_factory, request):
    runtime = PostgresRuntime(Path(__file__).resolve().parents[2] / "resources/postgresql",
                              tmp_path_factory.mktemp("pg-catalog") / "private")
    connections = []

    def cleanup():
        try:
            for connection in connections:
                connection.close()
        finally:
            runtime.stop()

    request.addfinalizer(cleanup)  # Own failures during start(), too.
    runtime.start()
    return runtime, connections


@pytest.fixture
def pg_connections(pg_runtime):
    runtime, connections = pg_runtime

    def factory(role):
        def connect():
            connection = runtime.open_connection(role=role)
            connections.append(connection)
            return connection
        return connect

    factories = {role: factory(role) for role in ("maintenance", "application")}
    with factories["maintenance"]() as connection:
        connection.execute("DROP SCHEMA IF EXISTS atom_search CASCADE")
    try:
        yield factories
    finally:
        for connection in connections:
            connection.close()
        connections.clear()
        with factories["maintenance"]() as connection:
            connection.execute("DROP SCHEMA IF EXISTS atom_search CASCADE")


@pytest.fixture
def pg_catalog(pg_connections):
    from atom_search_core.storage.migrations import apply_catalog_migrations
    from atom_search_core.storage.postgres import PostgresCatalog
    from atom_search_core.models import EmbeddingModel

    apply_catalog_migrations(pg_connections["maintenance"])
    catalog = PostgresCatalog(pg_connections["application"], model=EmbeddingModel(
        "fixture-model|filename-parent-v1", 384, "filename-parent-v1", "a" * 64))
    try:
        yield catalog
    finally:
        catalog.close()
