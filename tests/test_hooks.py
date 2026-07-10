"""
Subprocess-level tests for the Codex hooks.

The hooks are contractually fail-silent: whatever garbage arrives on stdin,
they must exit 0 and never traceback, because a crash here would block or
slow a live Codex session. So the first assertion in every test is the exit
code. Each run gets CODEX_CLEANUP_HOME pointed at a throwaway temp dir so
archives and throttle state never land in the real checkout (or ~/.codex).

The PreCompact goals DB path (~/.codex/goals_1.sqlite) is hardcoded in the
hook by design and not derivable from CODEX_CLEANUP_HOME, so only its
fail-silent paths are testable here; the sqlite happy path is not. A random
UUID session_id keeps the "valid payload" test inert even on a machine where
the real DB exists — no row can match, so the hook must stay silent.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTTOOL = os.path.join(ROOT, "hooks", "posttooluse_exec_compact.py")
PRECOMPACT = os.path.join(ROOT, "hooks", "precompact_warn.py")


def load_hook(path, name, home):
    """Import a hook module with CODEX_CLEANUP_HOME pinned to a temp dir.

    The hooks resolve CLEANUP_HOME/STATE_FILE at import time, so the env var
    has to be set around exec_module, not around the calls.
    """
    old = os.environ.get("CODEX_CLEANUP_HOME")
    os.environ["CODEX_CLEANUP_HOME"] = home
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("CODEX_CLEANUP_HOME", None)
        else:
            os.environ["CODEX_CLEANUP_HOME"] = old


def run_hook(script, stdin_text, home):
    # PYTHONUTF8 so the block reason's em dash survives the Windows pipe.
    env = dict(os.environ, CODEX_CLEANUP_HOME=home, PYTHONUTF8="1")
    return subprocess.run([sys.executable, script], input=stdin_text,
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60)


class HookTestBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="codex-hook-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)


class TestPostToolUse(HookTestBase):
    @classmethod
    def setUpClass(cls):
        # Constants come from the hook itself so the tests track any retune.
        with tempfile.TemporaryDirectory() as d:
            cls.hook = load_hook(POSTTOOL, "posttooluse_under_test", d)

    def payload(self, text, tool="exec_command", session="sess-t", tid="call-1"):
        return json.dumps({"tool_name": tool, "session_id": session,
                           "tool_use_id": tid,
                           "tool_response": {"output": text}})

    def test_under_threshold_silent(self):
        text = "x" * self.hook.TRUNCATE_THRESHOLD  # exactly at threshold: <=
        r = run_hook(POSTTOOL, self.payload(text), self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_over_threshold_blocks_with_head_tail_archive(self):
        h, t = self.hook.KEEP_HEAD, self.hook.KEEP_TAIL
        text = "A" * h + "M" * (self.hook.TRUNCATE_THRESHOLD - h - t + 500) + "Z" * t
        r = run_hook(POSTTOOL, self.payload(text), self.home)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        reason = out["reason"]
        omitted = len(text) - h - t
        self.assertGreater(omitted, 0)
        self.assertTrue(reason.startswith(text[:h]))
        self.assertTrue(reason.endswith(text[-t:]))
        self.assertIn(f"{omitted} chars truncated", reason)
        archive = os.path.join(self.home, "tool-output-archive",
                               "sess-t", "call-1.txt")
        self.assertTrue(os.path.exists(archive))
        with open(archive, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)  # FULL original, recoverable

    def test_just_below_head_plus_tail_no_negative_count(self):
        # Regression for the historical negative-omitted-count bug: text just
        # under KEEP_HEAD+KEEP_TAIL must never be truncated at all.
        text = "x" * (self.hook.KEEP_HEAD + self.hook.KEEP_TAIL - 1)
        r = run_hook(POSTTOOL, self.payload(text), self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_just_below_threshold_silent(self):
        text = "x" * (self.hook.TRUNCATE_THRESHOLD - 1)
        r = run_hook(POSTTOOL, self.payload(text), self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_other_tool_name_ignored(self):
        text = "x" * (self.hook.TRUNCATE_THRESHOLD * 3)
        r = run_hook(POSTTOOL, self.payload(text, tool="Bash"), self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertFalse(
            os.path.exists(os.path.join(self.home, "tool-output-archive")))

    def test_garbage_stdin_exits_0(self):
        for garbage in ("not json {{", "", '["list", "payload"]'):
            r = run_hook(POSTTOOL, garbage, self.home)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")


class TestPreCompactFailSilent(HookTestBase):
    def test_garbage_stdin_exits_0(self):
        for garbage in ("not json {{", "", "42"):
            r = run_hook(PRECOMPACT, garbage, self.home)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_payload_without_session_id_exits_0(self):
        r = run_hook(PRECOMPACT, json.dumps({"trigger": "auto"}), self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_unknown_session_exits_0(self):
        # Random UUID: no goals-db row can match whether or not the real
        # ~/.codex/goals_1.sqlite exists on this machine.
        payload = json.dumps({"session_id": str(uuid.uuid4())})
        r = run_hook(PRECOMPACT, payload, self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class TestShouldNotifyThrottle(HookTestBase):
    """In-process tests of the 12h popup throttle (state file in temp home)."""

    def setUp(self):
        super().setUp()
        self.mod = load_hook(PRECOMPACT, "precompact_under_test", self.home)

    def test_first_yes_then_throttled(self):
        self.assertTrue(self.mod.should_notify("t1"))
        self.assertFalse(self.mod.should_notify("t1"))
        self.assertTrue(self.mod.should_notify("t2"))  # per-thread, not global
        self.assertTrue(os.path.exists(self.mod.STATE_FILE))

    def test_corrupt_state_notifies_anyway(self):
        with open(self.mod.STATE_FILE, "wb") as f:
            f.write(b"\x00 not json at all")
        self.assertTrue(self.mod.should_notify("t1"))

    def test_non_dict_state_notifies_anyway(self):
        with open(self.mod.STATE_FILE, "w") as f:
            json.dump(["a", "list"], f)
        self.assertTrue(self.mod.should_notify("t1"))

    def test_expired_entry_notifies_again(self):
        stale_ms = (time.time() - 13 * 3600) * 1000  # older than the 12h window
        with open(self.mod.STATE_FILE, "w") as f:
            json.dump({"t1": stale_ms}, f)
        self.assertTrue(self.mod.should_notify("t1"))


if __name__ == "__main__":
    unittest.main()
