#!/usr/bin/env bash
# Run the loop as a real CHIA job:  ./submit.sh --design aes --turns 3 --picks 4
#
# Ray uploads the working directory and rejects anything over 100 MB, so this
# stages only the code. Submitting the repo itself fails with
#   RuntimeError: Request failed with status code 413
# because experiments/ accumulates GBs of ORFS artifacts during testing.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY=/opt/conda/envs/chia_env/bin/python
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp -r "$REPO/chia_openroad" "$REPO/examples" "$STAGE/"
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

cd "$STAGE"
exec chia ray job submit --working-dir . -- \
    "$PY" examples/openroad_orfs/surrogate_loop.py "$@"
