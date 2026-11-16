"""Offline web/media runtime and one-attempt video delivery checks."""
import email
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from adapters import ApiError, Telegram
from bot import UpdateDispatcher, maintain_media_access, process_media_deliveries, run, start_interfaces
from manager import Manager
from storage import Store
from test_adapters import Server
from test_manager import Printer
from test_runtime import RuntimeTelegram


class VideoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.server.respond = lambda *_: (200, {'Content-Type': 'application/json'},
                                         b'{"ok":true,"result":{"message_id":1}}')
        self.telegram = Telegram({'bot_token': '12345:LOCAL_TEST_TOKEN',
                                  'telegram_api_base': self.server.url})

    def tearDown(self):
        self.server.close()

    def test_video_multipart_stream_is_bounded_known_length_and_mp4(self):
        reads, original = [], os.fdopen

        class Reader:
            def __init__(self, wrapped): self.wrapped = wrapped
            def __getattr__(self, name): return getattr(self.wrapped, name)
            def __enter__(self): return self
            def __exit__(self, *args): return self.wrapped.__exit__(*args)
            def read(self, size=-1):
                if not 0 < size <= 65536:
                    raise AssertionError('unbounded video read')
                reads.append(size)
                return self.wrapped.read(size)

        def opened(fd, mode, *args, **kwargs):
            stream = original(fd, mode, *args, **kwargs)
            return Reader(stream) if mode == 'rb' else stream

        content = b'synthetic-no-camera\x00' * 100000
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'
            path.write_bytes(content)
            with patch('adapters.os.fdopen', side_effect=opened):
                self.assertEqual(self.telegram.send_video(7, 'Clip', path, duration=30.2), {'message_id': 1})
        self.assertGreater(len(reads), 20)
        self.assertLessEqual(max(reads), 65536)
        self.assertEqual(len(self.server.requests), 1)
        method, url, body, headers = self.server.requests[0]
        self.assertEqual((method, url), ('POST', '/bot12345:LOCAL_TEST_TOKEN/sendVideo'))
        self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertIsNone(headers.get('Transfer-Encoding'))
        mime = email.message_from_bytes(('Content-Type: ' + headers['Content-Type']
                                         + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
        fields = {part.get_param('name', header='Content-Disposition'): part for part in mime.get_payload()}
        self.assertEqual(fields['video'].get_content_type(), 'video/mp4')
        self.assertEqual(fields['video'].get_filename(), 'k1c-clip.mp4')
        self.assertEqual(fields['video'].get_payload(decode=True), content)
        self.assertEqual(fields['duration'].get_payload(decode=True), b'31')
        self.assertEqual(fields['supports_streaming'].get_payload(decode=True), b'true')

    def test_too_large_and_symlink_video_never_make_a_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'large.mp4'
            with path.open('wb') as stream: stream.truncate(Telegram.MAX_VIDEO_BYTES + 1)
            with self.assertRaises(ApiError) as caught:
                self.telegram.send_video(7, '', path)
            self.assertEqual(caught.exception.code, 'video_too_large')
            path.write_bytes(b'synthetic')
            link = Path(folder) / 'link.mp4'
            link.symlink_to(path)
            with self.assertRaises(ApiError): self.telegram.send_video(7, '', link)
        self.assertEqual(self.server.requests, [])

    def test_video_http_500_is_uncertain_and_not_retried(self):
        self.server.respond = lambda *_: (500, {}, b'{}')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'; path.write_bytes(b'synthetic')
            with self.assertRaises(ApiError) as caught:
                self.telegram.send_video(7, '', path)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.server.requests), 1)

    def test_video_rechecks_authorization_between_upload_chunks(self):
        checks = []
        def allowed():
            checks.append(True)
            return len(checks) < 3
        def consume(_url, **kwargs):
            list(kwargs['body'])
            return {'ok': True, 'result': {}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'; path.write_bytes(b'x' * 140000)
            with patch('adapters._json', side_effect=consume) as request:
                with self.assertRaises(ApiError) as caught:
                    self.telegram.send_video(7, '', path, authorize=allowed)
            self.assertEqual(caught.exception.code, 'access_revoked')
            self.assertEqual(request.call_count, 1)
        self.assertEqual(self.server.requests, [])


class MediaDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.access = types.SimpleNamespace(allowed=True)
        self.manager = types.SimpleNamespace(stopping=False,
                    access=types.SimpleNamespace(is_allowed=lambda uid: self.access.allowed))
        self.rows = [{'id': 'a' * 32, 'uid': 7, 'status': 'ready', 'media_id': 'b' * 32,
                      'actual_duration': 29.5, 'missing_seconds': .5}]
        owner = self
        class Media:
            size = 123
            after_download = None
            def __init__(self): self.completed, self.downloads = [], []
            def pending_deliveries(self, limit=1):
                rows, owner.rows = owner.rows[:limit], owner.rows[limit:]
                return rows
            def download(self, uid, media_id):
                self.downloads.append((uid, media_id))
                if self.after_download: self.after_download()
                return {'path': '/synthetic/clip.mp4', 'size': self.size}
            def complete_delivery(self, request_id, result): self.completed.append((request_id, result))
        class Messages:
            def __init__(self): self.videos, self.messages = [], []; self.error = None
            def send_video(self, uid, caption, path, **kwargs):
                self.videos.append((uid, caption, path, kwargs))
                if self.error: raise self.error
                self.assert_allowed = kwargs['authorize']()
            def send_message(self, uid, text):
                self.messages.append((uid, text))
                if self.error: raise self.error
        self.media, self.telegram = Media(), Messages()
        self.config = dict({'owner_id':10001}, **{'web_origin': 'https://dashboard.example.com'})

    def deliver(self):
        return process_media_deliveries(self.manager, self.telegram, self.media, self.config)

    def test_explicit_clip_delivered_once_with_gaps_metadata(self):
        self.assertEqual(self.deliver(), 1)
        self.assertEqual(self.deliver(), 0)
        self.assertEqual(len(self.telegram.videos), 1)
        self.assertIn('пропуски', self.telegram.videos[0][1])
        self.assertEqual(self.telegram.videos[0][3]['duration'], 29.5)
        self.assertEqual(self.media.completed[-1][1], 'sent')

    def test_revocation_before_or_during_download_prevents_delivery(self):
        self.access.allowed = False
        self.deliver()
        self.assertEqual(self.media.downloads, [])
        self.assertEqual(self.media.completed[-1][1], 'revoked')
        self.rows = [{'id': 'c' * 32, 'uid': 7, 'status': 'ready', 'media_id': 'd' * 32}]
        self.access.allowed = True
        self.media.after_download = lambda: setattr(self.access, 'allowed', False)
        self.deliver()
        self.assertEqual(self.telegram.videos, [])
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.media.completed[-1][1], 'revoked')

    def test_large_clip_uses_authenticated_archive_page(self):
        self.media.size = Telegram.MAX_VIDEO_BYTES + 1
        self.deliver()
        self.assertEqual(self.telegram.videos, [])
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn('https://dashboard.example.com/#media', self.telegram.messages[0][1])
        self.assertNotIn('/synthetic/', self.telegram.messages[0][1])
        self.assertEqual(self.media.completed[-1][1], 'link')

    def test_uncertain_send_is_terminal_without_replay(self):
        self.telegram.error = ApiError('network', uncertain=True)
        self.deliver(); self.deliver()
        self.assertEqual(len(self.telegram.videos), 1)
        self.assertEqual(self.media.completed[-1][1], 'unknown')

    def test_failed_preparation_notifies_requester_without_download(self):
        self.rows[0].update(status='failed', error='buffer_not_ready', media_id=None)
        self.deliver()
        self.assertEqual(self.media.downloads, [])
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.telegram.messages[0][0], 7)
        self.assertIn('интервала', self.telegram.messages[0][1])
        self.assertEqual(self.media.completed[-1][1], 'sent')

    def test_cancelled_request_is_silent(self):
        self.rows[0]['status'] = 'cancelled'
        self.deliver()
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.media.completed[-1][1], 'revoked')


class InterfaceRuntimeTests(unittest.TestCase):
    def test_revocation_sweep_and_quota_warning_are_owner_only_and_durable(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'bot.sqlite3')
            seen = []
            manager = types.SimpleNamespace(store=store, access=types.SimpleNamespace(
                owner_id=7, is_allowed=lambda uid: uid == 7))
            media = types.SimpleNamespace(
                cancel_disallowed=lambda checker: seen.append((checker(7), checker(8))),
                status=lambda: {'quota_paused': True, 'quota_warning_sequence': 1})
            try:
                maintain_media_access(manager, media)
                maintain_media_access(manager, media)
                pending = store.pending()
                self.assertEqual(seen, [(True, False), (True, False)])
                self.assertEqual([(row['event_key'], row['recipient']) for row in pending],
                                 [('media-quota:1', 7)])
                self.assertEqual(store.get('media_quota_warning_sequence'), 1)
            finally:
                store.close()

    def test_features_are_off_by_default_and_flags_are_typed(self):
        manager = types.SimpleNamespace()
        self.assertEqual(start_interfaces({}, manager), (None, None))
        self.assertIsNone(manager.media_client)
        with self.assertRaises(ValueError): start_interfaces({'web_enabled': 'false'}, manager)

    def test_web_start_failure_closes_created_resources(self):
        resources = []
        class Media:
            def __init__(self, config): self.closed = False; resources.append(self)
            def close(self): self.closed = True
        class Web:
            def __init__(self, manager, config, media):
                self.closed = False; resources.append(self)
                assert manager.media_client is media
            def start(self): raise RuntimeError('synthetic bind failure')
            def close(self): self.closed = True
        manager = types.SimpleNamespace()
        with patch('media_service.MediaClient', Media), patch('web_service.WebServer', Web):
            with self.assertRaises(RuntimeError):
                start_interfaces({'web_enabled': True, 'media_enabled': True}, manager)
        self.assertTrue(all(resource.closed for resource in resources))
        self.assertIsNone(manager.media_client)

    def test_video_camera_text_uses_ordered_lane(self):
        dispatcher = object.__new__(UpdateDispatcher)
        dispatcher.manager = types.SimpleNamespace(owners={7})
        update = {'message': {'from': {'id': 7}, 'chat': {'id': 7, 'type': 'private'},
                              'text': 'дай видео с камеры за30сек'}}
        self.assertEqual(dispatcher._lane(update), 'owner')
        update['message']['text'] = 'фото камеры'
        self.assertEqual(dispatcher._lane(update), 'photo')

    def test_runtime_closes_web_and_media_on_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'bot.sqlite3')
            telegram = RuntimeTelegram([])
            manager = Manager(dict({'owner_id':10001}, **{'allowed_user_ids': [10001], 'state_dir': folder}),
                              store, Printer(), telegram)
            manager.tick = lambda: None
            manager.check_deadlines = lambda: None
            manager.flush_outbox = lambda: None
            closed, handlers = [], {}
            media = types.SimpleNamespace(pending_deliveries=lambda **_: [],
                                          cancel_disallowed=lambda _: None,
                                          status=lambda: {},
                                          close=lambda: closed.append('media'))
            web = types.SimpleNamespace(close=lambda: closed.append('web'))
            with patch('bot.start_interfaces', return_value=(media, web)), \
                    patch('bot.signal.signal', side_effect=lambda sig, fn: handlers.setdefault(sig, fn)):
                worker = threading.Thread(target=run, args=({}, store, manager, telegram))
                worker.start()
                try:
                    self.assertTrue(telegram.polled.wait(2))
                finally:
                    for handler in handlers.values(): handler()
                    worker.join(4)
                    store.close()
                self.assertFalse(worker.is_alive())
                self.assertEqual(closed, ['web', 'media'])


if __name__ == '__main__':
    unittest.main()
