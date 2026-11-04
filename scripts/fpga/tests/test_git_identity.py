"""kv260_6a.git_identity: which work-tree states mark a build dirty (temporary git repository).

    python -m unittest discover -s scripts/fpga/tests
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kv260_6a as K  # noqa: E402


class TestGitIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.git("config", "core.autocrlf", "false")
        (self.r / "rtl").mkdir()
        (self.r / "docs").mkdir()
        (self.r / "rtl" / "a.v").write_text("module a; endmodule\n")
        (self.r / "docs" / "a.md").write_text("x\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        subprocess.run(["git", "-C", str(self.r), *a], check=True, capture_output=True)

    def test_clean(self):
        g = K.git_identity(self.r)
        self.assertEqual((g["dirty"], g["changes"], g["untracked_docs"], g["worktree_diff_sha256"]), (False, [], [], None))
        self.assertEqual(len(g["commit"]), 40)

    def test_untracked_docs_recorded_not_dirty(self):
        (self.r / "docs" / "soc").mkdir()
        (self.r / "docs" / "soc" / "arch.json").write_text("{}\n")
        (self.r / "docs" / "soc" / "arch.svg").write_text("<svg/>\n")
        g = K.git_identity(self.r)
        self.assertFalse(g["dirty"])
        self.assertEqual(g["changes"], [])
        self.assertEqual(sorted(g["untracked_docs"]), ["docs/soc/arch.json", "docs/soc/arch.svg"])

    def test_untracked_outside_docs_is_dirty(self):
        (self.r / "rtl" / "new.v").write_text("module n; endmodule\n")
        (self.r / "docs" / "n.md").write_text("y\n")
        g = K.git_identity(self.r)
        self.assertTrue(g["dirty"])
        self.assertEqual(g["changes"], ["?? rtl/new.v"])
        self.assertEqual(g["untracked_docs"], ["docs/n.md"])

    def test_untracked_in_new_top_level_directory_is_dirty(self):
        (self.r / "extra").mkdir()
        (self.r / "extra" / "x.tcl").write_text("puts x\n")
        g = K.git_identity(self.r)
        self.assertTrue(g["dirty"])
        self.assertEqual(g["changes"], ["?? extra/x.tcl"])        # listed per file, not as the directory

    def test_tracked_change_is_dirty_even_in_docs(self):
        (self.r / "docs" / "a.md").write_text("changed\n")
        g = K.git_identity(self.r)
        self.assertTrue(g["dirty"])
        self.assertEqual(g["changes"], [" M docs/a.md"])          # leading status column kept
        self.assertIsNotNone(g["worktree_diff_sha256"])

    def test_staged_and_deleted_are_dirty(self):
        (self.r / "rtl" / "a.v").unlink()
        g = K.git_identity(self.r)
        self.assertTrue(g["dirty"])
        self.assertEqual(g["changes"], [" D rtl/a.v"])


if __name__ == "__main__":
    unittest.main()
