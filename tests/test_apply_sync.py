from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

from _helpers import HARNESS_REPO, commit_all, git, make_consumer, make_harness, minimal_config, write

import apply_sync
from apply_sync import SyncError


class ApplySyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.harness, self.sha = make_harness(self.tmp / "harness")
        self.target, _ = make_consumer(self.tmp / "consumer", minimal_config())

    def tearDown(self):
        self._tmp.cleanup()

    def apply(self, **kw):
        with redirect_stderr(io.StringIO()) as err:
            result = apply_sync.apply_sync(self.harness, self.target, kw.pop("sha", self.sha),
                                           kw.pop("harness_repo", HARNESS_REPO), "main")
        self.stderr = err.getvalue()
        return result

    def test_copies_manifest_contents_and_records_version(self):
        r = self.apply()
        t = self.target
        self.assertTrue((t / ".claude/skills/linear-worker/SKILL.md").is_file())
        self.assertTrue((t / ".claude/skills/linear-worker/scripts/policy_gate.py").is_file())
        self.assertTrue((t / ".claude/rules/git-workflow.md").is_file())
        self.assertTrue((t / ".harness/invariants.json").is_file())
        self.assertEqual((t / ".github/workflows/policy-gate.yml").read_bytes(),
                         (self.harness / "templates/consumer/policy-gate.yml").read_bytes())
        self.assertIn(".github/workflows/harness-sync.yml", r["workflows"])
        self.assertTrue(r["claude_md"] == "appended")
        self.assertTrue((t / "CLAUDE.md").read_text(encoding="utf-8").endswith("\n\n@.claude/rules/git-workflow.md\n"))
        self.assertTrue((t / "CLAUDE.md").read_text(encoding="utf-8").startswith("# project notes\n"))
        ver = json.loads((t / ".harness/version").read_text(encoding="utf-8"))
        self.assertEqual(ver["repo"], HARNESS_REPO)
        self.assertEqual(ver["ref"], "main")
        self.assertEqual(ver["sha"], self.sha)
        self.assertIn(".claude/skills/linear-worker", ver["owned"])
        # 配布元の scripts/ や templates/ 自体は取り込み側に入らない
        self.assertFalse((t / "scripts/sync").exists())
        self.assertFalse((t / "sync-manifest.json").exists())

    def test_missing_templates_are_skipped_with_warning(self):
        os.remove(self.harness / "templates/consumer/docs-policy.yml")
        sha = commit_all(self.harness, "drop docs-policy")
        r = self.apply(sha=sha)
        self.assertNotIn(".github/workflows/docs-policy.yml", r["workflows"])
        self.assertIn("::warning::templates/consumer/docs-policy.yml", self.stderr)

    def test_applies_config_migrations(self):
        before = json.loads((self.target / ".kiro/orchestration/config.json").read_text(encoding="utf-8"))
        r = self.apply()
        after = json.loads((self.target / ".kiro/orchestration/config.json").read_text(encoding="utf-8"))
        self.assertEqual(r["config"], "migrated")
        self.assertNotEqual(before, after)
        self.assertIn(".kiro/orchestration/", after["auto_merge"]["protected_paths"])

    def test_idempotent_and_deterministic(self):
        self.apply()
        commit_all(self.target, "sync")
        tree1 = git(self.target, "rev-parse", "HEAD^{tree}")
        r = self.apply()
        self.assertEqual(r["claude_md"], "unchanged")
        self.assertEqual(git(self.target, "status", "--porcelain"), "")
        git(self.target, "add", "-A")
        self.assertEqual(git(self.target, "write-tree"), tree1)

    def test_directory_is_replaced_entirely(self):
        write(self.target / ".claude/skills/linear-worker/stale.md", "old\n")
        self.apply()
        self.assertFalse((self.target / ".claude/skills/linear-worker/stale.md").exists())

    def test_reads_committed_content_not_working_tree(self):
        write(self.harness / ".claude/rules/git-workflow.md", "uncommitted edit\n")
        self.apply()
        text = (self.target / ".claude/rules/git-workflow.md").read_text(encoding="utf-8")
        self.assertNotEqual(text, "uncommitted edit\n")

    def test_removes_paths_dropped_from_manifest(self):
        write(self.target / "old/owned.txt", "x\n")
        write(self.target / ".harness/version", json.dumps(
            {"repo": HARNESS_REPO, "ref": "main", "sha": "0" * 40, "owned": ["old/owned.txt"]}))
        r = self.apply()
        self.assertEqual(r["removed"], ["old/owned.txt"])
        self.assertFalse((self.target / "old/owned.txt").exists())

    def test_refuses_submodule_under_target(self):
        git(self.target, "update-index", "--add", "--cacheinfo",
            f"160000,{self.sha},.claude/skills/linear-worker")
        git(self.target, "commit", "-q", "-m", "gitlink")
        before = (self.target / "CLAUDE.md").read_bytes()
        with self.assertRaises(SyncError) as cm:
            self.apply()
        self.assertIn("submodule", str(cm.exception))
        self.assertEqual((self.target / "CLAUDE.md").read_bytes(), before)  # 何も書いていない
        self.assertFalse((self.target / ".harness/version").exists())

    def test_refuses_symlink_under_target(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.target / ".claude").mkdir()
        try:
            os.symlink(outside, self.target / ".claude/rules", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("この環境ではシンボリックリンクを作れない")
        with self.assertRaises(SyncError) as cm:
            self.apply()
        self.assertIn("シンボリックリンク", str(cm.exception))
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_config_aborts(self):
        write(self.target / ".kiro/orchestration/config.json", "{ broken")
        with self.assertRaises(SyncError):
            self.apply()

    def test_gitignored_destination_aborts(self):
        write(self.target / ".gitignore", ".harness/\n")
        commit_all(self.target, "ignore")
        with self.assertRaises(SyncError) as cm:
            self.apply()
        self.assertIn(".gitignore", str(cm.exception))

    def test_creates_claude_md_when_missing(self):
        os.remove(self.target / "CLAUDE.md")
        r = self.apply()
        self.assertEqual(r["claude_md"], "created")
        self.assertEqual((self.target / "CLAUDE.md").read_text(encoding="utf-8"), "@.claude/rules/git-workflow.md\n")

    def test_rejects_manifest_path_escape(self):
        m = json.loads((self.harness / "sync-manifest.json").read_text(encoding="utf-8"))
        m["owned"].append("../escape")
        write(self.harness / "sync-manifest.json", json.dumps(m))
        sha = commit_all(self.harness, "bad manifest")
        with self.assertRaises(SyncError):
            self.apply(sha=sha)

    def test_cli(self):
        result = self.tmp / "result.json"
        with redirect_stderr(io.StringIO()), unittest.mock.patch("sys.stdout", new=io.StringIO()):
            code = apply_sync.main(["--harness", str(self.harness), "--target", str(self.target),
                                    "--harness-repo", HARNESS_REPO, "--ref", "main", "--result", str(result)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["sha"], self.sha)


if __name__ == "__main__":
    unittest.main()
