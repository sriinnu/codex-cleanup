#!/usr/bin/env bash
# One-shot Codex session cleanup — Sriinnu's lazy button.
# Shows the damage, then cleans everything older than KEEP_DAYS after a confirm.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/codex_session_cleaner.py"
KEEP_DAYS="${KEEP_DAYS:-7}"      # override: KEEP_DAYS=14 ./cleanup.sh
MIN_SIZE="${MIN_SIZE:-1}"        # MB; skip tiny files, they're not the problem

echo "== Disk usage by date =="
python3 "$SCRIPT" report
echo
echo "== Dry run (keeping last $KEEP_DAYS days, files >= ${MIN_SIZE} MB) =="
python3 "$SCRIPT" clean --keep-days "$KEEP_DAYS" --min-size "$MIN_SIZE" --dry-run
echo
read -r -p "Blank these for real? Irreversible. [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
    python3 "$SCRIPT" clean --keep-days "$KEEP_DAYS" --min-size "$MIN_SIZE"
else
    echo "Aborted. Nothing touched."
fi
