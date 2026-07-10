"""
Regression tests for five specific fixed bugs. One class per bug:

1. posttooluse_exec_compact.py — archive written with encoding="utf-8", so a
   non-cp1252 char in a big tool_response no longer kills the whole block
   decision on a non-UTF-8 Windows locale.
2. precompact_warn.py — empty/NULL objective no longer IndexErrors inside the
   fail-silent envelope; the systemMessage still goes out.
3. goal_watch.py — same empty-objective fix, plus state is saved only AFTER
   the notify loop (a crash mid-loop must not throttle the thread for 12h).
4. codex_session_cleaner.py report — zero-byte .jsonl files are counted.
5. archive_prune.py remove_empty_dirs — nested empty dirs go in ONE run, and
   an archive with zero .txt files still gets its empty-dir sweep.
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

import codex_session_cleaner  # noqa: E402
from test_hooks import load_hook  # noqa: E402  (CODEX_CLEANUP_HOME import pattern)

POSTTOOL = os.path.join(ROOT, "hooks", "posttooluse_exec_compact.py")
PRECOMPACT = os.path.join(ROOT, "hooks", "precompact_warn.py")
GOAL_WATCH = os.path.join(ROOT, "goal_watch.py")


def make_goals_db(path, thread_id, objective, status, tokens_used, created_at_ms):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE thread_goals (thread_id TEXT, objective TEXT, "
        "status TEXT, tokens_used INTEGER, created_at_ms INTEGER)")
    conn.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?)",
                 (thread_id, objective, status, tokens_used, created_at_ms))
    conn.commit()
    conn.close()


class TempHomeBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="codex-regress-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)


class TestPostToolArchiveEncoding(TempHomeBase):
    """Bug 1: archive open() without encoding= used the locale codec (cp1252
    on Windows), so any exotic char made the write raise inside the
    fail-silent envelope — no block decision, bloat kept, 0-byte archive."""

    def test_non_cp1252_char_survives_without_utf8_env(self):
        mod = load_hook(POSTTOOL, "posttool_regress", self.home)
        # '✓' has no cp1252 encoding. Keep it in the MIDDLE so the head/tail
        # echoed into the stdout decision stays ASCII whatever the child's
        # pipe codec is — the archive write still sees the full text.
        text = ("A" * mod.KEEP_HEAD
                + "✓☃" + "M" * (mod.TRUNCATE_THRESHOLD + 500)
                + "Z" * mod.KEEP_TAIL)
        payload = json.dumps({  # ensure_ascii=True: stdin stays pure ASCII
            "tool_name": "exec_command", "session_id": "sess-enc",
            "tool_use_id": "call-enc", "tool_response": {"output": text}})

        # The repro condition: no PYTHONUTF8/PYTHONIOENCODING, so the child's
        # open() default falls back to the platform locale encoding.
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["CODEX_CLEANUP_HOME"] = self.home
        r = subprocess.run([sys.executable, POSTTOOL],
                           input=payload.encode("ascii"),
                           capture_output=True, env=env, timeout=60)

        self.assertEqual(r.returncode, 0)
        stdout = r.stdout.decode("utf-8", errors="replace")
        self.assertIn('"decision": "block"', stdout)
        archive = os.path.join(self.home, "tool-output-archive",
                               "sess-enc", "call-enc.txt")
        self.assertTrue(os.path.exists(archive))
        with open(archive, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)  # FULL original incl. ✓☃


class TestPreCompactEmptyObjective(TempHomeBase):
    """Bug 2: short_obj = objective.strip().splitlines()[0] IndexError'd on an
    empty objective; the fail-silent envelope then swallowed the one warning
    this hook exists to deliver."""

    def test_empty_objective_still_emits_system_message(self):
        mod = load_hook(PRECOMPACT, "precompact_regress", self.home)
        db = os.path.join(self.home, "goals.sqlite")
        thread_id = "regress-empty-obj-thread"
        make_goals_db(db, thread_id, objective="", status="active",
                      tokens_used=25_000_000,
                      created_at_ms=int(time.time() * 1000))
        mod.DB = db  # real path is hardcoded to ~/.codex; point at ours

        out = io.StringIO()
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps({"session_id": thread_id}))), \
                mock.patch.object(mod, "notify"), \
                contextlib.redirect_stdout(out):
            rc = mod.main()

        self.assertEqual(rc, 0)
        emitted = json.loads(out.getvalue())
        self.assertTrue(emitted["continue"])
        self.assertIn("systemMessage", emitted)  # pre-fix: silently swallowed
        self.assertIn("25,000,000 tokens", emitted["systemMessage"])


class TestGoalWatchEmptyObjective(TempHomeBase):
    """Bug 3: empty objective crashed the notify loop AFTER state was saved,
    marking the thread 'notified' for 12h with no notification ever fired.
    Fixed expression + save_state moved after the loop."""

    def test_empty_objective_flags_and_records_state(self):
        db = os.path.join(self.home, "goals.sqlite")
        thread_id = "regress-goalwatch-empty-obj"
        make_goals_db(db, thread_id, objective="", status="active",
                      tokens_used=25_000_000,
                      created_at_ms=int(time.time() * 1000))

        env = dict(os.environ, CODEX_CLEANUP_HOME=self.home)
        r = subprocess.run(
            [sys.executable, GOAL_WATCH, "--db", db, "--quiet"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=60)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(thread_id[:8], r.stdout)  # the flag line printed
        state_file = os.path.join(self.home, "goal_watch_state.json")
        self.assertTrue(os.path.exists(state_file))
        with open(state_file) as f:
            state = json.load(f)
        # State written only after the loop completed — thread recorded AND
        # its line actually made it out.
        self.assertIn(thread_id, state)


class TestReportCountsZeroByteFiles(TempHomeBase):
    """Bug 4: zero-byte .jsonl rollouts were skipped by report's file count."""

    def test_empty_jsonl_counted(self):
        day = os.path.join(self.home, "2026", "07", "01")
        os.makedirs(day)
        open(os.path.join(day, "empty.jsonl"), "w").close()  # 0 bytes
        with open(os.path.join(day, "full.jsonl"), "w") as f:
            f.write('{"type": "message"}\n')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = codex_session_cleaner.cmd_report(Namespace(root=self.home))

        self.assertEqual(rc, 0)
        total_line = [l for l in out.getvalue().splitlines()
                      if l.startswith("TOTAL")][0]
        self.assertEqual(total_line.split()[1], "2")  # both files counted


class TestPruneNestedAndEmptyArchive(TempHomeBase):
    """Bug 5: remove_empty_dirs left the parent of a nested emptied dir behind
    (needed a second run), and an archive with zero .txt files skipped the
    empty-dir sweep entirely."""

    def run_prune(self, root):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "archive_prune.py"),
             "--root", root, "--keep-days", "14"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)

    def test_nested_empty_dirs_removed_in_one_run(self):
        nested = os.path.join(self.home, "sessA", "nested")
        os.makedirs(nested)
        p = os.path.join(nested, "old.txt")
        with open(p, "w") as f:
            f.write("x" * 100)
        t = time.time() - 30 * 86400
        os.utime(p, (t, t))

        r = self.run_prune(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(nested))                      # child gone
        self.assertFalse(os.path.exists(os.path.join(self.home, "sessA")))  # AND parent, same run
        self.assertTrue(os.path.isdir(self.home))  # root itself survives

    def test_empty_archive_still_sweeps_dir_chain(self):
        chain = os.path.join(self.home, "a", "b", "c")
        os.makedirs(chain)  # no .txt anywhere: scan() finds nothing

        r = self.run_prune(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to prune", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, "a")))
        self.assertTrue(os.path.isdir(self.home))


if __name__ == "__main__":
    unittest.main()
