"""Bounded, independent media state. This database never contains printer actions."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

DAY = 86400
RETENTION = 7 * DAY
QUOTA = 10 * 1024 ** 3
ID_RE = re.compile(r'^[a-f0-9]{32}$')


class MediaError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def valid_uid(uid):
    if type(uid) is not int or not 0 < uid < 2 ** 63:
        raise MediaError('invalid_user', 400)
    return uid


def valid_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise MediaError('invalid_media_id', 400)
    return value


def job_token(job_id):
    return hashlib.sha256(job_id.encode()).hexdigest()[:32]


class MediaStore:
    def __init__(self, root, *, quota=QUOTA, retention=RETENTION, clock=time.time):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for name in ('archive', 'rolling', 'tmp', 'timelapse'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        self.quota = min(QUOTA, max(1, int(quota)))
        if int(retention) != RETENTION:
            raise MediaError('retention_must_be_seven_days')
        self.retention = RETENTION
        self.clock, self.lock = clock, threading.RLock()
        path = self.root / 'media.sqlite3'
        if path.is_symlink():
            raise MediaError('unsafe_media_path')
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          job_id TEXT PRIMARY KEY,filename TEXT NOT NULL,state TEXT NOT NULL,
          started REAL NOT NULL,last_seen REAL NOT NULL,ended REAL,
          timelapse_status TEXT NOT NULL DEFAULT 'recording',timelapse_interval REAL NOT NULL DEFAULT 10,
          last_frame REAL NOT NULL DEFAULT 0,error TEXT);
        CREATE TABLE IF NOT EXISTS segments(
          id TEXT PRIMARY KEY,job_id TEXT NOT NULL,capture_id TEXT NOT NULL,path TEXT NOT NULL UNIQUE,
          started REAL NOT NULL,ended REAL NOT NULL,size INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS segment_window ON segments(job_id,started,ended);
        CREATE TABLE IF NOT EXISTS frames(
          id TEXT PRIMARY KEY,job_id TEXT NOT NULL,path TEXT NOT NULL UNIQUE,at REAL NOT NULL,size INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS frame_job ON frames(job_id,at);
        CREATE TABLE IF NOT EXISTS requests(
          id TEXT PRIMARY KEY,uid INTEGER NOT NULL,job_id TEXT NOT NULL,filename TEXT NOT NULL,
          seconds REAL NOT NULL,anchor REAL NOT NULL,created REAL NOT NULL,status TEXT NOT NULL,
          started REAL,completed REAL,error TEXT,media_id TEXT,notify INTEGER NOT NULL,
          delivery_state TEXT NOT NULL,delivery_result TEXT,sources TEXT NOT NULL DEFAULT '[]',
          actual_duration REAL NOT NULL DEFAULT 0,missing_seconds REAL NOT NULL DEFAULT 0,gaps TEXT NOT NULL DEFAULT '[]');
        CREATE INDEX IF NOT EXISTS request_queue ON requests(status,created);
        CREATE TABLE IF NOT EXISTS archive(
          id TEXT PRIMARY KEY,job_id TEXT NOT NULL,kind TEXT NOT NULL,filename TEXT NOT NULL,path TEXT NOT NULL UNIQUE,
          created REAL NOT NULL,expires REAL NOT NULL,size INTEGER NOT NULL,duration REAL NOT NULL,
          request_id TEXT,metadata TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS archive_age ON archive(expires);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.db.in_transaction
            if nested:
                self.db.execute('SAVEPOINT media_nested')
            else:
                self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                if nested:
                    self.db.execute('RELEASE media_nested')
                else:
                    self.db.commit()
            except BaseException:
                if nested:
                    self.db.execute('ROLLBACK TO media_nested')
                    self.db.execute('RELEASE media_nested')
                else:
                    self.db.rollback()
                raise

    def path(self, relative, *, exists=False):
        if not isinstance(relative, str) or not relative or '\x00' in relative:
            raise MediaError('unsafe_media_path', 400)
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts:
            raise MediaError('unsafe_media_path', 400)
        result = (self.root / rel).resolve()
        try:
            result.relative_to(self.root)
        except ValueError:
            raise MediaError('unsafe_media_path', 400) from None
        if result == self.root or (exists and not result.is_file()):
            raise MediaError('media_not_found', 404)
        return result

    def relative(self, path):
        try:
            value = str(Path(path).resolve().relative_to(self.root))
        except ValueError:
            raise MediaError('unsafe_media_path') from None
        self.path(value)
        return value

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        with self.transaction():
            self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key, json.dumps(value, separators=(',', ':'), allow_nan=False)))

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def archive_bytes(self):
        return self.rows('SELECT COALESCE(SUM(size),0) n FROM archive')[0]['n']

    def capacity(self):
        used = self.archive_bytes()
        blocked = self.get_meta('quota_blocked_used')
        if used >= self.quota and blocked is None:
            self.block_quota()
            blocked = self.get_meta('quota_blocked_used')
        paused = used >= self.quota or (blocked is not None and used >= blocked)
        if blocked is not None and used < blocked:
            self.set_meta('quota_blocked_used', None)
            paused = used >= self.quota
        return {'used_bytes': used, 'quota_bytes': self.quota, 'paused': paused,
                'warning_sequence': self.get_meta('quota_warning_sequence', 0)}

    def block_quota(self):
        with self.transaction():
            if self.get_meta('quota_blocked_used') is None:
                self.set_meta('quota_warning_sequence', self.get_meta('quota_warning_sequence', 0) + 1)
            self.set_meta('quota_blocked_used', self.archive_bytes())

    def reserve_archive(self, media_id, job_id, kind, path, size, duration, *, request_id=None, metadata=None):
        """Publish only after an output is complete; no younger archive is evicted."""
        valid_id(media_id)
        actual = self.path(path, exists=True)
        if type(size) is not int or not 0 < size <= 128 * 1024 ** 2 or actual.stat().st_size != size:
            raise MediaError('invalid_media_size')
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 86400:
            raise MediaError('invalid_media_duration')
        now = self.clock()
        blocked = False
        with self.transaction():
            if self.capacity()['paused'] or self.archive_bytes() + size > self.quota:
                self.block_quota()
                blocked = True
            else:
                self.db.execute('INSERT INTO archive VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                (media_id, job_id, kind, 'k1c_' + kind + '_' + media_id + '.mp4', path,
                                 now, now + self.retention, int(size), float(duration), request_id,
                                 json.dumps(metadata or {}, separators=(',', ':'), allow_nan=False)))
        if blocked:
            raise MediaError('archive_full', 507)

    def cleanup_expired(self):
        now = self.clock()
        with self.transaction():
            rows = self.rows('SELECT id,path FROM archive WHERE expires<=?', (now,))
            for row in rows:
                try:
                    self.path(row['path']).unlink(missing_ok=True)
                except (OSError, MediaError):
                    continue
                self.db.execute('DELETE FROM archive WHERE id=?', (row['id'],))
            self.db.execute("DELETE FROM requests WHERE created<? AND status IN ('ready','failed','cancelled')", (now - RETENTION,))
            self.db.execute("DELETE FROM jobs WHERE ended<? AND job_id NOT IN (SELECT job_id FROM archive) AND job_id NOT IN (SELECT job_id FROM requests) AND job_id NOT IN (SELECT job_id FROM frames)", (now - RETENTION,))
            self.capacity()
            return len(rows)

    def add_segment(self, segment_id, job_id, capture_id, relative, started, ended, size):
        self.path(relative, exists=True)
        if not all(math.isfinite(x) for x in (started, ended)) or not 0 < ended - started <= 60:
            raise MediaError('invalid_segment')
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO segments VALUES(?,?,?,?,?,?,?)',
                            (segment_id, job_id, capture_id, relative, started, ended, int(size)))

    def window(self, job_id, start, end):
        return self.rows('SELECT * FROM segments WHERE job_id=? AND ended>? AND started<? ORDER BY started,id',
                         (job_id, start, end))

    def close(self):
        with self.lock:
            self.db.close()
