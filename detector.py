"""Client for the local Obico ONNX service; absence is never a clean result."""
import json
import math
import urllib.error
import urllib.parse
import urllib.request


class DetectorError(Exception):
    """A deliberately URL-free error safe for logs and owner notifications."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


class Detector:
    def __init__(self, config):
        raw = str(config.get("detector_url") or "").rstrip("/")
        if raw.endswith("/analyze"):
            raw = raw[:-8]
        self.base = raw
        self.token = config.get("detector_token")
        self.threshold = config.get("detector_threshold", 0.65)
        self.timeout = config.get("detector_timeout", 45)
        if (isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float))
                or not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1):
            raise ValueError("invalid_detector_threshold")
        if self.base:
            parsed = urllib.parse.urlsplit(self.base)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
                raise ValueError("detector_must_be_local_http")

    def _request(self, path, jpeg=None):
        if not self.base:
            raise DetectorError("detector_not_configured")
        headers = {"Accept": "application/json"}
        if jpeg is not None:
            headers["Content-Type"] = "image/jpeg"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, data=jpeg, headers=headers)
        try:
            # The model is hosted on this VPS, never sent through environment proxies.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(req, timeout=self.timeout) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise DetectorError("detector_response_too_large")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise DetectorError("detector_bad_response")
            return result
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if 300 <= code < 400:
                raise DetectorError("detector_redirect_rejected") from None
            raise DetectorError("detector_http_" + str(code)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise DetectorError("detector_unavailable_or_bad_json") from None

    def status(self):
        if not self.base:
            return {"configured": False, "available": False, "detail": "detector_not_configured"}
        try:
            data = self._request("/health")
            if data.get("ok") is not True or data.get("backend") != "onnx-cpu":
                raise DetectorError("detector_model_not_ready")
            return {"configured": True, "available": True, "detail": "Obico ONNX CPU"}
        except DetectorError as exc:
            return {"configured": True, "available": False, "detail": str(exc)}

    def analyze(self, jpeg):
        if not isinstance(jpeg, bytes) or not jpeg.startswith(b"\xff\xd8"):
            raise DetectorError("detector_input_not_jpeg")
        if len(jpeg) > 10 * 1024 * 1024:
            raise DetectorError("detector_input_too_large")
        data = self._request("/analyze", jpeg)
        detections = data.get("detections")
        if not isinstance(detections, list) or len(detections) > 1000:
            raise DetectorError("detector_bad_detections")
        accepted = []
        for item in detections:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise DetectorError("detector_bad_detection")
            label, score, box = item
            if (not isinstance(label, str) or isinstance(score, bool)
                    or not isinstance(score, (int, float)) or not math.isfinite(score)
                    or not 0 <= score <= 1 or not isinstance(box, (list, tuple)) or len(box) != 4
                    or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in box)):
                raise DetectorError("detector_bad_detection")
            if label != "failure":
                raise DetectorError("detector_unknown_label")
            accepted.append([label, float(score), list(box)])
        score = max((item[1] for item in accepted), default=0.0)
        return {"suspicious": bool(accepted) and score >= self.threshold, "score": score,
                "detail": "Obico: failure %.2f, boxes=%d" % (score, len(accepted)),
                "detections": accepted}
