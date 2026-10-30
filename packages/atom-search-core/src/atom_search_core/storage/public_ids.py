"""Durable random UUIDs; bigint identifiers remain internal to storage ports."""
from uuid import UUID

from ..models import SearchScope
from ..source import canonical_root


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('invalid_public_uuid')
    return value


def ensure_file_id(connection, file_id):
    """Called inside the metadata/import transaction, never from a read path."""
    connection.execute("""INSERT INTO atom_search.file_public_ids(file_id) VALUES (%s)
        ON CONFLICT(file_id) DO NOTHING""", (file_id,))


def transfer_catalog_ids(connection, old_catalog, new_catalog):
    """Maintenance activation keeps identities only for the same root/path.

    The active catalog row is already locked by activate_catalog(). Replacing
    staging UUIDs and transferring existing bindings is part of that transaction,
    so an activation failure restores both catalogs and every public identity.
    """
    # The server joins existing unique source/path keys. No source/file map is
    # materialized in Python, and statement count is independent of catalog size.
    connection.execute("""DELETE FROM atom_search.file_public_ids target
        USING atom_search.files incoming,atom_search.sources incoming_source,
              atom_search.files outgoing,atom_search.sources outgoing_source,
              atom_search.source_public_ids source_identity,atom_search.file_public_ids original
        WHERE target.file_id=incoming.id AND incoming.source_id=incoming_source.id
          AND outgoing.source_id=outgoing_source.id AND original.file_id=outgoing.id
          AND source_identity.source_id=outgoing_source.id
          AND incoming_source.catalog_id=%s AND outgoing_source.catalog_id=%s
          AND incoming_source.path=outgoing_source.path AND incoming.relative_path=outgoing.relative_path""",
        (new_catalog, old_catalog))
    connection.execute("""UPDATE atom_search.file_public_ids identity SET file_id=incoming.id
        FROM atom_search.files incoming,atom_search.sources incoming_source,
             atom_search.files outgoing,atom_search.sources outgoing_source,
             atom_search.source_public_ids source_identity
        WHERE identity.file_id=outgoing.id AND outgoing.source_id=outgoing_source.id
          AND incoming.source_id=incoming_source.id AND source_identity.source_id=outgoing_source.id
          AND incoming_source.catalog_id=%s AND outgoing_source.catalog_id=%s
          AND incoming_source.path=outgoing_source.path AND incoming.relative_path=outgoing.relative_path""",
        (new_catalog, old_catalog))
    connection.execute("""UPDATE atom_search.source_public_ids identity SET source_id=incoming.id
        FROM atom_search.sources outgoing,atom_search.sources incoming
        WHERE identity.source_id=outgoing.id AND outgoing.catalog_id=%s AND incoming.catalog_id=%s
          AND incoming.path=outgoing.path""", (old_catalog, new_catalog))


class PostgresPublicIds:
    def __init__(self, catalog):
        # Reuse the catalog's closed-state/connection lock and active-row order.
        self.catalog = catalog

    def bind_many(self, sources):
        sources = tuple((_uuid(identity), canonical_root(root)) for identity, root in sources)
        with self.catalog._connection() as connection:
            active = self.catalog._active(connection)
            result = []
            for identity, root in sources:
                bound = connection.execute("""SELECT s.id,s.catalog_id,s.path
                    FROM atom_search.source_public_ids p JOIN atom_search.sources s ON s.id=p.source_id
                    WHERE p.source_uuid=%s::uuid""", (identity,)).fetchone()
                if bound is not None:
                    if bound[1:] != (active, root):
                        raise ValueError('source_binding_conflict')
                    result.append(bound[0]); continue
                source = connection.execute("""INSERT INTO atom_search.sources(catalog_id,path,status)
                    VALUES (%s,%s,'new') ON CONFLICT(catalog_id,path) DO UPDATE SET path=excluded.path
                    RETURNING id""", (active, root)).fetchone()[0]
                existing = connection.execute('SELECT source_uuid::text FROM atom_search.source_public_ids WHERE source_id=%s',
                                              (source,)).fetchone()
                if existing is not None and existing[0] != identity:
                    raise ValueError('source_binding_conflict')
                connection.execute('INSERT INTO atom_search.source_public_ids(source_uuid,source_id) VALUES (%s::uuid,%s)',
                                   (identity, source))
                result.append(source)
            return tuple(result)

    def source_uuid(self, root):
        with self.catalog._connection() as connection:
            row = connection.execute("""SELECT p.source_uuid::text FROM atom_search.source_public_ids p
                JOIN atom_search.sources s ON s.id=p.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND s.path=%s""", (canonical_root(root),)).fetchone()
            return row[0] if row is not None else None

    def resolve_sources(self, identities):
        identities = tuple(_uuid(identity) for identity in identities)
        if not identities:
            return SearchScope(())
        with self.catalog._connection() as connection:
            rows = connection.execute("""SELECT p.source_uuid::text,s.id FROM atom_search.source_public_ids p
                JOIN atom_search.sources s ON s.id=p.source_id JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND p.source_uuid=ANY(%s::uuid[])""", (list(identities),)).fetchall()
            mapping = dict(rows)
            return SearchScope(tuple(mapping[identity] for identity in identities if identity in mapping))

    def public_file_id(self, file_id):
        from .postgres import _integer
        _integer(file_id)
        with self.catalog._connection() as connection:
            row = connection.execute("""SELECT p.file_uuid::text FROM atom_search.file_public_ids p
                JOIN atom_search.files f ON f.id=p.file_id JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id WHERE c.state='active' AND f.id=%s""", (file_id,)).fetchone()
            return row[0] if row is not None else None

    def metadata_ids(self, files, *, scope):
        """Read existing file/source UUID pairs for one captured result cohort.

        Match both the internal ID and its captured root: a file moved between
        retrieval and materialization cannot acquire another source's identity.
        Missing/retired/out-of-scope mappings are omitted, never created here.
        """
        from .postgres import _integer
        if not isinstance(scope, SearchScope):
            raise ValueError('invalid_search_scope')
        requested = tuple((_integer(identity), canonical_root(root)) for identity, root in files)
        sources = [_integer(identity) for identity in scope.source_ids]
        if not requested or not sources:
            return {}
        with self.catalog._connection() as connection:
            rows = connection.execute("""SELECT f.id,p.file_uuid::text,sp.source_uuid::text
                FROM unnest(%s::bigint[],%s::text[]) requested(file_id,root)
                JOIN atom_search.files f ON f.id=requested.file_id
                JOIN atom_search.sources s ON s.id=f.source_id AND s.path=requested.root
                JOIN atom_search.catalogs c ON c.id=s.catalog_id AND c.state='active'
                JOIN atom_search.file_public_ids p ON p.file_id=f.id
                JOIN atom_search.source_public_ids sp ON sp.source_id=s.id
                WHERE s.id=ANY(%s::bigint[])""",
                ([identity for identity, _ in requested], [root for _, root in requested], sources)).fetchall()
        return {identity: (file_uuid, source_uuid) for identity, file_uuid, source_uuid in rows}

    def internal_file_id(self, result_id, *, scope):
        from .postgres import _integer
        try:
            _uuid(result_id)
        except (ValueError, TypeError, AttributeError):
            return None
        ids = [_integer(identity) for identity in scope.source_ids]
        if not ids:
            return None
        with self.catalog._connection() as connection:
            row = connection.execute("""SELECT f.id FROM atom_search.file_public_ids p
                JOIN atom_search.files f ON f.id=p.file_id JOIN atom_search.sources s ON s.id=f.source_id
                JOIN atom_search.catalogs c ON c.id=s.catalog_id
                WHERE c.state='active' AND p.file_uuid=%s::uuid AND s.id=ANY(%s)""", (result_id, ids)).fetchone()
            return row[0] if row is not None else None
