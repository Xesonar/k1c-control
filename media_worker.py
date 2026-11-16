#!/usr/bin/env python3
"""Separate bounded FFmpeg worker. It only reads printer status/history via GET."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import fcntl
import shutil
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid

from media_store import MediaStore, MediaError, job_token, QUOTA, RETENTION
from media_service import ACTIVE_STATES

BUFFER_SECONDS = 420
MAX_ROLLING_BYTES = 512 * 1024 ** 2
MAX_FRAME_BYTES = 512 * 1024 ** 2
MAX_FRAME_COUNT = 10000
MAX_CLIP_BYTES = 49_000_000
MAX_TIMELAPSE_BYTES = 128 * 1024 ** 2
SEGMENT_RE = re.compile(r'^([a-f0-9]{32})-([0-9]{9})\.ts$')
TERMINAL = {'complete', 'completed', 'cancelled', 'error'}


def validated_source(config):
    source = config.get('media_source_url', config.get('media_rtsp_url', 'rtsp://printer.local:8554/chassis'))
    if not isinstance(source, str) or len(source) > 2048 or any(ord(c) < 33 for c in source):
        raise MediaError('invalid_media_source')
    try:
        parsed = urllib.parse.urlsplit(source)
        port = parsed.port
    except ValueError:
        raise MediaError('invalid_media_source') from None
    if (parsed.scheme not in {'http', 'https', 'rtsp'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)):
        raise MediaError('invalid_media_source')
    return source, parsed.scheme


def redact_capture_error(value):
    value = re.sub(r'((?:https?|rtsps?)://)[^/\s@]+@', r'\1<REDACTED>@', value)
    value = re.sub(r'((?:https?|rtsps?)://[^?\s]+)\?[^\s]*', r'\1?<REDACTED_QUERY>', value)
    return value[-4096:]


class ProcessRunner:
    def __init__(self, ffmpeg='ffmpeg', ffprobe='ffprobe'):
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)

    def start_capture(self, args):
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, start_new_session=True)
        process.media_errors = bytearray()
        process.media_error_lock = threading.Lock()
        def drain():
            try:
                while True:
                    chunk = process.stderr.read(1024)
                    if not chunk:
                        break
                    with process.media_error_lock:
                        process.media_errors.extend(chunk)
                        if len(process.media_errors) > 8192:
                            del process.media_errors[:-8192]
            finally:
                process.stderr.close()
        threading.Thread(target=drain, name='capture-stderr', daemon=True).start()
        return process

    @staticmethod
    def capture_error(process):
        if not hasattr(process, 'media_error_lock'):
            return ''
        with process.media_error_lock:
            return redact_capture_error(bytes(process.media_errors).decode('utf-8', errors='replace'))

    def run(self, args, *, output, timeout=120, max_bytes=MAX_CLIP_BYTES, cancelled=None):
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancelled and cancelled():
                    raise MediaError('request_cancelled', 409)
                if time.monotonic() > deadline:
                    raise MediaError('media_timeout', 503)
                if Path(output).exists() and Path(output).stat().st_size > max_bytes:
                    raise MediaError('media_output_too_large', 413)
                time.sleep(.1)
            if process.returncode:
                raise MediaError('ffmpeg_failed', 503)
            path = Path(output)
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
                raise MediaError('invalid_media_output', 503)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)

    def probe(self, path):
        result = subprocess.run([self.ffprobe, '-v', 'error', '-show_entries',
                                 'format=duration,size:stream=codec_name,width,height,r_frame_rate',
                                 '-of', 'json', str(path)], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise MediaError('invalid_media_output', 503)
        try:
            data = json.loads(result.stdout)
            duration = float(data['format']['duration'])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise MediaError('invalid_media_output', 503) from None
        return {'duration': duration, 'streams': data.get('streams', [])}


def parse_manifest(path, capture_id):
    """Only complete local HLS segments with explicit UTC program dates."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        return []
    timestamp = duration = None
    segments = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            try:
                timestamp = datetime.datetime.fromisoformat(line.split(':', 1)[1].replace('Z', '+00:00')).timestamp()
            except (ValueError, OverflowError):
                timestamp = None
        elif line.startswith('#EXTINF:'):
            try:
                duration = float(line.split(':', 1)[1].split(',')[0])
            except ValueError:
                duration = None
        elif line and not line.startswith('#'):
            match = SEGMENT_RE.fullmatch(line)
            if match and match[1] == capture_id and timestamp is not None and duration is not None and 0 < duration <= 60:
                segments.append({'name': line, 'started': timestamp, 'ended': timestamp + duration})
                timestamp += duration
            timestamp = duration = None
    return segments


def coverage(segments, start, end):
    """Return honest wall-clock coverage; gaps are not filled from earlier video."""
    available, gaps, cursor = [], [], start
    for segment in sorted(segments, key=lambda row: (row['started'], row['ended'])):
        left, right = max(start, segment['started']), min(end, segment['ended'])
        if right <= left:
            continue
        if left < cursor - .1:
            # Conservatively skip a producer-overlap segment. Do not duplicate
            # earlier real time inside a requested retrospective window.
            continue
        if right <= left:
            continue
        if left > cursor + .1:
            gaps.append({'start': cursor, 'end': left, 'seconds': left - cursor})
        available.append(dict(segment, clip_start=left, clip_end=right))
        cursor = max(cursor, right)
    if cursor < end - .1:
        gaps.append({'start': cursor, 'end': end, 'seconds': end - cursor})
    duration = sum(row['clip_end'] - row['clip_start'] for row in available)
    return available, duration, max(0.0, end - start - duration), gaps


def timelapse_coverage(job, frames):
    """Sparse-frame coverage estimate; normal sampling intervals are not gaps."""
    interval = max(1.0, float(job.get('timelapse_interval') or 10))
    started, ended = float(job['started']), float(job.get('ended') or frames[-1]['at'])
    times = sorted(float(row['at']) for row in frames)
    gaps = []
    if times[0] - started > 2 * interval:
        gaps.append({'start': started, 'end': times[0], 'seconds': times[0] - started})
    for left, right in zip(times, times[1:]):
        if right - left > 2 * interval:
            gaps.append({'start': left + interval, 'end': right, 'seconds': right - left - interval})
    if ended - times[-1] > 2 * interval:
        gaps.append({'start': times[-1] + interval, 'end': ended, 'seconds': ended - times[-1] - interval})
    return {'partial': job['state'] not in {'complete', 'completed'} or bool(gaps),
            'missing_seconds': sum(gap['seconds'] for gap in gaps), 'gaps': gaps,
            'sampling_interval_seconds': interval, 'coverage_estimate': 'sparse_frames'}


def concat_escape(path):
    return str(path).replace("'", "'\\''")


class MediaWorker:
    def __init__(self, config, *, clock=time.time, fetch_context=None, runner=None, executor=None):
        self.config, self.clock = dict(config), clock
        self.store = MediaStore(config.get('media_state_dir', '/var/lib/k1c-media'),
                                quota=config.get('media_quota_bytes', QUOTA),
                                retention=config.get('media_retention_seconds', RETENTION), clock=clock)
        lock_path = self.store.root / 'worker.lock'
        self.lock_file = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'a+b')
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close(); self.store.close()
            raise MediaError('media_worker_already_running', 409) from None
        os.chmod(lock_path, 0o600)
        # Only the worker owns tmp/. Discard interrupted, unpublished outputs.
        for item in (self.store.root / 'tmp').iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
        self.runner = runner or ProcessRunner(config.get('ffmpeg_path', '/usr/bin/ffmpeg'), config.get('ffprobe_path', '/usr/bin/ffprobe'))
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='media-encode')
        self.owns_executor = executor is None
        self.fetch_context = fetch_context or self._fetch_context
        self.capture = None
        self.capture_id = None
        self.capture_job = None
        self.capture_started = None
        self.capture_retry_at = 0
        self.current_job = self.store.get_meta('current_job')
        self.future = None
        self.stopping = threading.Event()
        # An interrupted encode is safe to regenerate; no Telegram send lives here.
        with self.store.transaction():
            self.store.db.execute("UPDATE requests SET status='queued',sources='[]' WHERE status='running'")
            self.store.db.execute("UPDATE jobs SET timelapse_status='queued' WHERE timelapse_status='running'")
        self._validate_source()
        self._cleanup_orphans(force=True)

    def _validate_source(self):
        self.source, self.source_scheme = validated_source(self.config)

    def _cleanup_orphans(self, *, force=False):
        # Interrupted, unpublished files have no seven-day archive promise.
        # A committed young archive is never removed by this reconciliation.
        with self.store.transaction():
            known = {row['path'] for row in self.store.rows('SELECT path FROM archive')}
            known.update(row['path'] for row in self.store.rows('SELECT path FROM frames'))
            for folder, suffix in [('archive', 'mp4'), ('timelapse', 'jpg')]:
                for path in (self.store.root / folder).iterdir():
                    if not re.fullmatch('[a-f0-9]{32}\\.' + suffix, path.name):
                        continue
                    relative = self.store.relative(path)
                    if relative not in known and (force or path.stat().st_mtime < self.clock() - 60):
                        path.unlink(missing_ok=True)

    def _get(self, endpoint):
        url = self.config.get('printer_url', 'http://printer.local:7126').rstrip('/') + endpoint
        request = urllib.request.Request(url, method='GET')
        class NoRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise MediaError('printer_redirect_not_allowed', 503)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
        with opener.open(request, timeout=min(8, max(1, self.config.get('request_timeout', 4)))) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise MediaError('invalid_printer_status', 503)
        return json.loads(data)['result']

    def _fetch_context(self):
        data = self._get('/printer/objects/query?print_stats&virtual_sdcard&webhooks')['status']
        if data.get('webhooks', {}).get('state') != 'ready':
            return None
        stats = data.get('print_stats') or {}
        history = self._get('/server/history/list?limit=1&order=desc').get('jobs') or []
        job = history[0] if history else {}
        filename = stats.get('filename') or ''
        if filename and job.get('filename') != filename:
            return None
        job_id = job.get('job_id')
        state = stats.get('state')
        # Firmware clears print_stats.filename after an end/reset. A matching
        # stable history ID still distinguishes cancel/error/completion.
        terminal = {'completed': 'complete', 'cancelled': 'cancelled', 'error': 'error'}
        if (state == 'standby' and not filename and job_id == self.current_job
                and job.get('status') in terminal):
            state = terminal[job['status']]
            filename = str(job.get('filename') or '')
        if state in ACTIVE_STATES and (not job_id or job.get('status') != 'in_progress'):
            return None
        return {'job_id': job_id, 'filename': filename, 'state': state,
                'started_at': job.get('start_time'), 'seen_at': self.clock()}

    def capture_args(self, capture_id):
        directory = self.store.path('rolling/' + capture_id)
        directory.mkdir(mode=0o700, exist_ok=True)
        source_options = (['-rtsp_transport', 'tcp', '-timeout', '10000000']
                          if self.source_scheme == 'rtsp' else ['-rw_timeout', '10000000'])
        return [self.runner.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                *source_options, '-use_wallclock_as_timestamps', '1', '-i', self.source,
                '-map', '0:v:0', '-an', '-c:v', 'copy', '-f', 'hls', '-hls_time', '2',
                '-hls_list_size', '210', '-hls_flags', 'program_date_time+independent_segments+temp_file',
                '-hls_segment_filename', str(directory / (capture_id + '-%09d.ts')),
                str(directory / 'index.m3u8')]

    def stop_capture(self):
        process = self.capture
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        self._index_capture()
        self.capture = self.capture_id = self.capture_job = self.capture_started = None

    def _ensure_capture(self, job_id):
        if not self.config.get('media_capture_enabled', True):
            self.stop_capture(); return
        if self.capture and self.capture.poll() is None and self.capture_job == job_id:
            return
        if self.capture and self.capture.poll() is not None:
            detail = self.runner.capture_error(self.capture) if hasattr(self.runner, 'capture_error') else ''
            self.store.set_meta('capture_error', 'source_unavailable')
            self.store.set_meta('capture_error_detail', detail)
            self.stop_capture()
            self.capture_retry_at = self.clock() + 5
        if self.clock() < self.capture_retry_at:
            return
        if self.store.get_meta('capture_error') == 'rolling_storage_limit':
            return
        self.stop_capture()
        self.capture_id = uuid.uuid4().hex
        self.capture_job, self.capture_started = job_id, self.clock()
        self.capture = self.runner.start_capture(self.capture_args(self.capture_id))

    def _update_context(self, context):
        now = self.clock()
        if not isinstance(context, dict) or not context.get('job_id'):
            self.stop_capture(); return
        job_id, state = str(context['job_id']), context.get('state')
        if state in ACTIVE_STATES:
            if self.current_job and self.current_job != job_id:
                self._end_job(self.current_job, 'interrupted')
            self.current_job = job_id
            self.store.set_meta('current_job', job_id)
            started = context.get('started_at')
            if type(started) not in (int, float) or not math.isfinite(started):
                started = now
            with self.store.transaction():
                self.store.db.execute('''INSERT INTO jobs(job_id,filename,state,started,last_seen)
                    VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,last_seen=excluded.last_seen,filename=excluded.filename''',
                    (job_id, str(context.get('filename') or '')[:2048], state, started, now))
            self._ensure_capture(job_id)
        elif state in TERMINAL or state in {'standby', 'unknown'}:
            if self.current_job:
                self._end_job(self.current_job, state if state in TERMINAL and job_id == self.current_job else 'interrupted')
            self.current_job = None
            self.store.set_meta('current_job', None)
            self.stop_capture()

    def _end_job(self, job_id, state):
        with self.store.transaction():
            self.store.db.execute("UPDATE jobs SET state=?,ended=COALESCE(ended,?),last_seen=?,timelapse_status=CASE WHEN timelapse_status='recording' THEN 'queued' ELSE timelapse_status END WHERE job_id=?", (state, self.clock(), self.clock(), job_id))

    def _index_capture(self):
        if not self.capture_id:
            return
        directory = self.store.path('rolling/' + self.capture_id)
        for row in parse_manifest(directory / 'index.m3u8', self.capture_id):
            path = directory / row['name']
            if path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 32 * 1024 * 1024:
                self.store.add_segment(row['name'][:-3], self.capture_job, self.capture_id,
                                       self.store.relative(path), row['started'], row['ended'], path.stat().st_size)
                if self.store.get_meta('capture_error') == 'source_unavailable':
                    self.store.set_meta('capture_error', None)

    def _prune_rolling(self):
        cutoff = self.clock() - BUFFER_SECONDS - 15
        paused_for_space = self.store.get_meta('capture_error') == 'rolling_storage_limit'
        target = MAX_ROLLING_BYTES - 64 * 1024 ** 2 if paused_for_space else MAX_ROLLING_BYTES
        with self.store.transaction():
            sources = set()
            for row in self.store.rows("SELECT sources FROM requests WHERE status IN ('queued','running')"):
                sources.update(json.loads(row['sources']))
            rows = self.store.rows('SELECT * FROM segments ORDER BY ended')
            total = sum(row['size'] for row in rows)
            for row in rows:
                if row['path'] in sources:
                    continue
                if row['ended'] < cutoff or total > target:
                    try:
                        self.store.path(row['path']).unlink(missing_ok=True)
                    except (OSError, MediaError):
                        continue
                    self.store.db.execute('DELETE FROM segments WHERE id=?', (row['id'],))
                    total -= row['size']
            indexed = {row['path'] for row in self.store.rows('SELECT path FROM segments')}
            for path in (self.store.root / 'rolling').rglob('*'):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = self.store.relative(path)
                if relative not in indexed and path.parent.name != self.capture_id and path.stat().st_mtime < self.clock() - 30:
                    path.unlink(missing_ok=True)
        for directory in (self.store.root / 'rolling').iterdir():
            if directory.is_dir() and directory.name != self.capture_id:
                try: directory.rmdir()
                except OSError: pass
        actual = sum(p.stat().st_size for p in (self.store.root / 'rolling').rglob('*') if p.is_file())
        if paused_for_space and actual <= target:
            self.store.set_meta('capture_error', None)
        if actual > MAX_ROLLING_BYTES + 32 * 1024 * 1024:
            self.stop_capture()
            self.store.set_meta('capture_error', 'rolling_storage_limit')

    def _clip_cancelled(self, request_id):
        rows = self.store.rows('SELECT status FROM requests WHERE id=?', (request_id,))
        return not rows or rows[0]['status'] != 'running' or self.stopping.is_set()

    def _clip_segments(self, request):
        start, end = request['anchor'] - request['seconds'], request['anchor']
        rows = self.store.window(request['job_id'], start, end)
        existing = []
        for row in rows:
            try:
                self.store.path(row['path'], exists=True)
                existing.append(row)
            except MediaError:
                continue
        selected, duration, missing, gaps = coverage(existing, start, end)
        return selected, duration, missing, gaps

    def _wait_for_anchor(self, request):
        # A pending current segment may contain the request's exact endpoint.
        # Wait for its close instead of systematically returning a short clip.
        if self.capture_job != request['job_id'] or self.clock() > request['anchor'] + 10:
            return False
        rows = self.store.window(request['job_id'], request['anchor'] - 1, request['anchor'] + .01)
        return not any(row['ended'] >= request['anchor'] for row in rows)

    def process_clip(self, request_id):
        request = self.store.rows('SELECT * FROM requests WHERE id=?', (request_id,))[0]
        output = self.store.path('tmp/' + request_id + '.mp4')
        concat = self.store.path('tmp/' + request_id + '.ffconcat')
        try:
            if self.store.capacity()['paused']:
                raise MediaError('archive_full', 507)
            selected, duration, missing, gaps = self._clip_segments(request)
            if duration < .25:
                raise MediaError('buffer_not_ready', 409)
            # Only one segment per instant; create a concat timeline, then make
            # accurate endpoint cuts while encoding bounded 720p H264 on the VPS.
            with self.store.transaction():
                if self._clip_cancelled(request_id):
                    raise MediaError('request_cancelled', 409)
                self.store.db.execute('UPDATE requests SET sources=? WHERE id=?', (json.dumps([r['path'] for r in selected]), request_id))
            concat.write_text('ffconcat version 1.0\n' + ''.join("file '" + concat_escape(self.store.path(r['path'], exists=True)) + "'\nduration " + format(r['ended'] - r['started'], '.6f') + "\n" for r in selected))
            # Concat removes real capture gaps. Compute only available time, so
            # no earlier or future frames are substituted for a missing interval.
            trim_start = selected[0]['clip_start'] - selected[0]['started']
            args = [self.runner.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-threads', '1', '-f', 'concat', '-safe', '0', '-i', str(concat), '-ss', format(trim_start, '.6f'),
                    '-t', format(duration, '.6f'), '-an', '-vf',
                    'scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=15',
                    '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-tune', 'zerolatency',
                    '-b:v', '1000k', '-maxrate', '1200k', '-bufsize', '2400k', '-pix_fmt', 'yuv420p',
                    '-movflags', '+faststart', str(output)]
            self.runner.run(args, output=output, timeout=180, max_bytes=MAX_CLIP_BYTES,
                            cancelled=lambda: self._clip_cancelled(request_id))
            probed = self.runner.probe(output)
            if probed['duration'] > duration + .15 or probed['duration'] > 300.15:
                raise MediaError('invalid_clip_duration', 503)
            media_id = uuid.uuid4().hex
            final = self.store.path('archive/' + media_id + '.mp4')
            with self.store.transaction():
                if self._clip_cancelled(request_id):
                    raise MediaError('request_cancelled', 409)
                output.replace(final)
                try:
                    self.store.reserve_archive(media_id, request['job_id'], 'clip', self.store.relative(final),
                                               final.stat().st_size, probed['duration'], request_id=request_id,
                                               metadata={'requested_seconds': request['seconds'], 'anchor': request['anchor'],
                                                         'window_start': request['anchor'] - request['seconds'],
                                                         'actual_start': selected[0]['clip_start'], 'actual_end': selected[-1]['clip_end'],
                                                         'missing_seconds': missing, 'gaps': gaps, 'filename': request['filename']})
                except BaseException:
                    final.unlink(missing_ok=True); raise
                self.store.db.execute("UPDATE requests SET status='ready',completed=?,media_id=?,actual_duration=?,missing_seconds=?,gaps=?,sources='[]' WHERE id=? AND status='running'",
                                      (self.clock(), media_id, probed['duration'], missing, json.dumps(gaps), request_id))
        except MediaError as exc:
            if exc.code == 'archive_full':
                self.store.block_quota()
            with self.store.transaction():
                self.store.db.execute("UPDATE requests SET status='failed',completed=?,error=?,sources='[]' WHERE id=? AND status='running'", (self.clock(), exc.code, request_id))
        except Exception:
            with self.store.transaction():
                self.store.db.execute("UPDATE requests SET status='failed',completed=?,error='media_processing_failed',sources='[]' WHERE id=? AND status='running'", (self.clock(), request_id))
        finally:
            output.unlink(missing_ok=True); concat.unlink(missing_ok=True)

    def _frame_job(self, job_id, segment):
        frame_id = uuid.uuid4().hex
        output = self.store.path('timelapse/' + frame_id + '.jpg')
        try:
            args = [self.runner.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-threads', '1', '-i', str(self.store.path(segment['path'], exists=True)), '-threads', '1', '-frames:v', '1',
                    '-vf', 'scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2', '-q:v', '7', str(output)]
            self.runner.run(args, output=output, timeout=15, max_bytes=1024 * 1024,
                            cancelled=self.stopping.is_set)
            with self.store.transaction():
                used = self.store.rows('SELECT COALESCE(SUM(size),0) n FROM frames')[0]['n']
                if used + output.stat().st_size > MAX_FRAME_BYTES:
                    raise MediaError('timelapse_storage_limit', 507)
                self.store.db.execute('INSERT INTO frames VALUES(?,?,?,?,?)',
                                      (frame_id, job_id, self.store.relative(output), segment['started'], output.stat().st_size))
                self.store.db.execute('UPDATE jobs SET last_frame=? WHERE job_id=?', (segment['started'], job_id))
        except Exception:
            output.unlink(missing_ok=True)

    def _compact_frames(self):
        for job in self.store.rows("SELECT * FROM jobs WHERE timelapse_status IN ('recording','queued')"):
            rows = self.store.rows('SELECT * FROM frames WHERE job_id=? ORDER BY at,id', (job['job_id'],))
            if not rows or (len(rows) < MAX_FRAME_COUNT and self.store.rows('SELECT COALESCE(SUM(size),0) n FROM frames')[0]['n'] < MAX_FRAME_BYTES):
                continue
            keep = {row['id'] for row in rows[::2]} | {rows[-1]['id']}
            with self.store.transaction():
                for row in rows:
                    if row['id'] not in keep:
                        self.store.path(row['path']).unlink(missing_ok=True)
                        self.store.db.execute('DELETE FROM frames WHERE id=?', (row['id'],))
                self.store.db.execute('UPDATE jobs SET timelapse_interval=timelapse_interval*2 WHERE job_id=?', (job['job_id'],))

    def process_timelapse(self, job_id):
        rows = self.store.rows('SELECT * FROM frames WHERE job_id=? ORDER BY at,id', (job_id,))
        output_id = uuid.uuid4().hex
        directory = self.store.path('tmp/' + output_id)
        output = directory / 'timelapse.mp4'
        error = None
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            if self.store.capacity()['paused']:
                raise MediaError('archive_full', 507)
            if not rows:
                raise MediaError('timelapse_unavailable', 409)
            for number, row in enumerate(rows):
                os.link(self.store.path(row['path'], exists=True), directory / ('frame_%08d.jpg' % number))
            args = [self.runner.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-framerate', '24', '-i', str(directory / 'frame_%08d.jpg'), '-an',
                    '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    '-b:v', '1000k', '-maxrate', '1200k', '-bufsize', '2400k', '-movflags', '+faststart', str(output)]
            self.runner.run(args, output=output, timeout=180, max_bytes=MAX_TIMELAPSE_BYTES, cancelled=self.stopping.is_set)
            probe = self.runner.probe(output)
            job = self.store.rows('SELECT * FROM jobs WHERE job_id=?', (job_id,))[0]
            final = self.store.path('archive/' + output_id + '.mp4')
            output.replace(final)
            try:
                self.store.reserve_archive(output_id, job_id, 'timelapse', self.store.relative(final),
                                           final.stat().st_size, probe['duration'], metadata={
                                               'status': job['state'], 'filename': job['filename'], 'frames': len(rows),
                                               'available_start': rows[0]['at'], 'available_end': rows[-1]['at'],
                                               'job_started': job['started'], 'job_ended': job['ended'], 'accelerated': True,
                                               **timelapse_coverage(job, rows)})
            except BaseException:
                final.unlink(missing_ok=True); raise
        except MediaError as exc:
            error = exc.code
        except Exception:
            error = 'media_processing_failed'
        finally:
            with self.store.transaction():
                self.store.db.execute('UPDATE jobs SET timelapse_status=?,error=? WHERE job_id=?', ('failed' if error else 'ready', error, job_id))
                self.store.db.execute('DELETE FROM frames WHERE job_id=?', (job_id,))
            for row in rows:
                self.store.path(row['path']).unlink(missing_ok=True)
            if directory.is_dir():
                for path in directory.iterdir():
                    path.unlink(missing_ok=True)
                directory.rmdir()

    def _schedule(self):
        if self.future is not None and not self.future.done():
            return
        if self.future is not None:
            # All task functions contain their own failure accounting.
            try:
                self.future.result()
            except Exception:
                self.store.set_meta('last_processing_error', 'media_task_failed')
            finally:
                self.future = None
        for request in self.store.rows("SELECT * FROM requests WHERE status='queued' ORDER BY created LIMIT 8"):
            if self._wait_for_anchor(request):
                continue
            with self.store.transaction():
                changed = self.store.db.execute("UPDATE requests SET status='running',started=? WHERE id=? AND status='queued'", (self.clock(), request['id'])).rowcount
            if changed:
                self.future = self.executor.submit(self.process_clip, request['id']); return
        jobs = self.store.rows("SELECT job_id FROM jobs WHERE timelapse_status='queued' ORDER BY ended LIMIT 1")
        if jobs:
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET timelapse_status='running' WHERE job_id=? AND timelapse_status='queued'", (jobs[0]['job_id'],))
            self.future = self.executor.submit(self.process_timelapse, jobs[0]['job_id']); return
        if self.current_job and not self.store.capacity()['paused']:
            jobs = self.store.rows('SELECT * FROM jobs WHERE job_id=?', (self.current_job,))
            rows = self.store.rows('SELECT * FROM segments WHERE job_id=? ORDER BY ended DESC LIMIT 1', (self.current_job,))
            if jobs and rows and rows[0]['started'] - jobs[0]['last_frame'] >= jobs[0]['timelapse_interval']:
                self.future = self.executor.submit(self._frame_job, self.current_job, rows[0])

    def run_once(self):
        self.store.cleanup_expired()
        self._cleanup_orphans()
        try:
            context = self.fetch_context()
            self._update_context(context)
            context_error = None
        except Exception:
            self.stop_capture(); context_error = 'printer_status_unavailable'
        self._index_capture()
        self._prune_rolling()
        self._compact_frames()
        self._schedule()
        recent = self.store.rows('SELECT MAX(ended) n FROM segments WHERE capture_id=?', (self.capture_id or '',))[0]['n']
        runtime = {'active_capture': self.capture_id, 'job_id': self.capture_job,
                   'stream_ready': recent is not None and self.clock() - recent < 20,
                   'last_segment_end': recent, 'seen_at': self.clock(),
                   'error': context_error or self.store.get_meta('capture_error')}
        self.store.set_meta('runtime', runtime)
        return runtime

    def run(self):
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                self.store.set_meta('runtime', {'error': 'media_worker_error', 'seen_at': self.clock()})
            self.stopping.wait(max(.1, 1 - (time.monotonic() - started)))

    def close(self):
        self.stopping.set()
        self.stop_capture()
        if self.owns_executor:
            self.executor.shutdown(wait=True)
        self.store.set_meta('runtime', {'active_capture': None, 'error': 'worker_stopped', 'seen_at': self.clock()})
        self.store.close()
        self.lock_file.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.check_config:
        for key in ('ffmpeg_path', 'ffprobe_path'):
            if not Path(config.get(key, '/usr/bin/' + key[:-5])).is_file():
                raise SystemExit('missing_' + key)
        try:
            validated_source(config)
        except MediaError as exc:
            raise SystemExit(exc.code) from None
        print('media config OK')
        return
    worker = MediaWorker(config)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stopping.set())
    try:
        worker.run()
    finally:
        worker.close()


if __name__ == '__main__':
    main()
