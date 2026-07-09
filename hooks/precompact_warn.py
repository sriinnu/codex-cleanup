#!/usr/bin/env python3
"""
precompact_warn.py — Codex PreCompact hook.

Fires the instant Codex decides a thread needs compacting — the earliest,
most precise signal available that a session has grown large. Cross-checks
the thread against ~/.codex/goals_1.sqlite; if it's already over the token
or age threshold, surfaces a warning directly inside the live session
(systemMessage) plus a macOS notification, so it's seen right where the
work is happening instead of only in a background log.

Always returns continue: true — informational only, never blocks Codex.
Fails silent and fast on any error.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

DB = os.path.expanduser("~/.codex/goals_1.sqlite")
TOKEN_THRESHOLD = 20_000_000
AGE_DAYS_THRESHOLD = 3.0


def notify(title: str, message: str) -> None:
    try:
        # osascript's -e argument handling is flaky with non-ASCII punctuation
        # (em-dashes, curly quotes) depending on locale — strip to plain ASCII
        # rather than risk a silent notification failure over cosmetics.
        title = title.encode("ascii", "ignore").decode()
        message = message.encode("ascii", "ignore").decode()
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], check=False, timeout=3,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        session_id = payload.get("session_id")
        if not session_id or not os.path.exists(DB):
            return 0

        conn = sqlite3.connect(DB, timeout=2)
        row = conn.execute(
            "SELECT objective, status, tokens_used, created_at_ms "
            "FROM thread_goals WHERE thread_id = ?",
            (session_id,),
        ).fetchone()
        conn.close()

        if not row:
            return 0

        objective, status, tokens_used, created_at_ms = row
        age_days = (time.time() * 1000 - created_at_ms) / 86_400_000

        if tokens_used < TOKEN_THRESHOLD and age_days < AGE_DAYS_THRESHOLD:
            return 0

        short_obj = (objective or "").strip().splitlines()[0][:80]
        msg = (
            f"This goal thread has used {tokens_used:,} tokens over "
            f"{age_days:.1f} days ({short_obj}). Consider archiving and "
            f"starting a fresh continuation before it compounds further."
        )
        print(json.dumps({"continue": True, "systemMessage": msg}))
        notify("Codex /goal running long", f"{tokens_used:,} tokens, {age_days:.1f}d - {short_obj}")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
