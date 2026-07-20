#!/usr/bin/env bash
# Installs the launchd agents in deployed/*.plist into ~/Library/LaunchAgents,
# substituting __CODEX_CLEANUP_HOME__ for this checkout's actual path (the
# plists are templated so the repo doesn't hardcode any one machine's
# username/path). Safe to re-run — unloads before reloading each agent.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/Library/LaunchAgents"
mkdir -p "$TARGET"

for plist in "$DIR"/deployed/com.sriinnu.*.plist; do
    name="$(basename "$plist")"
    dest="$TARGET/$name"
    sed "s|__CODEX_CLEANUP_HOME__|$DIR|g" "$plist" > "$dest"
    label="${name%.plist}"
    launchctl unload "$dest" 2>/dev/null || true
    launchctl load "$dest"
    echo "installed + loaded: $label"
done

echo
echo "Verify with: launchctl list | grep com.sriinnu.codex"
