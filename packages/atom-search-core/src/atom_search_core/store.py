"""Per-operation SQLite connections; directory-scoped, atomic reconciliation."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .search import compact, normalize, query_terms


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class CatalogStore:
    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            if connection.execute('PRAGMA user_version').fetchone()[0] > 2:
                raise ValueError('Индекс создан более новой версией приложения.')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS roots (
                  id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                  last_attempt TEXT, last_success TEXT, status TEXT NOT NULL DEFAULT 'new', error TEXT);
                CREATE TABLE IF NOT EXISTS files (
                  id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES roots(id),
                  relative_path TEXT NOT NULL, parent TEXT NOT NULL, name TEXT NOT NULL,
                  size INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                  compact_name TEXT NOT NULL, compact_path TEXT NOT NULL, compact_stem TEXT NOT NULL,
                  embedding BLOB, embedding_model TEXT, input_hash TEXT,
                  UNIQUE(root_id, relative_path));
                CREATE INDEX IF NOT EXISTS files_parent ON files(root_id,parent);
                CREATE TABLE IF NOT EXISTS directories (
                  root_id INTEGER NOT NULL REFERENCES roots(id), relative_path TEXT NOT NULL,
                  parent TEXT NOT NULL, PRIMARY KEY(root_id,relative_path));
                CREATE INDEX IF NOT EXISTS directories_parent ON directories(root_id,parent);
                CREATE VIRTUAL TABLE IF NOT EXISTS file_search USING fts5(name,relative_path);
                CREATE TABLE IF NOT EXISTS catalog_state (name TEXT PRIMARY KEY,value INTEGER NOT NULL);
                INSERT OR IGNORE INTO catalog_state VALUES ('generation',0);
            ''')
            # Serialize upgrades so two application instances cannot migrate the same index.
            connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(files)')}
            if 'compact_stem' not in columns:
                connection.execute("ALTER TABLE files ADD COLUMN compact_stem TEXT NOT NULL DEFAULT ''")
            if version < 2:
                cursor = connection.execute('SELECT id,name FROM files ORDER BY id')
                while rows := cursor.fetchmany(1000):
                    connection.executemany('UPDATE files SET compact_stem=? WHERE id=?',
                                           ((compact(row['name'].rsplit('.', 1)[0]), row['id'])
                                            for row in rows))
            connection.execute('CREATE INDEX IF NOT EXISTS files_compact_stem ON files(compact_stem)')
            connection.execute('CREATE INDEX IF NOT EXISTS files_compact_name ON files(compact_name)')
            connection.execute('PRAGMA user_version=2')

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin_root(self, root: str) -> int:
        with self.connection() as connection:
            connection.execute('''INSERT INTO roots(path,last_attempt,status) VALUES (?,?,'scanning')
                ON CONFLICT(path) DO UPDATE SET last_attempt=excluded.last_attempt,status='scanning' ''',
                               (root, utc_now()))
            self._bump(connection)
            return connection.execute('SELECT id FROM roots WHERE path=?', (root,)).fetchone()[0]

    def finish_root(self, root_id: int, status: str, message: str = ''):
        with self.connection() as connection:
            connection.execute('''UPDATE roots SET status=?,error=?,
                last_success=CASE WHEN ?='complete' THEN ? ELSE last_success END WHERE id=?''',
                               (status, message, status, utc_now(), root_id))
            self._bump(connection)

    @staticmethod
    def _bump(connection):
        connection.execute("UPDATE catalog_state SET value=value+1 WHERE name='generation'")

    @staticmethod
    def _remove_ids(connection, ids):
        connection.executemany('DELETE FROM file_search WHERE rowid=?', ((value,) for value in ids))
        connection.executemany('DELETE FROM files WHERE id=?', ((value,) for value in ids))

    def reconcile(self, root_id: int, snapshot) -> tuple[int, int, int]:
        added, updated, removed = 0, 0, 0
        with self.connection() as connection:
            existing = {row['name']: row for row in connection.execute(
                'SELECT * FROM files WHERE root_id=? AND parent=?', (root_id, snapshot.relative_path))}
            names = {item.name for item in snapshot.files}
            for item in snapshot.files:
                old = existing.get(item.name)
                if old is None:
                    cursor = connection.execute('''INSERT INTO files(root_id,relative_path,parent,name,
                        size,modified_ns,compact_name,compact_path,compact_stem) VALUES (?,?,?,?,?,?,?,?,?)''',
                        (root_id, item.relative_path, snapshot.relative_path, item.name, item.size,
                         item.modified_ns, compact(item.name), compact(item.relative_path),
                         compact(item.name.rsplit('.', 1)[0])))
                    connection.execute('INSERT INTO file_search(rowid,name,relative_path) VALUES (?,?,?)',
                                       (cursor.lastrowid, normalize(item.name), normalize(item.relative_path)))
                    added += 1
                elif (old['size'], old['modified_ns']) != (item.size, item.modified_ns):
                    connection.execute('UPDATE files SET size=?,modified_ns=? WHERE id=?',
                                       (item.size, item.modified_ns, old['id']))
                    updated += 1
            if snapshot.complete:
                missing = [row['id'] for name, row in existing.items()
                           if name not in names and name not in snapshot.protected_names]
                self._remove_ids(connection, missing)
                removed += len(missing)
                old_dirs = list(connection.execute(
                    'SELECT relative_path FROM directories WHERE root_id=? AND parent=?',
                    (root_id, snapshot.relative_path)))
                protected = set(snapshot.directories) | set(snapshot.protected_names)
                for row in old_dirs:
                    if row['relative_path'].rsplit('/', 1)[-1] in protected:
                        continue
                    prefix = row['relative_path'] + '/'
                    dead = [entry[0] for entry in connection.execute(
                        'SELECT id FROM files WHERE root_id=? AND substr(relative_path,1,?)=?',
                        (root_id, len(prefix), prefix))]
                    self._remove_ids(connection, dead)
                    removed += len(dead)
                    connection.execute('''DELETE FROM directories WHERE root_id=? AND
                        (relative_path=? OR substr(relative_path,1,?)=?)''',
                        (root_id, row['relative_path'], len(prefix), prefix))
            for name in snapshot.directories:
                relative = f'{snapshot.relative_path}/{name}' if snapshot.relative_path else name
                connection.execute('INSERT OR IGNORE INTO directories VALUES (?,?,?)',
                                   (root_id, relative, snapshot.relative_path))
            if added or updated or removed:
                self._bump(connection)
        return added, updated, removed

    def generation(self) -> int:
        with self.connection() as connection:
            return connection.execute("SELECT value FROM catalog_state WHERE name='generation'").fetchone()[0]

    def pending_embeddings(self, model_id: str, dimension: int, limit: int = 32, *, source_ids=None):
        if source_ids == ():
            return []
        predicate = '' if source_ids is None else ' AND root_id IN (' + ','.join('?' for _ in source_ids) + ')'
        with self.connection() as connection:
            return list(connection.execute(f'''SELECT id,root_id,relative_path,name FROM files
                WHERE (embedding IS NULL OR embedding_model IS NULL OR embedding_model!=?
                OR length(embedding)!=? OR input_hash IS NULL){predicate} ORDER BY id LIMIT ?''',
                (model_id, dimension * 4, *(source_ids or ()), limit)))

    def save_embeddings(self, records, model_id: str) -> int:
        with self.connection() as connection:
            cursor = connection.executemany('''UPDATE files SET embedding=?,embedding_model=?,input_hash=?
                WHERE id=? AND root_id=? AND relative_path=?''',
                ((blob, model_id, digest, row['id'], row['root_id'], row['relative_path'])
                 for row, blob, digest in records))
            self._bump(connection)
            return cursor.rowcount

    def vector_rows(self, model_id: str, dimension: int, *, root: str | None = None):
        scope = ' AND r.path=?' if root is not None else ''
        parameters = (model_id, dimension * 4) + ((root,) if root is not None else ())
        with self.connection() as connection:
            return list(connection.execute(f'''SELECT f.*,r.path root,r.status root_status
                FROM files f JOIN roots r ON r.id=f.root_id
                WHERE f.embedding_model=? AND length(f.embedding)=?{scope} ORDER BY f.id''',
                parameters))

    def candidates(self, query: str, limit: int = 1000, *, root: str | None = None):
        terms = query_terms(query)[:24]
        if not terms:
            return []
        expression = ' OR '.join('"' + term + '"*' for term in terms)
        scope = ' AND r.path=?' if root is not None else ''
        parameters = (root,) if root is not None else ()
        with self.connection() as connection:
            # Exact stems and full filenames must survive broad prefix/substring caps.
            exact_target = ''.join(terms)
            ids = [row[0] for row in connection.execute(f'''SELECT f.id FROM files f
                JOIN roots r ON r.id=f.root_id
                WHERE (f.compact_name=? OR f.compact_stem=?){scope} ORDER BY f.id LIMIT ?''',
                (exact_target, exact_target) + parameters + (limit,))]
            ids += [row[0] for row in connection.execute(f'''SELECT file_search.rowid FROM file_search
                JOIN files f ON f.id=file_search.rowid JOIN roots r ON r.id=f.root_id
                WHERE file_search MATCH ?{scope} ORDER BY bm25(file_search,4.0,1.0) LIMIT ?''',
                (expression,) + parameters + (limit,))]
            target = compact(query)
            if len(target) >= 2:
                ids += [row[0] for row in connection.execute(f'''SELECT f.id FROM files f
                    JOIN roots r ON r.id=f.root_id
                    WHERE (instr(f.compact_name,?)>0 OR instr(f.compact_path,?)>0){scope} LIMIT ?''',
                    (target, target) + parameters + (limit,))]
            ids = list(dict.fromkeys(ids))[:2000]
            if not ids:
                return []
            placeholders = ','.join('?' for _ in ids)
            return list(connection.execute(f'''SELECT f.*,r.path root,r.status root_status
                FROM files f JOIN roots r ON r.id=f.root_id WHERE f.id IN ({placeholders})''', ids))

    def status(self, *, root: str | None = None):
        scope = ' WHERE path=?' if root is not None else ''
        parameters = (root,) if root is not None else ()
        with self.connection() as connection:
            roots = [dict(row) for row in connection.execute(
                f'SELECT * FROM roots{scope} ORDER BY id', parameters)]
            files = connection.execute('SELECT count(*) FROM files WHERE root_id IN '
                                       f'(SELECT id FROM roots{scope})', parameters).fetchone()[0]
        successes = [row['last_success'] for row in roots if row['last_success']]
        return {'files': files, 'roots': roots, 'last_success': max(successes, default=None)}
