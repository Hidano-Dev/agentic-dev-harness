from __future__ import annotations

import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

from _helpers import HARNESS_REPO, commit_all, git, make_consumer, make_harness, minimal_config, write

import apply_sync
import verify_harness_sync as vhs


class VerifyHarnessSyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.harness, self.sha = make_harness(self.tmp / "harness")
        self.repo, self.base = make_consumer(self.tmp / "consumer", minimal_config())

    def tearDown(self):
        self._tmp.cleanup()

    def sync_branch(self, sha=None, harness_repo=HARNESS_REPO, branch="chore/harness-sync") -> str:
        git(self.repo, "checkout", "-q", "-b", branch, self.base)
        with redirect_stderr(io.StringIO()):
            apply_sync.apply_sync(self.harness, self.repo, sha or self.sha, harness_repo, "main")
        return commit_all(self.repo, "sync")

    def verify(self, head, base=None, **kw):
        with redirect_stderr(io.StringIO()):
            return vhs.verify(self.repo, base or self.base, head, HARNESS_REPO, "main",
                              harness_dir=kw.pop("harness_dir", self.harness), **kw)

    def test_exact_apply_result_passes(self):
        head = self.sync_branch()
        ok, msgs = self.verify(head)
        self.assertTrue(ok, msgs)

    def test_extra_file_change_fails(self):
        self.sync_branch()
        write(self.repo / "src/app.py", "print('evil')\n")
        head = commit_all(self.repo, "sneak")
        ok, msgs = self.verify(head)
        self.assertFalse(ok)
        self.assertTrue(any("src/app.py" in m for m in msgs), msgs)

    def test_extra_new_file_fails(self):
        self.sync_branch()
        write(self.repo / ".github/workflows/evil.yml", "on: push\n")
        head = commit_all(self.repo, "sneak")
        ok, _ = self.verify(head)
        self.assertFalse(ok)

    def test_tampered_owned_file_fails(self):
        self.sync_branch()
        f = self.repo / ".claude/skills/linear-worker/SKILL.md"
        write(f, f.read_text(encoding="utf-8").replace("force-push", "push"))
        head = commit_all(self.repo, "tamper")
        ok, msgs = self.verify(head)
        self.assertFalse(ok)
        self.assertTrue(any("SKILL.md" in m for m in msgs), msgs)

    def test_tampered_config_fails(self):
        self.sync_branch()
        cfg_path = self.repo / ".kiro/orchestration/config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["auto_merge"]["protected_paths"] = []
        write(cfg_path, json.dumps(cfg, indent=2) + "\n")
        head = commit_all(self.repo, "tamper config")
        ok, _ = self.verify(head)
        self.assertFalse(ok)

    def test_unexpected_harness_repo_fails(self):
        head = self.sync_branch(harness_repo="evil/harness")
        ok, msgs = self.verify(head)
        self.assertFalse(ok)
        self.assertIn("repo が想定外", msgs[0])

    def test_sha_not_on_reviewed_ref_fails(self):
        git(self.harness, "checkout", "-q", "-b", "unreviewed")
        write(self.harness / ".claude/rules/git-workflow.md", "relaxed rules\n")
        sha = commit_all(self.harness, "unreviewed")
        git(self.harness, "checkout", "-q", "main")
        head = self.sync_branch(sha=sha)
        ok, msgs = self.verify(head)
        self.assertFalse(ok)
        self.assertIn("含まれない", msgs[0])

    def test_downgrade_fails(self):
        old = self.sha
        write(self.harness / ".claude/rules/git-workflow.md", "v2\n")
        new = commit_all(self.harness, "v2")
        # base には新しい版が適用済み → head で古い版に戻す
        git(self.repo, "checkout", "-q", "develop")
        with redirect_stderr(io.StringIO()):
            apply_sync.apply_sync(self.harness, self.repo, new, HARNESS_REPO, "main")
        self.base = commit_all(self.repo, "sync v2")
        head = self.sync_branch(sha=old, branch="downgrade")
        ok, msgs = self.verify(head)
        self.assertFalse(ok)
        self.assertIn("巻き戻し", msgs[0])

    def test_base_advanced_after_sync_still_passes(self):
        head = self.sync_branch()
        git(self.repo, "checkout", "-q", "develop")
        write(self.repo / "src/other.py", "x = 1\n")
        new_base = commit_all(self.repo, "unrelated change on develop")
        ok, msgs = self.verify(head, base=new_base)
        self.assertTrue(ok, msgs)

    def test_missing_version_fails(self):
        git(self.repo, "checkout", "-q", "-b", "plain")
        write(self.repo / "README.md", "changed\n")
        head = commit_all(self.repo, "plain")
        ok, _ = self.verify(head)
        self.assertFalse(ok)

    def test_cli_clones_harness_from_url(self):
        head = self.sync_branch()
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), unittest.mock.patch("sys.stdout", new=out):
            code = vhs.main(["--repo-dir", str(self.repo), "--base", self.base, "--head", head,
                             "--harness-repo", HARNESS_REPO, "--harness-url", str(self.harness)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)  # 一時 worktree を残さない

    def test_cli_mismatch_exit_code(self):
        self.sync_branch()
        write(self.repo / "README.md", "changed\n")
        head = commit_all(self.repo, "extra")
        with redirect_stderr(io.StringIO()), unittest.mock.patch("sys.stdout", new=io.StringIO()):
            code = vhs.main(["--repo-dir", str(self.repo), "--base", self.base, "--head", head,
                             "--harness-repo", HARNESS_REPO, "--harness", str(self.harness)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
