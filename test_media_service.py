"""Durable authorization-boundary, quota and retention tests without camera I/O."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from media_service import MediaClient
from test_support import OWNER_ID
from media_store import MediaError, RETENTION
from test_media_worker import FakeRunner, InlineExecutor
from media_worker import MediaWorker


class MediaServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.config = dict({'owner_id':10001}, **{'owner_id':OWNER_ID, 'media_state_dir': self.temp.name, 'media_capture_enabled': False})
        self.client = MediaClient(self.config, clock=lambda: self.now); self.addCleanup(self.client.close)

    def context(self, **values):
        result = {'job_id': 'job-a', 'filename': 'part.gcode', 'state': 'printing', 'seen_at': self.now}
        result.update(values); return result

    def archive(self, size=12, kind='clip'):
        media_id = uuid.uuid4().hex
        path = Path(self.temp.name) / 'archive' / (media_id + '.mp4'); path.write_bytes(b'x' * size)
        self.client.store.reserve_archive(media_id, 'job-a', kind, self.client.store.relative(path), size, 30)
        return media_id, path

    def test_defaults_and_custom_duration_are_retrospective_not_new_capture(self):
        row = self.client.request_clip(12, context=self.context(), notify=True)
        self.assertEqual((row['seconds'], row['anchor'], row['status'], row['notify']), (30, 1000, 'queued', 1))
        for seconds in [1, 15, 30, 60, 299.5, 300]:
            other = MediaClient(Path(self.temp.name) / ('test-' + str(seconds)), clock=lambda: self.now)
            try: self.assertEqual(other.request_clip(12, seconds, self.context())['seconds'], seconds)
            finally: other.close()

    def test_invalid_seconds_users_and_contexts_rejected(self):
        for value in [True, 0, -1, 301, float('nan'), float('inf'), '30']:
            with self.subTest(seconds=value), self.assertRaises(MediaError): self.client.request_clip(12, value, self.context())
        for uid in [True, 0, -1, '12']:
            with self.subTest(uid=uid), self.assertRaises(MediaError): self.client.request_clip(uid, 30, self.context())
        for context in [None, self.context(state='complete'), self.context(job_id=''), self.context(seen_at=980)]:
            with self.assertRaises(MediaError): self.client.request_clip(12, 30, context)

    def test_queue_is_bounded_per_user_and_global(self):
        for _ in range(4): self.client.request_clip(12, 30, self.context())
        with self.assertRaisesRegex(MediaError, 'media_busy'): self.client.request_clip(12, 30, self.context())
        for uid in range(13, 20):
            for _ in range(4): self.client.request_clip(uid, 30, self.context())
        with self.assertRaisesRegex(MediaError, 'media_busy'): self.client.request_clip(999, 30, self.context())

    def test_web_request_does_not_auto_deliver_and_claims_never_replay_after_restart(self):
        one = self.client.request_clip(12, 30, self.context(), notify=True)
        two = self.client.request_clip(13, 30, self.context(), notify=False)
        media_id, _ = self.archive()
        with self.client.store.transaction():
            for request in [one, two]:
                self.client.store.db.execute("UPDATE requests SET status='ready',media_id=? WHERE id=?", (media_id, request['id']))
        items = self.client.pending_deliveries()
        self.assertEqual([row['uid'] for row in items], [12])
        restarted = MediaClient(self.temp.name, clock=lambda: self.now)
        try:
            self.assertEqual(restarted.pending_deliveries(), [])
            restarted.complete_delivery(one['id'], 'unknown')
            self.assertEqual(restarted.pending_deliveries(), [])
        finally: restarted.close()

    def test_revoke_cancels_pending_and_claimed_delivery(self):
        request = self.client.request_clip(12, 30, self.context(), notify=True)
        self.client.cancel_for_user(12)
        row = self.client.get(12, request['id'])
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['delivery_state'], 'revoked')
        self.assertEqual(self.client.pending_deliveries(), [])

    def test_revoke_poller_cancels_queued_work_before_encoding(self):
        one = self.client.request_clip(12, 30, self.context(), notify=True)
        two = self.client.request_clip(13, 30, self.context(), notify=True)
        calls = []
        def allowed(uid):
            calls.append(uid)
            return uid == 13
        self.assertEqual(self.client.cancel_disallowed(allowed), [12])
        self.assertEqual(set(calls), {12, 13})
        self.assertEqual(self.client.get(12, one['id'])['status'], 'cancelled')
        self.assertEqual(self.client.get(13, two['id'])['status'], 'queued')

    def test_quota_pauses_new_archival_without_early_deletion(self):
        self.client.store.quota = 20
        media_id, old = self.archive(size=16)
        new_id = uuid.uuid4().hex; new = Path(self.temp.name) / 'archive' / (new_id + '.mp4'); new.write_bytes(b'x' * 8)
        with self.assertRaisesRegex(MediaError, 'archive_full'):
            self.client.store.reserve_archive(new_id, 'job-a', 'clip', self.client.store.relative(new), 8, 30)
        self.assertTrue(self.client.status()['quota_paused'])
        self.assertTrue(old.exists())
        self.assertEqual([row['id'] for row in self.client.list(12)], [media_id])
        with self.assertRaisesRegex(MediaError, 'archive_full'): self.client.request_clip(12, 30, self.context())
        self.client.owner_delete(OWNER_ID, media_id, is_owner=True)
        self.assertFalse(self.client.status()['quota_paused'])
        self.assertEqual(self.client.request_clip(12, 30, self.context())['status'], 'queued')

    def test_age_cleanup_still_runs_when_quota_paused_and_resumes_saves(self):
        self.client.store.quota = 16
        media_id, old = self.archive(size=16)
        self.assertTrue(self.client.status()['quota_paused'])
        self.assertEqual(self.client.status()['quota_warning_sequence'], 1)
        self.now += RETENTION - 1
        self.client.store.cleanup_expired()
        self.assertTrue(old.exists())
        self.now += 1
        self.client.store.cleanup_expired()
        self.assertFalse(old.exists())
        self.assertFalse(self.client.status()['quota_paused'])

    def test_shared_read_but_only_owner_can_delete(self):
        media_id, path = self.archive()
        self.assertEqual(self.client.list(99)[0]['id'], media_id)
        for uid, flag in [(99, True), (OWNER_ID, False)]:
            with self.assertRaisesRegex(MediaError, 'owner_required'): self.client.owner_delete(uid, media_id, is_owner=flag)
        with self.client.open_download(99, media_id) as (stream, info):
            self.assertEqual(stream.read(), b'x' * 12)
            self.assertEqual(info['size'], 12)
        self.client.owner_delete(OWNER_ID, media_id, is_owner=True)
        self.assertFalse(path.exists())

    def test_safe_paths_download_size_and_symlink_escape(self):
        for value in ['../secret', '/etc/passwd', '.', 'archive/../../secret', 'bad\x00path']:
            with self.assertRaises(MediaError): self.client.store.path(value)
        media_id, path = self.archive()
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(MediaError, 'media_file_changed'): self.client.download(12, media_id)
        with tempfile.TemporaryDirectory() as other:
            link = Path(self.temp.name) / 'archive' / 'escape.mp4'; link.symlink_to(Path(other) / 'secret')
            with self.assertRaises(MediaError): self.client.store.path('archive/escape.mp4')

    def test_live_manifest_and_segments_reject_external_uris_and_traversal(self):
        capture = uuid.uuid4().hex
        directory = Path(self.temp.name) / 'rolling' / capture; directory.mkdir()
        name = capture + '-000000001.ts'; (directory / name).write_bytes(b'ts')
        path = directory / 'index.m3u8'
        self.client.store.set_meta('runtime', {'active_capture': capture, 'stream_ready': True})
        path.write_text('#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-11-16T00:00:00Z\n#EXTINF:2,\n' + name + '\n')
        self.assertIn(name, self.client.live_manifest())
        self.assertEqual(self.client.live_segment(name)['size'], 2)
        for name in ['../media.sqlite3', 'http://evil/a.ts', uuid.uuid4().hex + '-000000001.ts']:
            with self.assertRaises(MediaError): self.client.live_segment(name)
        path.write_text('#EXTM3U\n#EXTINF:2,\nhttp://evil/a.ts\n')
        with self.assertRaisesRegex(MediaError, 'invalid_live_manifest'): self.client.live_manifest()

    def test_worker_output_quota_failure_persists_warning_and_does_not_evict(self):
        self.client.store.quota = 20
        media_id, old = self.archive(16)
        request = self.client.request_clip(12, 30, self.context(), notify=True)
        runner = FakeRunner()
        worker = MediaWorker(dict(self.config, media_quota_bytes=20), clock=lambda: self.now,
                             runner=runner, fetch_context=lambda: None, executor=InlineExecutor())
        try:
            capture = uuid.uuid4().hex; directory = Path(self.temp.name) / 'rolling' / capture; directory.mkdir()
            path = directory / (capture + '-000000001.ts'); path.write_bytes(b'ts')
            worker.store.add_segment(path.stem, 'job-a', capture, worker.store.relative(path), 970, 1000, 2)
            with worker.store.transaction(): worker.store.db.execute("UPDATE requests SET status='running' WHERE id=?", (request['id'],))
            worker.process_clip(request['id'])
            result = self.client.get(12, request['id'])
            self.assertEqual(result['error'], 'archive_full')
            self.assertTrue(self.client.status()['quota_paused'])
            self.assertTrue(old.exists())
            self.assertEqual(len(self.client.list(12)), 1)
        finally: worker.close()

    def test_restart_removes_only_unpublished_orphans_not_young_archive(self):
        media_id, saved = self.archive()
        orphan = Path(self.temp.name) / 'archive' / (uuid.uuid4().hex + '.mp4')
        orphan.write_bytes(b'interrupted unpublished output')
        worker = MediaWorker(self.config, clock=lambda: self.now, runner=FakeRunner(), fetch_context=lambda: None, executor=InlineExecutor())
        try:
            self.assertTrue(saved.exists())
            self.assertFalse(orphan.exists())
            self.assertEqual(self.client.list(12)[0]['id'], media_id)
        finally: worker.close()

    def test_pending_request_pins_past_segments_before_async_worker_runs(self):
        capture = uuid.uuid4().hex; directory = Path(self.temp.name) / 'rolling' / capture; directory.mkdir()
        path = directory / (capture + '-000000001.ts'); path.write_bytes(b'ts')
        self.client.store.add_segment(path.stem, 'job-a', capture, self.client.store.relative(path), 970, 1000, 2)
        request = self.client.request_clip(12, 30, self.context())
        self.now += 1000
        worker = MediaWorker(self.config, clock=lambda: self.now, runner=FakeRunner(), fetch_context=lambda: None, executor=InlineExecutor())
        try:
            worker._prune_rolling()
            self.assertTrue(path.exists(), 'queued anchor window must survive rolling cleanup')
            self.client.cancel_for_user(12)
            worker._prune_rolling()
            self.assertFalse(path.exists())
        finally: worker.close()


if __name__ == '__main__': unittest.main()
