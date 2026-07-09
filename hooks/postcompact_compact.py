#!/usr/bin/env python3
"""
postcompact_compact.py — Codex PostCompact hook.

Fires right after Codex pauses to compact a thread. Hooks are blocking, so
Codex is genuinely idle on this transcript for the duration of this script —
a real, race-free window to run the same targeted compaction our cron/
launchd jobs do (drop token_count/agent_message telemetry, blank
exec_command noise), except immediately instead of waiting for the next
scheduled pass.

Fails silent and fast on any error: a bug here must never block or slow
down a real Codex session. Always exits 0.
"""

import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/Sriinnu/Personal/codex-cleanup"))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        transcript_path = payload.get("transcript_path")
        if not transcript_path or not os.path.exists(transcript_path):
            return 0

        from codex_session_cleaner import compact_file  # local import: keep hook startup fast

        before, after, dropped, edited, skip_reason = compact_file(transcript_path)
        if skip_reason or before == after:
            return 0

        pct = (1 - after / before) * 100 if before else 0
        print(json.dumps({
            "continue": True,
            "systemMessage": f"Compacted this transcript: {before/1024/1024:.1f}MB -> "
                              f"{after/1024/1024:.1f}MB ({pct:.0f}% reclaimed)",
        }))
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
