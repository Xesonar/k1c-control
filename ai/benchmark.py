#!/usr/bin/env python3
"""Run against the real local model using an explicit JPEG, no Telegram calls."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from detector import Detector

parser = argparse.ArgumentParser()
parser.add_argument("jpeg", type=Path)
parser.add_argument("--url", default="http://127.0.0.1:3333")
parser.add_argument("--runs", type=int, default=3)
args = parser.parse_args()
if not 1 <= args.runs <= 20:
    parser.error("--runs must be 1..20")
detector = Detector({"detector_url": args.url, "detector_timeout": 120})
image = args.jpeg.read_bytes()
print(json.dumps({"health": detector.status()}))
for index in range(args.runs):
    started = time.monotonic()
    result = detector.analyze(image)
    print(json.dumps({"run": index + 1, "seconds": round(time.monotonic() - started, 3), **result}))
