"""
Tests for codex_session_cleaner.py (archive_prune.py has its own file).

Everything runs against throwaway tempfile trees — never ~/.codex — because
this tool rewrites files in place, and a test must not be able to eat a real
session. The concurrent-writer guard is exercised by hijacking
tempfile.mkstemp to append to the source file mid-run: it's the one seam
between the initial stat and the guard's re-stat that doesn't require
patching os.stat itself (which tempfile and unittest both rely on
internally).
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codex_session_cleaner as csc  # noqa: E402


def jline(obj) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def event(payload_type, **extra) -> str:
    return jline({"type": "event_msg", "payload": dict(extra, type=payload_type)})


def resp(payload) -> str:
    return jline({"type": "response_item", "payload": payload})


def session_meta(text, **extra) -> str:
    return jline({"type": "session_meta",
                  "payload": dict(extra, base_instructions={"text": text})})


def compacted(history, **extra) -> str:
    return jline({"type": "compacted",
                  "payload": dict(extra, message="", replacement_history=history)})


def read_text(p: str) -> str:
    with open(p) as f:
        return f.read()


class TempTreeMixin(unittest.TestCase):
    """Shared helpers for building throwaway file trees."""

    def tmpdir(self) -> str:
        d = tempfile.mkdtemp(prefix="codex-cleanup-test-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def make_jsonl(self, lines, name="s.jsonl") -> str:
        p = os.path.join(self.tmpdir(), name)
        with open(p, "w") as f:
            f.writelines(lines)
        return p

    def assert_no_tmp_left(self, path: str) -> None:
        leftovers = [f for f in os.listdir(os.path.dirname(path))
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestCompactLine(unittest.TestCase):
    def test_drops_token_count_event(self):
        line = event("token_count", info={"total": 12345})
        self.assertEqual(csc.compact_line(line, {}), (None, True))

    def test_drops_agent_message_event(self):
        line = event("agent_message", message="dup of response_item copy")
        self.assertEqual(csc.compact_line(line, {}), (None, True))

    def test_keeps_response_item_mentioning_token_count(self):
        # Substring '"token_count"' present, but it's a response_item — the
        # pre-filter must not nuke it, and the bytes must come back untouched.
        line = resp({"type": "other", "token_count": 5})
        out, changed = csc.compact_line(line, {})
        self.assertIs(out, line)
        self.assertFalse(changed)

    def test_keeps_event_msg_of_other_type(self):
        line = event("task_started", token_count=1)
        self.assertEqual(csc.compact_line(line, {}), (line, False))

    def test_blanks_exec_command_arguments(self):
        names = {}
        line = resp({"type": "function_call", "name": "exec_command",
                     "call_id": "c1", "arguments": '{"cmd":["ls","-la"]}'})
        out, changed = csc.compact_line(line, names)
        self.assertTrue(changed)
        self.assertEqual(names, {"c1": "exec_command"})
        self.assertEqual(json.loads(out)["payload"]["arguments"], "")

    def test_blanks_correlated_output_str_form(self):
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": "wall of exec output"})
        out, changed = csc.compact_line(line, names)
        self.assertTrue(changed)
        self.assertEqual(json.loads(out)["payload"]["output"], "")

    def test_blanks_correlated_output_dict_form(self):
        names = {"c1": "exec_command"}
        line = resp({"type": "function_call_output", "call_id": "c1",
                     "output": {"content": "wall of exec output", "success": True}})
        out, changed = csc.compact_line(line, names)
        self.assertTrue(changed)
        payload = json.loads(out)["payload"]
        self.assertEqual(payload["output"]["content"], "")
        self.assertIs(payload["output"]["success"], True)

    def test_leaves_uncorrelated_output_alone(self):
        # call_id never registered as exec_command -> untouched, exact bytes.
        line = ('{ "type": "response_item", "payload": { "type": '
                '"function_call_output", "call_id": "cX", "output": "keep" } }\n')
        out, changed = csc.compact_line(line, {"c1": "exec_command"})
        self.assertIs(out, line)
        self.assertFalse(changed)

    def test_unparseable_line_with_trigger_passes_through(self):
        for line in ('not json but "exec_command" mention\n',
                     '{"token_count" broken json\n'):
            out, changed = csc.compact_line(line, {})
            self.assertIs(out, line)
            self.assertFalse(changed)

    def test_untouched_line_keeps_exact_bytes(self):
        # Odd spacing would be lost if this line were ever re-serialized.
        line = '{ "exec_command" : "mentioned", "type" : "other" }\n'
        out, changed = csc.compact_line(line, {})
        self.assertIs(out, line)
        self.assertFalse(changed)

    def state(self, total_compacted=0):
        return {"seen_base_instructions": False, "compacted_seen": 0,
                "total_compacted": total_compacted}

    def test_first_session_meta_kept_intact(self):
        line = session_meta("full system prompt", id="a")
        out, changed = csc.compact_line(line, {}, self.state())
        self.assertIs(out, line)
        self.assertFalse(changed)

    def test_repeat_session_meta_base_instructions_blanked(self):
        state = self.state()
        csc.compact_line(session_meta("full system prompt", id="a"), {}, state)
        out, changed = csc.compact_line(
            session_meta("full system prompt", id="b"), {}, state)
        self.assertTrue(changed)
        obj = json.loads(out)
        self.assertEqual(obj["payload"]["base_instructions"]["text"], "")
        self.assertEqual(obj["payload"]["id"], "b")  # structural fields survive

    def test_session_meta_without_state_passes_through(self):
        line = session_meta("full system prompt", id="a")
        out, changed = csc.compact_line(line, {})
        self.assertIs(out, line)
        self.assertFalse(changed)

    def test_earlier_compacted_history_trimmed(self):
        state = self.state(total_compacted=2)
        line = compacted([{"role": "user", "text": "old"}], window_id="w1")
        out, changed = csc.compact_line(line, {}, state)
        self.assertTrue(changed)
        obj = json.loads(out)
        self.assertEqual(obj["payload"]["replacement_history"], [])
        self.assertEqual(obj["payload"]["window_id"], "w1")  # structural fields survive

    def test_last_compacted_history_kept(self):
        state = self.state(total_compacted=1)
        line = compacted([{"role": "user", "text": "current"}], window_id="w1")
        out, changed = csc.compact_line(line, {}, state)
        self.assertIs(out, line)
        self.assertFalse(changed)

    def test_compacted_without_state_passes_through(self):
        line = compacted([{"role": "user", "text": "x"}])
        out, changed = csc.compact_line(line, {})
        self.assertIs(out, line)
        self.assertFalse(changed)


class TestBlank(unittest.TestCase):
    def test_blanks_strings_keeps_preserved_and_scalars(self):
        obj = {"type": "message", "text": "secret", "n": 3, "ok": True,
               "nothing": None, "cwd": "/home/x"}
        out = csc.blank(obj)
        self.assertEqual(out, {"type": "message", "text": "", "n": 3,
                               "ok": True, "nothing": None, "cwd": "/home/x"})

    def test_recurses_lists_and_dicts(self):
        obj = {"payload": {"content": [{"text": "hi", "type": "output_text"},
                                       "loose", 7]}}
        out = csc.blank(obj)
        self.assertEqual(out["payload"]["content"],
                         [{"text": "", "type": "output_text"}, "", 7])

    def test_preserve_only_shields_string_values(self):
        # A PRESERVE key holding a dict still gets recursed into.
        out = csc.blank({"id": {"inner": "gone"}})
        self.assertEqual(out, {"id": {"inner": ""}})


class TestCleanFile(TempTreeMixin):
    def lines(self):
        return [
            jline({"type": "message", "text": "heavy secret", "id": "abc"}),
            "this is not json\n",
            jline({"nested": {"cwd": "/x", "data": "blob"}, "arr": ["a", 1]}),
        ]

    def test_clean_roundtrip(self):
        p = self.make_jsonl(self.lines())
        before, after, bad, skip = csc.clean_file(p)
        self.assertIsNone(skip)
        self.assertEqual(bad, 1)
        self.assertEqual(after, os.path.getsize(p))
        self.assertGreater(before, 0)
        with open(p) as f:
            out = f.read().splitlines()
        self.assertEqual(len(out), 3)  # line count survives, bad line -> {}
        self.assertEqual(out[1], "{}")
        first = json.loads(out[0])
        self.assertEqual(first, {"type": "message", "text": "", "id": "abc"})
        third = json.loads(out[2])
        self.assertEqual(third, {"nested": {"cwd": "/x", "data": ""},
                                 "arr": ["", 1]})
        self.assert_no_tmp_left(p)

    def test_mtime_restored(self):
        p = self.make_jsonl(self.lines())
        t = 1600000000.0
        os.utime(p, (t, t))
        csc.clean_file(p)
        self.assertAlmostEqual(os.stat(p).st_mtime, t, delta=0.01)

    def test_concurrent_writer_guard_skips(self):
        p = self.make_jsonl(self.lines())
        original = read_text(p)
        real_mkstemp = tempfile.mkstemp

        def sneaky(*a, **kw):  # append between orig_stat and the re-stat
            with open(p, "a") as f:
                f.write("late append\n")
            return real_mkstemp(*a, **kw)

        with mock.patch.object(csc.tempfile, "mkstemp", side_effect=sneaky):
            before, after, bad, skip = csc.clean_file(p)
        self.assertIn("changed during processing", skip)
        self.assertEqual(before, after)
        self.assertEqual(read_text(p), original + "late append\n")  # untouched
        self.assert_no_tmp_left(p)


class TestCompactFile(TempTreeMixin):
    def lines(self):
        return [
            event("token_count", info={"total": 1}),
            event("agent_message", message="dup"),
            resp({"type": "function_call", "name": "exec_command",
                  "call_id": "c1", "arguments": '{"cmd":["ls"]}'}),
            resp({"type": "function_call_output", "call_id": "c1",
                  "output": "EXEC-OUT"}),
            resp({"type": "function_call_output", "call_id": "c2",
                  "output": "PATCH-OUT"}),
            resp({"type": "message", "content": [{"type": "output_text",
                                                  "text": "keep me"}]}),
            "garbage line\n",
        ]

    def test_compact_roundtrip(self):
        p = self.make_jsonl(self.lines())
        before, after, dropped, edited, skip = csc.compact_file(p)
        self.assertIsNone(skip)
        self.assertEqual(dropped, 2)   # both telemetry events
        self.assertEqual(edited, 2)    # exec args + exec output
        self.assertLess(after, before)
        with open(p) as f:
            out = f.read().splitlines()
        self.assertEqual(len(out), 5)
        objs = [json.loads(l) for l in out[:4]]
        self.assertEqual(objs[0]["payload"]["arguments"], "")
        self.assertEqual(objs[1]["payload"]["output"], "")
        self.assertEqual(objs[2]["payload"]["output"], "PATCH-OUT")  # not exec's
        self.assertEqual(objs[3]["payload"]["content"][0]["text"], "keep me")
        self.assertEqual(out[4], "garbage line")
        self.assert_no_tmp_left(p)

    def test_nothing_to_touch_is_byte_identical(self):
        lines = [resp({"type": "message", "content": "hello"})]
        p = self.make_jsonl(lines)
        before, after, dropped, edited, skip = csc.compact_file(p)
        self.assertEqual((dropped, edited, skip), (0, 0, None))
        self.assertEqual(read_text(p), "".join(lines))

    def test_mtime_restored(self):
        p = self.make_jsonl(self.lines())
        t = 1600000000.0
        os.utime(p, (t, t))
        csc.compact_file(p)
        self.assertAlmostEqual(os.stat(p).st_mtime, t, delta=0.01)

    def test_concurrent_writer_guard_skips(self):
        p = self.make_jsonl(self.lines())
        original = read_text(p)
        real_mkstemp = tempfile.mkstemp

        def sneaky(*a, **kw):
            with open(p, "a") as f:
                f.write("x")
            return real_mkstemp(*a, **kw)

        with mock.patch.object(csc.tempfile, "mkstemp", side_effect=sneaky):
            before, after, dropped, edited, skip = csc.compact_file(p)
        self.assertIn("changed during processing", skip)
        self.assertEqual(before, after)
        self.assertEqual(read_text(p), original + "x")
        self.assert_no_tmp_left(p)

    def test_dedupes_session_meta_and_trims_old_compacted(self):
        lines = [
            session_meta("SYSTEM PROMPT", id="a"),
            compacted([{"text": "w1"}], window_id="w1"),
            session_meta("SYSTEM PROMPT", id="b"),
            compacted([{"text": "w2 latest"}], window_id="w2"),
        ]
        p = self.make_jsonl(lines)
        before, after, dropped, edited, skip = csc.compact_file(p)
        self.assertIsNone(skip)
        self.assertLess(after, before)
        with open(p) as f:
            out = [json.loads(l) for l in f.read().splitlines()]
        self.assertEqual(out[0]["payload"]["base_instructions"]["text"], "SYSTEM PROMPT")
        self.assertEqual(out[1]["payload"]["replacement_history"], [])
        self.assertEqual(out[2]["payload"]["base_instructions"]["text"], "")
        self.assertEqual(out[2]["payload"]["id"], "b")
        self.assertEqual(out[3]["payload"]["replacement_history"], [{"text": "w2 latest"}])
        self.assert_no_tmp_left(p)


class TestFileDate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(csc.file_date(os.path.join("r", "2026", "01", "31", "s.jsonl")),
                         date(2026, 1, 31))

    def test_invalid_month(self):
        self.assertIsNone(csc.file_date(os.path.join("r", "2026", "13", "01", "s.jsonl")))

    def test_no_date_in_path(self):
        self.assertIsNone(csc.file_date(os.path.join("r", "loose.jsonl")))


class TestSelectTargets(TempTreeMixin):
    def build_tree(self):
        root = self.tmpdir()
        old_day = date.today() - timedelta(days=200)
        old_dir = os.path.join(root, f"{old_day:%Y}", f"{old_day:%m}", f"{old_day:%d}")
        today = date.today()
        new_dir = os.path.join(root, f"{today:%Y}", f"{today:%m}", f"{today:%d}")
        os.makedirs(old_dir)
        os.makedirs(new_dir, exist_ok=True)
        big = os.path.join(old_dir, "big.jsonl")
        small = os.path.join(new_dir, "small.jsonl")
        undated = os.path.join(root, "undated.jsonl")
        for p, size in ((big, 2000), (small, 10), (undated, 50)):
            with open(p, "w") as f:
                f.write("x" * size)
        return root, big, small, undated

    def args(self, root, **kw):
        base = dict(root=root, before=None, keep_days=None, min_size=0,
                    dry_run=False)
        base.update(kw)
        return Namespace(**base)

    def test_conflict_returns_error_2(self):
        root, *_ = self.build_tree()
        err_out = io.StringIO()
        with contextlib.redirect_stderr(err_out):
            targets, err = csc.select_targets(
                self.args(root, before="2026-01-01", keep_days=5))
        self.assertEqual((targets, err), ([], 2))
        self.assertIn("not both", err_out.getvalue())

    def test_no_filters_sorted_biggest_first(self):
        root, big, small, undated = self.build_tree()
        targets, err = csc.select_targets(self.args(root))
        self.assertIsNone(err)
        self.assertEqual(targets, [big, undated, small])

    def test_before_excludes_recent_and_undated(self):
        root, big, small, undated = self.build_tree()
        cutoff = (date.today() - timedelta(days=100)).isoformat()
        targets, err = csc.select_targets(self.args(root, before=cutoff))
        self.assertIsNone(err)
        self.assertEqual(targets, [big])

    def test_keep_days_zero_spares_today_and_undated(self):
        root, big, small, undated = self.build_tree()
        targets, err = csc.select_targets(self.args(root, keep_days=0))
        self.assertIsNone(err)
        self.assertEqual(targets, [big])

    def test_min_size_filter(self):
        root, big, small, undated = self.build_tree()
        targets, err = csc.select_targets(self.args(root, min_size=0.001))
        self.assertIsNone(err)
        self.assertEqual(targets, [big])  # 2000 B >= 1048 B; others under


class TestRunBatch(TempTreeMixin):
    def args(self, **kw):
        base = dict(root=".", before=None, keep_days=None, min_size=0,
                    dry_run=False)
        base.update(kw)
        return Namespace(**base)

    def run_quiet(self, *a, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = csc.run_batch(*a, **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_empty_worklist(self):
        rc, out, _ = self.run_quiet([], self.args(), None, "clean")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing matches", out)

    def test_dry_run_changes_nothing(self):
        p = self.make_jsonl([event("token_count")])
        original = read_text(p)
        rc, out, _ = self.run_quiet([p], self.args(dry_run=True), None, "compact")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(read_text(p), original)

    def test_failure_exits_1(self):
        p = self.make_jsonl([jline({"a": 1})])

        def boom(path):
            raise ValueError("kaput")

        rc, out, err = self.run_quiet([p], self.args(), boom, "clean")
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", err)
        self.assertIn("1 failed", out)

    def test_skip_accounting(self):
        p = self.make_jsonl([jline({"a": 1})])
        rc, out, _ = self.run_quiet(
            [p], self.args(), lambda _: (5, 5, "", "busy"), "clean")
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED", out)
        self.assertIn("1 skipped", out)

    def test_cmd_compact_end_to_end(self):
        root = self.tmpdir()
        day = os.path.join(root, "2026", "01", "01")
        os.makedirs(day)
        p = os.path.join(day, "s.jsonl")
        with open(p, "w") as f:
            f.writelines([event("token_count"), resp({"type": "message"})])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = csc.cmd_compact(Namespace(root=root, before=None,
                                           keep_days=None, min_size=0,
                                           dry_run=False))
        self.assertEqual(rc, 0)
        self.assertIn("TOTAL", out.getvalue())
        with open(p) as f:
            self.assertEqual(len(f.read().splitlines()), 1)  # telemetry gone


if __name__ == "__main__":
    unittest.main()
