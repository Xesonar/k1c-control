"""Durable local state and notification delivery for the K1C bot.

Values crossing the public interface are JSON copies. Mutations and claims commit
before returning; callers must claim a physical action before issuing its POST.
"""
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _copy(value):
    return json.loads(_encode(value))


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self.lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, created REAL NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, created REAL NOT NULL)")
        self._db.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
            action TEXT NOT NULL, result TEXT NOT NULL, detail TEXT NOT NULL
        )""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY,
            event_key TEXT NOT NULL,
            recipient INTEGER NOT NULL,
            text TEXT NOT NULL,
            photo BLOB,
            markup TEXT,
            created REAL NOT NULL,
            next_attempt REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 0,
            UNIQUE(event_key,recipient)
        )""")
        self._db.execute("CREATE INDEX IF NOT EXISTS outbox_due ON outbox(delivered,next_attempt,id)")

    @contextmanager
    def _transaction(self):
        # Savepoints permit a mutation callback to use another Store operation
        # without allowing that operation to commit the outer mutation early.
        with self.lock:
            nested = self._db.in_transaction
            if nested:
                self._db.execute("SAVEPOINT store_nested")
            else:
                self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                if nested:
                    self._db.execute("RELEASE store_nested")
                else:
                    self._db.commit()
            except BaseException:
                if self._db.in_transaction:
                    if nested:
                        self._db.execute("ROLLBACK TO store_nested")
                        self._db.execute("RELEASE store_nested")
                    else:
                        self._db.rollback()
                raise

    def get(self, key, default=None):
        with self.lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row is not None else _copy(default)

    def set(self, key, value):
        encoded = _encode(value)
        with self._transaction():
            self._db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def mutate(self, key, fn, default=None):
        with self._transaction():
            value = fn(self.get(key, default))
            self.set(key, value)
            return _copy(value)

    def consume_update(self, update_id):
        """Commit consumption before dispatching an update to the bot handler."""
        if type(update_id) is not int or update_id < 0:
            raise ValueError("invalid_update_id")
        with self._transaction():
            inserted = self._db.execute("INSERT OR IGNORE INTO updates(id,created) VALUES(?,?)", (update_id, time.time())).rowcount
            if not inserted:
                return False
            self.set("offset", update_id + 1)
            return True

    def claim_action(self, key):
        """An uncertain network outcome must not result in a second claim."""
        if not isinstance(key, str) or not key:
            raise ValueError("invalid_action_key")
        with self._transaction():
            return bool(self._db.execute("INSERT OR IGNORE INTO actions(key,created) VALUES(?,?)", (key, time.time())).rowcount)

    def audit(self, actor, action, result, detail=""):
        with self._transaction():
            self._db.execute("INSERT INTO audit(created,actor,action,result,detail) VALUES(?,?,?,?,?)", (time.time(), _encode(actor), action, result, detail))

    def recent_audit(self, limit=10):
        with self.lock:
            rows = self._db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(0, int(limit)),)).fetchall()
            return [dict(row, actor=json.loads(row["actor"])) for row in rows]

    def enqueue(self, event_key, recipients, text, photo=None, markup=None):
        """Insert one delivery per recipient without replacing an existing event."""
        if not isinstance(event_key, str) or not event_key:
            raise ValueError("invalid_event_key")
        encoded_markup = _encode(markup) if markup is not None else None
        created = time.time()
        with self._transaction():
            for recipient in recipients:
                self._db.execute("""INSERT INTO outbox
                    (event_key,recipient,text,photo,markup,created,next_attempt)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_key,recipient) DO NOTHING""", (event_key, recipient, text, photo, encoded_markup, created, created))

    def pending(self, now=None, limit=10):
        """Return due deliveries in creation order; returned metadata is independent."""
        if now is None:
            now = time.time()
        with self.lock:
            rows = self._db.execute("""SELECT id,event_key,recipient,text,photo,markup,attempts,created,next_attempt
                FROM outbox WHERE delivered=0 AND next_attempt<=? ORDER BY id LIMIT ?""", (now, max(0, int(limit)))).fetchall()
            return [dict(row, markup=json.loads(row["markup"]) if row["markup"] is not None else None) for row in rows]

    def delivered(self, id):
        with self._transaction():
            # Retain the unique event key for deduplication, free image pages for
            # reuse instead of retaining every historical camera snapshot.
            self._db.execute("UPDATE outbox SET delivered=1,photo=NULL,markup=NULL WHERE id=?", (id,))

    def retry(self, id, delay=30):
        delay = float(delay)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("invalid_retry_delay")
        with self._transaction():
            self._db.execute("""UPDATE outbox SET attempts=attempts+1,next_attempt=?
                WHERE id=? AND delivered=0""", (time.time() + delay, id))

    def close(self):
        with self.lock:
            self._db.close()
