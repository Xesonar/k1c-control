#!/usr/bin/env python3
"""Private single-job OrcaSlicer worker. Never contacts a printer."""
import argparse
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger('k1c-slicer')
MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_TRIANGLES = 1_000_000
NUMBER = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'


class SliceError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def validate_stl(path, bed_size=(220, 220, 250), max_triangles=MAX_TRIANGLES):
    """Read STL geometry only. STL has no units; dimensions are millimetres."""
    raw = Path(path).read_bytes()
    if not raw or len(raw) > MAX_INPUT:
        raise SliceError('invalid_stl')
    lower = [math.inf] * 3
    upper = [-math.inf] * 3
    count = 0

    def triangle(vertices):
        nonlocal count
        count += 1
        if count > max_triangles:
            raise SliceError('too_many_triangles')
        for vertex in vertices:
            if len(vertex) != 3 or not all(math.isfinite(value) for value in vertex):
                raise SliceError('invalid_stl')
            for axis, value in enumerate(vertex):
                lower[axis] = min(lower[axis], value)
                upper[axis] = max(upper[axis], value)

    binary_count = struct.unpack_from('<I', raw, 80)[0] if len(raw) >= 84 else 0
    if len(raw) >= 84 and len(raw) == 84 + 50 * binary_count:
        if not binary_count or binary_count > max_triangles:
            raise SliceError('too_many_triangles' if binary_count else 'invalid_stl')
        for offset in range(84, len(raw), 50):
            values = struct.unpack_from('<12f', raw, offset)
            if not all(math.isfinite(value) for value in values):
                raise SliceError('invalid_stl')
            triangle((values[3:6], values[6:9], values[9:12]))
    else:
        try:
            text = raw.decode('ascii')
        except UnicodeError:
            raise SliceError('invalid_stl') from None
        # Strict facet structure prevents arbitrary text from being accepted as
        # a mesh; solid names remain descriptive and never become paths/flags.
        in_solid, state, vertices = False, 'facet', []
        for line in text.splitlines():
            words = line.strip().split()
            if not words:
                continue
            key = words[0].lower()
            try:
                if key == 'solid' and not in_solid and state == 'facet':
                    in_solid = True
                elif key == 'endsolid' and in_solid and state == 'facet':
                    in_solid = False
                elif key == 'facet' and in_solid and state == 'facet' and len(words) == 5 and words[1].lower() == 'normal':
                    if not all(math.isfinite(float(value)) for value in words[2:]):
                        raise SliceError('invalid_stl')
                    vertices, state = [], 'outer'
                elif key == 'outer' and state == 'outer' and [part.lower() for part in words] == ['outer', 'loop']:
                    state = 'vertices'
                elif key == 'vertex' and state == 'vertices' and len(words) == 4 and len(vertices) < 3:
                    vertices.append(tuple(float(value) for value in words[1:]))
                elif key == 'endloop' and state == 'vertices' and len(words) == 1 and len(vertices) == 3:
                    state = 'endfacet'
                elif key == 'endfacet' and state == 'endfacet' and len(words) == 1:
                    triangle(vertices)
                    state = 'facet'
                else:
                    raise SliceError('invalid_stl')
            except (ValueError, OverflowError):
                raise SliceError('invalid_stl') from None
        if in_solid or state != 'facet':
            raise SliceError('invalid_stl')
    dimensions = [upper[i] - lower[i] for i in range(3)]
    if not count or any(not math.isfinite(value) for value in dimensions) or max(dimensions) <= 0:
        raise SliceError('invalid_stl')
    # Auto-orient may exchange axes. A part wider than the bed's space diagonal
    # cannot fit in any orientation; finer fit/path checks belong to Orca.
    diagonal = math.sqrt(sum(value * value for value in bed_size))
    if max(dimensions) > diagonal + .01:
        raise SliceError('oversize')
    return {'triangles': count, 'dimensions_mm': dimensions}


def gcode_metadata(path):
    metadata = {}
    with Path(path).open('r', encoding='utf-8', errors='replace') as source:
        for line in source:
            if not line.startswith(';'):
                continue
            if 'estimated printing time (normal mode)' in line:
                text = line.partition('=')[2].strip()
                parts = re.findall(r'(' + NUMBER + r')\s*([dhms])', text)
                if parts and re.fullmatch(r'\s*(?:' + NUMBER + r'\s*[dhms]\s*)+', text):
                    seconds = sum(float(value) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit] for value, unit in parts)
                    if math.isfinite(seconds):
                        metadata['print_time_seconds'] = seconds
            if re.match(r';\s*(?:total\s+)?filament used \[g\]\s*=', line):
                text = line.partition('=')[2].strip()
                if re.fullmatch(NUMBER + r'(?:\s*,\s*' + NUMBER + r')*', text):
                    grams = sum(float(value) for value in text.split(','))
                    if math.isfinite(grams):
                        metadata['filament_grams'] = grams
    return metadata


class Slicer:
    def __init__(self, config):
        self.config = config
        self.key = config.get('api_key')
        if not isinstance(self.key, str) or len(self.key) < 16:
            raise ValueError('invalid_api_key')
        self.command = config.get('orca_command')
        self.settings = config.get('settings_files')
        self.filament = config.get('filament_file')
        self.data_dir = config.get('data_dir')
        self.state_dir = Path(config.get('state_dir', os.environ.get('STATE_DIRECTORY', '/var/lib/k1c-slicer')))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout = config.get('timeout_seconds', 240)
        self.bed_size = config.get('bed_size', [220, 220, 250])
        self.strict = config.get('strict_slicing', True)
        if (not isinstance(self.command, list) or not self.command or
                not all(isinstance(item, str) and item and '\x00' not in item for item in self.command) or
                not Path(self.command[0]).is_absolute() or not isinstance(self.settings, list) or not self.settings or
                not all(isinstance(item, str) and Path(item).is_absolute() for item in self.settings) or
                not isinstance(self.filament, str) or not Path(self.filament).is_absolute() or
                not isinstance(self.data_dir, str) or not Path(self.data_dir).is_absolute() or
                type(self.timeout) not in {int, float} or not 0 < self.timeout <= 240 or
                not isinstance(self.bed_size, list) or len(self.bed_size) != 3 or
                not all(type(value) in {int, float} and math.isfinite(value) and value > 0 for value in self.bed_size) or
                type(self.strict) is not bool):
            raise ValueError('invalid_configuration')
        self.slot = threading.Lock()
        self.process_lock = threading.Lock()
        self.process = None
        self.stopping = False

    def ready(self):
        try:
            return (not self.stopping and Path(self.command[0]).is_file() and os.access(self.command[0], os.X_OK)
                    and all(Path(path).is_file() for path in self.settings + [self.filament]) and Path(self.data_dir).is_dir())
        except OSError:
            # DynamicUser must report unavailable if an installed bundle or
            # profile is unreadable, rather than dropping the HTTP request.
            return False

    def stop(self):
        self.stopping = True
        with self.process_lock:
            process = self.process
        if process is not None:
            self._terminate(process)

    @staticmethod
    def _terminate(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # Kill any grandchildren even if the parent already exited on TERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def _filament_profile(self, directory, temperatures):
        try:
            if Path(self.filament).stat().st_size > 1024 * 1024:
                raise ValueError()
            profile = json.loads(Path(self.filament).read_text())
            if not isinstance(profile, dict) or profile.get('inherits'):
                # Setup must provide the fully resolved, owner-controlled file.
                raise ValueError()
            profile.pop('inherits', None)
        except (OSError, ValueError):
            raise SliceError('configuration', 503) from None
        nozzle, bed = 240, 70
        if temperatures is not None:
            nozzle, bed = temperatures['nozzle'], temperatures['bed']
        def set_value(key, value):
            rendered = format(value, '.12g')
            profile[key] = [rendered] if isinstance(profile.get(key), list) else rendered
        for key in ('nozzle_temperature', 'nozzle_temperature_initial_layer'):
            # Orca filament temperatures are per extruder arrays.
            profile[key] = [format(nozzle, '.12g')]
        for plate in ('cool', 'eng', 'hot', 'textured', 'epoxy_resin'):
            for suffix in ('_plate_temp', '_plate_temp_initial_layer'):
                set_value(plate + suffix, bed)
        destination = Path(directory) / 'filament.json'
        destination.write_text(json.dumps(profile, ensure_ascii=False))
        return destination

    def slice(self, source, directory, temperatures=None):
        validate_stl(source, self.bed_size)
        filament = self._filament_profile(directory, temperatures)
        output = Path(directory) / 'output'
        output.mkdir(mode=0o700)
        arguments = self.command + ['--datadir', self.data_dir, '--load-settings', ';'.join(self.settings),
            '--load-filaments', str(filament), '--arrange', '1', '--orient', '1', '--ensure-on-bed',
            '--slice', '0', '--outputdir', str(output)]
        if '--curr-bed-type' not in self.command:
            arguments.extend(['--curr-bed-type', 'Textured PEI Plate'])
        if self.strict:
            arguments.append('--strict')
        arguments.extend(['--', str(source)])
        # The executable and all arguments except the generated STL path are
        # owner-controlled configuration. No shell, network upload or printer command.
        with (Path(directory) / 'engine.log').open('wb') as log:
            try:
                with self.process_lock:
                    if self.stopping:
                        raise SliceError('unavailable', 503)
                    process = subprocess.Popen(arguments, cwd=directory, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    self.process = process
                try:
                    returncode = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    raise SliceError('slice_timeout', 504) from None
                finally:
                    with self.process_lock:
                        self.process = None
            except OSError:
                raise SliceError('engine_unavailable', 503) from None
        if self.stopping:
            raise SliceError('unavailable', 503)
        if returncode != 0:
            raise SliceError('invalid_model')
        result = output / 'result.json'
        if result.exists():
            try:
                if result.stat().st_size > 1024 * 1024:
                    raise ValueError('oversize_result')
                details = json.loads(result.read_text())
                if type(details.get('return_code')) is not int or details['return_code'] != 0:
                    raise SliceError('invalid_model')
            except (ValueError, AttributeError, OSError):
                raise SliceError('invalid_engine_result', 502) from None
        files = sorted(output.glob('plate_*.gcode'))
        if len(files) != 1 or files[0].name != 'plate_1.gcode' or files[0].is_symlink():
            raise SliceError('invalid_engine_result', 502)
        target = files[0]
        if not 0 < target.stat().st_size <= MAX_OUTPUT:
            raise SliceError('output_too_large', 502)
        with target.open('rb') as generated:
            prefix = generated.read(4096)
        if b'\x00' in prefix or not any(part in prefix for part in (b'G-code', b'gcode', b'OrcaSlicer', b'G90', b'M104')):
            raise SliceError('invalid_engine_result', 502)
        return target, gcode_metadata(target)


class SlicerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address, slicer):
        if address[0] != '127.0.0.1':
            raise ValueError('localhost_required')
        self.slicer = slicer
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        # Request headers and arbitrary user data never enter the journal.
        pass

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authorized(self):
        supplied = self.headers.get('X-Slicer-Key', '')
        if not hmac.compare_digest(supplied.encode(), self.server.slicer.key.encode()):
            self._json(401, {'error': 'unauthorized'})
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        if self.path != '/health':
            self._json(404, {'error': 'not_found'})
            return
        ready = self.server.slicer.ready()
        self._json(200 if ready else 503, {'ready': ready, 'busy': self.server.slicer.slot.locked(),
            'engine': 'OrcaSlicer', 'material': 'PETG', **({} if ready else {'error': 'configuration'})})

    def do_POST(self):
        if not self._authorized():
            return
        if self.path != '/slice':
            self._json(404, {'error': 'not_found'})
            return
        try:
            temperatures = None
            nozzle_header, bed_header = self.headers.get('X-Nozzle-Temperature'), self.headers.get('X-Bed-Temperature')
            if nozzle_header is not None or bed_header is not None:
                if (len(self.headers.get_all('X-Nozzle-Temperature', [])) != 1
                        or len(self.headers.get_all('X-Bed-Temperature', [])) != 1):
                    raise SliceError('invalid_temperature')
                try:
                    nozzle, bed = float(nozzle_header), float(bed_header)
                except (ValueError, TypeError):
                    raise SliceError('invalid_temperature') from None
                if not math.isfinite(nozzle) or not math.isfinite(bed) or not 170 <= nozzle <= 300 or not 0 <= bed <= 100:
                    raise SliceError('invalid_temperature')
                temperatures = {'nozzle': nozzle, 'bed': bed}
            length_text = self.headers.get('Content-Length', '')
            if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                    or not re.fullmatch(r'[0-9]{1,9}', length_text)):
                raise SliceError('content_length_required', 411)
            length = int(length_text)
            if not 0 < length <= MAX_INPUT:
                raise SliceError('input_too_large', 413)
            if not self.server.slicer.ready():
                raise SliceError('unavailable', 503)
            if not self.server.slicer.slot.acquire(blocking=False):
                raise SliceError('busy', 503)
        except SliceError as exc:
            self._json(exc.status, {'error': exc.code})
            return
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix='slice-', dir=self.server.slicer.state_dir) as directory:
                source = Path(directory) / 'input.stl'
                with source.open('wb') as target:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise SliceError('incomplete_input', 400)
                        target.write(chunk)
                        remaining -= len(chunk)
                target, metadata = self.server.slicer.slice(source, directory, temperatures)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(target.stat().st_size))
                self.send_header('Connection', 'close')
                for key, header in [('print_time_seconds', 'X-Print-Time-Seconds'), ('filament_grams', 'X-Filament-Grams')]:
                    if key in metadata:
                        self.send_header(header, str(metadata[key]))
                self.end_headers()
                with target.open('rb') as generated:
                    while True:
                        chunk = generated.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                self.close_connection = True
            LOG.info('slice completed elapsed_ms=%s', round((time.monotonic() - started) * 1000))
        except SliceError as exc:
            self._json(exc.status, {'error': exc.code})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            LOG.warning('slice client disconnected')
        except Exception as exc:
            LOG.error('slice failure type=%s', type(exc).__name__)
            self._json(500, {'error': 'internal_error'})
        finally:
            self.server.slicer.slot.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(Path(args.config).read_text())
    slicer = Slicer(config)
    server = SlicerHTTPServer(('127.0.0.1', config.get('port', 3334)), slicer)
    def stop(*_):
        slicer.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        slicer.stop()
        server.server_close()


if __name__ == '__main__':
    main()
