#!/usr/bin/env python3
"""
goal_watch.py — nudge before a /goal thread turns into another 560M-token disaster.

Codex doesn't rotate or checkpoint long-running /goal threads on its own —
see codex_session_cleaner.py's docstring for the disk-side symptom of the
same root cause. This polls ~/.codex/goals_1.sqlite and flags any thread
that's either burned too many tokens or run too many days without hitting
'complete', so it can be archived+restarted deliberately instead of found
out about only after it hits 'usage_limited'.

Usage:
  python3 goal_watch.py
  python3 goal_watch.py --token-threshold 20000000 --age-days 3
  python3 goal_watch.py --quiet   # no macOS notification, just stdout
"""

import argparse
import os
import sqlite3
import sys
import time

# notify_common.py sits next to this file at the repo root; running the
# script directly (or via subprocess, as the tests and launchd both do)
# already puts that directory at sys.path[0], so no path surgery needed.
#
# Unlike hooks/precompact_warn.py, this import is deliberately NOT wrapped
# in a fallback: this is a standalone launchd job, not something wired into
# a live Codex session, so a broken notify_common.py should fail loudly
# (non-zero exit, visible in launchd logs) rather than silently run "clean"
# while never actually notifying anyone about a runaway thread.
from notify_common import notify, load_notify_state, save_notify_state

# Env override exists for the deployed/launchd case if someone relocates the checkout.
CLEANUP_HOME = os.environ.get("CODEX_CLEANUP_HOME") or os.path.dirname(os.path.abspath(__file__))

DEFAULT_DB = os.path.expanduser("~/.codex/goals_1.sqlite")
STATE_FILE = os.path.join(CLEANUP_HOME, "goal_watch_state.json")
RENOTIFY_SECONDS = 12 * 3600  # don't re-nag about the same thread more than every 12h

LIVE_STATUSES = ("active", "paused", "blocked", "usage_limited", "budget_limited")


def load_state() -> dict:
    return load_notify_state(STATE_FILE)


def save_state(state: dict) -> None:
    save_notify_state(STATE_FILE, state)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--token-threshold", type=int, default=20_000_000,
                    help="flag a thread once tokens_used crosses this (default: 20M)")
    ap.add_argument("--age-days", type=float, default=3.0,
                    help="flag a thread once it's been open this many days (default: 3)")
    ap.add_argument("--quiet", action="store_true",
                    help="print only, skip the macOS notification")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no goals db at {args.db}")
        return 0

    now_ms = time.time() * 1000
    conn = sqlite3.connect(args.db)
    placeholders = ",".join("?" * len(LIVE_STATUSES))
    rows = conn.execute(
        f"SELECT thread_id, objective, status, tokens_used, created_at_ms "
        f"FROM thread_goals WHERE status IN ({placeholders})",
        LIVE_STATUSES,
    ).fetchall()
    conn.close()

    state = load_state()
    flagged = []
    for thread_id, objective, status, tokens_used, created_at_ms in rows:
        tokens_used = tokens_used or 0  # NULL until the thread logs its first token count
        age_days = (now_ms - created_at_ms) / 86_400_000
        over_tokens = tokens_used >= args.token_threshold
        over_age = age_days >= args.age_days
        if not (over_tokens or over_age):
            continue

        last_notified = state.get(thread_id, 0)
        if now_ms - last_notified < RENOTIFY_SECONDS * 1000:
            continue  # nagged about this one recently, hold off

        flagged.append((thread_id, objective, status, tokens_used, age_days))
        state[thread_id] = now_ms

    if not flagged:
        save_state(state)
        print("no goal threads over threshold right now")
        return 0

    for thread_id, objective, status, tokens_used, age_days in flagged:
        # `or [""]`: empty/NULL objective -> splitlines() is [], and [0] on
        # that crashed here once — with state already saved, the thread got
        # marked "notified" for 12h without any notification ever firing.
        short_obj = ((objective or "").strip().splitlines() or [""])[0][:80]
        print(f"{thread_id[:8]}  {status:14}  {tokens_used:>12,} tok  {age_days:5.1f}d  {short_obj}")
        if not args.quiet:
            notify(
                "Codex /goal running long",
                f"{tokens_used:,} tokens, {age_days:.1f} days - {short_obj}",
            )

    # Saved only after the notifications actually went out — recording a
    # thread as "notified" before trying would throttle it for 12h even if
    # this run crashed mid-loop.
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
