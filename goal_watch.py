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
import json
import os
import sqlite3
import subprocess
import sys
import time

DEFAULT_DB = os.path.expanduser("~/.codex/goals_1.sqlite")
STATE_FILE = os.path.expanduser("~/Sriinnu/Personal/codex-cleanup/goal_watch_state.json")
RENOTIFY_SECONDS = 12 * 3600  # don't re-nag about the same thread more than every 12h

LIVE_STATUSES = ("active", "paused", "blocked", "usage_limited", "budget_limited")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(title: str, message: str) -> None:
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], check=False)


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

    save_state(state)

    if not flagged:
        print("no goal threads over threshold right now")
        return 0

    for thread_id, objective, status, tokens_used, age_days in flagged:
        short_obj = (objective or "").strip().splitlines()[0][:80]
        print(f"{thread_id[:8]}  {status:14}  {tokens_used:>12,} tok  {age_days:5.1f}d  {short_obj}")
        if not args.quiet:
            notify(
                "Codex /goal running long",
                f"{tokens_used:,} tokens, {age_days:.1f} days — {short_obj}",
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
