#!/usr/bin/env python3
"""
codex_session_cleaner.py — Sriinnu's Codex rollout de-bloater.

Codex CLI writes session rollouts to ~/.codex/sessions/YYYY/MM/DD/*.jsonl and
every context compaction re-snapshots the ENTIRE history into the file, so
they balloon into the gigabytes. This tool:

  report   show disk usage grouped by date (find where the bloat lives)
  clean    blank heavy string VALUES in-place — keys, ids, timestamps,
           line counts all survive, so Codex can still list the sessions
  compact  lighter touch than clean: drops pure-telemetry lines
           (token_count, and agent_message since it's a duplicate of the
           response_item copy) and blanks only exec_command args/output.
           Reasoning, messages, patches, plans, goal state, sub-agent
           markers all survive untouched — for sessions you still want to
           read later, not ones you're writing off.

Usage:
  python3 codex_session_cleaner.py report
  python3 codex_session_cleaner.py report --root ~/.codex/sessions
  python3 codex_session_cleaner.py clean --dry-run
  python3 codex_session_cleaner.py clean --before 2026-06-01
  python3 codex_session_cleaner.py clean --keep-days 14
  python3 codex_session_cleaner.py clean --min-size 10        # only files >= 10 MB
  python3 codex_session_cleaner.py compact --dry-run
  python3 codex_session_cleaner.py compact --min-size 1
"""

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta

DEFAULT_ROOT = os.path.expanduser("~/.codex/sessions")

# Structural keys whose string values survive. Everything else string -> "".
# Numbers, bools, nulls are untouched either way.
PRESERVE = {
    "type", "id", "call_id", "turn_id", "role", "name", "status", "phase",
    "execution", "timestamp", "started_at", "completed_at", "source",
    "originator", "cli_version", "model", "provider", "limit_id",
    "session_id", "conversation_id", "rollout_id", "collaboration_mode_kind",
    "approval_policy", "sandbox_mode", "cwd", "git_branch", "thread_source",
    "model_provider",
}

# Matches .../YYYY/MM/DD/... in a session path
DATE_RE = re.compile(r"(\d{4})[/\\](\d{2})[/\\](\d{2})")


def file_date(path: str):
    """Pull YYYY-MM-DD out of the directory layout. None if it doesn't match."""
    m = DATE_RE.search(path)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def iter_rollouts(root: str):
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:7.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # unreachable, keeps type-checkers calm


# ---------------------------------------------------------------- report ----

def cmd_report(args) -> int:
    by_date = defaultdict(lambda: [0, 0])  # date -> [bytes, file count]
    total = 0
    for p in iter_rollouts(args.root):
        d = file_date(p) or "?"
        sz = os.path.getsize(p)
        by_date[str(d)][0] += sz
        by_date[str(d)][1] += sz and 1
        total += sz

    if not by_date:
        print(f"No .jsonl rollouts under {args.root}")
        return 1

    print(f"{'date':<12} {'files':>5} {'size':>10}")
    print("-" * 30)
    for d in sorted(by_date):
        sz, n = by_date[d]
        print(f"{d:<12} {n:>5} {human(sz):>10}")
    print("-" * 30)
    print(f"{'TOTAL':<12} {sum(v[1] for v in by_date.values()):>5} {human(total):>10}")
    return 0


# ----------------------------------------------------------------- clean ----

def blank(obj):
    """Recursively empty string values, keeping structural keys intact."""
    if isinstance(obj, dict):
        return {
            k: (v if (k in PRESERVE and isinstance(v, str)) else blank(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [blank(x) for x in obj]
    if isinstance(obj, str):
        return ""
    return obj


def clean_file(path: str) -> tuple:
    """Blank one file atomically (write temp sibling, then replace).

    Restores the original atime/mtime after the rewrite — blanking is a
    content-only operation, and a bare os.replace() would otherwise stamp
    every touched file with today's date, making an old session look like
    it was just modified.

    Returns (before_bytes, after_bytes, bad_line_count, skip_reason).
    skip_reason is None on a normal clean; if it's set, the file was left
    completely untouched and before == after.
    """
    before = os.path.getsize(path)
    orig_stat = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    bad = 0
    try:
        with os.fdopen(fd, "w") as out, open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.write(json.dumps(blank(json.loads(line)),
                                         separators=(",", ":")) + "\n")
                except json.JSONDecodeError:
                    bad += 1
                    out.write("{}\n")  # unparseable line -> keep line count anyway

        # Guard against a concurrent writer: if the file's size or mtime
        # changed since we started reading it (e.g. Codex resumed and
        # appended to this exact session mid-cleanup), our tmp content was
        # built from a stale read. Blindly replacing now would silently
        # discard whatever got written in the meantime. Bail instead —
        # leave the original untouched, let a future run pick it up.
        current_stat = os.stat(path)
        if (current_stat.st_mtime != orig_stat.st_mtime or
                current_stat.st_size != orig_stat.st_size):
            os.unlink(tmp)
            return before, before, bad, "file changed during processing, skipped"

        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass  # best-effort cleanup; don't mask the real exception below
        raise

    # Best-effort: the blank already succeeded and is irreversible at this
    # point, so a utime failure (unusual filesystem, permission quirk) must
    # not raise and abort the rest of the batch over a cosmetic timestamp.
    try:
        os.utime(path, (orig_stat.st_atime, orig_stat.st_mtime))
    except OSError as e:
        print(f"  (warning: could not restore mtime for {path}: {e})", file=sys.stderr)

    return before, os.path.getsize(path), bad, None


def cmd_clean(args) -> int:
    cutoff = None
    if args.before and args.keep_days is not None:
        print("Pick --before OR --keep-days, not both.", file=sys.stderr)
        return 2
    if args.before:
        cutoff = datetime.strptime(args.before, "%Y-%m-%d").date()
    elif args.keep_days is not None:
        cutoff = date.today() - timedelta(days=args.keep_days)

    min_bytes = int(args.min_size * 1024 * 1024)

    targets = []
    for p in iter_rollouts(args.root):
        d = file_date(p)
        if cutoff and (d is None or d >= cutoff):
            continue  # too recent (or undated) -> leave it alone
        if os.path.getsize(p) < min_bytes:
            continue
        targets.append(p)

    if not targets:
        print("Nothing matches — no files to clean.")
        return 0

    targets.sort(key=os.path.getsize, reverse=True)  # big fish first

    if args.dry_run:
        print(f"DRY RUN — would clean {len(targets)} files "
              f"({human(sum(map(os.path.getsize, targets)))}):")
        for p in targets:
            print(f"  {human(os.path.getsize(p))}  {p}")
        return 0

    tb = ta = 0
    cleaned = skipped = failed = 0
    for p in targets:
        # Catching Exception (not BaseException) is deliberate: one bad file
        # must not stop the batch, but Ctrl-C (KeyboardInterrupt) still has
        # to work — it isn't an Exception subclass, so it isn't swallowed.
        try:
            b, a, bad, skip_reason = clean_file(p)
        except Exception as e:
            failed += 1
            print(f"  ERROR: {p}: {e}", file=sys.stderr)
            continue
        if skip_reason:
            skipped += 1
            print(f"  SKIPPED: {p}: {skip_reason}")
            continue
        cleaned += 1
        tb += b
        ta += a
        flag = f"  ({bad} unparseable lines)" if bad else ""
        print(f"{human(b)} -> {human(a)}  {p}{flag}")
    print("-" * 30)
    pct = (1 - ta / tb) * 100 if tb else 0
    summary = f"TOTAL {human(tb)} -> {human(ta)}  ({pct:.1f}% reclaimed, {cleaned} files)"
    if skipped:
        summary += f", {skipped} skipped"
    if failed:
        summary += f", {failed} failed"
    print(summary)
    return 1 if failed else 0


# ---------------------------------------------------------------- compact ----

DROP_EVENT_TYPES = {"token_count", "agent_message"}


def compact_line(line: str, call_names: dict):
    """Return (output_line_or_None, changed) for one raw JSONL line.

    None means drop the line entirely. changed=False means: don't bother
    re-serializing, write the original bytes back untouched. The substring
    pre-filter skips json.loads for the majority of lines (reasoning,
    messages, patches, plan updates...) that can't possibly match —
    both for speed and so untouched lines never risk picking up
    json.dumps' default whitespace and quietly growing the file.
    """
    if '"token_count"' in line or '"agent_message"' in line:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return line, False
        if obj.get("type") == "event_msg" and obj.get("payload", {}).get("type") in DROP_EVENT_TYPES:
            return None, True
        return line, False

    if '"exec_command"' in line or '"function_call_output"' in line:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return line, False
        if obj.get("type") != "response_item":
            return line, False
        p = obj.get("payload", {})
        pt = p.get("type")
        changed = False
        if pt == "function_call":
            name = p.get("name", "?")
            cid = p.get("call_id")
            if cid:
                call_names[cid] = name
            if name == "exec_command" and p.get("arguments"):
                p["arguments"] = ""
                changed = True
        elif pt == "function_call_output":
            cid = p.get("call_id")
            if call_names.get(cid) == "exec_command":
                out = p.get("output")
                if isinstance(out, str) and out:
                    p["output"] = ""
                    changed = True
                elif isinstance(out, dict) and out.get("content"):
                    out["content"] = ""
                    changed = True
        if changed:
            return json.dumps(obj, separators=(",", ":")) + "\n", True
        return line, False

    return line, False


def compact_file(path: str) -> tuple:
    """Drop telemetry lines and blank exec_command payloads, atomically.

    Returns (before_bytes, after_bytes, dropped_lines, edited_lines, skip_reason).
    """
    before = os.path.getsize(path)
    orig_stat = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    call_names = {}
    dropped = edited = 0
    try:
        with os.fdopen(fd, "w") as out, open(path, "r", errors="replace") as f:
            for line in f:
                new_line, changed = compact_line(line, call_names)
                if new_line is None:
                    dropped += 1
                    continue
                if changed:
                    edited += 1
                out.write(new_line if new_line.endswith("\n") else new_line + "\n")

        # Same concurrent-writer guard as clean_file: bail rather than
        # silently discard anything Codex appended mid-run.
        current_stat = os.stat(path)
        if (current_stat.st_mtime != orig_stat.st_mtime or
                current_stat.st_size != orig_stat.st_size):
            os.unlink(tmp)
            return before, before, 0, 0, "file changed during processing, skipped"

        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    try:
        os.utime(path, (orig_stat.st_atime, orig_stat.st_mtime))
    except OSError as e:
        print(f"  (warning: could not restore mtime for {path}: {e})", file=sys.stderr)

    return before, os.path.getsize(path), dropped, edited, None


def cmd_compact(args) -> int:
    cutoff = None
    if args.before and args.keep_days is not None:
        print("Pick --before OR --keep-days, not both.", file=sys.stderr)
        return 2
    if args.before:
        cutoff = datetime.strptime(args.before, "%Y-%m-%d").date()
    elif args.keep_days is not None:
        cutoff = date.today() - timedelta(days=args.keep_days)

    min_bytes = int(args.min_size * 1024 * 1024)

    targets = []
    for p in iter_rollouts(args.root):
        d = file_date(p)
        if cutoff and (d is None or d >= cutoff):
            continue
        if os.path.getsize(p) < min_bytes:
            continue
        targets.append(p)

    if not targets:
        print("Nothing matches — no files to compact.")
        return 0

    targets.sort(key=os.path.getsize, reverse=True)

    if args.dry_run:
        print(f"DRY RUN — would compact {len(targets)} files "
              f"({human(sum(map(os.path.getsize, targets)))}):")
        for p in targets:
            print(f"  {human(os.path.getsize(p))}  {p}")
        return 0

    tb = ta = 0
    done = skipped = failed = 0
    for p in targets:
        try:
            b, a, dropped, edited, skip_reason = compact_file(p)
        except Exception as e:
            failed += 1
            print(f"  ERROR: {p}: {e}", file=sys.stderr)
            continue
        if skip_reason:
            skipped += 1
            print(f"  SKIPPED: {p}: {skip_reason}")
            continue
        done += 1
        tb += b
        ta += a
        flag = f"  (-{dropped} lines, {edited} blanked)" if (dropped or edited) else "  (nothing to touch)"
        print(f"{human(b)} -> {human(a)}  {p}{flag}")
    print("-" * 30)
    pct = (1 - ta / tb) * 100 if tb else 0
    summary = f"TOTAL {human(tb)} -> {human(ta)}  ({pct:.1f}% reclaimed, {done} files)"
    if skipped:
        summary += f", {skipped} skipped"
    if failed:
        summary += f", {failed} failed"
    print(summary)
    return 1 if failed else 0


# ------------------------------------------------------------------ main ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help=f"sessions root (default: {DEFAULT_ROOT})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="disk usage by date")

    cl = sub.add_parser("clean", help="blank heavy string values in-place")
    cl.add_argument("--before", metavar="YYYY-MM-DD",
                    help="only clean sessions dated strictly before this")
    cl.add_argument("--keep-days", type=int, metavar="N",
                    help="keep the last N days untouched, clean the rest")
    cl.add_argument("--min-size", type=float, default=0, metavar="MB",
                    help="only touch files >= this many MB (default: all)")
    cl.add_argument("--dry-run", action="store_true",
                    help="list what would be cleaned, change nothing")

    co = sub.add_parser("compact", help="drop telemetry + blank exec_command, keep everything else")
    co.add_argument("--before", metavar="YYYY-MM-DD",
                    help="only compact sessions dated strictly before this")
    co.add_argument("--keep-days", type=int, metavar="N",
                    help="keep the last N days untouched, compact the rest")
    co.add_argument("--min-size", type=float, default=0, metavar="MB",
                    help="only touch files >= this many MB (default: all)")
    co.add_argument("--dry-run", action="store_true",
                    help="list what would be compacted, change nothing")

    args = ap.parse_args()
    args.root = os.path.expanduser(args.root)
    if not os.path.isdir(args.root):
        print(f"Root not found: {args.root}", file=sys.stderr)
        return 2
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "compact":
        return cmd_compact(args)
    return cmd_clean(args)


if __name__ == "__main__":
    sys.exit(main())
