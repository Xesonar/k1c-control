"""Narrow RFC 6455 client for stock Creality lightSw, over the private VPN.

No arbitrary G-code or Creality method is exposed. Each mutation is sent once;
readback confirms the requested state. The transport follows RFC 6455 sections
4.1, 5.2-5.5 (handshake, client masking, fragmentation and ping/pong).
"""
import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.parse

from adapters import ApiError


_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
_MAX_MESSAGE = 256 * 1024


class CrealityLight:
    def __init__(self, url, timeout=6):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'ws' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or any(ord(c) < 33 for c in url)):
            raise ApiError('invalid_light_url')
        try:
            self.port = parsed.port or 80
        except ValueError:
            raise ApiError('invalid_light_url') from None
        self.host = parsed.hostname
        self.target = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        self.timeout = timeout
        self.sock = None
        self.buffer = bytearray()
        self.deadline = 0

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError('light_timeout')
        return remaining

    def _send_bytes(self, data):
        self.sock.settimeout(self._remaining())
        self.sock.sendall(data)

    def _read_exact(self, count):
        while len(self.buffer) < count:
            self.sock.settimeout(self._remaining())
            chunk = self.sock.recv(min(65536, count - len(self.buffer)))
            if not chunk:
                raise ApiError('light_disconnected')
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def _open(self):
        self.deadline = time.monotonic() + self.timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=self._remaining())
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        authority = ('[' + self.host + ']') if ':' in self.host else self.host
        request = (f'GET {self.target} HTTP/1.1\r\nHost: {authority}:{self.port}\r\n'
                   f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                   'Sec-WebSocket-Version: 13\r\n\r\n')
        self._send_bytes(request.encode('ascii'))
        while b'\r\n\r\n' not in self.buffer:
            self.sock.settimeout(self._remaining())
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ApiError('light_disconnected')
            self.buffer.extend(chunk)
            if len(self.buffer) > 16384:
                raise ApiError('light_invalid_handshake')
        raw, extra = bytes(self.buffer).split(b'\r\n\r\n', 1)
        self.buffer = bytearray(extra)
        try:
            status, *lines = raw.decode('ascii').split('\r\n')
            headers = {}
            for line in lines:
                name, value = line.split(':', 1)
                name = name.strip().lower()
                if name in headers:
                    raise ValueError('duplicate header')
                headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + _GUID).encode('ascii')).digest()).decode('ascii')
            if (status.split()[:2] != ['HTTP/1.1', '101']
                    or headers.get('upgrade', '').lower() != 'websocket'
                    or 'upgrade' not in {v.strip().lower() for v in headers.get('connection', '').split(',')}
                    or headers.get('sec-websocket-accept') != expected
                    or 'sec-websocket-extensions' in headers):
                raise ValueError('invalid upgrade')
        except (ValueError, UnicodeError):
            raise ApiError('light_invalid_handshake') from None

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 65535:
            header = bytes((0x80 | opcode, 0xfe)) + struct.pack('!H', length)
        else:
            header = bytes((0x80 | opcode, 0xff)) + struct.pack('!Q', length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._send_bytes(header + mask + masked)

    def _send_json(self, value):
        self._send_frame(1, json.dumps(value, separators=(',', ':')).encode('utf-8'))

    def _message(self):
        fragments = bytearray()
        started = False
        for _ in range(128):
            first, second = self._read_exact(2)
            final, opcode = bool(first & 0x80), first & 0xf
            if first & 0x70 or second & 0x80:
                raise ApiError('light_invalid_frame')
            length = second & 0x7f
            if length == 126:
                length = struct.unpack('!H', self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read_exact(8))[0]
            if length > _MAX_MESSAGE or len(fragments) + length > _MAX_MESSAGE:
                raise ApiError('light_message_too_large')
            if opcode >= 8 and (not final or length > 125):
                raise ApiError('light_invalid_frame')
            payload = self._read_exact(length)
            if opcode == 8:
                raise ApiError('light_disconnected')
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if (opcode == 1 and started) or (opcode == 0 and not started) or opcode not in {0, 1}:
                raise ApiError('light_invalid_frame')
            started = True
            fragments.extend(payload)
            if final:
                try:
                    value = json.loads(fragments.decode('utf-8'))
                except (ValueError, UnicodeError):
                    raise ApiError('light_invalid_json') from None
                if not isinstance(value, dict):
                    raise ApiError('light_invalid_response')
                return value
        raise ApiError('light_invalid_frame')

    def _exchange(self, desired=None):
        attempted = False
        confirmed = False
        try:
            self._open()
            if desired is not None:
                # A failure during this write may happen after the printer saw it.
                attempted = True
                self._send_json({'method': 'set', 'params': {'lightSw': int(desired)}})
            self._send_json({'method': 'get', 'params': {'ReqPrinterPara': 1}})
            for _ in range(64):
                message = self._message()
                value = message.get('params', message)
                state = value.get('lightSw') if isinstance(value, dict) else None
                if type(state) not in {int, bool} or state not in (0, 1):
                    continue
                if desired is None or bool(state) == desired:
                    confirmed = True
                    return bool(state)
            raise ApiError('light_unconfirmed')
        except ApiError as exc:
            exc.uncertain = exc.uncertain or attempted
            raise
        except (OSError, TimeoutError):
            raise ApiError('light_network', uncertain=attempted) from None
        finally:
            if self.sock is not None:
                if confirmed:
                    try:
                        self._send_frame(8, b'')
                    except (ApiError, OSError):
                        pass
                self.sock.close()
                self.sock = None

    def status(self):
        return self._exchange()

    def set(self, on):
        if type(on) is not bool:
            raise ApiError('invalid_light_state')
        return self._exchange(on)
