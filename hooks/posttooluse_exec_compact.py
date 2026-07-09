#!/usr/bin/env python3
"""
posttooluse_exec_compact.py — Codex PostToolUse hook.

Fires after every tool call completes. For exec_command specifically, if the
output is large, archives the full raw output to a sidecar file and replaces
the recorded tool result with a head-tail-truncated version — so the bloat
never enters the transcript and never gets resent on future turns. Nothing
the model needs (failure signals concentrate at the start/end of output, per
the salience-aware truncation practice used by AutoPentest/EnIGMA) is lost,
and the full original is always recoverable from the archive.

Deliberately fails silent and fast on any error: a bug here must never block
or slow down a real Codex session. Always exits 0.
"""

import json
import os
import sys
import time

ARCHIVE_ROOT = os.path.expanduser("~/Sriinnu/Personal/codex-cleanup/tool-output-archive")
# Codex's own native exec-output truncation kicks in somewhere around
# 1000-2000 raw chars (confirmed empirically: 1000 chars passed through
# untouched, 2000 chars got truncated to ~300 by Codex itself before this
# hook ever saw it). Anything above that ceiling is already handled — our
# real gap is the band Codex lets through untouched but that's still worth
# trimming further. Keep head/tail small enough to sit below that ceiling.
KEEP_HEAD = 300
KEEP_TAIL = 300
MIN_SAVINGS = 400
TRUNCATE_THRESHOLD = KEEP_HEAD + KEEP_TAIL + MIN_SAVINGS
TOOL_NAMES = {"exec_command", "Bash", "bash", "shell"}


def extract_text(tool_response) -> str:
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        for key in ("output", "content", "stdout", "text"):
            v = tool_response.get(key)
            if isinstance(v, str):
                parts = [v]
                err = tool_response.get("stderr")
                if isinstance(err, str) and err:
                    parts.append("\n--- stderr ---\n" + err)
                return "".join(parts)
    return json.dumps(tool_response)


DEBUG_LOG = os.path.expanduser("~/Sriinnu/Personal/codex-cleanup/hooks/debug.log")
DEBUG_ENABLED = bool(os.environ.get("CODEX_HOOK_DEBUG"))


def debug(msg: str) -> None:
    if not DEBUG_ENABLED:
        return
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        debug(f"INVOKED raw_len={len(raw)} raw={raw[:500]}")
        payload = json.loads(raw)
        debug(f"tool_name={payload.get('tool_name')!r}")
        if payload.get("tool_name") not in TOOL_NAMES:
            debug("SKIP: tool_name not matched")
            return 0

        text = extract_text(payload.get("tool_response"))
        debug(f"text_len={len(text)} threshold={TRUNCATE_THRESHOLD} text={text[:200]!r}")
        if len(text) <= TRUNCATE_THRESHOLD:
            debug("SKIP: under threshold")
            return 0

        session_id = payload.get("session_id", "unknown")
        tool_use_id = payload.get("tool_use_id") or f"noid-{int(time.time()*1000)}"

        archive_dir = os.path.join(ARCHIVE_ROOT, session_id)
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(archive_dir, f"{tool_use_id}.txt")
        with open(archive_path, "w") as f:
            f.write(text)

        head = text[:KEEP_HEAD]
        tail = text[-KEEP_TAIL:]
        omitted = len(text) - KEEP_HEAD - KEEP_TAIL
        reason = (
            f"{head}\n"
            f"...[{omitted} chars truncated — full output archived at {archive_path}]...\n"
            f"{tail}"
        )

        debug(f"BLOCKING with reason_len={len(reason)}")
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    except Exception as e:
        debug(f"EXCEPTION: {type(e).__name__}: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
