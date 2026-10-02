#!/usr/bin/env bash
# load_images.sh — Load all SaaS-Bench docker images from docker/images/*.tar
#
# Download the .tar archives from the ModelScope link in docker/README.md and
# place them under docker/images/ before running this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGES_DIR="$REPO_ROOT/docker/images"

if [[ ! -d "$IMAGES_DIR" ]]; then
    echo "[ERROR] Images directory does not exist: $IMAGES_DIR" >&2
    exit 1
fi

shopt -s nullglob
TARS=("$IMAGES_DIR"/*.tar "$IMAGES_DIR"/*.tar.gz)
if [[ ${#TARS[@]} -eq 0 ]]; then
    echo "[ERROR] No .tar / .tar.gz files found in $IMAGES_DIR" >&2
    echo "        Download the images first (see docker/README.md)." >&2
    exit 1
fi

echo "Found ${#TARS[@]} image archive(s) to load."
for tar in "${TARS[@]}"; do
    echo "==> docker load -i $(basename "$tar")"
    docker load -i "$tar"
done

echo ""
echo "Done. Loaded images:"
docker images | awk 'NR==1 || /^mw-/'
