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
           response_item copy), blanks exec_command args/output, dedupes
           the full system prompt that session_meta re-embeds on every
           fork/compaction (kept once per file, blanked after), and trims
           replacement_history out of every compacted snapshot except the
           newest (older ones are superseded the moment a later compaction
           happens — Codex only needs the latest to resume).
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
        by_date[str(d)][1] += 1
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


# ------------------------------------------------------- batch machinery ----
# clean and compact share the exact same selection + batch-run skeleton;
# only the per-file transform and the wording differ, so both live here once.

def select_targets(args):
    """Resolve --before/--keep-days/--min-size into a worklist.

    Returns (targets, error_code_or_None). targets is sorted biggest-first;
    error_code is 2 on the --before/--keep-days conflict (message already
    printed), else None.
    """
    if args.before and args.keep_days is not None:
        print("Pick --before OR --keep-days, not both.", file=sys.stderr)
        return [], 2
    cutoff = None
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

    targets.sort(key=os.path.getsize, reverse=True)  # big fish first
    return targets, None


def run_batch(targets, args, process_fn, verb: str) -> int:
    """Dry-run listing, per-file processing loop, and TOTAL summary.

    process_fn(path) -> (before_bytes, after_bytes, flag_suffix, skip_reason);
    the flag suffix is the command-specific per-file annotation, already
    formatted (empty string for none).
    """
    if not targets:
        print(f"Nothing matches — no files to {verb}.")
        return 0

    if args.dry_run:
        print(f"DRY RUN — would {verb} {len(targets)} files "
              f"({human(sum(map(os.path.getsize, targets)))}):")
        for p in targets:
            print(f"  {human(os.path.getsize(p))}  {p}")
        return 0

    tb = ta = 0
    done = skipped = failed = 0
    for p in targets:
        # Catching Exception (not BaseException) is deliberate: one bad file
        # must not stop the batch, but Ctrl-C (KeyboardInterrupt) still has
        # to work — it isn't an Exception subclass, so it isn't swallowed.
        try:
            b, a, flag, skip_reason = process_fn(p)
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


def _clean_one(path: str) -> tuple:
    """Adapt clean_file to the run_batch process_fn contract."""
    b, a, bad, skip_reason = clean_file(path)
    flag = f"  ({bad} unparseable lines)" if bad else ""
    return b, a, flag, skip_reason


def cmd_clean(args) -> int:
    targets, err = select_targets(args)
    if err is not None:
        return err
    return run_batch(targets, args, _clean_one, "clean")


# ---------------------------------------------------------------- compact ----

DROP_EVENT_TYPES = {"token_count", "agent_message"}

# Substrings that mean a line might need work at all — skips json.loads for
# the majority of lines (reasoning, messages, patches, plan updates...) that
# can't possibly match, both for speed and so untouched lines never risk
# picking up json.dumps' default whitespace and quietly growing the file.
# session_meta/compacted use bare words (no surrounding quotes) since their
# JSON spacing isn't guaranteed and the type check right after json.loads
# is what actually decides correctness — this is just the cheap pre-filter.
TRIGGER_SUBSTRINGS = ('"token_count"', '"agent_message"',
                      '"exec_command"', '"function_call_output"',
                      'session_meta', 'compacted')

# Wording must match the placeholder hooks/posttooluse_exec_compact.py writes
# into function_call_output.output when it archives+truncates a large exec
# result (see its `reason` string). If output already carries this marker,
# it's already the small pointer to the archive — blanking it would erase
# the transcript's only recorded path to the original.
ARCHIVED_MARKER = "chars truncated — full output archived at"


def _already_archived(text) -> bool:
    return isinstance(text, str) and ARCHIVED_MARKER in text


def compact_line(line: str, call_names: dict, session_state: dict = None):
    """Return (output_line_or_None, changed) for one raw JSONL line.

    None means drop the line entirely. changed=False means: don't bother
    re-serializing, write the original bytes back untouched. Every trigger
    substring is checked up front and the line parsed at most once — an
    exec_command function_call_output whose own content happens to contain
    an incidental "token_count"/"agent_message" substring must still reach
    the exec-blanking branch below, not bail out on the telemetry check.

    session_state carries the two cross-line counters session_meta/compacted
    handling need (call_names only ever needs the current line). None means
    "caller doesn't want this dedup" — used by tests that only care about the
    exec_command path — and both branches degrade to a no-op passthrough.
    """
    if not any(s in line for s in TRIGGER_SUBSTRINGS):
        return line, False
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line, False

    t = obj.get("type")

    if t == "session_meta":
        if session_state is None:
            return line, False
        bi = obj.get("payload", {}).get("base_instructions")
        if not isinstance(bi, dict) or not bi.get("text"):
            return line, False
        if not session_state.get("seen_base_instructions"):
            session_state["seen_base_instructions"] = True
            return line, False
        # Every later fork/compaction re-embeds the same fixed system prompt
        # byte-for-byte — one copy per file is enough for Codex to know what
        # was in play; the rest is pure duplication.
        bi["text"] = ""
        return json.dumps(obj, separators=(",", ":")) + "\n", True

    if t == "compacted":
        if session_state is None:
            return line, False
        session_state["compacted_seen"] = session_state.get("compacted_seen", 0) + 1
        is_last = session_state["compacted_seen"] >= session_state.get("total_compacted", 0)
        # replacement_history lives under payload, not the top level.
        cp = obj.get("payload", {})
        if is_last or not cp.get("replacement_history"):
            return line, False
        # Superseded the moment a later compaction happened — only the
        # newest replacement_history is what Codex actually reloads to
        # resume the thread; earlier ones are stale audit snapshots that
        # never shrink on their own (that's the bug this whole tool exists
        # to route around).
        cp["replacement_history"] = []
        return json.dumps(obj, separators=(",", ":")) + "\n", True

    if t == "event_msg":
        if obj.get("payload", {}).get("type") in DROP_EVENT_TYPES:
            return None, True
        return line, False

    if t != "response_item":
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
            if isinstance(out, str) and out and not _already_archived(out):
                p["output"] = ""
                changed = True
            elif isinstance(out, dict) and not _already_archived(out.get("content")):
                if out.get("content"):
                    out["content"] = ""
                    changed = True
    if changed:
        return json.dumps(obj, separators=(",", ":")) + "\n", True
    return line, False


def _count_compacted(path: str) -> int:
    """Pre-pass so compact_line can tell, while streaming forward, whether
    the compacted entry it's looking at is the last one in the file (and
    therefore the only one worth keeping full). Cheap: same substring
    pre-filter, one extra sequential read."""
    n = 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            if "compacted" not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "compacted":
                n += 1
    return n


def compact_file(path: str) -> tuple:
    """Drop telemetry lines, blank exec_command payloads, dedupe repeated
    session_meta system prompts, and trim stale compacted snapshots —
    atomically.

    Returns (before_bytes, after_bytes, dropped_lines, edited_lines, skip_reason).
    """
    total_compacted = _count_compacted(path)
    before = os.path.getsize(path)
    orig_stat = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    call_names = {}
    session_state = {"seen_base_instructions": False, "compacted_seen": 0,
                     "total_compacted": total_compacted}
    dropped = edited = 0
    try:
        with os.fdopen(fd, "w") as out, open(path, "r", errors="replace") as f:
            for line in f:
                new_line, changed = compact_line(line, call_names, session_state)
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


def _compact_one(path: str) -> tuple:
    """Adapt compact_file to the run_batch process_fn contract."""
    b, a, dropped, edited, skip_reason = compact_file(path)
    flag = (f"  (-{dropped} lines, {edited} blanked)"
            if (dropped or edited) else "  (nothing to touch)")
    return b, a, flag, skip_reason


def cmd_compact(args) -> int:
    targets, err = select_targets(args)
    if err is not None:
        return err
    return run_batch(targets, args, _compact_one, "compact")


# ------------------------------------------------------------------ main ----

def add_selection_flags(sp, verb: str) -> None:
    """Attach the shared clean/compact selection flags, worded per-command."""
    add_root_flag(sp)
    sp.add_argument("--before", metavar="YYYY-MM-DD",
                    help=f"only {verb} sessions dated strictly before this")
    sp.add_argument("--keep-days", type=int, metavar="N",
                    help=f"keep the last N days untouched, {verb} the rest")
    sp.add_argument("--min-size", type=float, default=0, metavar="MB",
                    help="only touch files >= this many MB (default: all)")
    sp.add_argument("--dry-run", action="store_true",
                    help=f"list what would be {verb}ed, change nothing")


def add_root_flag(sp) -> None:
    # Per-subcommand: on the main parser the documented `report --root ...`
    # order is a parse error, and dual definition lets defaults clobber it
    # (argparse subparsers build their own namespace and overwrite the
    # caller's value — see _normalize_root_argv below for the other half
    # of this fix, which lets `--root ... <cmd>` keep working too).
    sp.add_argument("--root", default=DEFAULT_ROOT,
                    help=f"sessions root (default: {DEFAULT_ROOT})")


SUBCOMMANDS = ("report", "clean", "compact")


def _normalize_root_argv(argv):
    """Splice a leading `--root VALUE`/`--root=VALUE` to just after the subcommand.

    --root only lives on the subparsers (see add_root_flag's comment), so
    `<cmd> --root X` works natively. `--root X <cmd>` used to work before
    --root moved off the main parser; rewriting it into the subparser-native
    order here keeps both documented forms working without re-adding --root
    to the main parser (which reintroduces the clobbering bug).
    """
    out = []
    pending = []
    inserted = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if not inserted and tok == "--root" and i + 1 < len(argv):
            pending += [tok, argv[i + 1]]
            i += 2
            continue
        if not inserted and tok.startswith("--root="):
            pending.append(tok)
            i += 1
            continue
        out.append(tok)
        if not inserted and tok in SUBCOMMANDS:
            out += pending
            pending = []
            inserted = True
        i += 1
    out += pending  # no subcommand found; let argparse report the usage error
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    add_root_flag(sub.add_parser("report", help="disk usage by date"))

    add_selection_flags(sub.add_parser(
        "clean", help="blank heavy string values in-place"), "clean")
    add_selection_flags(sub.add_parser(
        "compact", help="drop telemetry + blank exec_command, keep everything else"), "compact")

    args = ap.parse_args(_normalize_root_argv(sys.argv[1:]))
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
