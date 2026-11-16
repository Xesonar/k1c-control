Local K1C failure detector — Obico ONNX CPU

This is a real model integration, not a simulated detector. A missing service,
invalid response or missing configuration produces DetectorError/unavailable.
The camera is currently offline, so accuracy on K1C footage is NOT verified.

Pinned official source:
https://github.com/TheSpaghettiDetective/obico-server/tree/df8190ef7500459599ebf398c3d1f653754792fd
CPU-only official image recipe:
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/ml_api/Containerfile.cpu
ONNX implementation and post-processing:
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/ml_api/lib/onnx.py
Model URL and SHA256 are supplied by that source and verified during image build.
Expected ONNX SHA256: 0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa
Source is AGPL-3.0; the pinned upstream source URL must remain available.

Why the bridge exists:
Upstream /p/?img=URL fetches a picture by URL. Our wrapper instead accepts a
binary JPEG at POST /analyze, calls the same official model/post-processing,
and returns {"detections":[["failure", confidence0to1, [cx,cy,w,h]], ...]}.
Only /health and /analyze are exposed. GET /health returns model readiness.
No Django, Celery, Redis, external cloud or bot token is used by this service.

Install on VPS (operator/root only):
1. Before starting Docker on this VPN VPS, preserve its nftables forwarding:
   configure /etc/docker/daemon.json with iptables=false, ip6tables=false,
   ip-forward=false and bridge="none". Install Docker, git and CA certificates
   through the normal Ubuntu packages. Do not overwrite unrelated existing
   daemon settings. The root deployment owns this prerequisite, not install.sh.
2. Copy this ai directory alongside detector.py and run sh ai/install.sh.
3. Check docker logs k1c-obico-ml, docker stats --no-stream k1c-obico-ml,
   and GET http://127.0.0.1:3333/health.
4. Run python3 ai/benchmark.py /path/to/real-test.jpg and record latency/RAM.
5. Set detector_url to http://127.0.0.1:3333 in the bot config only after health.

If K1C is still offline, the checked-out official repo contains a public example
JPEG at frontend/src/components/first-layer-review/example-images/detached-1.jpg.
It can exercise the real inference path and measure latency without accessing
a private camera. Its result is NOT an accuracy check for K1C, nor a known
positive for this model (it is an example from the first-layer UI).

The script fetches the exact source commit, builds the official CPU-only recipe,
then adds bridge.py. It does not install apt packages. Both builds use
--network host; the runtime also uses --network host without published ports
or a Docker bridge. Gunicorn itself binds ONLY127.0.0.1:3333 on the host.
This avoids Docker NAT/forwarding rules on the existing VPN server. It uses
one gunicorn worker/two HTTP threads and an unprivileged UID, drops caps,
limits the container to1500MiB RAM/1800MiB RAM+swap and1.25CPU, and enables
restart-unless-stopped. It replaces only a container named k1c-obico-ml.
The official base is Python3.8-slim to retain the upstream pinned inference
stack (onnxruntime1.16.3/opencv4.12); it is a loopback-only inference appliance.
The model lock permits one inference at a time; the second HTTP thread keeps
health responses available during a slow CPU inference without loading a
second copy of the model.

Resources and limitations:
The published Obico full-server minimum is4GB RAM:
https://www.obico.io/docs/server-guides/hardware-requirements/
Our VPS has2vCPU/4GB and avoids the full-server services, but this image has
NOT been built or benchmarked on the VPS by this module's author. Local Mac
has no Docker. Do not claim throughput/memory until the real measurements.
Allow several minutes for package/image/model downloads; ONNX weights are
roughly200MB. The model input is416x416. Start with one check every30seconds,
not a video stream; let actual CPU latency determine the final interval.
detector_threshold defaults to0.65 on per-frame confidence; it is an initial
tunable value, NOT Obico's full temporal/normalized probability algorithm.
The bot must require repeated suspicious frames and bind any reaction to the
current print job. A score is not proof of a failed print. Model is class
"failure"; it does not universally detect every PETG quality problem.
Detection background:
https://www.obico.io/blog/how-obico-ai-failure-detection-works/

Client config:
  detector_url: http://127.0.0.1:3333
  detector_threshold: 0.65
  detector_timeout: 45
Only local HTTP URLs are accepted. Optional detector_token adds a Bearer
header; if used, provide the same ML_API_TOKEN to the container explicitly.
The supplied script relies on the loopback boundary and sets no token.
