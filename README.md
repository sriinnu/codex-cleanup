# codex-cleanup

Codex CLI session rollouts (`~/.codex/sessions/YYYY/MM/DD/*.jsonl`) grow into the
gigabytes because every context compaction re-writes the *entire* conversation
history into the file as a `compacted` snapshot, on top of raw tool outputs and
reasoning blobs. A week of heavy use hit 13 GB on this machine.

This tool reclaims that space **without deleting files**: it blanks the heavy
string *values* in place. Keys, ids, timestamps, types, and line counts all
survive, so the files stay valid JSONL and Codex can still list the sessions.
Real-world result: 13 GB → 1.4 GB (~90%).

## What gets blanked vs. kept

| Blanked (→ `""`)                              | Kept                                   |
|-----------------------------------------------|----------------------------------------|
| Tool/shell outputs (`function_call_output`)   | `type`, `id`, `call_id`, `turn_id`     |
| User + agent messages                         | `timestamp`, `role`, `name`, `status`  |
| Reasoning / `encrypted_content`               | `cwd`, `model`, `cli_version`, …       |
| `compacted` history snapshots                 | All numbers, booleans, nulls           |
| Patch contents, stdout/stderr                 | File and line counts (unchanged)       |

**Warning:** blanking is irreversible. Blanked sessions still list in Codex but
resume with no memory of their content. Use `--keep-days` to protect recent work.

## Quick start

```bash
./cleanup.sh
```

That shows usage by date, a dry-run of what would be cleaned (keeping the last
7 days), and asks for confirmation before touching anything. Tune with env vars:

```bash
KEEP_DAYS=14 MIN_SIZE=5 ./cleanup.sh
```

## Manual usage

```bash
# 1. Where's the bloat? (grouped by session date)
python3 codex_session_cleaner.py report

# 2. Preview — lists targets, changes nothing
python3 codex_session_cleaner.py clean --keep-days 7 --dry-run

# 3. Clean for real
python3 codex_session_cleaner.py clean --keep-days 7 --min-size 5
```

### Options

| Flag                  | Meaning                                                |
|-----------------------|--------------------------------------------------------|
| `--root PATH`         | Sessions root (default `~/.codex/sessions`)            |
| `--keep-days N`       | Leave the last N days untouched                        |
| `--before YYYY-MM-DD` | Only clean sessions dated strictly before this         |
| `--min-size MB`       | Only touch files at least this big (default: all)      |
| `--dry-run`           | List what would be cleaned, modify nothing             |

## Safety notes

- Writes go to a temp sibling file, then an atomic `os.replace` — a crash
  mid-run can't corrupt a session file.
- Unparseable lines become `{}` instead of aborting, preserving line counts.
- No dependencies — stdlib only, runs on macOS system `python3` (3.8+).

## Run it on a schedule (optional)

```bash
# crontab -e  — every Sunday at 10:00, no prompt:
0 10 * * 0 /usr/bin/python3 __CODEX_CLEANUP_HOME__/codex_session_cleaner.py clean --keep-days 14 --min-size 5
```
