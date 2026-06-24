#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# scripts/setup_cron.sh — Install the ajoy guardian cron job.
#
# Adds a cron entry that runs guardian.py at 2:50 PM ET every weekday.
# DST-aware: detects current UTC offset automatically.
#
# Usage:
#   bash scripts/setup_cron.sh
# ─────────────────────────────────────────────────────────────────────────

AJOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$AJOY_DIR/.venv/bin/python"
LOG="$AJOY_DIR/guardian.log"

# ── Detect UTC offset for America/New_York ───────────────────────────────
# EDT (Mar–Nov) = UTC-4  →  2:50 PM ET = 18:50 UTC
# EST (Nov–Mar) = UTC-5  →  2:50 PM ET = 19:50 UTC
UTC_OFFSET=$(TZ="America/New_York" date +%z)   # e.g. "-0400" or "-0500"
OFFSET_H=${UTC_OFFSET:1:2}                      # "04" or "05"

CRON_HOUR=$((14 + OFFSET_H))                   # 14 (2 PM) + offset
CRON_MIN=50
CRON_LINE="$CRON_MIN $CRON_HOUR * * 1-5  cd $AJOY_DIR && $PYTHON guardian.py >> $LOG 2>&1"

echo "─────────────────────────────────────────────────────────────"
echo "  Ajoy Guardian Cron Setup"
echo "─────────────────────────────────────────────────────────────"
echo "  Ajoy dir : $AJOY_DIR"
echo "  Python   : $PYTHON"
echo "  Log file : $LOG"
echo "  UTC offset (ET): $UTC_OFFSET → runs at ${CRON_HOUR}:${CRON_MIN} UTC"
echo "  Cron line: $CRON_LINE"
echo ""

# ── Check if already installed ───────────────────────────────────────────
if crontab -l 2>/dev/null | grep -q "guardian.py"; then
    echo "  Guardian cron already installed:"
    crontab -l | grep "guardian.py"
    echo ""
    read -p "  Reinstall? (y/N): " ANSWER
    if [[ "$ANSWER" != "y" && "$ANSWER" != "Y" ]]; then
        echo "  Skipped."
        exit 0
    fi
    # Remove old entry
    (crontab -l 2>/dev/null | grep -v "guardian.py") | crontab -
fi

# ── Install ──────────────────────────────────────────────────────────────
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -

echo "  ✓ Cron job installed."
echo ""
echo "  Verify with:  crontab -l"
echo "  Monitor with: tail -f $LOG"
echo "─────────────────────────────────────────────────────────────"
