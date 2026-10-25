"""Two durable SQLite stores and fsync-before-progress CSV commits."""
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

_locked_runs = set()
_lock_guard = threading.Lock()

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


class CheckpointError(RuntimeError):
    pass


def text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      default=lambda n: format(n, 'f'))


def digest(value):
    return hashlib.sha256(text(value).encode()).hexdigest()


def fault(point, **context):
    """Fault-injection seam; production always executes the same commit protocol."""


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA cache_size=-32768')
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, text(value)))


@contextmanager
def run_lock(path):
    """Lock the progress file itself; a rejected writer never changes outputs."""
    key = str(Path(path).resolve())
    with _lock_guard:
        if key in _locked_runs:
            raise CheckpointError('Another process is writing this run')
        _locked_runs.add(key)
    handle = Path(path).open('a+b')
    try:
        try:
            # Separate from SQLite's own byte-range locks at 0x40000000.
            # Whole-file flock conflicts with SQLite on WSL DrvFS/Windows.
            offset = 1 << 44
            if os.name == 'nt':
                import msvcrt
                handle.seek(offset)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, offset)
        except OSError as exc:
            raise CheckpointError('Another process is writing this run') from exc
        yield
    finally:
        handle.close()
        with _lock_guard:
            _locked_runs.discard(key)


class Cache:
    def __init__(self, path, log):
        self.path, self.log = Path(path), log
        try:
            self.db = connect(path)
        except sqlite3.DatabaseError as exc:
            code=getattr(exc,'sqlite_errorcode',0)&0xff
            corrupt=code in (11,26) or (not code and str(exc) in ('file is not a database','database disk image is malformed'))
            if not corrupt or self.path.name!='cache.sqlite':
                raise
            self.path.unlink()
            log.warning('CACHE_DATABASE_REBUILT',source=str(self.path),reason=str(exc))
            self.db=connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS entries (
                kind TEXT, key TEXT, body BLOB, sha TEXT, PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS rmp_pending (
                scan TEXT, family TEXT, position INTEGER, body BLOB,
                PRIMARY KEY(scan,family,position));
            CREATE TABLE IF NOT EXISTS diff_blocks (
                source TEXT, line INTEGER, height INTEGER, body BLOB, sha TEXT,
                PRIMARY KEY(source,line));
            CREATE INDEX IF NOT EXISTS diff_height ON diff_blocks(source,height);
        ''')
        self.db.commit()
        self.stats = {}

    def count(self, event, **details):
        self.stats[event] = self.stats.get(event, 0) + 1
        with self.db:
            total = get(self.db, 'count:' + event, 0) + 1
            put(self.db, 'count:' + event, total)
        self.log.info(event, session_count=self.stats[event], run_total=total, **details)

    def read(self, kind, key):
        row = self.db.execute('SELECT body,sha FROM entries WHERE kind=? AND key=?',
                              (kind, key)).fetchone()
        if not row:
            return None
        try:
            body = bytes(row['body'])
            if hashlib.sha256(body).hexdigest() != row['sha']:
                raise ValueError('Cache digest mismatch')
            return json.loads(body)
        except (ValueError, TypeError, UnicodeError):
            with self.db:
                self.db.execute('DELETE FROM entries WHERE kind=? AND key=?', (kind, key))
            self.count('CACHE_ENTRY_INVALID', kind=kind, key=key)
            return None

    def write(self, kind, key, value):
        body = text(value).encode()
        self.db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?)',
                        (kind, key, body, hashlib.sha256(body).hexdigest()))

    def close(self):
        self.db.close()


def csv_bytes(fields, row=None):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fields, lineterminator='\n', extrasaction='raise')
    if row is None:
        writer.writeheader()
    else:
        writer.writerow({key: (text(value) if isinstance(value, (list, dict)) else
                               ('true' if value else 'false') if isinstance(value, bool) else
                               '' if value is None else str(value)) for key, value in row.items()})
    return stream.getvalue().encode('utf-8')


def init_progress(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS outputs (
            name TEXT PRIMARY KEY, fields TEXT, offset INTEGER, rows INTEGER, complete INTEGER);
        CREATE TABLE IF NOT EXISTS output_chunks (
            name TEXT, position INTEGER, end INTEGER, sha TEXT, PRIMARY KEY(name,position));
        CREATE TABLE IF NOT EXISTS row_hashes (
            name TEXT, row_key TEXT, sha TEXT, PRIMARY KEY(name,row_key));
        CREATE TABLE IF NOT EXISTS ingest (
            seq INTEGER PRIMARY KEY, kind TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY, body TEXT, selected INTEGER DEFAULT 0, episode_id TEXT);
        CREATE TABLE IF NOT EXISTS assignment (
            seq INTEGER PRIMARY KEY, group_id INTEGER);
        CREATE INDEX IF NOT EXISTS assignment_group ON assignment(group_id,seq);
        CREATE TABLE IF NOT EXISTS csv2_index (
            episode_id TEXT PRIMARY KEY, position INTEGER, end INTEGER, row_hash TEXT, problem TEXT);
        CREATE TABLE IF NOT EXISTS csv1_import_rows (position INTEGER PRIMARY KEY, body TEXT NOT NULL);
    ''')
    db.commit()


class AppendCSV:
    def __init__(self, directory, number, fields, db, log):
        self.name, self.db, self.log = f'csv{number}', db, log
        self.fields = fields
        self.path = Path(directory) / (self.name + '.csv')
        row = db.execute('SELECT * FROM outputs WHERE name=?', (self.name,)).fetchone()
        self.handle = self.path.open('r+b' if self.path.exists() else 'w+b')
        self.pending = []
        if row:
            if json.loads(row['fields']) != fields:
                self.handle.close()
                raise CheckpointError('CSV schema differs from the committed run')
            try:
                for part in db.execute('SELECT * FROM output_chunks WHERE name=? ORDER BY position', (self.name,)):
                    self.handle.seek(part['position'])
                    data = self.handle.read(part['end'] - part['position'])
                    if hashlib.sha256(data).hexdigest() != part['sha']:
                        raise CheckpointError(f'Committed {self.name} content is corrupt/truncated')
                self.handle.seek(0, 2)
                if self.handle.tell() != row['offset']:
                    if self.handle.tell() < row['offset']:
                        raise CheckpointError('Committed CSV is truncated')
                    self.handle.truncate(row['offset'])
                    self.handle.flush()
                    os.fsync(self.handle.fileno())
                    log.warning('CSV_TAIL_TRUNCATED', csv=self.name, offset=row['offset'])
                self.handle.seek(row['offset'])
                self.rows, self.done = row['rows'], bool(row['complete'])
            except BaseException:
                self.handle.close()
                raise
            log.info('CSV_RECOVERED', csv=self.name, rows=self.rows, complete=self.done)
        else:
            self.handle.truncate(0)
            header = csv_bytes(fields)
            self.handle.write(header)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            fault('csv_header_written', csv=self.name)
            with db:
                db.execute('INSERT INTO outputs VALUES (?,?,?,?,0)',
                           (self.name, text(fields), len(header), 0))
                db.execute('INSERT INTO output_chunks VALUES (?,?,?,?)',
                           (self.name, 0, len(header), hashlib.sha256(header).hexdigest()))
            self.rows, self.done = 0, False

    def write(self, row):
        if self.done:
            raise CheckpointError('Cannot append a completed CSV')
        data = csv_bytes(self.fields, row)
        start = self.handle.tell()
        half = len(data) // 2
        self.handle.write(data[:half])
        fault('csv_half_written', csv=self.name, episode=row.get('episode_id'))
        self.handle.write(data[half:])
        key = row.get('event_id') if self.name == 'csv1' and row.get('record_type') == 'fill' else row.get('episode_id')
        if row.get('record_type') == 'issue':
            key = 'issue:' + str(row.get('issue_id', row.get('issues_json', '')))
        self.pending.append((start, start + len(data), hashlib.sha256(data).hexdigest(),
                             key, digest({k: str(v) if v is not None else '' for k, v in row.items()}), row))

    def commit(self, cursor_key=None, cursor=None, complete=False):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        fault('csv_fsynced', csv=self.name)
        with self.db:
            for start, end, sha, key, _, row in self.pending:
                self.db.execute('INSERT INTO output_chunks VALUES (?,?,?,?)', (self.name, start, end, sha))
                if key:
                    # Hash the canonical parsed CSV representation, including blank columns.
                    parsed = next(csv.DictReader(io.StringIO(csv_bytes(self.fields).decode() + csv_bytes(self.fields, row).decode())))
                    self.db.execute('INSERT OR REPLACE INTO row_hashes VALUES (?,?,?)', (self.name, key, digest(parsed)))
            self.rows += len(self.pending)
            self.db.execute('UPDATE outputs SET offset=?,rows=?,complete=? WHERE name=?',
                            (self.handle.tell(), self.rows, int(complete), self.name))
            if cursor_key:
                put(self.db, cursor_key, cursor)
        self.pending.clear()
        self.done = complete
        self.log.info('CSV_COMMIT', csv=self.name, rows=self.rows, offset=self.handle.tell(), complete=complete)
        fault('progress_committed', csv=self.name)

    def close(self):
        self.handle.close()


def completed(db, number):
    row = db.execute('SELECT complete FROM outputs WHERE name=?', (f'csv{number}',)).fetchone()
    return bool(row and row[0])
