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
import re
import sys
import time

# Env override exists for the deployed/launchd case if someone relocates the checkout.
CLEANUP_HOME = os.environ.get("CODEX_CLEANUP_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

ARCHIVE_ROOT = os.path.join(CLEANUP_HOME, "tool-output-archive")
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
# Codex only ever sends "exec_command" (empirically confirmed); "shell" kept as cheap insurance against a rename.
TOOL_NAMES = {"exec_command", "shell"}

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_id(value) -> str:
    """Make an untrusted id safe to use as a single archive path component.

    session_id/tool_use_id come straight off the hook's stdin payload with
    no validation — including no guarantee they're even strings. Coerce
    first (a non-str value must degrade to *some* safe id, not raise and
    silently skip archiving+truncation for that call). Collapse anything
    but a conservative charset, then strip leading/trailing '.'/'_' so a
    value like "../../x" can't survive as "..", and cap the length well
    under filesystem name limits so a pathological id can't ENAMETOOLONG.
    """
    cleaned = _UNSAFE_ID_CHARS.sub("_", str(value)).strip("._")
    return (cleaned or "unknown")[:200]


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


DEBUG_LOG = os.path.join(CLEANUP_HOME, "hooks", "debug.log")
DEBUG_ENABLED = bool(os.environ.get("CODEX_HOOK_DEBUG"))


def debug(msg: str) -> None:
    if not DEBUG_ENABLED:
        return
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
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

        session_id = sanitize_id(payload.get("session_id") or "unknown")
        tool_use_id = sanitize_id(payload.get("tool_use_id") or f"noid-{int(time.time()*1000)}")

        archive_dir = os.path.join(ARCHIVE_ROOT, session_id)
        os.makedirs(archive_dir, exist_ok=True)

        # sanitize_id() blocks traversal via the id text itself, but not a
        # symlink someone (same-user local access) already planted in the
        # archive tree, which os.makedirs/open would otherwise follow right
        # out of ARCHIVE_ROOT. Refuse rather than write through it.
        real_root = os.path.realpath(ARCHIVE_ROOT)
        real_dir = os.path.realpath(archive_dir)
        if real_dir != real_root and not real_dir.startswith(real_root + os.sep):
            debug(f"REFUSED: archive_dir escaped ARCHIVE_ROOT via symlink: {real_dir}")
            return 0

        archive_path = os.path.join(archive_dir, f"{tool_use_id}.txt")
        # Explicit utf-8: on a non-UTF-8 locale the platform default (e.g.
        # cp1252) makes this write raise on any exotic char, and the
        # fail-silent envelope would then skip the block decision entirely —
        # full bloat in the transcript plus a misleading 0-byte archive.
        # O_NOFOLLOW: refuse to write through archive_path itself if it's
        # already a symlink (same escape as above, one level lower).
        fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
