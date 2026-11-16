# Known issues

Plain engineering notes on what's still open — not sales copy, just the
current gaps.

- **`PostToolUse`'s `block`-substitution assumption is still unverified —
  but for a new reason.** The hook emits `decision: "block"`, assuming Codex
  substitutes the truncated reason into the recorded transcript; that
  semantics comes from Claude Code's hook contract and Codex was never
  confirmed to honor it. Until 2026-09-03 this could not even be tested:
  the hook matched `tool_name` against `{"exec_command", "shell"}`, but
  under `features.code_mode_host` the model calls **`exec`** (measured over
  24h: 5011 `exec` calls / 52.2MB of output vs 123 `exec_command` calls).
  So the hook never fired once between 2026-07-10 and 2026-09-03 —
  `tool-output-archive/` contained nothing but `.DS_Store`. Two further
  bugs hid it: `extract_text()` had no branch for `exec`'s
  list-of-content-blocks response shape, and `verify_block_behavior.py`
  scanned only `function_call*` records while code_mode writes
  `custom_tool_call*`, so the verifier reported INCONCLUSIVE regardless of
  the truth. All three are fixed. Runs against pre-fix sessions correctly
  report FAIL (oversized `exec` output recorded raw). **Still open:** run a
  session *after* the fix with a deliberately huge exec output, then
  `python3 verify_block_behavior.py --latest`, for the real verdict on
  whether Codex honors `block`.

- **Codex's native tool-output cap defaults to 10000 tokens, which is far
  too generous.** Tool output never leaves the transcript — it is re-sent
  on every subsequent request in the session. Measured re-send multiplier
  over 24h of real sessions: **8.0x**, making re-sent tool output **56% of
  the entire token bill** (13.2M tokens of output billed as 105M). One
  output at the default cap therefore costs ~80k tokens. `config.toml` now
  pins the documented-but-unset root key `tool_output_token_limit = 2000`
  (~8KB), the knee of the savings curve at ~36% of daily spend. This is the
  reliable layer: unlike the hook, it does not depend on the unverified
  `block` contract. Raise to 4000 if the model starts re-running commands
  to recover detail it lost.

- **`[features.rollout_budget]` is half-usable on codex 0.152.0.** The
  binary's `RolloutBudgetConfigToml` advertises five fields; only three
  actually load. Verified field-by-field against an isolated `CODEX_HOME`:

  | field | 0.152.0 |
  |---|---|
  | `limit_tokens` | accepted |
  | `sampling_token_weight` | accepted |
  | `prefill_token_weight` | accepted |
  | `reminder_at_remaining_tokens` | **rejected** |
  | `enabled` | **rejected** |

  A rejected key does not degrade gracefully — Codex refuses to load the
  *entire* config (`codex doctor`: "config could not be loaded", feature
  flags drop to `0 enabled · 0 overridden`), so every session loses all
  settings, not just this feature. `config.toml` therefore sets
  `limit_tokens = 6000000` alone. Re-run the isolated-`CODEX_HOME` check
  after any `codex update`.

  Related TOML footgun worth writing down: `[features.rollout_budget]` must
  sit *below* every bare key in `[features]`. Placed directly under the
  `[features]` header it silently reparents `unified_exec`, `hooks`,
  `memories`, `goals` and the rest into the sub-table — `tomllib` parses it
  happily and `codex doctor` still reports `parse ok`, so the only visible
  symptom is the feature-flag count quietly dropping.

- **2 of 6 dead `usage_limited` goal threads never archived.** `codex
  archive` failed on them with a generic CLI error. Low stakes — retry
  from the TUI if it matters.

- **`logs_2.sqlite`'s root cause isn't fixed upstream.** Codex deletes old
  log rows without ever vacuuming, so the weekly `VACUUM` job here is a
  recurring workaround, not a real fix. Worth revisiting if OpenAI ships
  something.

- **No native way to continue a goal in a fresh thread.** `thread_goals`
  keys `goal_id` off `thread_id` as a 1:1 primary key, so a `/goal` run
  that hits its limits can only be abandoned and restarted, never
  continued. Flagged in
  [openai/codex#24948](https://github.com/openai/codex/issues/24948),
  not something fixable from outside Codex.
