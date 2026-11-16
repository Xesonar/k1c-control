import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from detector import Detector, DetectorError


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.reply({"ok": True, "backend": "onnx-cpu"})

    def do_POST(self):
        self.server.received = {"path": self.path, "content_type": self.headers.get("Content-Type"),
                                "auth": self.headers.get("Authorization"),
                                "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))}
        self.reply(self.server.result)

    def reply(self, result):
        self.send_response(self.server.code)
        self.send_header("Content-Type", "application/json")
        if self.server.code == 302:
            self.send_header("Location", "http://example.invalid/private-image")
        self.end_headers()
        self.wfile.write(result if isinstance(result, bytes) else json.dumps(result).encode())


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        self.server.result = {"detections": []}
        self.server.code = 200
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        self.detector = Detector({"detector_url": self.url, "detector_threshold": 0.65})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_real_http_upload_and_official_detection_schema(self):
        self.server.result = {"detections": [["failure", 0.83, [50, 60, 20, 30]], ["failure", 0.25, [10, 10, 5, 5]]]}
        detector = Detector({"detector_url": self.url + "/analyze", "detector_token": "test-private-value"})
        result = detector.analyze(b"\xff\xd8test-jpeg")
        self.assertTrue(result["suspicious"])
        self.assertEqual(result["score"], 0.83)
        self.assertEqual(self.server.received["path"], "/analyze")
        self.assertEqual(self.server.received["content_type"], "image/jpeg")
        self.assertEqual(self.server.received["body"], b"\xff\xd8test-jpeg")
        self.assertEqual(self.server.received["auth"], "Bearer test-private-value")

    def test_clean_frame_is_distinct_from_missing_model(self):
        result = self.detector.analyze(b"\xff\xd8test-jpeg")
        self.assertFalse(result["suspicious"])
        self.assertEqual(result["score"], 0.0)
        missing = Detector({})
        self.assertEqual(missing.status()["available"], False)
        with self.assertRaises(DetectorError):
            missing.analyze(b"\xff\xd8test-jpeg")

    def test_real_model_health_contract(self):
        self.assertEqual(self.detector.status(), {"configured": True, "available": True, "detail": "Obico ONNX CPU"})
        self.server.code = 503
        self.assertFalse(self.detector.status()["available"])

    def test_empty_detection_list_stays_clean_at_lowest_threshold(self):
        detector = Detector({"detector_url": self.url, "detector_threshold": 0})
        self.assertFalse(detector.analyze(b"\xff\xd8test-jpeg")["suspicious"])

    def test_failure_is_not_replaced_with_a_zero_score(self):
        for result in (b"not-json", {}, {"detections": None}, {"detections": [["failure", 83, [1, 2, 3, 4]]]},
                       {"detections": [["failure", float("nan"), [1, 2, 3, 4]]]},
                       {"detections": [["different-model-class", 0.9, [1, 2, 3, 4]]]}):
            self.server.result = result
            with self.assertRaises(DetectorError):
                self.detector.analyze(b"\xff\xd8test-jpeg")

    def test_transport_errors_do_not_retain_token_or_url(self):
        self.server.code = 500
        detector = Detector({"detector_url": self.url, "detector_token": "test-private-value"})
        with self.assertRaises(DetectorError) as caught:
            detector.analyze(b"\xff\xd8test-jpeg")
        self.assertNotIn("test-private-value", str(caught.exception))
        self.assertNotIn(self.url, str(caught.exception))

    def test_external_endpoint_and_invalid_inputs_rejected(self):
        for url in ("https://cloud.example", "http://127.0.0.1@cloud.example", self.url + "?secret=x"):
            with self.assertRaises(ValueError):
                Detector({"detector_url": url})
        with self.assertRaises(DetectorError):
            self.detector.analyze(b"not-jpeg")
        with self.assertRaises(DetectorError):
            self.detector.analyze(b"\xff\xd8" + b"a" * (10 * 1024 * 1024))

    def test_local_service_cannot_redirect_camera_data_to_another_host(self):
        self.server.code = 302
        with self.assertRaisesRegex(DetectorError, "detector_redirect_rejected"):
            self.detector.analyze(b"\xff\xd8test-jpeg")


if __name__ == "__main__":
    unittest.main()
