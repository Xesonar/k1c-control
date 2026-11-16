#!/bin/sh
# Run explicitly as root on the VPS after Docker is available. Installs no apt packages.
set -eu

K1C_AI_COMMIT=df8190ef7500459599ebf398c3d1f653754792fd
K1C_AI_ROOT=${K1C_AI_ROOT:-/opt/k1c-obico-ml}
K1C_AI_ASSETS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
K1C_AI_BASE=k1c-obico-cpu:df8190ef7500
K1C_AI_IMAGE=k1c-obico-bridge:df8190ef7500

command -v docker >/dev/null 2>&1 || { echo 'Docker must be installed before this script.' >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo 'Git must be installed before this script.' >&2; exit 1; }
docker info >/dev/null
mkdir -p "$K1C_AI_ROOT"
if [ ! -d "$K1C_AI_ROOT/upstream/.git" ]; then
    git clone --filter=blob:none --no-checkout https://github.com/TheSpaghettiDetective/obico-server.git "$K1C_AI_ROOT/upstream"
else
    test -z "$(git -C "$K1C_AI_ROOT/upstream" status --porcelain)"
fi
test "$(git -C "$K1C_AI_ROOT/upstream" remote get-url origin)" = 'https://github.com/TheSpaghettiDetective/obico-server.git'
git -C "$K1C_AI_ROOT/upstream" fetch --depth 1 origin "$K1C_AI_COMMIT"
git -C "$K1C_AI_ROOT/upstream" checkout --detach "$K1C_AI_COMMIT"
docker build --network host --file "$K1C_AI_ROOT/upstream/ml_api/Containerfile.cpu" --tag "$K1C_AI_BASE" "$K1C_AI_ROOT/upstream/ml_api"
docker build --network host --build-arg "BASE_IMAGE=$K1C_AI_BASE" --tag "$K1C_AI_IMAGE" "$K1C_AI_ASSETS"

# Replace only our named container; existing application/VPN services are untouched.
if docker container inspect k1c-obico-ml >/dev/null 2>&1; then
    docker container rm --force k1c-obico-ml
fi
docker run --detach --name k1c-obico-ml --restart unless-stopped \
    --network host \
    --memory 1500m --memory-swap 1800m --cpus 1.25 --pids-limit 64 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m \
    --cap-drop ALL --security-opt no-new-privileges \
    --log-opt max-size=10m --log-opt max-file=2 \
    --health-cmd "python -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:3333/health\", timeout=5).read()'" \
    --health-start-period 120s --health-interval 30s --health-timeout 10s --health-retries 3 \
    "$K1C_AI_IMAGE"
echo 'Local detector started. Verify /health and run benchmark.py before enabling automatic reactions.'
