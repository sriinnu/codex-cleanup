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
import glob
import json
import os
import sys

# Must match the reason string emitted by hooks/posttooluse_exec_compact.py.
MARKER = "chars truncated — full output archived at"
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
    from posttooluse_exec_compact import TRUNCATE_THRESHOLD
except Exception:
    TRUNCATE_THRESHOLD = 1000  # fallback: hook default (KEEP_HEAD + KEEP_TAIL + MIN_SAVINGS)


def find_latest() -> str:
    root = os.path.expanduser("~/.codex/sessions")
    paths = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    if not paths:
        sys.exit(f"no .jsonl files found under {root}")
    return max(paths, key=os.path.getmtime)


def output_text(out) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, dict) and isinstance(out.get("content"), str):
        return out["content"]
    return json.dumps(out) if out is not None else ""


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
            if '"function_call' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = obj.get("payload", obj)
            pt = p.get("type")
            if pt == "function_call":
                call_names[p.get("call_id")] = p.get("name")
            elif pt == "function_call_output":
                total_outputs += 1
                text = output_text(p.get("output"))
                if MARKER in text:
                    substituted += 1
                elif call_names.get(p.get("call_id")) in ("exec_command", "shell") \
                        and len(text) > TRUNCATE_THRESHOLD:
                    oversized_unmarked += 1

    print(f"file: {path}")
    print(f"function_call_output records: {total_outputs}")
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
