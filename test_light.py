import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
import unittest

from adapters import ApiError, Printer
from light import CrealityLight


JPEG = b'\xff\xd8\xff\xd9'


def frame(opcode, payload, final=True):
    if len(payload) < 126:
        return bytes(((0x80 if final else 0) | opcode, len(payload))) + payload
    return bytes(((0x80 if final else 0) | opcode, 126)) + struct.pack('!H', len(payload)) + payload


def read_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        data = connection.recv(count - len(result))
        if not data:
            raise EOFError()
        result.extend(data)
    return bytes(result)


def read_client_frame(connection):
    first, second = read_exact(connection, 2)
    if not second & 0x80:
        raise AssertionError('client frame must be masked')
    size = second & 0x7f
    if size == 126:
        size = struct.unpack('!H', read_exact(connection, 2))[0]
    elif size == 127:
        size = struct.unpack('!Q', read_exact(connection, 8))[0]
    mask = read_exact(connection, 4)
    raw = read_exact(connection, size)
    return first & 0xf, bytes(value ^ mask[i % 4] for i, value in enumerate(raw))


class WebSocketServer:
    def __init__(self, handler=None, bad_handshake=False):
        self.commands = []
        self.frames = []
        self.errors = []
        self.handler = handler or self.default_handler
        self.bad_handshake = bad_handshake
        self.on = True
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2)
                try:
                    request = bytearray()
                    while not request.endswith(b'\r\n\r\n'):
                        request.extend(read_exact(self.request, 1))
                    lines = request.decode('ascii').split('\r\n')
                    headers = {name.lower(): value.strip() for name, value in (line.split(':', 1) for line in lines[1:] if ':' in line)}
                    accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                    if owner.bad_handshake:
                        accept = 'invalid'
                    self.request.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
                    if owner.bad_handshake:
                        return
                    while True:
                        opcode, payload = read_client_frame(self.request)
                        owner.frames.append((opcode, payload))
                        if opcode == 1:
                            value = json.loads(payload)
                            owner.commands.append(value)
                            if owner.handler(self.request, value) is False:
                                return
                except (EOFError, ConnectionError, socket.timeout):
                    pass
                except Exception as exc:
                    owner.errors.append(exc)

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'ws://127.0.0.1:' + str(self.server.server_address[1]) + '/'

    def default_handler(self, connection, value):
        if value['method'] == 'set':
            self.on = bool(value['params']['lightSw'])
        elif value['method'] == 'get':
            connection.sendall(frame(1, json.dumps({'params': {'lightSw': int(self.on)}}).encode()))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class LightTests(unittest.TestCase):
    def setUp(self):
        self.server = WebSocketServer()
        self.light = CrealityLight(self.server.url, timeout=.3)

    def tearDown(self):
        self.server.close()
        self.assertEqual(self.server.errors, [])

    def test_status_uses_masked_frame_and_reports_stock_light_switch(self):
        self.assertTrue(self.light.status())
        self.assertEqual(self.server.commands, [{'method': 'get', 'params': {'ReqPrinterPara': 1}}])

    def test_set_sends_only_light_switch_then_verifies_readback(self):
        self.assertFalse(self.light.set(False))
        self.assertEqual(self.server.commands, [{'method': 'set', 'params': {'lightSw': 0}}, {'method': 'get', 'params': {'ReqPrinterPara': 1}}])
        self.assertFalse(self.server.on)

    def test_status_accepts_stock_top_level_light_switch(self):
        self.server.handler = lambda connection, _: connection.sendall(frame(1, b'{"lightSw":0,"printProgress":0}'))
        self.assertFalse(self.light.status())

    def test_printer_light_api_uses_configured_private_endpoint(self):
        printer = Printer({'printer_url': 'http://127.0.0.1:7126', 'snapshot_url': 'http://127.0.0.1/photo', 'light_ws_url': self.server.url})
        self.assertTrue(printer.light_status())
        self.assertFalse(printer.set_light(False))

    def test_ping_and_fragmented_json_are_processed(self):
        payload = b'{"params":{"lightSw":0}}'
        self.server.handler = lambda connection, _: connection.sendall(frame(9, b'ping') + frame(1, payload[:8], final=False) + frame(0, payload[8:]))
        self.assertFalse(self.light.status())
        # Pong can be observed just after the client has returned.
        self.assertTrue(any(opcode == 1 for opcode, _ in self.server.frames))

    def test_handshake_rejects_invalid_accept_before_any_command(self):
        self.server.bad_handshake = True
        with self.assertRaises(ApiError) as caught:
            self.light.set(False)
        self.assertEqual(caught.exception.code, 'light_invalid_handshake')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.server.commands, [])

    def test_disconnected_write_is_not_replayed_and_is_uncertain(self):
        self.server.handler = lambda _connection, _value: False
        with self.assertRaises(ApiError) as caught:
            self.light.set(False)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.server.commands), 1)
        self.assertEqual(self.server.commands[0], {'method': 'set', 'params': {'lightSw': 0}})

    def test_mismatched_readback_never_claims_success(self):
        def mismatch(connection, value):
            if value['method'] == 'get':
                connection.sendall(frame(1, b'{"params":{"lightSw":1}}'))
        self.server.handler = mismatch
        with self.assertRaises(ApiError) as caught:
            self.light.set(False)
        self.assertTrue(caught.exception.uncertain)
        self.assertIn(caught.exception.code, {'light_network', 'light_timeout'})
        self.assertEqual(sum(value['method'] == 'set' for value in self.server.commands), 1)

    def test_unrequested_compression_and_oversize_frame_fail_closed(self):
        self.server.handler = lambda connection, _: connection.sendall(bytes((0x81, 127)) + struct.pack('!Q', 256 * 1024 + 1))
        with self.assertRaises(ApiError) as caught:
            self.light.status()
        self.assertEqual(caught.exception.code, 'light_message_too_large')
        self.assertFalse(caught.exception.uncertain)

    def test_only_boolean_switch_values_are_accepted(self):
        for value in (0, 1, 'on', None, {'method': 'set'}):
            with self.subTest(value=value), self.assertRaises(ApiError) as caught:
                self.light.set(value)
            self.assertEqual(caught.exception.code, 'invalid_light_state')
        self.assertEqual(self.server.commands, [])


if __name__ == '__main__':
    unittest.main()
