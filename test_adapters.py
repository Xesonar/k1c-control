import json
import email
import hashlib
import os
import socket
import time
import tempfile
import urllib.parse
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adapters import ApiError, Printer, Slicer, Telegram


class Server:
    def __init__(self):
        self.requests = []
        self.send_length = True
        self.stream_delay = 0
        self.respond = lambda method, path, body, headers: (200, {}, b"{}")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.requests.append((self.command, self.path, body, self.headers))
                response = owner.respond(self.command, self.path, body, self.headers)
                if response is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                code, headers, data = response
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                if owner.send_length and "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    if owner.stream_delay:
                        for value in data:
                            self.wfile.write(bytes((value,)))
                            self.wfile.flush()
                            time.sleep(owner.stream_delay)
                    else:
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.printer = Printer({"printer_url": self.server.url, "snapshot_url": self.server.url + "/camera"})
        self.telegram = Telegram({"bot_token": "12345:LOCAL_TEST_TOKEN", "telegram_api_base": self.server.url})

    def tearDown(self):
        self.server.close()

    def json_response(self, value, code=200):
        return code, {"Content-Type": "application/json"}, json.dumps(value).encode()

    def test_status_preserves_klipper_shutdown_and_message(self):
        state = {"webhooks": {"state": "shutdown", "message": "MCU disconnected"}, "print_stats": None}
        self.server.respond = lambda *_: self.json_response({"result": {"status": state}})
        self.assertEqual(self.printer.status(), state)

    def test_physical_command_is_not_retried_after_ambiguous_failure(self):
        self.server.respond = lambda *_: self.json_response({"error": "server died"}, code=500)
        with self.assertRaises(ApiError) as caught:
            self.printer.pause()
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.http_status, 500)
        self.assertEqual([(r[0], r[1]) for r in self.server.requests], [("POST", "/printer/print/pause")])

    def test_start_encodes_nested_filename_and_rejects_traversal(self):
        self.server.respond = lambda *_: self.json_response({"result": "ok"})
        self.printer.start("telegram/корпус 1.gcode")
        self.assertEqual(json.loads(self.server.requests[-1][2]), {"filename": "telegram/корпус 1.gcode"})
        for filename in ("../config/printer.cfg", "/tmp/job.gcode", "one/../two.gcode", "one\\two.gcode", "bad\r\nname.gcode", "\x00.gcode"):
            with self.subTest(filename=filename), self.assertRaises(ApiError):
                self.printer.start(filename)
        self.assertEqual(len(self.server.requests), 1)

    def test_history_and_files_keep_server_identities(self):
        jobs = [{"job_id": "000003", "filename": "same.gcode", "status": "in_progress"}, {"job_id": "000002", "filename": "same.gcode", "status": "completed"}]
        files = [{"path": "old.gcode", "modified": 1}, {"path": "telegram/new.gcode", "modified": 20}]

        def respond(_method, path, _body, _headers):
            if path.startswith("/server/history/list"):
                return self.json_response({"result": {"jobs": jobs}})
            return self.json_response({"result": files})

        self.server.respond = respond
        self.assertEqual(self.printer.history(), jobs)
        self.assertEqual(self.printer.active_job(), jobs[0])
        self.assertEqual(self.printer.files(limit=1), [files[1]])
        self.assertIn("root=gcodes", self.server.requests[-1][1])

    def test_metadata_filename_is_a_query_value_not_an_injected_argument(self):
        self.server.respond = lambda *_: self.json_response({"result": {"estimated_time": 123}})
        self.assertEqual(self.printer.metadata("dir/a&b.gcode"), {"estimated_time": 123})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.server.requests[-1][1]).query)
        self.assertEqual(query, {"filename": ["dir/a&b.gcode"]})

    def test_upload_sends_file_to_gcodes_telegram_without_starting(self):
        # Moonraker's separate FileUploadHandler returns an unwrapped HTTP201.
        self.server.respond = lambda *_: self.json_response({"item": {"root": "gcodes", "path": "telegram/abc_part 1.gcode"}, "print_started": False}, code=201)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cached.gcode"
            path.write_bytes(b"; sample\nG1 X1 Y2\n")
            uploaded = self.printer.upload("abc_part 1.gcode", path)
        self.assertEqual(uploaded, "telegram/abc_part 1.gcode")
        method, url, body, headers = self.server.requests[0]
        self.assertEqual((method, url), ("POST", "/server/files/upload"))
        mime = email.message_from_bytes(("Content-Type: " + headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        fields = {part.get_param("name", header="Content-Disposition"): part for part in mime.get_payload()}
        self.assertEqual(fields["root"].get_payload(decode=True), b"gcodes")
        self.assertEqual(fields["path"].get_payload(decode=True), b"telegram")
        self.assertEqual(fields["print"].get_payload(decode=True), b"false")
        self.assertEqual(fields["file"].get_filename(), "abc_part 1.gcode")
        self.assertEqual(fields["file"].get_payload(decode=True), b"; sample\nG1 X1 Y2\n")

    def test_large_upload_streams_bounded_chunks_with_checksum_and_no_print_flag(self):
        self.server.respond = lambda *_: self.json_response({"item": {"root": "gcodes", "path": "telegram/part.gcode"}, "print_started": False}, code=201)
        read_sizes = []
        original_fdopen = os.fdopen
        class BoundedReader:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return self.wrapped.__exit__(*_args)
            def read(self, size=-1):
                if not 0 < size <= 65536:
                    raise AssertionError("unbounded upload read")
                read_sizes.append(size)
                return self.wrapped.read(size)
        def opened(descriptor, mode, *args, **kwargs):
            wrapped = original_fdopen(descriptor, mode, *args, **kwargs)
            return BoundedReader(wrapped) if mode == "rb" else wrapped
        content = (b"G1 X1 Y2 E.05\n" * 160000)[:2 * 1024 * 1024]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "part.gcode"
            path.write_bytes(content)
            with patch("adapters.os.fdopen", side_effect=opened):
                self.assertEqual(self.printer.upload("part.gcode", path), "telegram/part.gcode")
        self.assertGreater(len(read_sizes), 64)
        self.assertLessEqual(max(read_sizes), 65536)
        method, url, body, headers = self.server.requests[0]
        self.assertEqual((method, url), ("POST", "/server/files/upload"))
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIsNone(headers.get("Transfer-Encoding"))
        mime = email.message_from_bytes(("Content-Type: " + headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        fields = {part.get_param("name", header="Content-Disposition"): part for part in mime.get_payload()}
        self.assertEqual(fields["checksum"].get_payload(decode=True).decode(), hashlib.sha256(content).hexdigest())
        self.assertEqual(fields["print"].get_payload(decode=True), b"false")
        self.assertEqual(fields["file"].get_payload(decode=True), content)

    def test_upload_rejects_file_changed_during_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "part.gcode"
            path.write_bytes(b"G1 X1\n")
            def changed(_url, **kwargs):
                for _chunk in kwargs["body"]:
                    pass
                path.write_bytes(b"G1 X2\n")
                return {"item": {"root": "gcodes", "path": "telegram/part.gcode"}, "print_started": False}
            with patch("adapters._json", side_effect=changed) as request:
                with self.assertRaises(ApiError) as caught:
                    self.printer.upload("part.gcode", path)
        self.assertEqual(caught.exception.code, "upload_file_changed")
        self.assertEqual(request.call_count, 1)

    def test_upload_progress_reports_file_bytes_and_failed_ui_does_not_corrupt_post(self):
        self.server.respond = lambda *_: self.json_response({"item": {"root": "gcodes", "path": "telegram/part.gcode"}})
        seen = []
        def progress(sent, total):
            seen.append((sent, total))
            if sent:
                raise ApiError("network")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "part.gcode"; path.write_bytes(b"G1 X1\n" * 25000)
            self.printer.upload("part.gcode", path, progress=progress)
            size = path.stat().st_size
        self.assertEqual(seen[0], (0, size))
        self.assertEqual(seen[-1], (size, size))
        self.assertEqual([x[0] for x in seen], sorted(x[0] for x in seen))
        self.assertEqual(len(self.server.requests), 1)

    def test_calibrated_start_and_gate_resume_are_single_physical_posts(self):
        self.server.respond = lambda *_: self.json_response({"result": "ok"})
        self.printer.start_calibrated('telegram/part "one".gcode')
        script = json.loads(self.server.requests[-1][2])["script"]
        self.assertEqual(script, 'SDCARD_PRINT_FILE FILENAME="telegram/part \\"one\\".gcode" NEEDCALIBRATE=1')
        gate = {"token": "abcdef0123456789", "state_name": "K1C_GATE_abcdef0123456789"}
        self.printer.resume_gate(gate)
        self.assertEqual(json.loads(self.server.requests[-1][2])["script"], "RESTORE_GCODE_STATE NAME=K1C_GATE_abcdef0123456789 MOVE=1\nM24")
        self.server.requests.clear()
        self.server.respond = lambda *_: self.json_response({"error": "lost response"}, 500)
        with self.assertRaises(ApiError) as caught:
            self.printer.start_calibrated("part.gcode")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.server.requests), 1)

    def test_temperature_validation_and_long_recalibration_never_resume(self):
        self.server.respond = lambda *_: self.json_response({"result": "ok"})
        self.printer.set_temperature("extruder", 245)
        self.assertEqual(json.loads(self.server.requests[-1][2])["script"], "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=245")
        for heater, value in (("heater_bed", 101), ("extruder", 301), ("extruder", float('nan')), ("extruder", True), ("anything", 20)):
            with self.subTest(heater=heater, value=value), self.assertRaises(ApiError):
                self.printer.set_temperature(heater, value)
        with patch("adapters._json", return_value={"result": "ok"}) as request:
            self.printer.recalibrate(70, 240)
        self.assertEqual(request.call_args.kwargs["timeout"], 900)
        self.assertTrue(request.call_args.kwargs["physical"])
        self.assertEqual(request.call_args.kwargs["payload"]["script"], "M190 S70\nM109 S140\nBED_MESH_CALIBRATE ADAPTIVE=1\nSAVE_CONFIG_NORESTART\nM109 S240")
        for bad in ({"token": "abcdefgh", "state_name": "OTHER"}, {"token": "abcd\nM24", "state_name": "x"}):
            with self.assertRaises(ApiError):
                self.printer.resume_gate(bad)

    def test_cancellation_is_not_serialized_behind_recalibration_in_adapter(self):
        entered, release = threading.Event(), threading.Event()
        def respond(_method, path, body, _headers):
            if path == "/printer/gcode/script":
                entered.set()
                release.wait(2)
            return self.json_response({"result": "ok"})
        self.server.respond = respond
        worker = threading.Thread(target=self.printer.recalibrate, args=(70, 240))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.printer.cancel()
            self.assertLess(time.monotonic() - started, .3)
        finally:
            release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_download_gcode_quotes_path_and_replaces_only_complete_private_file(self):
        self.server.respond = lambda *_: (200, {}, b"; code\nG90\n")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "job.gcode"
            self.assertEqual(self.printer.download_gcode("telegram/a b&c.gcode", target), target)
            self.assertEqual(target.read_bytes(), b"; code\nG90\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.server.requests[-1][1], "/server/files/gcodes/telegram/a%20b%26c.gcode")
            self.server.respond = lambda *_: (200, {"Content-Length": "20"}, b"truncated")
            with self.assertRaises(ApiError):
                self.printer.download_gcode("part.gcode", target)
            self.assertEqual(target.read_bytes(), b"; code\nG90\n")
            self.assertEqual([path.name for path in Path(folder).iterdir()], ["job.gcode"])

    def test_edit_message_keeps_panel_and_ignores_only_not_modified(self):
        self.server.respond = lambda *_: self.json_response({"ok": True, "result": {"message_id": 3}})
        self.telegram.edit_message(123, 3, "Статус", {"inline_keyboard": []})
        self.assertEqual(json.loads(self.server.requests[-1][2]), {"chat_id": 123, "message_id": 3, "text": "Статус", "reply_markup": {"inline_keyboard": []}})
        self.server.respond = lambda *_: self.json_response({"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}, 400)
        self.assertTrue(self.telegram.edit_message(123, 3, "Статус"))
        self.server.respond = lambda *_: self.json_response({"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}, 400)
        with self.assertRaises(ApiError):
            self.telegram.edit_message(123, 3, "Статус")

    def test_camera_rejects_html_and_oversize_response(self):
        self.server.respond = lambda *_: (200, {}, b"<html>camera unavailable</html>")
        with self.assertRaises(ApiError) as caught:
            self.printer.photo()
        self.assertEqual(caught.exception.code, "invalid_jpeg")
        self.server.respond = lambda *_: (200, {"Content-Length": str(10 * 1024 * 1024 + 1)}, b"")
        with self.assertRaises(ApiError) as caught:
            self.printer.photo()
        self.assertEqual(caught.exception.code, "response_too_large")

    def test_camera_recovers_after_one_connection_reset(self):
        jpeg = b"\xff\xd8snapshot\xff\xd9"
        self.server.respond = lambda *_: None if len(self.server.requests) == 1 else (200, {}, jpeg)
        self.assertEqual(self.printer.photo(), jpeg)
        self.assertEqual([(r[0], r[1]) for r in self.server.requests], [("GET", "/camera"), ("GET", "/camera")])

    def test_camera_retries_truncated_frame_but_does_not_loop_forever(self):
        jpeg = b"\xff\xd8snapshot\xff\xd9"
        self.server.respond = lambda *_: (200, {}, jpeg[:-2] if len(self.server.requests) == 1 else jpeg)
        self.assertEqual(self.printer.photo(), jpeg)
        self.assertEqual(len(self.server.requests), 2)
        self.server.requests.clear()
        self.server.respond = lambda *_: None
        with self.assertRaises(ApiError) as caught:
            self.printer.photo()
        self.assertEqual(caught.exception.code, "network")
        self.assertEqual(len(self.server.requests), 2)

    def test_safe_status_read_recovers_from_transient_gateway_error(self):
        state = {"webhooks": {"state": "ready"}}
        self.server.respond = lambda *_: self.json_response({"error": "temporary"}, 502) if len(self.server.requests) == 1 else self.json_response({"result": {"status": state}})
        self.assertEqual(self.printer.status(), state)
        self.assertEqual([r[0] for r in self.server.requests], ["GET", "GET"])

    def test_camera_does_not_retry_authorization_failure(self):
        self.server.respond = lambda *_: (401, {}, b"unauthorized")
        with self.assertRaises(ApiError) as caught:
            self.printer.photo()
        self.assertEqual(caught.exception.http_status, 401)
        self.assertEqual(len(self.server.requests), 1)

    def test_telegram_send_is_not_repeated_after_connection_reset(self):
        self.server.respond = lambda *_: None
        with self.assertRaises(ApiError):
            self.telegram.send_photo(123, "Камера", b"\xff\xd8\xff\xd9")
        self.assertEqual(len(self.server.requests), 1)

    def test_callback_ack_has_a_short_budget_and_is_not_retried(self):
        with patch.object(self.telegram, "call", side_effect=ApiError("network")) as call:
            with self.assertRaises(ApiError):
                self.telegram.answer_callback("button-1", "Принято")
        call.assert_called_once_with("answerCallbackQuery", {"callback_query_id": "button-1", "text": "Принято"}, timeout=3)

    def test_telegram_429_exposes_retry_delay_without_credential_or_description(self):
        self.server.respond = lambda *_: self.json_response({"ok": False, "error_code": 429, "description": "credential12345:LOCAL_TEST_TOKEN must not escape", "parameters": {"retry_after": 17}}, code=429)
        with self.assertRaises(ApiError) as caught:
            self.telegram.call("sendMessage", {"chat_id": 1, "text": "test"})
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertNotIn("LOCAL_TEST_TOKEN", str(caught.exception))
        self.assertNotIn("credential", repr(caught.exception))

    def test_get_updates_includes_callbacks_and_returns_result(self):
        updates = [{"update_id": 11, "callback_query": {"id": "callback"}}]
        self.server.respond = lambda *_: self.json_response({"ok": True, "result": updates})
        self.assertEqual(self.telegram.get_updates(11, timeout=0), updates)
        body = json.loads(self.server.requests[-1][2])
        self.assertEqual(body, {"offset": 11, "timeout": 0, "allowed_updates": ["message", "callback_query"]})

    def test_messages_and_photo_keep_inline_buttons(self):
        self.server.respond = lambda *_: self.json_response({"ok": True, "result": {"message_id": 77}})
        markup = {"inline_keyboard": [[{"text": "Пауза", "callback_data": "alarm:123:pause"}]]}
        self.assertEqual(self.telegram.send_message(123, "Проверка", markup), {"message_id": 77})
        self.assertEqual(json.loads(self.server.requests[-1][2])["reply_markup"], markup)
        self.telegram.send_photo(123, "Камера", b"\xff\xd8\xff\xd9", markup)
        _method, _url, body, headers = self.server.requests[-1]
        mime = email.message_from_bytes(("Content-Type: " + headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        fields = {part.get_param("name", header="Content-Disposition"): part for part in mime.get_payload()}
        self.assertEqual(json.loads(fields["reply_markup"].get_payload(decode=True)), markup)
        self.assertEqual(fields["photo"].get_payload(decode=True), b"\xff\xd8\xff\xd9")
        self.telegram.answer_callback("callback-id", "Принято")
        self.assertEqual(json.loads(self.server.requests[-1][2]), {"callback_query_id": "callback-id", "text": "Принято"})

    def test_file_download_is_stream_bounded_and_does_not_leave_partial_file(self):
        self.server.send_length = False

        def respond(_method, path, _body, _headers):
            if path.endswith("/getFile"):
                return self.json_response({"ok": True, "result": {"file_path": "documents/job.gcode"}})
            return 200, {}, b"123456789"

        self.server.respond = respond
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "job.gcode"
            target.write_bytes(b"existing")
            with self.assertRaises(ApiError) as caught:
                self.telegram.download_file("file-id", target, max_bytes=8)
            self.assertEqual(caught.exception.code, "download_too_large")
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["job.gcode"])

    def test_file_download_returns_exact_private_destination(self):
        def respond(_method, path, _body, _headers):
            if path.endswith("/getFile"):
                return self.json_response({"ok": True, "result": {"file_path": "documents/job.gcode", "file_size": 8}})
            return 200, {}, b"G1 X100\n"

        self.server.respond = respond
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "cached.gcode"
            self.assertEqual(self.telegram.download_file("file-id", target), target)
            self.assertEqual(target.read_bytes(), b"G1 X100\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_malformed_physical_reply_stays_uncertain_and_is_not_retried(self):
        for data in (b"not-json", b"[]", b'{"result": null}'):
            with self.subTest(data=data):
                self.server.requests.clear()
                self.server.respond = lambda *_: (200, {}, data)
                with self.assertRaises(ApiError) as caught:
                    self.printer.resume()
                self.assertTrue(caught.exception.uncertain)
                self.assertEqual(len(self.server.requests), 1)

    def test_getfile_rejects_unsafe_or_oversized_files_before_download(self):
        for info, code in (({"file_path": "../../etc/passwd"}, "unsafe_filename"), ({"file_path": "/etc/passwd"}, "unsafe_filename"), ({"file_path": "documents/a.gcode", "file_size": 21 * 1024 * 1024}, "download_too_large")):
            with self.subTest(info=info), tempfile.TemporaryDirectory() as folder:
                self.server.requests.clear()
                self.server.respond = lambda *_: self.json_response({"ok": True, "result": info})
                with self.assertRaises(ApiError) as caught:
                    self.telegram.download_file("file-id", Path(folder) / "target.gcode")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.server.requests), 1)
                self.assertEqual(list(Path(folder).iterdir()), [])


class SlicerTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.folder = tempfile.TemporaryDirectory()
        self.source = Path(self.folder.name) / "model.stl"
        self.target = Path(self.folder.name) / "result.gcode"
        self.stl = b"binary\x00STL\xff\x00data"
        self.source.write_bytes(self.stl)
        self.config = dict({'owner_id':10001}, **{"slicer_url": self.server.url, "slicer_api_key": "LOCAL_TEST_SLICER_KEY_123456"})
        self.slicer = Slicer(self.config)

    def tearDown(self):
        self.server.close()
        self.folder.cleanup()

    def test_binary_stl_and_gcode_are_streamed_exactly_with_private_atomic_result(self):
        gcode = b";PETG\n" + b"G1 X1 Y2 E.2\n" * 12000
        self.server.respond = lambda *_: (200, {"X-Print-Time-Seconds": "125.25", "X-Filament-Grams": "7.5"}, gcode)
        result = self.slicer.slice(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), gcode)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result, {"path": str(self.target), "size": len(gcode), "material": "PETG", "estimated_time": 125.25, "filament_grams": 7.5})
        method, path, body, headers = self.server.requests[0]
        self.assertEqual((method, path, body), ("POST", "/slice", self.stl))
        self.assertEqual(headers["X-Slicer-Key"], self.config["slicer_api_key"])
        self.assertEqual(int(headers["Content-Length"]), len(self.stl))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(sorted(p.name for p in Path(self.folder.name).iterdir()), ["model.stl", "result.gcode"])

    def test_health_checks_material_engine_and_authenticated_ready_state(self):
        health = {"ready": True, "busy": False, "engine": "OrcaSlicer", "material": "PETG"}
        self.server.respond = lambda *_: (200, {}, json.dumps(health).encode())
        self.assertEqual(self.slicer.health(), health)
        method, path, _body, headers = self.server.requests[0]
        self.assertEqual((method, path), ("GET", "/health"))
        self.assertEqual(headers["X-Slicer-Key"], self.config["slicer_api_key"])
        health["material"] = "PLA"
        with self.assertRaises(ApiError) as caught:
            self.slicer.health()
        self.assertEqual(caught.exception.code, "slicer_invalid_health")

    def test_oversize_and_incomplete_outputs_preserve_existing_file(self):
        self.target.write_bytes(b"existing")
        self.slicer = Slicer(dict(self.config, slicer_max_output_bytes=8))
        for headers, body, expected in (({"Content-Length": "9"}, b"", "slicer_output_too_large"), ({"Content-Length": "8"}, b"small", "slicer_incomplete_response")):
            with self.subTest(expected=expected):
                self.server.respond = lambda *_: (200, headers, body)
                with self.assertRaises(ApiError) as caught:
                    self.slicer.slice(self.source, self.target)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(self.target.read_bytes(), b"existing")
                self.assertEqual(sorted(p.name for p in Path(self.folder.name).iterdir()), ["model.stl", "result.gcode"])

    def test_slicer_timeout_does_not_retry_or_replace_output(self):
        self.slicer = Slicer(dict(self.config, slicer_timeout=.08))
        self.target.write_bytes(b"existing")
        def slow(*_args):
            time.sleep(.2)
            return 200, {}, b"G1 X1"
        self.server.respond = slow
        started = time.monotonic()
        with self.assertRaises(ApiError) as caught:
            self.slicer.slice(self.source, self.target)
        self.assertEqual(caught.exception.code, "slicer_timeout")
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.target.read_bytes(), b"existing")

    def test_slicer_deadline_limits_a_slow_trickle_in_response_body(self):
        self.slicer = Slicer(dict(self.config, slicer_timeout=.09))
        self.server.stream_delay = .04
        self.server.respond = lambda *_: (200, {}, b"G1 X1\n")
        started = time.monotonic()
        with self.assertRaises(ApiError) as caught:
            self.slicer.slice(self.source, self.target)
        self.assertEqual(caught.exception.code, "slicer_timeout")
        self.assertLess(time.monotonic() - started, .3)
        self.assertFalse(self.target.exists())
        self.assertEqual([p.name for p in Path(self.folder.name).iterdir()], ["model.stl"])

    def test_slicer_does_not_follow_redirect_or_forward_secret(self):
        self.server.respond = lambda *_: (302, {"Location": self.server.url + "/leak"}, b"")
        with self.assertRaises(ApiError) as caught:
            self.slicer.slice(self.source, self.target)
        self.assertEqual(caught.exception.code, "slicer_http_302")
        self.assertEqual([request[1] for request in self.server.requests], ["/slice"])

    def test_slicer_errors_never_reveal_key_or_server_description_and_no_retry(self):
        secret = self.config["slicer_api_key"]
        for error, status, expected in (("busy", 503, "slicer_busy"), ("slice_timeout", 504, "slicer_timeout"), ("invalid_stl", 422, "slicer_invalid_stl"), (secret, 500, "slicer_http_500")):
            with self.subTest(error=error):
                self.server.requests.clear()
                self.server.respond = lambda *_: (status, {}, json.dumps({"error": error, "description": secret}).encode())
                with self.assertRaises(ApiError) as caught:
                    self.slicer.slice(self.source, self.target)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(caught.exception))
                self.assertEqual(len(self.server.requests), 1)
                self.assertFalse(self.target.exists())

    def test_slicer_rejects_symlink_and_oversize_source_before_any_request(self):
        self.slicer = Slicer(dict(self.config, slicer_max_input_bytes=8))
        with self.assertRaises(ApiError) as caught:
            self.slicer.slice(self.source, self.target)
        self.assertEqual(caught.exception.code, "slicer_input_too_large")
        link = Path(self.folder.name) / "link.stl"
        link.symlink_to(self.source)
        with self.assertRaises(ApiError) as caught:
            self.slicer.slice(link, self.target)
        self.assertEqual(caught.exception.code, "slicer_invalid_input")
        self.assertEqual(self.server.requests, [])

    def test_unconfigured_slicer_does_not_send_credentials_or_requests(self):
        slicer = Slicer({"slicer_url": self.server.url})
        self.assertFalse(slicer.configured)
        for action in (slicer.health, lambda: slicer.slice(self.source, self.target)):
            with self.assertRaises(ApiError) as caught:
                action()
            self.assertEqual(caught.exception.code, "slicer_not_configured")
        self.assertEqual(self.server.requests, [])

    def test_slicer_temperature_headers_are_per_job_and_invalid_ranges_fail_before_post(self):
        self.server.respond = lambda *_: (200, {}, b"; PETG\nG90\n")
        self.slicer.slice(self.source, self.target, temperatures={"nozzle": 260, "bed": 80})
        headers = self.server.requests[-1][3]
        self.assertEqual(headers["X-Nozzle-Temperature"], "260")
        self.assertEqual(headers["X-Bed-Temperature"], "80")
        self.slicer.slice(self.source, self.target)
        self.assertIsNone(self.server.requests[-1][3].get("X-Nozzle-Temperature"))
        count = len(self.server.requests)
        for temperatures in ({"nozzle": 301, "bed": 70}, {"nozzle": 160, "bed": 70}, {"nozzle": 240, "bed": 101}, {"nozzle": True, "bed": 70}, {"nozzle": 240}, {"nozzle": float('nan'), "bed": 70}):
            with self.subTest(temperatures=temperatures), self.assertRaises(ApiError) as caught:
                self.slicer.slice(self.source, self.target, temperatures=temperatures)
            self.assertEqual(caught.exception.code, "slicer_invalid_temperature")
        self.assertEqual(len(self.server.requests), count)


if __name__ == "__main__":
    unittest.main()
