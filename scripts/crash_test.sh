#!/usr/bin/env bash
# A real crash test: SIGKILL friday mid-write and prove a resume adds no duplicate row.
# Re-runnable. Run from the repo root:  bash scripts/crash_test.sh
set -euo pipefail

CRASH_DIR="$(mktemp -d)"
export CRASH_DIR
trap 'rm -rf "$CRASH_DIR"' EXIT
echo "crash test dir: $CRASH_DIR"

# Start the writer; it writes the row, then sleeps in the kill window before confirming the journal.
uv run python scripts/crash_test.py write &
WRITER=$!

# Wait until the writer signals (flag file) that the row has landed and the journal step is started.
ready=0
for _ in $(seq 1 150); do
  if [ -f "$CRASH_DIR/written.flag" ]; then ready=1; break; fi
  sleep 0.2
done
if [ "$ready" != "1" ]; then echo "writer never reached the mid-write state"; kill -9 "$WRITER" 2>/dev/null || true; exit 1; fi
# Confirm the mid-write state the way recovery will see it (row present, journal started-not-finished).
uv run python scripts/crash_test.py check-written

# Kill it for real, mid-write (after the row, before journal-finish).
kill -9 "$WRITER" 2>/dev/null || true
wait "$WRITER" 2>/dev/null || true
echo "SIGKILLed the writer mid-write (row written, journal not yet confirmed)"

# Restart: recover + resume, and assert no duplicate.
echo "--- restart: recover + resume ---"
uv run python scripts/crash_test.py resume
echo "crash test passed: the interrupted step was uncertain, recovery surfaced it, and the resume added no duplicate row."
