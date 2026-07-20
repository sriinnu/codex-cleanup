#!/usr/bin/env python3
"""
archive_prune.py — retention for the PostToolUse exec-output archive.

posttooluse_exec_compact.py saves every large exec_command output in full to
tool-output-archive/<session_id>/<call_id>.txt so the truncated transcript
copy stays recoverable — but nothing ever deletes those files, which quietly
rebuilds the exact unbounded-disk-growth problem this repo exists to solve.
This prunes the archive on two axes:

  age    delete .txt files whose mtime is older than --keep-days
  size   AFTER the age pass, if the archive still exceeds --max-total-mb,
         delete oldest-first until it fits — catches a single runaway day
         that dumps gigabytes before anything is old enough to age out

Session directories left empty by pruning are removed. Runs unattended from
launchd, so a missing or empty archive is normal (fresh install) and exits 0.

Usage:
  python3 archive_prune.py
  python3 archive_prune.py --keep-days 7 --max-total-mb 200
  python3 archive_prune.py --dry-run
"""

import argparse
import os
import sys
import time

# archive_prune.py sits next to codex_session_cleaner.py at the repo root;
# running either directly puts that directory at sys.path[0].
from codex_session_cleaner import human

DEFAULT_ROOT = os.path.join(
    os.environ.get("CODEX_CLEANUP_HOME")
    or os.path.dirname(os.path.abspath(__file__)),
    "tool-output-archive",
)


def scan(root: str) -> list:
    """Collect (mtime, size, path, session_dir) for every archived .txt under root.

    Anything that isn't a plain .txt regular file — loose non-archive files,
    symlinks, unexpected nesting — is left strictly alone: this tool only
    owns what the PostToolUse hook wrote.
    """
    entries = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".txt"):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue  # vanished between listing and stat — nothing to prune
            entries.append((st.st_mtime, st.st_size, p, dirpath))
    return entries


def pick_targets(entries: list, keep_days: int, max_total_bytes: int, now: float):
    """Return ([(mtime, size, path, reason), ...], kept_entries).

    Age pass first, then the size cap over whatever the age pass kept.
    A future mtime (clock skew, touch gone wrong) can never satisfy
    mtime < cutoff, so the age rule inherently never deletes it; the size
    cap sorts oldest-first, so future-dated files go last there too.

    The age pass additionally spares a whole session directory if ANY of its
    archived outputs is still within the retention window — posttooluse_
    exec_compact.py promises the full original is "always recoverable from
    the archive", but a long-running /goal thread (the exact incident this
    repo exists to fix — see README) keeps archiving new calls for weeks,
    so its early calls would otherwise individually age out from under a
    still-open, still-referencing transcript. The size cap stays activity-
    blind on purpose: it's the safety valve for a single runaway day dumping
    gigabytes, and that's most likely to be an active session.

    A future mtime is excluded from that "session is active" signal (though
    still individually spared by the m < cutoff check above): clock skew on
    one file must not stand in for real activity and permanently shield
    every other file in that directory, forever, on every future run.
    """
    cutoff = now - keep_days * 86400
    session_latest = {}
    for m, _s, _p, d in entries:
        if m > now:
            continue  # clock skew, not real activity — doesn't protect siblings
        if m > session_latest.get(d, float("-inf")):
            session_latest[d] = m

    targets = [(m, s, p, "age") for m, s, p, d in entries
              if m < cutoff and session_latest.get(d, float("-inf")) < cutoff]
    doomed = {p for _, _, p, _ in targets}
    kept = sorted((m, s, p) for m, s, p, d in entries if p not in doomed)  # oldest first

    kept_bytes = sum(s for _, s, _ in kept)
    while kept and kept_bytes > max_total_bytes:
        m, s, p = kept.pop(0)
        targets.append((m, s, p, "cap"))
        kept_bytes -= s
    return targets, kept


def remove_empty_dirs(root: str) -> int:
    """Drop session dirs (and any nesting) emptied by the prune. Not root itself."""
    removed = 0
    for dirpath, _, _ in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        # rmdir is the emptiness check: walk's cached dirnames still list
        # children this same pass already removed, so testing those would
        # leave emptied parents behind. rmdir on a non-empty dir just raises.
        try:
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            pass  # non-empty, raced with a writer, or permissions — leave it
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help=f"archive root (default: {DEFAULT_ROOT})")
    ap.add_argument("--keep-days", type=int, default=14, metavar="N",
                    help="delete archived outputs older than N days (default: 14)")
    ap.add_argument("--max-total-mb", type=int, default=500, metavar="N",
                    help="after the age pass, prune oldest-first past this total (default: 500)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be deleted with sizes, change nothing")
    args = ap.parse_args()
    root = os.path.expanduser(args.root)

    if not os.path.isdir(root):
        print(f"No archive at {root} — nothing to prune.")
        return 0

    entries = scan(root)
    if not entries:
        # Still sweep empty dirs: a prior run that deleted the last .txt may
        # have left emptied parents, and skipping here would strand them
        # until new archive files show up.
        if not args.dry_run:
            remove_empty_dirs(root)
        print(f"Archive at {root} is empty — nothing to prune.")
        return 0

    targets, kept = pick_targets(entries, args.keep_days,
                                 args.max_total_mb * 1024 * 1024, time.time())

    if args.dry_run:
        if not targets:
            print(f"DRY RUN — nothing to delete, {len(kept)} files "
                  f"({human(sum(s for _, s, _ in kept))}) all within limits.")
            return 0
        print(f"DRY RUN — would delete {len(targets)} files "
              f"({human(sum(s for _, s, _, _ in targets))}):")
        for _, s, p, reason in sorted(targets):
            print(f"  [{reason}] {human(s)}  {p}")
        print(f"Would keep {len(kept)} files ({human(sum(s for _, s, _ in kept))}).")
        return 0

    deleted = failed = 0
    reclaimed = 0
    for _, size, p, _ in targets:
        try:
            os.unlink(p)
            deleted += 1
            reclaimed += size
        except FileNotFoundError:
            deleted += 1  # someone beat us to it — the goal is met either way
        except OSError as e:
            failed += 1
            print(f"  WARN: could not delete {p}: {e}", file=sys.stderr)

    removed_dirs = remove_empty_dirs(root)

    summary = (f"Deleted {deleted} files, reclaimed {human(reclaimed).strip()}, "
               f"kept {len(entries) - deleted} files")
    if removed_dirs:
        summary += f", removed {removed_dirs} empty session dirs"
    if failed:
        summary += f", {failed} FAILED"
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
