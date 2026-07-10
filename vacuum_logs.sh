#!/usr/bin/env bash
# Weekly VACUUM of Codex's internal logs_2.sqlite — it deletes old rows on
# its own but never reclaims the freed pages, so the file just grows forever
# until someone vacuums it. Skips if Codex looks like it's actually running,
# since VACUUM wants the file to itself.
set -euo pipefail

DB="$HOME/.codex/logs_2.sqlite"
# Backup lands next to this script; CODEX_CLEANUP_HOME overrides for the
# deployed/launchd case, same convention as the python scripts and hooks.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="${CODEX_CLEANUP_HOME:-$SCRIPT_DIR}/backups/logs_2.sqlite.bak"
mkdir -p "$(dirname "$BACKUP")"

if [[ ! -f "$DB" ]]; then
    echo "$(date): no logs_2.sqlite found, nothing to do"
    exit 0
fi

if pgrep -f "Codex.app/Contents/MacOS" >/dev/null 2>&1 || pgrep -x codex >/dev/null 2>&1; then
    echo "$(date): Codex appears to be running, skipping this week"
    exit 0
fi

BEFORE=$(stat -f%z "$DB")
cp -p "$DB" "$BACKUP"
sqlite3 "$DB" "VACUUM;"
AFTER=$(stat -f%z "$DB")

echo "$(date): logs_2.sqlite $((BEFORE/1024/1024))MB -> $((AFTER/1024/1024))MB"
