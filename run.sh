#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# run.sh — Start ajoy with auto-restart and sleep prevention.
#
# Features:
#   • Auto-restarts on crash (10-second cooldown between restarts)
#   • Prevents macOS from sleeping while running (caffeinate)
#   • Logs to both terminal and ajoy.log
#   • Clean Ctrl+C shutdown
#
# Usage:
#   bash run.sh
#
# Oracle Cloud / Linux:
#   caffeinate is macOS-only and is skipped automatically on Linux.
#   For a proper daemon on Linux, use systemd instead (see below).
#
# Linux systemd alternative (create /etc/systemd/system/ajoy.service):
#   [Unit]
#   Description=Ajoy Trading Bot
#   After=network.target
#
#   [Service]
#   WorkingDirectory=/path/to/ajoy
#   ExecStart=/path/to/ajoy/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
#   Restart=on-failure
#   RestartSec=10
#
#   [Install]
#   WantedBy=multi-user.target
#
#   Then: sudo systemctl enable ajoy && sudo systemctl start ajoy
# ─────────────────────────────────────────────────────────────────────────

AJOY_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$AJOY_DIR/.venv/bin/python"
LOG="$AJOY_DIR/ajoy.log"
PORT=8000
RESTART_DELAY=10

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ajoy Trading Bot"
echo "  Dir  : $AJOY_DIR"
echo "  Log  : $LOG"
echo "  Port : $PORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Prevent macOS sleep ──────────────────────────────────────────────────
CAFF_PID=""
if command -v caffeinate &>/dev/null; then
    caffeinate -s -i &
    CAFF_PID=$!
    echo "  Sleep prevention: active (caffeinate PID $CAFF_PID)"
else
    echo "  Sleep prevention: caffeinate not found (Linux/Oracle — no-op)"
fi

# ── Clean shutdown handler ───────────────────────────────────────────────
cleanup() {
    echo ""
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Shutdown requested — stopping ajoy."
    [[ -n "$CAFF_PID" ]] && kill "$CAFF_PID" 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

cd "$AJOY_DIR"

# ── Startup mode check ───────────────────────────────────────────────────
USE_SANDBOX=$(grep -E "^USE_SANDBOX=" .env 2>/dev/null | cut -d= -f2 | tr -d ' "')
if [[ "$USE_SANDBOX" == "0" ]]; then
    echo ""
    echo "  ⚠  LIVE MODE — real money orders will be placed"
    echo ""
fi

# ── Main restart loop ────────────────────────────────────────────────────
while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting ajoy..."

    # Tee output to both terminal and log file
    "$PYTHON" -m uvicorn app.main:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        --log-level info \
        2>&1 | tee -a "$LOG"

    EXIT_CODE=${PIPESTATUS[0]}

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ajoy exited (code $EXIT_CODE)"

    # Exit code 0 = clean shutdown (Ctrl+C propagated) — don't restart
    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "Clean exit — not restarting."
        break
    fi

    echo "Crash detected — restarting in ${RESTART_DELAY}s... (Ctrl+C to stop)"
    sleep "$RESTART_DELAY"
done

[[ -n "$CAFF_PID" ]] && kill "$CAFF_PID" 2>/dev/null
echo "Ajoy stopped."
