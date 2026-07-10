# Session notes — 2026-07-09

What we found, what we fixed, and what's still open. Written up so this
doesn't have to be re-derived from scratch next time.

## The problem

One Codex `/goal` thread (`019ec74f-924b-7713-b86c-9b79983cf87f`, project
`AUriva`) ran continuously for **24 days** (2026-06-14 → 2026-07-08) without
ever rotating to a fresh session:

- 613,100 lines / 202MB rollout file
- 126,748 `exec_command` calls, 14,681 `apply_patch` calls, 883 nested
  `spawn_agent` calls
- **559,871,882 tokens** burned on that one thread alone, ending in
  `usage_limited`

Cross-checking `~/.codex/goals_1.sqlite` (`thread_goals` table): **6 of 12
tracked goal threads have independently hit `usage_limited`**, totaling
**929M of ~1.05B cumulative tokens**. Not a one-off — the default outcome
for any `/goal` run left unattended more than a few days.

Root cause: every context compaction re-snapshots the *entire* history into
the rollout file instead of trimming it, and `thread_goals` keys `goal_id`
off `thread_id` as a 1:1 primary key — there's no supported way to continue
a goal in a fresh thread, only start a new one and lose continuity.

Filed as OpenAI issue [#24948](https://github.com/openai/codex/issues/24948)
(originally filed 2026-05-28, reopened, partially addressed with a
`codex delete` command in 0.141.0 — the underlying compaction/rotation
problem is still open as of this writing).

## What we fixed on disk

- **Fixed mtime/atime on 1,284 rollout files** — a prior cleanup pass had
  stamped every file with today's date; reset each one back to the
  timestamp encoded in its filename.
- **`codex_session_cleaner.py compact`** (new subcommand, alongside the
  existing `report`/`clean`) — lighter touch than `clean`: drops pure
  telemetry lines (`token_count`, and `agent_message` since it's a verified
  byte-for-byte duplicate of the `response_item` copy) and blanks only
  `exec_command` args/output. Reasoning, messages, patches, plans, goal
  state, and sub-agent markers survive untouched.
  - Ran it across `~/.codex/sessions`: **3.9GB → 1.2GB** (158 files, 68.6%
    reclaimed).
  - Verified rigorously: re-derived the expected output from untouched
    backups and diffed byte-for-byte against the live files — 158/158
    exact matches. Also structurally re-parsed every file (real JSON
    parsing, not text search) to confirm every user message, assistant
    message, reasoning block, and `apply_patch` call is preserved exactly.
    Zero content loss.
- **`~/.codex/logs_2.sqlite`: 2.5GB → 679MB via `VACUUM`.** Turned out
  1.75GB of that file (68.5%) was dead free-list space — SQLite deletes
  rows but doesn't shrink the file until vacuumed, and Codex was deleting
  old log rows on its own without ever vacuuming. No rows touched, purely
  reclaiming already-freed pages.
- **4 of 6 dead `usage_limited` goal threads archived** via `codex archive`
  (2 failed with a generic error, non-blocking, retry from the TUI if it
  matters).
- **`codex_session_cleaner.py` internals deduped** — `clean` and `compact`
  now share one target-selection + batch-run path instead of two parallel
  copies. Invisible to users: output and `--help` verified byte-identical.

Full backups of everything touched: `codex-cleanup/backups/` (gitignored,
too large for the repo — local safety net only).

## Ongoing automation

Switched from `cron` to `launchd` — cron has no catch-up on macOS, so a job
scheduled for a fixed time just silently doesn't run if the laptop's asleep
then. `launchd` agents trigger on load (login) plus a periodic interval, so
they survive sleep/wake instead of depending on hitting an exact clock time.

| Agent | Does | Cadence |
|---|---|---|
| `com.sriinnu.codex-compact` | `codex_session_cleaner.py compact --keep-days 1` | RunAtLoad + every 6h |
| `com.sriinnu.codex-vacuum` | `VACUUM`s `logs_2.sqlite` | RunAtLoad + weekly |
| `com.sriinnu.codex-goal-watch` | `goal_watch.py` — flags any goal thread over 20M tokens or 3 days old, macOS notification, throttled to one nag per thread per 12h | RunAtLoad + every 2h |
| `com.sriinnu.codex-archive-prune` | `archive_prune.py` — retention for the PostToolUse `tool-output-archive/`, which otherwise grows forever (the exact unbounded-growth problem this repo exists to solve): age pass (>14 days) then oldest-first size cap (500MB total) | RunAtLoad + daily |

Plists live in `~/Library/LaunchAgents/`, copies kept in `deployed/` here.

All scripts and hooks now derive their paths from their own checkout
location, with `CODEX_CLEANUP_HOME` as an env override for the
deployed/launchd case — nothing hardcodes the repo path anymore except the
plists themselves, which are Mac-specific by nature.

## Codex-native hooks (the real fix, not just disk cleanup)

Discovered Codex has its own hooks system (`hooks = true` in
`config.toml`, config at `~/.codex/hooks.json`) — separate from MCP,
separate from the plugin system. Three installed:

- **`PostToolUse`** (`hooks/posttooluse_exec_compact.py`) — the actual
  token-cost fix, not just disk hygiene. Fires after every tool call; for
  large `exec_command` output, archives the full raw text to
  `tool-output-archive/<session_id>/<call_id>.txt` and substitutes a
  head-tail-truncated version into the *recorded* transcript via
  `decision: "block"` — so the bloat never gets resent on future turns in
  the first place, instead of being cleaned up after the fact.
- **`PreCompact`** (`hooks/precompact_warn.py`) — fires the instant Codex
  decides a thread needs compacting (the earliest, most precise signal
  available). Cross-checks `goals_1.sqlite`; if the thread's already over
  threshold, surfaces a warning via `systemMessage` right inside the live
  session, plus a macOS notification. The notification is throttled to once
  per thread per 12h (state in `precompact_state.json`, same pattern as
  `goal_watch.py` — a runaway thread compacts often); the `systemMessage`
  fires on every compaction. Corrupt or unwritable throttle state means
  notify anyway — a lost throttle is annoying, a crashed hook unacceptable.
- **`PostCompact`** (`hooks/postcompact_compact.py`) — hooks are blocking,
  so Codex is genuinely idle on the transcript during this hook's
  execution — a real, race-free window. Runs the same `compact_file()`
  logic immediately, instead of waiting for the next scheduled pass.

All three are wrapped to fail silent and fast on any error — a bug in our
code must never block or slow down a real Codex session.

### What live testing found (important — don't skip this if touching the hooks again)

- Confirmed hooks actually fire in a real session (not just standalone) by
  adding debug instrumentation (`CODEX_HOOK_DEBUG=1` env var) and running
  `codex exec` end to end.
- **Codex has its own native exec-output truncation ceiling**, somewhere
  between 1,000 and 2,000 raw characters — confirmed empirically (1,000
  chars passed through untouched, 2,000 chars got truncated to ~300 by
  Codex itself before our hook ever saw it). Our original threshold (4KB)
  sat *above* that ceiling, making the hook a near no-op in practice.
  Recalibrated `KEEP_HEAD`/`KEEP_TAIL` to 300/300 (threshold 1000) so it
  actually catches the band Codex lets through untouched.
- Found and fixed a real bug: when the raw text was shorter than
  `KEEP_HEAD + KEEP_TAIL` combined, the head/tail slices overlapped,
  producing a **negative** "chars truncated" count and literally
  duplicating the content instead of shrinking it. Fixed by deriving
  `TRUNCATE_THRESHOLD` from `KEEP_HEAD + KEEP_TAIL + MIN_SAVINGS` so the
  math can't go negative by construction.
- Codex only ever sends `exec_command` as the tool name (empirically
  confirmed) — `TOOL_NAMES` trimmed to `{"exec_command", "shell"}`, with
  `shell` kept as cheap insurance against an upstream rename.
- Also discovered `gpt-5.3-codex-spark` is a real, working model
  (confirmed live against the backend) that draws from a *separate quota
  pool* than the main model — useful when the primary model is
  `usage_limited` (which it was, until 2026-07-12, while all this testing
  happened).

## What's still open

- The OpenAI issue comment (drafted, includes the hard numbers above) —
  posted by Sriinnu, not auto-sent.
- The PostToolUse `decision: "block"` transcript-substitution assumption is
  still unverified end-to-end — the "block" semantics come from Claude
  Code's hook contract, and Codex may record the raw output regardless.
  `verify_block_behavior.py` exists for exactly this; pending a live Mac
  test: run a session with a deliberately huge exec output, then
  `python3 verify_block_behavior.py --latest` for a PASS/FAIL/INCONCLUSIVE
  verdict.
- 2 of the 6 dead goal threads never archived (generic CLI error, low
  stakes).
- `logs_2.sqlite`'s root cause (Codex deletes without vacuuming) isn't
  actually fixed upstream — the weekly `VACUUM` job is a recurring
  workaround, not a real fix. Worth revisiting if OpenAI ships something.
- No native way to continue a goal in a fresh thread (the `thread_id`
  primary key issue) — flagged in the issue comment, not something we can
  fix from outside.
