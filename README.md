<div align="center">
  <img src="assets/logo.svg" alt="codex-cleanup — bloated JSONL compacted down to one clean line" width="120"/>
</div>

# codex-cleanup

Codex CLI session rollouts (`~/.codex/sessions/YYYY/MM/DD/*.jsonl`) grow into
the gigabytes because every context compaction re-writes the *entire*
conversation history into the file, on top of raw tool outputs and reasoning
blobs, and `thread_goals` keys `goal_id` off `thread_id` as a 1:1 primary
key — there's no supported way to continue a goal in a fresh thread, only
start a new one and lose continuity. One `/goal` thread left running for 24
days burned 559M tokens on its own before hitting `usage_limited`. Not a
one-off: cross-checking `goals_1.sqlite` found 6 of 12 tracked goal threads
had independently hit `usage_limited`, totaling 929M of ~1.05B cumulative
tokens — the default outcome for any `/goal` run left unattended more than
a few days. Upstream bug report:
[openai/codex#24948](https://github.com/openai/codex/issues/24948).

This repo does two things:

1. **Cleans up rollouts already on disk** — two modes, from "purge sessions
   I don't need anymore" to "shrink a session I still want to read, without
   losing anything real."
2. **Fixes the actual token-cost problem at the source** — Codex-native hooks
   that stop bloat from ever being resent to the model in the first place,
   plus a watchdog that flags a runaway `/goal` thread in hours, not weeks.

## Cleanup modes — `codex_session_cleaner.py`

| | `report` | `compact` | `clean` |
|---|---|---|---|
| **What it does** | Shows disk usage by date, changes nothing | Drops pure telemetry lines (`token_count`, and `agent_message` — verified duplicate of the real record), blanks only `exec_command` args/output | Blanks *all* heavy string values |
| **Keeps** | — | Reasoning, messages, patches, plans, goal state, sub-agent markers — everything you'd want to read again | Structural keys only (`id`, `timestamp`, `role`, …) |
| **Use for** | Finding where the bloat lives | Sessions you still want to review, just smaller | Old sessions you're writing off entirely |
| **Real result** | — | 3.9GB → 1.2GB (68.6%), verified byte-for-byte against untouched backups, zero content loss | 13GB → 1.4GB (~90%), irreversible |

```bash
python3 codex_session_cleaner.py report
python3 codex_session_cleaner.py compact --keep-days 1 --dry-run
python3 codex_session_cleaner.py compact --keep-days 1 --min-size 1
python3 codex_session_cleaner.py clean --keep-days 7 --dry-run
```

Or the interactive one-shot (`clean`, asks first):

```bash
./cleanup.sh
KEEP_DAYS=14 MIN_SIZE=5 ./cleanup.sh
```

### Options (both `compact` and `clean`)

| Flag                  | Meaning                                                |
|-----------------------|--------------------------------------------------------|
| `--root PATH`         | Sessions root (default `~/.codex/sessions`)            |
| `--keep-days N`       | Leave the last N days untouched                        |
| `--before YYYY-MM-DD` | Only touch sessions dated strictly before this         |
| `--min-size MB`       | Only touch files at least this big (default: all)      |
| `--dry-run`           | List what would happen, modify nothing                 |

### Safety notes

- Writes go to a temp sibling file, then an atomic `os.replace` — a crash
  mid-run can't corrupt a session file.
- Restores the original atime/mtime after every rewrite — a compacted file
  still looks like it was created when it actually was, not "just now."
- Bails (leaves the file untouched) if it detects Codex modified the file
  mid-run, rather than risk discarding a concurrent write.
- Unparseable lines become `{}` instead of aborting, preserving line counts.
- No dependencies — stdlib only, runs on macOS system `python3` (3.8+).

## Real-time hooks — `hooks/`

Codex has its own hooks system (`hooks = true` in `config.toml`, config at
`~/.codex/hooks.json`, template in `deployed/hooks.json`) — separate
from MCP, separate from plugins. These intervene *while a session is live*,
instead of cleaning up after the fact. All three are wrapped to fail silent
and fast on any error — a bug here must never block or slow down a real
Codex session. Every script and hook here derives its paths from its own
checkout location (`CODEX_CLEANUP_HOME` overrides, for the deployed/launchd
case) — the repo can live anywhere.

To install: swap `__CODEX_CLEANUP_HOME__` in `deployed/hooks.json` for this
checkout's absolute path and copy the result to `~/.codex/hooks.json`
(merge by hand if you already have hooks configured there):

```bash
sed "s|__CODEX_CLEANUP_HOME__|$PWD|g" deployed/hooks.json > ~/.codex/hooks.json
```

| Hook | Script | Does |
|---|---|---|
| `PostToolUse` | `posttooluse_exec_compact.py` | **The actual token-cost fix.** Fires after every tool call. If `exec_command` output is large, archives the full raw text to `tool-output-archive/<session_id>/<call_id>.txt` and substitutes a head-tail-truncated version into the *recorded* transcript — so the bloat never gets resent on a future turn in the first place. |
| `PreCompact` | `precompact_warn.py` | Fires the instant Codex decides a thread needs compacting. Cross-checks `~/.codex/goals_1.sqlite`; if the thread's already over 20M tokens or 3 days old, surfaces a warning right inside the live session plus a macOS notification — the notification throttled to once per thread per 12h (a runaway thread compacts often), the in-session warning every time. |
| `PostCompact` | `postcompact_compact.py` | Hooks are blocking, so Codex is genuinely idle on the transcript during this hook — a real, race-free window. Runs the same targeted compaction immediately, instead of waiting for the next scheduled pass. |

**Tuning note:** Codex has its own native exec-output truncation ceiling
(~1,000–2,000 raw chars, confirmed empirically — 1,000 chars passed through
untouched, 2,000 got truncated to ~300 by Codex itself before the hook ever
saw it). `posttooluse_exec_compact.py`'s `KEEP_HEAD`/`KEEP_TAIL` are set
below that ceiling on purpose, to catch what Codex lets through untouched
rather than duplicate work it already does. Found and calibrated via live
testing with `CODEX_HOOK_DEBUG=1`; also caught a bug where text shorter
than `KEEP_HEAD + KEEP_TAIL` produced a negative "chars truncated" count,
fixed by deriving the truncation threshold from `KEEP_HEAD + KEEP_TAIL +
MIN_SAVINGS` so the math can't go negative by construction.

Debug a hook without touching a real session:

```bash
echo '{"session_id":"test","tool_name":"exec_command","tool_use_id":"c1","tool_response":{"output":"..."}}' \
  | python3 hooks/posttooluse_exec_compact.py

# or trace what a hook actually receives from a live session:
CODEX_HOOK_DEBUG=1 codex exec "..." 2>&1
cat hooks/debug.log
```

**Unverified assumption:** the PostToolUse hook emits `decision: "block"` and
*assumes* Codex substitutes the truncated reason into the recorded transcript
— that semantics comes from Claude Code's hook contract, and Codex was never
verified end-to-end to honor it. `verify_block_behavior.py` settles it: run a
session with a deliberately huge exec output, then check the rollout for the
hook's marker. Reports PASS / FAIL / INCONCLUSIVE.

```bash
python3 verify_block_behavior.py --latest   # newest rollout under ~/.codex/sessions
```

## Watchdogs — `goal_watch.py`, `vacuum_logs.sh`, `archive_prune.py`

Run via `launchd`, not `cron` — cron has no catch-up on macOS, so a job
scheduled for a fixed time just silently doesn't run if the laptop's asleep
then. `launchd` agents trigger on load (login) plus a periodic interval, so
they survive sleep/wake. Plists live in `~/Library/LaunchAgents/`, templates
kept in `deployed/` here.

Install all four with `./install_launchd.sh` — it substitutes
`__CODEX_CLEANUP_HOME__` for this checkout's actual path, copies each plist
into `~/Library/LaunchAgents/`, and loads it. Re-running is safe (unloads
before reloading).

| Agent | Does | Cadence |
|---|---|---|
| `com.sriinnu.codex-compact` | `codex_session_cleaner.py compact --keep-days 1` | RunAtLoad + every 6h |
| `com.sriinnu.codex-vacuum` | `VACUUM`s `~/.codex/logs_2.sqlite` (Codex deletes old log rows but never reclaims the freed pages — a plain `VACUUM` alone took this from 2.5GB to 679MB with zero rows touched) | RunAtLoad + weekly |
| `com.sriinnu.codex-goal-watch` | `goal_watch.py` — flags any `/goal` thread over 20M tokens or 3 days old via macOS notification, throttled to one nag per thread per 12h | RunAtLoad + every 2h |
| `com.sriinnu.codex-archive-prune` | `archive_prune.py` — retention for `tool-output-archive/`, which the PostToolUse hook otherwise grows forever: deletes archived outputs older than 14 days, then oldest-first past a 500MB total cap (catches a single runaway day) | RunAtLoad + daily |

```bash
python3 goal_watch.py
python3 goal_watch.py --token-threshold 20000000 --age-days 3 --quiet

python3 archive_prune.py --dry-run
python3 archive_prune.py --keep-days 7 --max-total-mb 200

bash vacuum_logs.sh   # backs up first, skips if Codex looks like it's running
```

## Directory layout

```
codex_session_cleaner.py   report / compact / clean
cleanup.sh                  interactive one-shot wrapper around clean
goal_watch.py               goals_1.sqlite watchdog
archive_prune.py            tool-output-archive retention (age pass + size cap)
verify_block_behavior.py    checks the PostToolUse block-substitution assumption
vacuum_logs.sh               logs_2.sqlite VACUUM
hooks/                      Codex-native PostToolUse/PreCompact/PostCompact
tests/                      run with: python -m unittest discover -s tests
deployed/                   templated hooks.json + launchd plists (install_launchd.sh)
assets/                     the logo up top
backups/                    gitignored — local safety net, too large for the repo
tool-output-archive/        gitignored — full raw exec output PostToolUse archives
KNOWN_ISSUES.md             plain engineering notes on what's still open
```

## What's still open

See [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
