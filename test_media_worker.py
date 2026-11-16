"""Offline fixtures only. Integration generates synthetic colors, never a camera."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import uuid

from media_service import MediaClient
from media_store import MediaError
from media_worker import MediaWorker, ProcessRunner, coverage, parse_manifest, timelapse_coverage, validated_source, redact_capture_error


class FakeProcess:
    def __init__(self):
        self.returncode = None
    def poll(self): return self.returncode
    def terminate(self): self.returncode = 0
    def kill(self): self.returncode = -9
    def wait(self, timeout=None): self.returncode = 0; return 0


class FakeRunner:
    ffmpeg = 'fake-ffmpeg'
    def __init__(self):
        self.commands, self.durations = [], {}
        self.fail = False
        self.on_run = None
    def start_capture(self, args):
        self.commands.append(args); return FakeProcess()
    def run(self, args, *, output, **kwargs):
        self.commands.append(args)
        if self.on_run: self.on_run()
        if kwargs.get('cancelled') and kwargs['cancelled'](): raise MediaError('request_cancelled')
        if self.fail: raise MediaError('ffmpeg_failed')
        Path(output).write_bytes(b'synthetic-media')
        self.durations[str(output)] = float(args[args.index('-t') + 1]) if '-t' in args else 1.0
    def probe(self, path): return {'duration': self.durations[str(path)], 'streams': [{'codec_name': 'h264'}]}


class InlineExecutor:
    def submit(self, fn, *args):
        future = Future()
        try: future.set_result(fn(*args))
        except BaseException as exc: future.set_exception(exc)
        return future


class MediaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = 1001.0
        self.config = dict({'owner_id':10001}, **{'media_state_dir': self.temp.name, 'media_capture_enabled': False})
        self.client = MediaClient(self.config, clock=lambda: self.now); self.addCleanup(self.client.close)
        self.runner = FakeRunner()
        self.worker = MediaWorker(self.config, clock=lambda: self.now, runner=self.runner,
                                  fetch_context=lambda: None, executor=InlineExecutor())
        self.addCleanup(self.worker.close)
        self.capture_id = uuid.uuid4().hex
        self.directory = Path(self.temp.name) / 'rolling' / self.capture_id; self.directory.mkdir()

    def segment(self, start, end, job='job-a', number=None):
        number = int(start * 100) if number is None else number
        name = self.capture_id + '-%09d.ts' % number
        path = self.directory / name; path.write_bytes(b'fake-ts')
        self.worker.store.add_segment(name[:-3], job, self.capture_id, self.worker.store.relative(path), start, end, path.stat().st_size)
        return path

    def request(self, seconds=30, notify=False):
        return self.client.request_clip(123, seconds, {'state': 'printing', 'job_id': 'job-a', 'filename': 'part.gcode', 'seen_at': self.now}, notify=notify)

    def process(self, request):
        with self.worker.store.transaction():
            self.worker.store.db.execute("UPDATE requests SET status='running' WHERE id=?", (request['id'],))
        self.worker.process_clip(request['id'])
        return self.client.get(123, request['id'])

    def test_request_anchor_does_not_move_while_queued_and_exact_endpoint_is_trimmed(self):
        for start in range(970, 1004, 2): self.segment(start, start + 2)
        request = self.request()
        self.now += 20
        result = self.process(request)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['anchor'], 1001)
        self.assertEqual(result['actual_duration'], 30)
        command = self.runner.commands[-1]
        self.assertEqual(command[command.index('-ss') + 1], '1.000000')
        self.assertEqual(command[command.index('-t') + 1], '30.000000')
        archived = self.client.list(123)[0]['metadata']
        self.assertEqual(archived['actual_end'], 1001)
        self.assertEqual(archived['actual_start'], 971)
        self.assertEqual(result['missing_seconds'], 0)
        self.assertNotIn('rtsp://', ' '.join(command))

    def test_job_binding_and_real_gap_do_not_substitute_other_frames(self):
        self.segment(971, 980); self.segment(985, 1001)
        other = self.segment(971, 1001, job='job-b', number=99999999)
        result = self.process(self.request())
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['actual_duration'], 25)
        self.assertEqual(result['missing_seconds'], 5)
        self.assertEqual(result['gaps'], [{'start': 980.0, 'end': 985.0, 'seconds': 5.0}])
        # The generated concat file is removed after encoding; paths are only
        # selected from job-a and the archived metadata remains job-bound.
        self.assertEqual(self.client.list(123)[0]['job_id'], 'job-a')

    def test_current_segment_can_finish_before_processing_fixed_anchor(self):
        self.worker.capture_job = 'job-a'
        self.segment(970, 1000)
        request = self.request()
        self.assertTrue(self.worker._wait_for_anchor(request))
        self.segment(1000, 1002)
        self.assertFalse(self.worker._wait_for_anchor(request))
        self.assertEqual(self.process(request)['actual_duration'], 30)

    def test_revoked_running_request_cannot_publish_or_deliver(self):
        self.segment(971, 1001)
        request = self.request(notify=True)
        self.runner.on_run = lambda: self.client.cancel_for_user(123)
        result = self.process(request)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.client.list(123), [])
        self.assertEqual(self.client.pending_deliveries(), [])

    def test_failed_encode_has_no_archive_or_delivery(self):
        self.segment(971, 1001)
        self.runner.fail = True
        result = self.process(self.request(notify=True))
        self.assertEqual(result['error'], 'ffmpeg_failed')
        self.assertEqual(self.client.list(123), [])
        errors = self.client.pending_deliveries()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['status'], 'failed')
        self.assertEqual(self.client.pending_deliveries(), [])

    def test_output_limit_and_cpu_bound_are_explicit(self):
        self.segment(971, 1001)
        self.process(self.request())
        command = self.runner.commands[-1]
        self.assertEqual(command[command.index('-threads') + 1], '1')
        self.assertEqual(command[command.index('-maxrate') + 1], '1200k')
        self.assertNotIn('shell', command)

    def test_pause_is_active_and_job_change_finishes_old_job(self):
        self.worker._update_context({'job_id': 'old', 'filename': 'old.gcode', 'state': 'paused'})
        self.worker._update_context({'job_id': 'new', 'filename': 'new.gcode', 'state': 'preparing'})
        old = self.worker.store.rows('SELECT * FROM jobs WHERE job_id=?', ('old',))[0]
        self.assertEqual(old['state'], 'interrupted')
        self.assertEqual(old['timelapse_status'], 'queued')
        self.assertEqual(self.worker.current_job, 'new')
        self.assertEqual(self.runner.commands, [])

    def test_terminal_timelapse_is_site_only_and_carries_cancel_status(self):
        self.worker._update_context({'job_id': 'old', 'filename': 'old.gcode', 'state': 'printing'})
        for i in range(3):
            name = uuid.uuid4().hex; path = Path(self.temp.name) / 'timelapse' / (name + '.jpg'); path.write_bytes(b'fake-jpeg')
            with self.worker.store.transaction():
                self.worker.store.db.execute('INSERT INTO frames VALUES(?,?,?,?,?)', (name, 'old', self.worker.store.relative(path), 950 + i * 10, 9))
        self.worker._update_context({'job_id': 'old', 'state': 'cancelled'})
        self.worker.process_timelapse('old')
        items = self.client.list(123)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['kind'], 'timelapse')
        self.assertEqual(items[0]['metadata']['status'], 'cancelled')
        self.assertEqual(self.client.pending_deliveries(), [])

    def test_failed_background_future_does_not_stall_next_work(self):
        broken = Future(); broken.set_exception(OSError('injected storage failure'))
        self.worker.future = broken
        self.worker._schedule()
        self.assertIsNone(self.worker.future)
        self.assertEqual(self.worker.store.get_meta('last_processing_error'), 'media_task_failed')

    def test_timelapse_metadata_marks_only_real_gaps_and_noncomplete_status(self):
        full = {'state': 'complete', 'started': 1000, 'ended': 1030, 'timelapse_interval': 10}
        frames = [{'at': value} for value in [1000, 1010, 1020]]
        result = timelapse_coverage(full, frames)
        self.assertFalse(result['partial']); self.assertEqual(result['missing_seconds'], 0)
        late = timelapse_coverage(dict(full, started=900), frames)
        self.assertTrue(late['partial']); self.assertEqual(late['missing_seconds'], 100)
        cancelled = timelapse_coverage(dict(full, state='cancelled'), frames)
        self.assertTrue(cancelled['partial']); self.assertEqual(cancelled['missing_seconds'], 0)
        missing = timelapse_coverage(dict(full, ended=1080), [{'at': 1000}, {'at': 1040}])
        self.assertTrue(missing['partial']); self.assertEqual(missing['missing_seconds'], 60)

    def test_http_and_rtsp_inputs_have_only_protocol_appropriate_options(self):
        cap = uuid.uuid4().hex
        self.worker.config['media_source_url'] = 'http://printer.local:1984/api/stream.mp4?src=chassis'
        self.worker._validate_source()
        args = self.worker.capture_args(cap)
        self.assertIn('-rw_timeout', args)
        self.assertEqual(args[args.index('-use_wallclock_as_timestamps') + 1], '1')
        self.assertLess(args.index('-use_wallclock_as_timestamps'), args.index('-i'))
        self.assertNotIn('-rtsp_transport', args)
        self.assertNotIn('-timeout', args)
        self.worker.config.pop('media_source_url')
        self.worker.config['media_rtsp_url'] = 'rtsp://printer.local:8554/chassis'
        self.worker._validate_source()
        args = self.worker.capture_args(cap)
        self.assertIn('-rtsp_transport', args)
        self.assertIn('-timeout', args)
        self.assertNotIn('-rw_timeout', args)

    def test_media_input_validator_rejects_credentials_fragments_controls_and_other_protocols(self):
        for source in ['file:///etc/passwd', 'http:///missing', 'rtsp://user:secret@host/chassis',
                       'https://user@host/live', 'http://host/live#secret', 'http://host/live\n',
                       'http://host:99999/live', 'ftp://host/live', None]:
            with self.subTest(source=source), self.assertRaises(MediaError):
                validated_source({'media_source_url': source})
        self.assertEqual(validated_source({'media_source_url': 'https://host/live?src=chassis'})[1], 'https')
        self.assertEqual(validated_source({'media_rtsp_url': 'rtsp://host/chassis'})[1], 'rtsp')

    def test_capture_error_tail_is_bounded_and_redacts_credentials_and_queries(self):
        error = 'x' * 9000 + ' rtsp://user:pass@host/live?token=private http://host/live?key=secret'
        redacted = redact_capture_error(error)
        self.assertLessEqual(len(redacted), 4096)
        self.assertNotIn('user:pass', redacted)
        self.assertNotIn('token=private', redacted)
        self.assertNotIn('key=secret', redacted)

    def test_empty_standby_stats_use_only_matching_terminal_history_label(self):
        for status, expected in [('cancelled', 'cancelled'), ('completed', 'complete'), ('error', 'error')]:
            with self.subTest(status=status):
                job_id = 'job-' + status
                self.worker._update_context({'job_id': job_id, 'filename': 'part.gcode', 'state': 'printing'})
                def get(endpoint):
                    if endpoint.startswith('/printer/'):
                        return {'status': {'webhooks': {'state': 'ready'}, 'print_stats': {'state': 'standby', 'filename': ''}}}
                    return {'jobs': [{'job_id': job_id, 'filename': 'part.gcode', 'status': status, 'start_time': 900}]}
                self.worker._get = get
                context = self.worker._fetch_context()
                self.assertEqual(context['state'], expected)
                self.assertEqual(context['filename'], 'part.gcode')
                self.worker._update_context(context)
                self.assertEqual(self.worker.store.rows('SELECT state FROM jobs WHERE job_id=?', (job_id,))[0]['state'], expected)
        self.worker.current_job = 'different-job'
        self.assertEqual(self.worker._fetch_context()['state'], 'standby')

    def test_manifest_only_accepts_local_completed_timestamped_segments(self):
        cap = self.capture_id
        path = self.directory / 'index.m3u8'
        path.write_text('#EXTM3U\n#EXTINF:2.000,\n#EXT-X-PROGRAM-DATE-TIME:2026-11-16T00:00:00.000+00:00\n' + cap + '-000000001.ts\n#EXTINF:2,\nhttp://evil/file.ts\n')
        rows = parse_manifest(path, cap)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['ended'] - rows[0]['started'], 2)


FFMPEG = os.environ.get('MEDIA_TEST_FFMPEG') or shutil.which('ffmpeg')
FFPROBE = os.environ.get('MEDIA_TEST_FFPROBE') or shutil.which('ffprobe')


@unittest.skipUnless(FFMPEG and FFPROBE, 'synthetic integration requires ffmpeg + ffprobe')
class SyntheticFFmpegTests(unittest.TestCase):
    def test_exact_past_thirty_seconds_does_not_include_future_color_frames(self):
        """Synthetic red0..31s + blue31..40s; request1..31s, never future blue."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); capture = uuid.uuid4().hex
            directory = root / 'rolling' / capture; directory.mkdir(parents=True)
            command = [FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                       '-f', 'lavfi', '-i', 'color=red:s=64x64:r=10:d=31',
                       '-f', 'lavfi', '-i', 'color=blue:s=64x64:r=10:d=9',
                       '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
                       '-c:v', 'libx264', '-threads', '1', '-g', '20', '-keyint_min', '20', '-sc_threshold', '0',
                       '-pix_fmt', 'yuv420p', '-f', 'segment', '-segment_time', '2', '-reset_timestamps', '1',
                       str(directory / (capture + '-%09d.ts'))]
            subprocess.run(command, stdin=subprocess.DEVNULL, check=True, timeout=30, capture_output=True)
            clock = lambda: 1031.0
            client = MediaClient(td, clock=clock)
            worker = MediaWorker({'media_state_dir': td, 'media_capture_enabled': False}, clock=clock,
                                 runner=ProcessRunner(FFMPEG, FFPROBE), fetch_context=lambda: None)
            try:
                for index, path in enumerate(sorted(directory.glob('*.ts'))):
                    worker.store.add_segment(path.stem, 'synthetic', capture, worker.store.relative(path), 1000 + index * 2, 1002 + index * 2, path.stat().st_size)
                request = client.request_clip(123, 30, {'job_id': 'synthetic', 'state': 'printing', 'filename': 'synthetic.gcode'})
                with worker.store.transaction(): worker.store.db.execute("UPDATE requests SET status='running' WHERE id=?", (request['id'],))
                worker.process_clip(request['id'])
                result = client.get(123, request['id'])
                self.assertEqual(result['status'], 'ready', result)
                self.assertAlmostEqual(result['actual_duration'], 30.0, delta=.08)
                info = client.download(123, result['media_id'])
                # Read only a numeric pixel from synthetic output. No real
                # images, streams, videos or camera are accessed by this test.
                raw = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-sseof', '-0.1', '-i', info['path'],
                                      '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                     stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10).stdout
                self.assertGreater(len(raw), 2)
                self.assertGreater(raw[0], 200, 'last frame must be red before anchor')
                self.assertLess(raw[2], 50, 'future blue frame leaked past request anchor')
            finally:
                worker.close(); client.close()

    def test_completed_timelapse_from_synthetic_jpegs_is_h264_and_site_only(self):
        with tempfile.TemporaryDirectory() as td:
            now = [1000.0]
            client = MediaClient(td, clock=lambda: now[0])
            worker = MediaWorker({'media_state_dir': td, 'media_capture_enabled': False}, clock=lambda: now[0],
                                 runner=ProcessRunner(FFMPEG, FFPROBE), fetch_context=lambda: None)
            try:
                worker._update_context({'job_id': 'synthetic-tl', 'filename': 'synthetic.gcode', 'state': 'printing', 'started_at': 1000})
                generated = Path(td) / 'generated'; generated.mkdir()
                subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                                '-f', 'lavfi', '-i', 'color=red:s=64x64:r=1:d=3', '-frames:v', '3', '-threads', '1',
                                str(generated / 'frame_%03d.jpg')], stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10)
                for index, source in enumerate(sorted(generated.glob('*.jpg'))):
                    frame_id = uuid.uuid4().hex
                    target = Path(td) / 'timelapse' / (frame_id + '.jpg'); source.replace(target)
                    with worker.store.transaction():
                        worker.store.db.execute('INSERT INTO frames VALUES(?,?,?,?,?)',
                                                (frame_id, 'synthetic-tl', worker.store.relative(target), 1000 + index * 10, target.stat().st_size))
                now[0] = 1030
                worker._update_context({'job_id': 'synthetic-tl', 'state': 'complete'})
                worker.process_timelapse('synthetic-tl')
                items = client.list(123)
                self.assertEqual(len(items), 1, worker.store.rows('SELECT * FROM jobs'))
                item = items[0]
                self.assertEqual(item['kind'], 'timelapse')
                self.assertEqual(item['metadata']['frames'], 3)
                self.assertEqual(item['metadata']['status'], 'complete')
                self.assertFalse(item['metadata']['partial'])
                self.assertAlmostEqual(item['duration'], 3 / 24, delta=.08)
                probe = worker.runner.probe(client.download(123, item['id'])['path'])
                self.assertEqual(probe['streams'][0]['codec_name'], 'h264')
                self.assertEqual(client.pending_deliveries(), [])
            finally:
                worker.close(); client.close()

    def test_http_mp4_input_uses_real_capture_argv_and_produces_hls_segments(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'source.mp4'
            subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'color=red:s=64x64:r=10:d=8', '-c:v', 'libx264', '-threads', '1',
                            '-g', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(source)],
                           stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=15)
            payload = source.read_bytes()
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
                def log_message(self, *_): pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            worker = MediaWorker({'media_state_dir': str(Path(td) / 'media'), 'media_capture_enabled': False,
                                  'media_source_url': 'http://127.0.0.1:%s/source.mp4?src=synthetic' % server.server_port},
                                 runner=ProcessRunner(FFMPEG, FFPROBE), fetch_context=lambda: None)
            try:
                capture = uuid.uuid4().hex
                command = worker.capture_args(capture)
                self.assertNotIn('-rtsp_transport', command)
                result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr[-1000:])
                directory = Path(td) / 'media' / 'rolling' / capture
                segments = parse_manifest(directory / 'index.m3u8', capture)
                # A finite HTTP file arrives in a burst; wallclock timestamping
                # may legitimately mux one segment. Live rate is separately
                # verified by a bounded metadata-only deployment probe.
                self.assertGreaterEqual(len(segments), 1)
                self.assertTrue(all((directory / row['name']).stat().st_size > 0 for row in segments))
                probe = worker.runner.probe(directory / segments[0]['name'])
                self.assertEqual(probe['streams'][0]['codec_name'], 'h264')
            finally:
                worker.close(); server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__': unittest.main()
