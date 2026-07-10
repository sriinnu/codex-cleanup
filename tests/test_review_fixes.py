"""
Regression tests for the review findings fixed in this pass. One class per
bug, same convention as test_regressions.py:

1. codex_session_cleaner.compact_line — an exec_command function_call_output
   whose own content happens to contain "token_count"/"agent_message" no
   longer skips the exec-blanking branch entirely.
2. codex_session_cleaner.compact_line — an output already carrying the
   posttooluse_exec_compact.py archive-pointer placeholder is no longer
   re-blanked (that placeholder is the only recorded path back to the
   archived original).
3. codex_session_cleaner.py CLI — `--root X <cmd>` works again, not just
   `<cmd> --root X`.
4. goal_watch.py — a NULL tokens_used row no longer crashes the whole run
   before any thread is flagged.
5. hooks/posttooluse_exec_compact.py — a session_id/tool_use_id containing
   path-traversal segments can't write outside tool-output-archive/.
6. archive_prune.py — a still-active session's early archived outputs are
   spared by the age pass even if individually older than --keep-days; a
   fully dormant session still gets pruned once ALL its files age out; a
   single clock-skewed future-mtime file no longer shields its siblings
   forever; the size cap stays activity-blind by design.
7. hooks/precompact_warn.py — the same NULL tokens_used guard applied to
   goal_watch.py is applied here too (it has an identical comparison and
   was missed in the first pass); a missing/broken notify_common.py
   degrades the popup silently instead of crashing this "always exits 0"
   hook.
8. hooks/posttooluse_exec_compact.py — sanitize_id() coerces non-string
   ids instead of raising; a pre-existing symlink in the archive tree can
   no longer redirect an archive write outside tool-output-archive/.
9. notify_common.py — direct tests of the shared notify()/load/save
   helpers, independent of their two callers.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archive_prune  # noqa: E402
import codex_session_cleaner as csc  # noqa: E402
import notify_common  # noqa: E402
from test_hooks import load_hook  # noqa: E402

POSTTOOL = os.path.join(ROOT, "hooks", "posttooluse_exec_compact.py")
GOAL_WATCH = os.path.join(ROOT, "goal_watch.py")
CLEANER = os.path.join(ROOT, "codex_session_cleaner.py")


def resp(payload) -> str:
    return json.dumps({"type": "response_item", "payload": payload},
                      separators=(",", ":")) + "\n"


class TestCompactLinePreFilterDoesNotShadowExecBranch(unittest.TestCase):
    """Bug 1: the telemetry-substring pre-filter used to return before ever
    checking whether the line was actually a response_item, so an exec
    output whose own content happened to contain 'token_count' skipped
    blanking entirely."""

    def test_exec_output_containing_token_count_still_blanked(self):
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": {"content": "wall of output",
                                "token_count": 1234}})
        out, changed = csc.compact_line(line, names)
        self.assertTrue(changed)
        self.assertEqual(json.loads(out)["payload"]["output"]["content"], "")

    def test_exec_output_containing_agent_message_key_still_blanked(self):
        # The trigger substring must be '"agent_message"' verbatim (quotes
        # adjacent) once JSON-serialized -- a key named that in the output
        # dict reproduces this, same as the token_count case above. A bare
        # word "agent_message" embedded mid-string would never have entered
        # the old shadowing branch in the first place, so it wouldn't have
        # pinned the bug.
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": {"content": "wall of output",
                                "agent_message": False}})
        out, changed = csc.compact_line(line, names)
        self.assertTrue(changed)
        self.assertEqual(json.loads(out)["payload"]["output"]["content"], "")


class TestCompactLineSparesArchivedPlaceholder(unittest.TestCase):
    """Bug 2: compact_line used to blank function_call_output.output
    unconditionally for exec_command calls, even when it was already the
    short placeholder pointing at the archived original — erasing the
    transcript's only recorded path back to it."""

    PLACEHOLDER = ("AAA\n...[500 chars truncated — full output archived "
                   "at /x/tool-output-archive/s/c1.txt]...\nZZZ")

    def test_str_form_placeholder_left_alone(self):
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": self.PLACEHOLDER})
        out, changed = csc.compact_line(line, names)
        self.assertFalse(changed)
        self.assertIs(out, line)

    def test_dict_form_placeholder_left_alone(self):
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": {"content": self.PLACEHOLDER, "success": True}})
        out, changed = csc.compact_line(line, names)
        self.assertFalse(changed)
        self.assertIs(out, line)

    def test_non_placeholder_output_still_blanked(self):
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": "wall of raw exec output"})
        out, changed = csc.compact_line(line, names)
        self.assertTrue(changed)
        self.assertEqual(json.loads(out)["payload"]["output"], "")


class TestRootFlagBeforeSubcommand(unittest.TestCase):
    """Bug 3: --root moved to the subparsers broke the documented
    `--root X <cmd>` order, which worked before that refactor."""

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, CLEANER] + list(argv),
                              capture_output=True, text=True, timeout=60)

    def test_root_before_subcommand_report(self):
        d = tempfile.mkdtemp(prefix="codex-cli-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        day = os.path.join(d, "2026", "01", "01")
        os.makedirs(day)
        with open(os.path.join(day, "s.jsonl"), "w") as f:
            f.write('{"type": "message"}\n')

        r = self.run_cli("--root", d, "report")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TOTAL", r.stdout)

    def test_root_after_subcommand_still_works(self):
        d = tempfile.mkdtemp(prefix="codex-cli-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        r = self.run_cli("report", "--root", d)
        self.assertEqual(r.returncode, 1)  # no rollouts under an empty root
        self.assertIn("No .jsonl rollouts", r.stdout)

    def test_normalize_root_argv_unit(self):
        self.assertEqual(
            csc._normalize_root_argv(["--root", "/x", "clean", "--dry-run"]),
            ["clean", "--root", "/x", "--dry-run"])
        self.assertEqual(
            csc._normalize_root_argv(["clean", "--root", "/x", "--dry-run"]),
            ["clean", "--root", "/x", "--dry-run"])
        self.assertEqual(
            csc._normalize_root_argv(["--root=/x", "report"]),
            ["report", "--root=/x"])

    def test_normalize_root_argv_no_value_passed_through(self):
        # Dangling --root with nothing after it: let argparse report its
        # own "expected one argument" error rather than swallowing the flag.
        self.assertEqual(csc._normalize_root_argv(["--root"]), ["--root"])
        self.assertEqual(csc._normalize_root_argv(["clean", "--root"]),
                         ["clean", "--root"])

    def test_normalize_root_argv_value_matching_subcommand_name(self):
        # --root's VALUE happens to equal a subcommand name ("report") --
        # it must stay paired with --root, not get mistaken for the
        # subcommand token itself.
        self.assertEqual(
            csc._normalize_root_argv(["--root", "report", "clean"]),
            ["clean", "--root", "report"])

    def test_normalize_root_argv_repeated_flag_preserves_both(self):
        # Both occurrences survive the splice in subparser-native order;
        # argparse's normal last-explicit-wins rule decides the final value.
        self.assertEqual(
            csc._normalize_root_argv(["--root", "/a", "clean", "--root", "/b"]),
            ["clean", "--root", "/a", "--root", "/b"])


class TestGoalWatchNullTokensUsed(unittest.TestCase):
    """Bug 4: tokens_used >= threshold with no None guard crashed the whole
    run (uncaught) before any thread was flagged, including other genuinely
    over-threshold threads in the same batch."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="codex-goalwatch-null-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_null_tokens_used_row_does_not_crash_other_threads(self):
        db = os.path.join(self.home, "goals.sqlite")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE thread_goals (thread_id TEXT, objective TEXT, "
            "status TEXT, tokens_used INTEGER, created_at_ms INTEGER)")
        now_ms = int(time.time() * 1000)
        conn.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?)",
                     ("null-tokens-thread", "still warming up", "active",
                      None, now_ms))
        conn.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?)",
                     ("over-threshold-thread", "runaway", "active",
                      25_000_000, now_ms))
        conn.commit()
        conn.close()

        env = dict(os.environ, CODEX_CLEANUP_HOME=self.home)
        r = subprocess.run(
            [sys.executable, GOAL_WATCH, "--db", db, "--quiet"],
            capture_output=True, text=True, env=env, timeout=60)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("over-threshold-thread"[:8], r.stdout)

    def test_null_tokens_used_flows_through_print_formatting(self):
        # The other test only proves the crash doesn't happen; with the
        # default 20M threshold a NULL->0 row never crosses over_tokens and
        # is filtered out before the print/notify line ever runs. Force it
        # through with --token-threshold 0 so the {tokens_used:>12,} format
        # call itself is actually exercised against the coerced value.
        db = os.path.join(self.home, "goals.sqlite")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE thread_goals (thread_id TEXT, objective TEXT, "
            "status TEXT, tokens_used INTEGER, created_at_ms INTEGER)")
        conn.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?)",
                     ("null-tokens-thread", "still warming up", "active",
                      None, int(time.time() * 1000)))
        conn.commit()
        conn.close()

        env = dict(os.environ, CODEX_CLEANUP_HOME=self.home)
        r = subprocess.run(
            [sys.executable, GOAL_WATCH, "--db", db, "--quiet",
             "--token-threshold", "0"],
            capture_output=True, text=True, env=env, timeout=60)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 tok", r.stdout)


class TestPostToolArchivePathTraversal(unittest.TestCase):
    """Bug 5: session_id/tool_use_id from stdin were joined into the archive
    path with no sanitization, permitting a path-traversal write."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="codex-traversal-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_traversal_session_id_confined_to_archive_root(self):
        mod = load_hook(POSTTOOL, "posttool_traversal", self.home)
        text = "x" * (mod.TRUNCATE_THRESHOLD + 500)
        payload = json.dumps({
            "tool_name": "exec_command",
            "session_id": "../../../../etc/evil",
            "tool_use_id": "../../also-evil",
            "tool_response": {"output": text},
        })
        r = subprocess.run(
            [sys.executable, POSTTOOL], input=payload,
            capture_output=True, text=True,
            env=dict(os.environ, CODEX_CLEANUP_HOME=self.home), timeout=60)
        self.assertEqual(r.returncode, 0)

        archive_root = os.path.join(self.home, "tool-output-archive")
        for dirpath, _, filenames in os.walk(archive_root):
            for fn in filenames:
                full = os.path.realpath(os.path.join(dirpath, fn))
                self.assertTrue(
                    full.startswith(os.path.realpath(archive_root) + os.sep),
                    f"archive file escaped archive root: {full}")
        # Nothing written outside the checkout via the traversal attempt.
        self.assertFalse(os.path.exists(os.path.join(self.home, "..", "evil")))

    def test_sanitize_id_examples(self):
        mod = load_hook(POSTTOOL, "posttool_sanitize_unit", self.home)
        self.assertEqual(mod.sanitize_id("../../etc/passwd"), "etc_passwd")
        self.assertEqual(mod.sanitize_id("normal-session_id.1"),
                         "normal-session_id.1")
        self.assertEqual(mod.sanitize_id("..."), "unknown")

    def test_sanitize_id_coerces_non_string_instead_of_raising(self):
        # payload.get(...) makes no type guarantee -- a non-string id must
        # degrade to some safe id, not TypeError and silently skip
        # archiving+truncation for that call (the exact bug this hook
        # exists to prevent, reintroduced via a crash instead of a bypass).
        mod = load_hook(POSTTOOL, "posttool_sanitize_nonstring", self.home)
        self.assertEqual(mod.sanitize_id(12345), "12345")
        self.assertEqual(mod.sanitize_id(True), "True")
        self.assertNotEqual(mod.sanitize_id({"a": 1}), "")

    def test_sanitize_id_caps_length(self):
        mod = load_hook(POSTTOOL, "posttool_sanitize_length", self.home)
        self.assertLessEqual(len(mod.sanitize_id("x" * 5000)), 200)

    def test_symlinked_session_dir_refused_not_followed(self):
        # A symlink already sitting where a session dir would go (same-user
        # local write access) must not redirect the archive write outside
        # tool-output-archive/ -- sanitize_id() only guards the id text,
        # not a symlink component already present in the archive tree.
        mod = load_hook(POSTTOOL, "posttool_symlink_dir", self.home)
        archive_root = os.path.join(self.home, "tool-output-archive")
        os.makedirs(archive_root)
        outside = os.path.join(self.home, "outside_target")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(archive_root, "s1"))

        text = "x" * (mod.TRUNCATE_THRESHOLD + 500)
        payload = json.dumps({"tool_name": "exec_command", "session_id": "s1",
                              "tool_use_id": "t1",
                              "tool_response": {"output": text}})
        r = subprocess.run([sys.executable, POSTTOOL], input=payload,
                           capture_output=True, text=True,
                           env=dict(os.environ, CODEX_CLEANUP_HOME=self.home),
                           timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(outside, "t1.txt")),
                         "archive write followed the symlink outside the archive root")


class TestArchivePruneSparesActiveSession(unittest.TestCase):
    """Bug 6: the age pass deleted purely by individual file mtime, so a
    still-running long thread's early archived calls aged out from under a
    transcript that still points at them."""

    def tmpdir(self) -> str:
        d = tempfile.mkdtemp(prefix="codex-prune-active-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def add(self, root, sess, name, size, age_days):
        d = os.path.join(root, sess)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
        return p

    def run_prune(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["archive_prune.py"] + list(argv)), \
                contextlib.redirect_stdout(out):
            rc = archive_prune.main()
        return rc, out.getvalue()

    def test_active_session_old_file_spared(self):
        root = self.tmpdir()
        ancient = self.add(root, "long-thread", "call1.txt", 100, age_days=20)
        fresh = self.add(root, "long-thread", "call2.txt", 100, age_days=1)
        rc, out = self.run_prune("--root", root, "--keep-days", "14")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(ancient))
        self.assertTrue(os.path.exists(fresh))
        self.assertIn("Deleted 0", out)

    def test_dormant_session_still_pruned_once_all_files_age_out(self):
        root = self.tmpdir()
        old1 = self.add(root, "done-thread", "call1.txt", 100, age_days=20)
        old2 = self.add(root, "done-thread", "call2.txt", 100, age_days=15)
        rc, out = self.run_prune("--root", root, "--keep-days", "14")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(old1))
        self.assertFalse(os.path.exists(old2))
        self.assertIn("Deleted 2", out)

    def test_future_mtime_sibling_does_not_permanently_shield_directory(self):
        # A single clock-skewed file must not stand in for real activity
        # and shield every other file in its directory forever.
        root = self.tmpdir()
        ancient = self.add(root, "skewed-thread", "old.txt", 100, age_days=30)
        future = self.add(root, "skewed-thread", "clock-skew.txt", 100, age_days=-10)
        rc, out = self.run_prune("--root", root, "--keep-days", "14")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(ancient))  # not shielded by the skewed sibling
        self.assertTrue(os.path.exists(future))    # future mtime itself never ages out

    def test_size_cap_can_still_delete_from_a_spared_active_session(self):
        # The size cap is documented as activity-blind on purpose: it must
        # still delete a file the age pass just spared for belonging to a
        # still-active session, once the archive exceeds --max-total-mb.
        root = self.tmpdir()
        old_but_spared = self.add(root, "s", "old.txt", 600 * 1024, age_days=20)
        recent = self.add(root, "s", "recent.txt", 600 * 1024, age_days=1)
        rc, _ = self.run_prune("--root", root, "--keep-days", "14",
                               "--max-total-mb", "1")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(old_but_spared))  # size cap still gets it
        self.assertTrue(os.path.exists(recent))


class TestPickTargetsBoundary(unittest.TestCase):
    """Exact-cutoff boundary: mtime == cutoff must be kept, not pruned
    (matches the existing strict `<` semantics for the age rule)."""

    def test_mtime_exactly_at_cutoff_is_kept(self):
        now = 2_000_000_000.0
        keep_days = 14
        cutoff = now - keep_days * 86400
        entries = [(cutoff, 100, "/r/s/a.txt", "/r/s")]
        targets, kept = archive_prune.pick_targets(entries, keep_days, 10**12, now)
        self.assertEqual(targets, [])
        self.assertEqual(kept, [(cutoff, 100, "/r/s/a.txt")])

    def test_mtime_one_second_past_cutoff_is_pruned(self):
        now = 2_000_000_000.0
        keep_days = 14
        cutoff = now - keep_days * 86400
        entries = [(cutoff - 1, 100, "/r/s/a.txt", "/r/s")]
        targets, kept = archive_prune.pick_targets(entries, keep_days, 10**12, now)
        self.assertEqual(len(targets), 1)
        self.assertEqual(kept, [])


class TestNotifyCommon(unittest.TestCase):
    """Direct tests of the shared module -- previously only exercised
    indirectly through goal_watch.py/precompact_warn.py subprocess tests."""

    def tmpdir(self) -> str:
        d = tempfile.mkdtemp(prefix="codex-notifycommon-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def test_load_missing_file_returns_empty(self):
        d = self.tmpdir()
        self.assertEqual(
            notify_common.load_notify_state(os.path.join(d, "nope.json")), {})

    def test_load_corrupt_file_returns_empty(self):
        d = self.tmpdir()
        p = os.path.join(d, "state.json")
        with open(p, "wb") as f:
            f.write(b"\x00 not json at all")
        self.assertEqual(notify_common.load_notify_state(p), {})

    def test_load_non_dict_returns_empty(self):
        d = self.tmpdir()
        p = os.path.join(d, "state.json")
        with open(p, "w") as f:
            json.dump(["a", "list"], f)
        self.assertEqual(notify_common.load_notify_state(p), {})

    def test_save_then_load_roundtrip_creates_parent_dir(self):
        d = self.tmpdir()
        p = os.path.join(d, "nested", "state.json")
        notify_common.save_notify_state(p, {"t1": 123.0})
        self.assertEqual(notify_common.load_notify_state(p), {"t1": 123.0})

    def test_notify_strips_non_ascii_and_never_raises(self):
        with mock.patch.object(notify_common.subprocess, "run") as run:
            notify_common.notify("Title — em dash", "curly ’quotes’")
        script = run.call_args[0][0][2]  # ["osascript", "-e", script]
        self.assertNotIn("—", script)
        self.assertNotIn("’", script)

    def test_notify_swallows_subprocess_errors(self):
        with mock.patch.object(notify_common.subprocess, "run",
                               side_effect=OSError("boom")):
            notify_common.notify("t", "m")  # must not raise


class TestPreCompactWarnFixes(unittest.TestCase):
    """Bug 7: precompact_warn.py had the identical unguarded NULL
    tokens_used comparison as goal_watch.py, missed in the first pass; and
    its new notify_common import must degrade gracefully, not crash, since
    this hook's whole contract is 'always exits 0'."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="codex-precompact-fix-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_null_tokens_used_over_age_threshold_still_warns(self):
        mod = load_hook(os.path.join(ROOT, "hooks", "precompact_warn.py"),
                        "precompact_null_tokens", self.home)
        db = os.path.join(self.home, "goals.sqlite")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE thread_goals (thread_id TEXT, objective TEXT, "
            "status TEXT, tokens_used INTEGER, created_at_ms INTEGER)")
        old_ms = int((time.time() - 5 * 86400) * 1000)  # 5d > 3d age threshold
        conn.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?)",
                     ("null-tok-thread", "still warming up", "active",
                      None, old_ms))
        conn.commit()
        conn.close()
        mod.DB = db

        out = io.StringIO()
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps({"session_id": "null-tok-thread"}))), \
                mock.patch.object(mod, "notify"), \
                contextlib.redirect_stdout(out):
            rc = mod.main()

        self.assertEqual(rc, 0)
        emitted = json.loads(out.getvalue())
        self.assertIn("systemMessage", emitted)
        self.assertIn("0 tokens", emitted["systemMessage"])  # NULL coerced to 0

    def test_missing_notify_common_degrades_instead_of_crashing(self):
        # Copy only hooks/precompact_warn.py to a scratch dir with no
        # notify_common.py alongside it -- reproduces a partial deployment.
        scratch = tempfile.mkdtemp(prefix="codex-precompact-noimport-test-")
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        hooks_dir = os.path.join(scratch, "hooks")
        os.makedirs(hooks_dir)
        shutil.copy(os.path.join(ROOT, "hooks", "precompact_warn.py"), hooks_dir)

        r = subprocess.run(
            [sys.executable, os.path.join(hooks_dir, "precompact_warn.py")],
            input=json.dumps({"trigger": "auto"}),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


class TestHumanDedup(unittest.TestCase):
    """Bug 9 (cleanup): archive_prune.py re-implemented human() instead of
    importing it -- format drift risk between report/clean/compact output
    and archive_prune --dry-run output."""

    def test_archive_prune_reuses_codex_session_cleaner_human(self):
        self.assertIs(archive_prune.human, csc.human)


if __name__ == "__main__":
    unittest.main()
