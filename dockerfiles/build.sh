#!/usr/bin/env bash
# Build (and optionally push) the chia-orfs worker image.
#
#   ./dockerfiles/build.sh                  # build locally as chia-orfs:latest
#   ./dockerfiles/build.sh us-central1-docker.pkg.dev/<project>/chia/chia-orfs:latest
#
# The build context is the chia checkout so the Dockerfile's `COPY . /tmp/chia`
# picks up the source. --platform is pinned because openroad/orfs is amd64-only.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHIA_SRC="${CHIA_SRC:-$REPO_ROOT/external/chia}"
TAG="${1:-chia-orfs:latest}"

[[ -f "$CHIA_SRC/pyproject.toml" ]] || {
  echo "error: no chia checkout at $CHIA_SRC (set CHIA_SRC)" >&2; exit 1; }

echo "==> building $TAG from context $CHIA_SRC"
docker buildx build --platform linux/amd64 \
  -f "$REPO_ROOT/dockerfiles/OrfsDockerfile" \
  -t "$TAG" --load "$CHIA_SRC"

if [[ "$TAG" == *"/"* ]]; then
  echo "==> pushing $TAG"
  docker push "$TAG"
fi
