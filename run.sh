#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export RUST_PORTAL_PROJECT_ROOT="${RUST_PORTAL_PROJECT_ROOT:-$SCRIPT_DIR}"
export OPEN_BROWSER="${OPEN_BROWSER:-1}"
export PORT="${PORT:-3000}"

echo "Starting algorithm web release service on port ${PORT} ..."

cargo run --release --manifest-path "$SCRIPT_DIR/Cargo.toml"
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    echo
    echo "Service stopped with exit code ${EXIT_CODE}."
fi

exit "$EXIT_CODE"
