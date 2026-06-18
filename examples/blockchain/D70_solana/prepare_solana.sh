#!/usr/bin/env bash
#
# Prepare the D70_solana emulation and leave all containers running.
#
# This script is intentionally separate from test_solana.sh:
#   - prepare_solana.sh builds/generates/starts the environment
#   - test_solana.sh only verifies an already-running environment
#
# Usage:
#   ./prepare_solana.sh
#   PLATFORM=linux/amd64 ./prepare_solana.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
OUTPUT_DIR="$HERE/output"
COMPOSE="docker compose"
PYTHON_BIN="${PYTHON_BIN:-$(cd "$REPO_ROOT" && command -v python3)}"

case "$(uname -m)" in
  arm64|aarch64) DEFAULT_PLATFORM="linux/arm64" ;;
  *)             DEFAULT_PLATFORM="linux/amd64" ;;
esac
PLATFORM="${PLATFORM:-$DEFAULT_PLATFORM}"
case "$PLATFORM" in
  linux/arm64) EMU_ARCH="arm" ;;
  linux/amd64) EMU_ARCH="amd" ;;
  *) echo "Unsupported PLATFORM=$PLATFORM (expected linux/amd64 or linux/arm64)" >&2; exit 1 ;;
esac
export DOCKER_DEFAULT_PLATFORM="$PLATFORM"

log()  { echo -e "\033[1;34m[prepare]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[ ok ]\033[0m $*"; }
fail() { echo -e "\033[1;31m[fail]\033[0m $*"; }

log "building seedemu-solana base image ($PLATFORM) ..."
docker build --platform "$PLATFORM" -t seedemu-solana "$REPO_ROOT/docker_images/seedemu-solana" \
  || { fail "base image build failed"; exit 1; }
ok "base image ready."

log "generating emulation from solana_basic.py (arch: $EMU_ARCH) ..."
( cd "$HERE" && "$PYTHON_BIN" solana_basic.py "$EMU_ARCH" ) \
  || { fail "emulation generation failed"; exit 1; }
[[ -f "$OUTPUT_DIR/docker-compose.yml" ]] || { fail "no docker-compose.yml produced"; exit 1; }
ok "emulation generated."

# seedemu node images do `FROM <digest>`, where each <digest> base image is
# built by a "dummy" compose service (one file per digest under output/dummies/).
# docker compose v2 builds services in parallel, so build these first.
dummy_services="$(ls "$OUTPUT_DIR/dummies" 2>/dev/null | tr '\n' ' ')"
if [[ -n "$dummy_services" ]]; then
  log "pre-building base images: $dummy_services"
  ( cd "$OUTPUT_DIR" && $COMPOSE build $dummy_services ) \
    || { fail "base (dummy) image build failed"; exit 1; }
fi

log "building containers and starting the cluster ..."
( cd "$OUTPUT_DIR" && $COMPOSE up -d --build ) \
  || { fail "docker compose up failed"; exit 1; }

ok "cluster containers are running."
echo "Verify with: ./test_solana.sh"
echo "Inspect bootstrap RPC with:"
echo "  BOOT=\$(docker ps --format '{{.Names}}' | grep '^as150h-Solana-Bootstrap-150-' | head -n 1)"
echo "  docker exec \"\$BOOT\" solana --url http://127.0.0.1:8899 cluster-version"
echo "Tear down manually with:"
echo "  (cd output && docker compose down -v --remove-orphans)"
