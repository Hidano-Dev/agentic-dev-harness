"""check_docs_policy.py の Release PR(作業ブランチ → リリースブランチ)の扱いのテスト。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/skills/linear-worker/scripts/check_docs_policy.py"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ReleasePrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        git(d, "init", "-q", "-b", "main")
        git(d, "config", "user.email", "t@example.invalid")
        git(d, "config", "user.name", "t")
        git(d, "config", "core.autocrlf", "false")
        cfg = Path(d) / ".kiro/orchestration/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({
            "linear": {"team": "T"},
            "github": {"default_branch": "develop", "release_branch": "main"},
        }), encoding="utf-8")
        git(d, "add", "-A")
        git(d, "commit", "-qm", "init")
        (Path(d) / "README.md").write_text("x\n", encoding="utf-8")
        git(d, "add", "-A")
        git(d, "commit", "-qm", "readme")

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, head_ref, base_ref):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--base", "HEAD~1", "--head", "HEAD",
             "--head-ref", head_ref, "--base-ref", base_ref],
            cwd=self.d, capture_output=True, text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    def test_release_pr_may_change_aggregate_docs(self):
        self.assertEqual(self.run_check("develop", "main").returncode, 0)

    def test_feature_pr_may_not(self):
        self.assertEqual(self.run_check("feature/x", "develop").returncode, 1)

    def test_develop_to_other_base_is_not_release(self):
        self.assertEqual(self.run_check("develop", "feature/y").returncode, 1)


if __name__ == "__main__":
    unittest.main()
