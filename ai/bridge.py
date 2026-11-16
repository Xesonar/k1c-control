"""Binary-JPEG API over Obico's unmodified ONNX model and post-processing.

Installed into the pinned official CPU image by install.sh. No Django, Redis,
webcam URL fetcher, cloud service, or bot credentials are needed here.
"""
import hmac
import os
import threading

import cv2
import numpy as np
from flask import Flask, jsonify, request
from lib.onnx import OnnxNet

MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
net = OnnxNet("/model_cache/ml_api/onnx/model-weights.onnx", "/app/model/model.meta", use_gpu=False)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
inference_lock = threading.Lock()


@app.before_request
def authenticate():
    token = os.environ.get("ML_API_TOKEN")
    if token and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
        return jsonify(error="unauthorized"), 401


@app.get("/health")
def health():
    return jsonify(ok=True, backend="onnx-cpu", model_sha256=MODEL_SHA256,
                   providers=net.session.get_providers())


@app.post("/analyze")
def analyze():
    if request.mimetype != "image/jpeg":
        return jsonify(error="image/jpeg_required"), 415
    raw = request.get_data()
    if not raw.startswith(b"\xff\xd8"):
        return jsonify(error="invalid_jpeg"), 400
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jsonify(error="invalid_jpeg"), 400
    height, width = image.shape[:2]
    if width * height > 40_000_000:
        return jsonify(error="image_too_large"), 413
    with inference_lock:
        detections = net.detect(net.meta, image, None, thresh=0.08)
    return jsonify(detections=detections)


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="image_too_large"), 413
