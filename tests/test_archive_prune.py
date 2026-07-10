"""
Tests for archive_prune.py.

All against a throwaway temp tree — never the repo's own tool-output-archive
— because this tool deletes files for a living. Ages are faked with os.utime
so the age pass, the oldest-first size cap, and the future-mtime protection
can all be exercised deterministically.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import archive_prune  # noqa: E402


class TestArchivePrune(unittest.TestCase):
    def tmpdir(self) -> str:
        d = tempfile.mkdtemp(prefix="codex-prune-test-")
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

    def test_age_pass_and_empty_dir_removal(self):
        root = self.tmpdir()
        old = self.add(root, "s1", "a.txt", 100, age_days=20)
        recent = self.add(root, "s2", "b.txt", 100, age_days=1)
        future = self.add(root, "s2", "c.txt", 100, age_days=-10)  # clock skew
        loose = self.add(root, "s3", "keep.log", 100, age_days=20)  # not ours
        rc, out = self.run_prune("--root", root, "--keep-days", "14")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(os.path.dirname(old)))  # emptied dir gone
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(future))  # future mtime never ages out
        self.assertTrue(os.path.exists(loose))   # non-.txt left strictly alone
        self.assertTrue(os.path.isdir(root))     # root itself survives
        self.assertIn("Deleted 1", out)

    def test_size_cap_deletes_oldest_first(self):
        root = self.tmpdir()
        oldest = self.add(root, "s", "a.txt", 600 * 1024, age_days=3)
        middle = self.add(root, "s", "b.txt", 600 * 1024, age_days=2)
        newest = self.add(root, "s", "c.txt", 600 * 1024, age_days=1)
        rc, _ = self.run_prune("--root", root, "--keep-days", "14",
                               "--max-total-mb", "1")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(oldest))
        self.assertFalse(os.path.exists(middle))  # still over cap after first
        self.assertTrue(os.path.exists(newest))   # 600 KB fits under 1 MB

    def test_dry_run_changes_nothing(self):
        root = self.tmpdir()
        old = self.add(root, "s1", "a.txt", 100, age_days=20)
        rc, out = self.run_prune("--root", root, "--keep-days", "14", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertTrue(os.path.exists(old))

    def test_missing_root_exits_0(self):
        rc, out = self.run_prune("--root", os.path.join(self.tmpdir(), "nope"))
        self.assertEqual(rc, 0)
        self.assertIn("nothing to prune", out)

    def test_empty_root_exits_0(self):
        rc, out = self.run_prune("--root", self.tmpdir())
        self.assertEqual(rc, 0)
        self.assertIn("nothing to prune", out)


if __name__ == "__main__":
    unittest.main()
