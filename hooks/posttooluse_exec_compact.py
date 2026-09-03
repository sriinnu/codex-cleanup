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
# Codex natively truncates tool output at `tool_output_token_limit`
# (default 10000 tokens — verified from truncation notices clustering at
# "original token count: 10017/10025/10041", and from a hard cliff at 40KB
# in the output-size distribution). ~/.codex/config.toml now pins that to
# 2000 tokens (~8KB), which does the bulk of the work. This hook is the
# second stage under it: cut further to a head/tail window and archive the
# full original to disk so nothing is actually lost. Failure signals
# concentrate at the start and end of output, so head+tail is the right
# shape. Keep the window generous enough that the model doesn't re-run the
# command to recover detail — a re-run costs more than the bytes saved.
KEEP_HEAD = 1500
KEEP_TAIL = 1500
MIN_SAVINGS = 500
TRUNCATE_THRESHOLD = KEEP_HEAD + KEEP_TAIL + MIN_SAVINGS
# "exec" is the code_mode tool (features.code_mode_host) and is what the
# model actually calls — 5011 calls / 52.2MB over a measured 24h, versus
# 123 calls for "exec_command". Omitting it is why this hook never fired
# once between 2026-07-10 and 2026-09-03: tool-output-archive/ held nothing
# but .DS_Store. "exec_command"/"shell" kept for the non-code_mode path.
TOOL_NAMES = {"exec", "exec_command", "shell"}

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
    # code_mode's "exec" returns a list of content blocks
    # ([{"type": "input_text", "text": ...}, ...]), not a string or a dict.
    # Without this branch it fell through to json.dumps() and the truncated
    # replacement would have been a JSON blob instead of readable output.
    if isinstance(tool_response, list):
        parts = []
        for item in tool_response:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                v = item.get("text") or item.get("output") or item.get("content")
                parts.append(v if isinstance(v, str) else json.dumps(item))
            else:
                parts.append(json.dumps(item))
        return "".join(parts)
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
