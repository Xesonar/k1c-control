"""Fast local media API used by authenticated bot/web callers; no camera I/O."""
import json
import math
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import re
import time
import uuid

from media_store import MediaStore, MediaError, valid_id, valid_uid, QUOTA, RETENTION

ACTIVE_STATES = {'printing', 'paused', 'preparing'}
SEGMENT_NAME = re.compile(r'^[a-f0-9]{32}-[0-9]{9}\.ts$')


class MediaClient:
    def __init__(self, config_or_path=None, *, clock=time.time):
        config = config_or_path if isinstance(config_or_path, dict) else {}
        root = config.get('media_state_dir', '/var/lib/k1c-media') if config_or_path is None or isinstance(config_or_path, dict) else config_or_path
        self.config, self.clock = config, clock
        self.store = MediaStore(root, quota=config.get('media_quota_bytes', QUOTA),
                                retention=config.get('media_retention_seconds', RETENTION), clock=clock)

    def close(self):
        self.store.close()

    def status(self, context=None):
        cap = self.store.capacity()
        runtime = self.store.get_meta('runtime', {})
        return {'available': bool(runtime.get('active_capture') and runtime.get('stream_ready')), 'capture': runtime,
                'archive_bytes': cap['used_bytes'], 'quota_bytes': cap['quota_bytes'],
                'quota_paused': cap['paused'], 'quota_warning_sequence': cap['warning_sequence'],
                'retention_seconds': self.store.retention,
                'queued': self.store.rows("SELECT COUNT(*) n FROM requests WHERE status IN ('queued','running')")[0]['n']}

    @staticmethod
    def _public(row):
        result = {key: value for key, value in row.items() if key not in {'path', 'sources'}}
        for key in ('metadata', 'gaps'):
            if key in result and isinstance(result[key], str):
                result[key] = json.loads(result[key])
        return result

    def list(self, uid, limit=50):
        valid_uid(uid)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MediaError('invalid_limit', 400)
        return [self._public(row) for row in self.store.rows('SELECT * FROM archive WHERE expires>? ORDER BY created DESC LIMIT ?', (self.clock(), limit))]

    def request_clip(self, uid, seconds=30, context=None, *, notify=False):
        valid_uid(uid)
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 300:
            raise MediaError('invalid_clip_duration', 400)
        if not isinstance(context, dict) or context.get('state') not in ACTIVE_STATES:
            raise MediaError('no_active_job', 409)
        job_id = context.get('job_id')
        if not isinstance(job_id, str) or not job_id or len(job_id) > 256 or any(ord(c) < 32 for c in job_id):
            raise MediaError('no_stable_job_id', 409)
        now = self.clock()
        seen = context.get('seen_at', now)
        if type(seen) not in (int, float) or not math.isfinite(seen) or not -2 <= now - seen <= 15:
            raise MediaError('stale_job_context', 409)
        filename = str(context.get('filename') or '')[:2048]
        request_id = uuid.uuid4().hex
        with self.store.transaction():
            if self.store.capacity()['paused']:
                raise MediaError('archive_full', 507)
            pending = self.store.rows("SELECT uid FROM requests WHERE status IN ('queued','running')")
            if len(pending) >= 32 or sum(row['uid'] == uid for row in pending) >= 4:
                raise MediaError('media_busy', 429)
            source_rows = self.store.window(job_id, now - float(seconds), now)
            source_paths = [row['path'] for row in source_rows]
            pinned = set(source_paths)
            for row in self.store.rows("SELECT sources FROM requests WHERE status IN ('queued','running')"):
                pinned.update(json.loads(row['sources']))
            pinned_bytes = sum(row['size'] for row in self.store.rows('SELECT path,size FROM segments') if row['path'] in pinned)
            if pinned_bytes > 256 * 1024 ** 2:
                raise MediaError('media_busy', 429)
            self.store.db.execute('''INSERT INTO requests
                (id,uid,job_id,filename,seconds,anchor,created,status,notify,delivery_state,sources)
                VALUES(?,?,?,?,?,?,?,'queued',?,?,?)''',
                (request_id, uid, job_id, filename, float(seconds), now, now, int(bool(notify)), 'pending' if notify else 'none', json.dumps(source_paths)))
        return self.get(uid, request_id)

    def get(self, uid, request_id):
        valid_uid(uid); valid_id(request_id)
        rows = self.store.rows('SELECT * FROM requests WHERE id=?', (request_id,))
        if not rows:
            raise MediaError('media_job_not_found', 404)
        return self._public(rows[0])

    def download(self, uid, media_id):
        valid_uid(uid); valid_id(media_id)
        rows = self.store.rows('SELECT * FROM archive WHERE id=? AND expires>?', (media_id, self.clock()))
        if not rows:
            requests = self.store.rows("SELECT media_id,status FROM requests WHERE id=?", (media_id,))
            if requests and requests[0]['media_id']:
                return self.download(uid, requests[0]['media_id'])
            raise MediaError('media_not_found', 404)
        row = rows[0]
        path = self.store.path(row['path'], exists=True)
        if path.stat().st_size != row['size']:
            raise MediaError('media_file_changed', 409)
        return {'id': row['id'], 'path': str(path), 'size': row['size'], 'mime': 'video/mp4',
                'filename': row['filename'], 'name': row['filename'], 'expires': row['expires']}

    @contextmanager
    def open_download(self, uid, media_id):
        info = self.download(uid, media_id)
        descriptor = os.open(info['path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            check = os.fstat(descriptor)
            if not stat.S_ISREG(check.st_mode) or check.st_size != info['size']:
                raise MediaError('media_file_changed', 409)
            with os.fdopen(descriptor, 'rb') as stream:
                descriptor = None
                yield stream, info
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def owner_delete(self, uid, media_id, *, is_owner=False):
        valid_uid(uid); valid_id(media_id)
        if uid != self.config.get('owner_id') or type(self.config.get('owner_id')) is not int or not is_owner:
            raise MediaError('owner_required', 403)
        with self.store.transaction():
            rows = self.store.rows('SELECT path FROM archive WHERE id=?', (media_id,))
            if not rows:
                raise MediaError('media_not_found', 404)
            self.store.path(rows[0]['path']).unlink(missing_ok=True)
            self.store.db.execute('DELETE FROM archive WHERE id=?', (media_id,))
            self.store.capacity()
        return {'deleted': media_id}

    def cancel_for_user(self, uid):
        """Called when common access is revoked; running output cannot commit."""
        valid_uid(uid)
        with self.store.transaction():
            self.store.db.execute("UPDATE requests SET status='cancelled',error='access_revoked',completed=? WHERE uid=? AND status IN ('queued','running')", (self.clock(), uid))
            self.store.db.execute("UPDATE requests SET delivery_state='revoked',delivery_result='access_revoked' WHERE uid=? AND delivery_state IN ('pending','claimed')", (uid,))

    def cancel_disallowed(self, checker):
        if not callable(checker):
            raise MediaError('invalid_access_checker', 400)
        # Call the common access checker outside the media database lock.
        users = {row['uid'] for row in self.store.rows("SELECT DISTINCT uid FROM requests WHERE status IN ('queued','running') OR delivery_state IN ('pending','claimed')")}
        revoked = []
        for uid in users:
            if not checker(uid):
                self.cancel_for_user(uid)
                revoked.append(uid)
        return revoked

    def pending_deliveries(self, limit=8):
        """Claim once; a process crash never automatically replays a TG send."""
        if type(limit) is not int or not 1 <= limit <= 32:
            raise MediaError('invalid_limit', 400)
        with self.store.transaction():
            rows = self.store.rows('''SELECT r.* FROM requests r LEFT JOIN archive a ON a.id=r.media_id
                WHERE r.notify=1 AND r.delivery_state='pending' AND (r.status='failed' OR (r.status='ready' AND a.expires>?))
                ORDER BY r.created LIMIT ?''', (self.clock(), limit))
            for row in rows:
                self.store.db.execute("UPDATE requests SET delivery_state='claimed' WHERE id=? AND delivery_state='pending'", (row['id'],))
            return [self._public(row) for row in rows]

    def complete_delivery(self, request_id, result):
        valid_id(request_id)
        if result not in {'sent', 'link', 'revoked', 'failed', 'unknown'}:
            raise MediaError('invalid_delivery_result', 400)
        with self.store.transaction():
            self.store.db.execute("UPDATE requests SET delivery_state=?,delivery_result=? WHERE id=? AND delivery_state='claimed'", (result, result, request_id))

    def live_manifest(self):
        runtime = self.store.get_meta('runtime', {})
        capture = runtime.get('active_capture')
        if not isinstance(capture, str) or not re.fullmatch('[a-f0-9]{32}', capture):
            raise MediaError('live_unavailable', 503)
        path = self.store.path('rolling/' + capture + '/index.m3u8', exists=True)
        if path.stat().st_size > 256 * 1024:
            raise MediaError('invalid_live_manifest', 503)
        text = path.read_text()
        lines = []
        allowed = ('#EXTM3U', '#EXT-X-VERSION:', '#EXT-X-TARGETDURATION:', '#EXT-X-MEDIA-SEQUENCE:',
                   '#EXT-X-PROGRAM-DATE-TIME:', '#EXTINF:', '#EXT-X-DISCONTINUITY', '#EXT-X-INDEPENDENT-SEGMENTS')
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                if line.startswith(allowed):
                    lines.append(line)
            elif SEGMENT_NAME.fullmatch(line) and line.startswith(capture + '-'):
                lines.append(line)
            else:
                raise MediaError('invalid_live_manifest', 503)
        if not lines or lines[0] != '#EXTM3U':
            raise MediaError('invalid_live_manifest', 503)
        return '\n'.join(lines) + '\n'

    def live_segment(self, name):
        if not isinstance(name, str) or not SEGMENT_NAME.fullmatch(name):
            raise MediaError('invalid_live_segment', 400)
        capture = self.store.get_meta('runtime', {}).get('active_capture')
        if not capture or not name.startswith(capture + '-'):
            raise MediaError('live_segment_expired', 404)
        path = self.store.path('rolling/' + capture + '/' + name, exists=True)
        size = path.stat().st_size
        if size > 32 * 1024 * 1024:
            raise MediaError('live_segment_too_large', 503)
        return {'path': str(path), 'size': size, 'mime': 'video/mp2t', 'filename': name, 'name': name}
