# Known issues

Plain engineering notes on what's still open — not sales copy, just the
current gaps.

- **`PostToolUse`'s `block`-substitution assumption is unverified
  end-to-end.** The hook emits `decision: "block"`, assuming Codex
  substitutes the truncated reason into the recorded transcript — that
  semantics comes from Claude Code's hook contract, and Codex was never
  confirmed to honor it. `verify_block_behavior.py` settles it: run a
  session with a deliberately huge exec output, then
  `python3 verify_block_behavior.py --latest` for a PASS/FAIL/INCONCLUSIVE
  verdict.

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
