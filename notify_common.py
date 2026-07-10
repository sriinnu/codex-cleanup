#!/usr/bin/env python3
"""
notify_common.py — shared macOS-notification + throttle-state helpers for
goal_watch.py and hooks/precompact_warn.py.

Both watch the same class of thing (a long-running /goal thread) and had
independently re-implemented the identical osascript notifier and
{thread_id: last_notified_ms} JSON state file — with one copy missing a
safety fix the other had. One copy now, so a future fix lands in both.
"""

import json
import os
import subprocess


def notify(title: str, message: str) -> None:
    """Fire a macOS notification. Best-effort: never raises.

    osascript's -e argument handling is flaky with non-ASCII punctuation
    (em-dashes, curly quotes) depending on locale — strip to plain ASCII
    rather than risk a silent notification failure over cosmetics.
    """
    try:
        title = title.encode("ascii", "ignore").decode()
        message = message.encode("ascii", "ignore").decode()
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], check=False, timeout=3,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_notify_state(state_file: str) -> dict:
    """Read a {thread_id: last_notified_ms} throttle file. Corrupt/missing -> {}."""
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_notify_state(state_file: str, state: dict) -> None:
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
