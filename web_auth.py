"""Individual website credentials; Telegram access remains the authority."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time

COOKIE = '__Host-k1c_session'
ITERATIONS = 600_000


class WebError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


class WebAuth:
    def __init__(self, path, allowed, is_owner, clock=time.time, session_seconds=43200):
        self.allowed, self.is_owner, self.clock = allowed, is_owner, clock
        self.session_seconds = max(300, min(int(session_seconds), 7 * 86400))
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.lock = threading.RLock()
        self.hash_slots = threading.BoundedSemaphore(2)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS users(
                telegram_id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                iterations INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL,
                version INTEGER NOT NULL, csrf TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL,
                FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS login_limits(
                key TEXT PRIMARY KEY, start REAL NOT NULL, failures INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(
                telegram_id INTEGER NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, response TEXT, created REAL NOT NULL,
                PRIMARY KEY(telegram_id,request_id));
        ''')

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def username(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', value):
            raise WebError('invalid_username', 'Логин: от 3 до 64 латинских букв, цифр, точек, дефисов или подчёркиваний.')
        return value.lower()

    @staticmethod
    def password(value):
        if not isinstance(value, str) or not 12 <= len(value) <= 256:
            raise WebError('invalid_password', 'Пароль должен содержать от 12 до 256 символов.')
        return value

    def _hash(self, password, salt, iterations=ITERATIONS):
        if not self.hash_slots.acquire(blocking=False):
            raise WebError('auth_busy', 'Вход занят. Повтори через несколько секунд.', 429)
        try:
            return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations, dklen=32)
        finally:
            self.hash_slots.release()

    def _owner(self, actor):
        if not self.is_owner(actor) or not self.allowed(actor):
            raise WebError('owner_required', 'Действие доступно только владельцу.', 403)

    def create(self, actor, telegram_id, username, password):
        self._owner(actor)
        if type(telegram_id) is not int or not self.allowed(telegram_id):
            raise WebError('user_not_allowed', 'Сначала разреши пользователю общий доступ к K1C.', 403)
        username, password = self.username(username), self.password(password)
        salt = secrets.token_bytes(16)
        encoded = self._hash(password, salt)
        with self.lock:
            # Permission is rechecked after the potentially slow password hash.
            self._owner(actor)
            if not self.allowed(telegram_id):
                raise WebError('user_not_allowed', 'Доступ пользователя уже отозван.', 403)
            try:
                self.db.execute('INSERT INTO users(telegram_id,username,salt,password_hash,iterations,created) VALUES(?,?,?,?,?,?)',
                                (telegram_id, username, salt, encoded, ITERATIONS, self.clock()))
            except sqlite3.IntegrityError:
                raise WebError('account_exists', 'Учётная запись или логин уже существуют.', 409)
        return {'telegram_id': telegram_id, 'username': username}

    def reset(self, actor, telegram_id, password):
        self._owner(actor)
        salt = secrets.token_bytes(16)
        encoded = self._hash(self.password(password), salt)
        with self.lock, self.db:
            self._owner(actor)
            row = self.db.execute('UPDATE users SET salt=?,password_hash=?,iterations=?,version=version+1 WHERE telegram_id=?',
                                  (salt, encoded, ITERATIONS, telegram_id))
            if not row.rowcount:
                raise WebError('account_missing', 'Учётная запись не найдена.', 404)
            self.db.execute('DELETE FROM sessions WHERE telegram_id=?', (telegram_id,))
        return {'telegram_id': telegram_id, 'sessions_closed': True}

    def delete(self, actor, telegram_id):
        self._owner(actor)
        if self.is_owner(telegram_id):
            raise WebError('owner_account_required', 'Учётную запись владельца можно восстановить или сменить её пароль.', 409)
        with self.lock:
            self._owner(actor)
            self.db.execute('DELETE FROM users WHERE telegram_id=?', (telegram_id,))
        return {'deleted': True}

    def users(self, actor):
        self._owner(actor)
        with self.lock:
            rows = self.db.execute('SELECT telegram_id,username,created FROM users ORDER BY username').fetchall()
        return [dict(row, allowed=self.allowed(row['telegram_id']), is_owner=self.is_owner(row['telegram_id'])) for row in rows]

    def _rate_check(self, keys):
        now = self.clock()
        with self.lock:
            for key, limit in keys:
                row = self.db.execute('SELECT start,failures FROM login_limits WHERE key=?', (key,)).fetchone()
                if row and 0 <= now - row['start'] < 900 and row['failures'] >= limit:
                    raise WebError('login_rate_limited', 'Слишком много попыток входа. Подожди 15 минут.', 429)

    def _rate_failure(self, keys):
        now = self.clock()
        with self.lock:
            for key, _ in keys:
                row = self.db.execute('SELECT start,failures FROM login_limits WHERE key=?', (key,)).fetchone()
                start, failures = (row['start'], row['failures'] + 1) if row and 0 <= now-row['start'] < 900 else (now, 1)
                self.db.execute('INSERT INTO login_limits VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET start=excluded.start,failures=excluded.failures', (key, start, failures))
            self.db.execute('DELETE FROM login_limits WHERE start<?', (now-86400,))

    def login(self, username, password, client_ip):
        # All rejection paths use the same public message and perform a hash.
        name = str(username).lower()[:256] if isinstance(username, str) else ''
        keys = [('ip:'+str(client_ip)[:80], 30), ('user:'+hashlib.sha256(name.encode()).hexdigest(), 5)]
        self._rate_check(keys)
        if not isinstance(password, str) or len(password) > 256:
            self._rate_failure(keys)
            raise WebError('bad_credentials', 'Неверный логин или пароль.', 401)
        with self.lock:
            row = self.db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        salt = bytes(row['salt']) if row else b'K1C-dummy-salt16'
        actual = self._hash(password, salt, row['iterations'] if row else ITERATIONS)
        valid = row is not None and hmac.compare_digest(actual, bytes(row['password_hash']))
        if not valid or not self.allowed(row['telegram_id']):
            self._rate_failure(keys)
            raise WebError('bad_credentials', 'Неверный логин или пароль.', 401)
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock()
        with self.lock:
            fresh = self.db.execute('SELECT version FROM users WHERE telegram_id=?', (row['telegram_id'],)).fetchone()
            if not fresh or fresh['version'] != row['version'] or not self.allowed(row['telegram_id']):
                raise WebError('bad_credentials', 'Неверный логин или пароль.', 401)
            self.db.execute('DELETE FROM sessions WHERE expires<?', (now,))
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',
                            (self._token_hash(token), row['telegram_id'], row['version'], csrf, now, now+self.session_seconds))
            self.db.execute('DELETE FROM login_limits WHERE key=?', (keys[1][0],))
        return token, self.session(token)

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        digest = self._token_hash(token)
        with self.lock:
            row = self.db.execute('SELECT s.*,u.username,u.version AS current_version FROM sessions s JOIN users u USING(telegram_id) WHERE token_hash=?', (digest,)).fetchone()
            if not row:
                return None
            if (row['expires'] <= self.clock() or row['version'] != row['current_version'] or not self.allowed(row['telegram_id'])):
                self.db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))
                return None
            return {'user': {'telegram_id': row['telegram_id'], 'username': row['username'], 'is_owner': self.is_owner(row['telegram_id'])},
                    'csrf': row['csrf'], 'expires': row['expires']}

    def logout(self, token):
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE token_hash=?', (self._token_hash(token),))

    def claim_request(self, uid, request_id, fingerprint):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise WebError('request_id_required', 'Нужен уникальный идентификатор команды.')
        with self.lock:
            row = self.db.execute('SELECT * FROM requests WHERE telegram_id=? AND request_id=?', (uid, request_id)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise WebError('request_id_conflict', 'Этот идентификатор уже использован для другой команды.', 409)
                if row['response'] is None:
                    raise WebError('request_in_progress', 'Команда уже принята. Уточни её результат в пульте; повторно она не выполняется.', 409)
                return json.loads(row['response'])
            self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)', (uid, request_id, fingerprint, None, self.clock()))
        return None

    def complete_request(self, uid, request_id, response):
        with self.lock:
            self.db.execute('UPDATE requests SET response=? WHERE telegram_id=? AND request_id=?',
                            (json.dumps(response, ensure_ascii=False, allow_nan=False), uid, request_id))


def main():
    parser = argparse.ArgumentParser(description='Создание веб-входа владельца. JSON с логином и паролем читается из stdin.')
    parser.add_argument('--db', required=True)
    parser.add_argument('--state-db', required=True)
    parser.add_argument('--owner-id', required=True, type=int, help='The same owner_id as the bot configuration')
    parser.add_argument('command', choices=['bootstrap-owner'])
    args = parser.parse_args()
    from access import AccessController
    from storage import Store
    owner_id = args.owner_id
    if type(owner_id) is not int or not 0 < owner_id < 2**63:
        parser.error('--owner-id must be a positive Telegram user ID')
    store = Store(args.state_db)
    access = AccessController(store, owner_id=owner_id, initial_users=[])
    auth = WebAuth(args.db, access.is_allowed, access.is_owner)
    try:
        raw = sys.stdin.read(8193)
        if len(raw) > 8192:
            raise WebError('credentials_too_large', 'Слишком большой ввод.')
        data = json.loads(raw)
        existing = auth.users(owner_id)
        if any(u['telegram_id'] == owner_id for u in existing):
            auth.reset(owner_id, owner_id, data['password'])
        else:
            auth.create(owner_id, owner_id, data['username'], data['password'])
        print(json.dumps({'ok': True, 'message': 'Веб-вход владельца готов.'}, ensure_ascii=False))
    except (WebError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'ok': False, 'message': exc.message if isinstance(exc, WebError) else 'Неверный формат входных данных.'}, ensure_ascii=False))
        return 1
    finally:
        auth.close()
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
