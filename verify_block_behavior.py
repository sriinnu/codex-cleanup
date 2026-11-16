#!/usr/bin/env python3
"""
verify_block_behavior.py — check that the PostToolUse 'block' trick actually works.

hooks/posttooluse_exec_compact.py emits {"decision": "block", "reason": <truncated
text>} and ASSUMES Codex substitutes that reason into the recorded transcript in
place of the raw exec output. That assumption was never verified end-to-end —
"block" semantics come from Claude Code's hook contract and Codex may record the
original output regardless. Run this on a rollout .jsonl after a test session
(one with some deliberately huge exec outputs) to find out which way it went.

Usage:
  python3 verify_block_behavior.py ~/.codex/sessions/.../rollout-*.jsonl
  python3 verify_block_behavior.py --latest   # newest .jsonl under ~/.codex/sessions
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

# Must match the reason string emitted by hooks/posttooluse_exec_compact.py.
MARKER = "chars truncated — full output archived at"
# json.dumps() escapes the em-dash to \u2014, so any code path that falls back
# to dumping a structure would hide the marker from a plain substring test.
MARKER_ESCAPED = MARKER.replace("—", "\\u2014")


def has_marker(text: str) -> bool:
    return MARKER in text or MARKER_ESCAPED in text
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
    from posttooluse_exec_compact import TRUNCATE_THRESHOLD
except Exception:
    TRUNCATE_THRESHOLD = 1000  # fallback: hook default (KEEP_HEAD + KEEP_TAIL + MIN_SAVINGS)


# rollout-2026-09-03T07-39-26-<uuid>.jsonl -> the session's start time.
_ROLLOUT_TS = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-")


def session_start(path: str):
    """Session start from the filename, or None if it doesn't parse."""
    m = _ROLLOUT_TS.search(os.path.basename(path))
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


def find_latest() -> str:
    """Newest session by START TIME, not mtime.

    mtime is not trustworthy here: housekeeping (the compact/clean jobs, a
    resumed thread, Codex's own rollout migration) rewrites old rollout
    files and bumps their timestamps. Sorting by mtime picked a session
    from 2026-07-10 while sessions from today sat right beside it, and the
    resulting FAIL looked like a real verdict instead of a stale file. The
    filename carries the session start and nothing ever rewrites it.
    """
    root = os.path.expanduser("~/.codex/sessions")
    paths = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    if not paths:
        sys.exit(f"no .jsonl files found under {root}")
    dated = [(session_start(p), p) for p in paths]
    dated = [(ts, p) for ts, p in dated if ts is not None]
    if not dated:
        # No parseable names at all: mtime is all that's left, but say so.
        print("warning: no rollout-<timestamp> filenames found; "
              "falling back to mtime, which housekeeping can bump.",
              file=sys.stderr)
        return max(paths, key=os.path.getmtime)
    return max(dated)[1]


def output_text(out) -> str:
    if isinstance(out, str):
        return out
    # code_mode's "exec" returns [{"type": "input_text", "text": ...}, ...].
    # Without this branch it fell through to json.dumps() and the marker's
    # em-dash got escaped, so a working hook still read as "no marker".
    if isinstance(out, list):
        parts = []
        for item in out:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                v = item.get("text") or item.get("output") or item.get("content")
                parts.append(v if isinstance(v, str) else json.dumps(item))
            else:
                parts.append(json.dumps(item))
        return "".join(parts)
    if isinstance(out, dict) and isinstance(out.get("content"), str):
        return out["content"]
    return json.dumps(out) if out is not None else ""


# code_mode (features.code_mode_host) records custom_tool_call/_output; the
# non-code_mode path records function_call/_output. Scanning only the latter
# meant this verifier saw zero traffic in a code_mode session and reported
# INCONCLUSIVE unconditionally -- which is why the block-substitution question
# stayed open. Accept both shapes.
CALL_TYPES = ("function_call", "custom_tool_call")
OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")
EXEC_TOOLS = ("exec", "exec_command", "shell")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="rollout .jsonl to inspect")
    ap.add_argument("--latest", action="store_true",
                    help="inspect the newest .jsonl under ~/.codex/sessions")
    args = ap.parse_args()
    path = find_latest() if args.latest else args.path
    if not path:
        ap.error("give a rollout path or --latest")

    call_names = {}          # call_id -> tool name, so outputs can be attributed
    total_outputs = 0
    substituted = 0          # outputs carrying the hook's marker: block WORKED
    oversized_unmarked = 0   # exec outputs over threshold with no marker: block did NOT substitute

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"function_call' not in line and '"custom_tool_call' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = obj.get("payload", obj)
            pt = p.get("type")
            if pt in CALL_TYPES:
                call_names[p.get("call_id")] = p.get("name")
            elif pt in OUTPUT_TYPES:
                total_outputs += 1
                text = output_text(p.get("output"))
                if has_marker(text):
                    substituted += 1
                elif call_names.get(p.get("call_id")) in EXEC_TOOLS \
                        and len(text) > TRUNCATE_THRESHOLD:
                    oversized_unmarked += 1

    started = session_start(path)
    print(f"file: {path}")
    if started is not None:
        age_h = (datetime.datetime.now() - started).total_seconds() / 3600
        note = f"session started: {started:%Y-%m-%d %H:%M} ({age_h:.1f}h ago)"
        if age_h > 6:
            note += "  <-- STALE: this is not a session you just ran"
        print(note)
    print(f"tool output records (function_call + custom_tool_call): {total_outputs}")
    print(f"  with hook marker (substitution WORKED):        {substituted}")
    print(f"  exec outputs > {TRUNCATE_THRESHOLD} chars, NO marker (NOT working): {oversized_unmarked}")
    if substituted and not oversized_unmarked:
        print("PASS: Codex substitutes the block reason into the transcript.")
    elif oversized_unmarked:
        # plain ASCII: Windows consoles on legacy code pages mangle em-dashes
        print("FAIL: oversized exec outputs recorded raw - the 'block' assumption is wrong.")
    else:
        print("INCONCLUSIVE: no output in this session was big enough to trigger the hook; "
              "rerun a session with an exec output well over the threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
