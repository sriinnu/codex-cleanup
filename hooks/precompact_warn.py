#!/usr/bin/env python3
"""
precompact_warn.py — Codex PreCompact hook.

Fires the instant Codex decides a thread needs compacting — the earliest,
most precise signal available that a session has grown large. Cross-checks
the thread against ~/.codex/goals_1.sqlite; if it's already over the token
or age threshold, surfaces a warning directly inside the live session
(systemMessage) plus a macOS notification, so it's seen right where the
work is happening instead of only in a background log. The notification is
throttled to once per thread per 12h (a runaway thread compacts often); the
systemMessage fires every time.

Always returns continue: true — informational only, never blocks Codex.
Fails silent and fast on any error.
"""

import json
import os
import sqlite3
import sys
import time

# Env override exists for the deployed/launchd case if someone relocates the checkout.
CLEANUP_HOME = os.environ.get("CODEX_CLEANUP_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

try:
    # notify_common.py lives at the repo root, one level up from hooks/.
    # Resolve via this file's own real path (not CLEANUP_HOME, which tests
    # deliberately override to a throwaway state dir) so the sibling import
    # always finds it.
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from notify_common import notify, load_notify_state, save_notify_state  # noqa: E402
except Exception:
    # This file's whole contract is "always exits 0, never raises uncaught"
    # — a missing/broken notify_common.py (partial deployment, someone
    # copied just hooks/ around) must degrade the popup silently, not turn
    # a documented no-op-on-error hook into a crashing one.
    def notify(title, message):
        pass

    def load_notify_state(state_file):
        return {}

    def save_notify_state(state_file, state):
        pass

DB = os.path.expanduser("~/.codex/goals_1.sqlite")
TOKEN_THRESHOLD = 20_000_000
AGE_DAYS_THRESHOLD = 3.0
STATE_FILE = os.path.join(CLEANUP_HOME, "precompact_state.json")
RENOTIFY_SECONDS = 12 * 3600  # popup at most once per thread per 12h; systemMessage still fires every time


def should_notify(thread_id: str) -> bool:
    """True if this thread hasn't triggered a popup in the last 12h.

    Same state-file pattern as goal_watch.py, but wrapped entirely in the
    fail-silent envelope: corrupt/missing/unwritable state means notify anyway
    — a lost throttle is annoying, a crashed hook is unacceptable.
    """
    try:
        now_ms = time.time() * 1000
        state = load_notify_state(STATE_FILE)
        if now_ms - state.get(thread_id, 0) < RENOTIFY_SECONDS * 1000:
            return False
        state[thread_id] = now_ms
        try:
            save_notify_state(STATE_FILE, state)
        except Exception:
            pass
        return True
    except Exception:
        return True


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
        tokens_used = tokens_used or 0  # NULL until the thread logs its first token count
        age_days = (time.time() * 1000 - created_at_ms) / 86_400_000

        if tokens_used < TOKEN_THRESHOLD and age_days < AGE_DAYS_THRESHOLD:
            return 0

        # `or [""]`: an empty/NULL objective makes splitlines() return [],
        # and [0] on that would silently kill the whole warning via the
        # fail-silent envelope — the one message this hook exists to deliver.
        short_obj = ((objective or "").strip().splitlines() or [""])[0][:80]
        msg = (
            f"This goal thread has used {tokens_used:,} tokens over "
            f"{age_days:.1f} days ({short_obj}). Consider archiving and "
            f"starting a fresh continuation before it compounds further."
        )
        print(json.dumps({"continue": True, "systemMessage": msg}))
        # systemMessage every firing (in-session, cheap); popup throttled so a
        # frequently-compacting runaway thread doesn't spam the desktop.
        if should_notify(session_id):
            notify("Codex /goal running long", f"{tokens_used:,} tokens, {age_days:.1f}d - {short_obj}")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
