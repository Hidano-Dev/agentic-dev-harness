from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

from _helpers import (HARNESS_REPO, ROOT, commit_all, git, make_consumer, make_harness, minimal_config,
                      write)

import apply_sync
import policy_gate as pg

HEAD = "a" * 40


def review(login="alice", state="APPROVED", commit=HEAD, typ="User", assoc="MEMBER"):
    return {"user": {"login": login, "type": typ}, "state": state, "commit_id": commit,
            "author_association": assoc}


class HumanApprovalTest(unittest.TestCase):
    def test_member_approval_on_head(self):
        self.assertTrue(pg.human_approval([review()], HEAD)[0])

    def test_owner_and_collaborator_count(self):
        self.assertTrue(pg.human_approval([review(assoc="OWNER")], HEAD)[0])
        self.assertTrue(pg.human_approval([review(assoc="COLLABORATOR")], HEAD)[0])

    def test_bot_is_not_human(self):
        self.assertFalse(pg.human_approval([review(login="ci[bot]", typ="Bot")], HEAD)[0])
        self.assertFalse(pg.human_approval([review(login="x[bot]")], HEAD)[0])

    def test_untrusted_association(self):
        self.assertFalse(pg.human_approval([review(assoc="CONTRIBUTOR")], HEAD)[0])
        self.assertFalse(pg.human_approval([review(assoc="NONE")], HEAD)[0])

    def test_stale_commit(self):
        ok, why = pg.human_approval([review(commit="b" * 40)], HEAD)
        self.assertFalse(ok)
        self.assertIn("古い", why)

    def test_latest_decision_wins(self):
        self.assertFalse(pg.human_approval([review(), review(state="DISMISSED")], HEAD)[0])
        self.assertTrue(pg.human_approval([review(state="CHANGES_REQUESTED"), review()], HEAD)[0])
        # COMMENTED は判断を変えない
        self.assertTrue(pg.human_approval([review(), review(state="COMMENTED")], HEAD)[0])

    def test_changes_requested_blocks(self):
        reviews = [review(), review(login="bob", state="CHANGES_REQUESTED")]
        self.assertFalse(pg.human_approval(reviews, HEAD)[0])


class MatchesTest(unittest.TestCase):
    def test_prefix_and_file(self):
        self.assertTrue(pg.matches(".github/workflows/ci.yml", [".github/"]))
        self.assertTrue(pg.matches(".claude/skills/linear-worker/SKILL.md", [".claude/skills/linear-worker"]))
        self.assertFalse(pg.matches(".claude/skills/linear-worker-x/a", [".claude/skills/linear-worker"]))
        self.assertTrue(pg.matches(".claude/settings.json", [".claude/settings.json"]))
        self.assertFalse(pg.matches(".claude/settings.local.json", [".claude/settings.json"]))


class InvariantsMatchCurrentDocsTest(unittest.TestCase):
    """配布する invariants.json が、現行の方針文書で満たされていること(文書を改稿したら合わせる)。"""

    def test_current_docs_satisfy_invariants(self):
        invariants = json.loads((ROOT / ".harness/invariants.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(invariants), 4)
        for inv in invariants:
            text = (ROOT / inv["file"]).read_text(encoding="utf-8")
            self.assertIsNotNone(re.search(inv["pattern"], text), inv["id"])

    def test_invariants_detect_removal(self):
        invariants = json.loads((ROOT / ".harness/invariants.json").read_text(encoding="utf-8"))
        for inv in invariants:
            self.assertIsNone(re.search(inv["pattern"], "# 無関係な文書\n\n何も書いていない。\n"), inv["id"])


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.harness, self.sha = make_harness(self.tmp / "harness")
        self.repo, _ = make_consumer(self.tmp / "consumer", minimal_config())
        # base = 同期済みの develop
        with redirect_stderr(io.StringIO()):
            apply_sync.apply_sync(self.harness, self.repo, self.sha, HARNESS_REPO, "main")
        self.base = commit_all(self.repo, "synced")
        self.approvals = 0
        self.verifies = 0

    def tearDown(self):
        self._tmp.cleanup()

    def branch(self, name="feature/x"):
        git(self.repo, "checkout", "-q", "-b", name, self.base)

    def evaluate(self, head, approved=False, sync_result=(False, ["not verified"]), base=None):
        def approval_fn():
            self.approvals += 1
            return (approved, "承認: alice" if approved else "人間の承認なし")

        def verify_fn():
            self.verifies += 1
            return sync_result

        return pg.evaluate(self.repo, base or self.base, head, approval_fn=approval_fn, sync_verify_fn=verify_fn)

    def test_plain_change_passes_without_approval(self):
        self.branch()
        write(self.repo / "src/app.py", "print('x')\n")
        r = self.evaluate(commit_all(self.repo))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(self.approvals, 0)

    def test_instruction_files_are_not_core(self):
        cfg_path = self.repo / ".kiro/orchestration/config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["auto_merge"]["protected_paths"] = list(pg.DEFAULT_CORE)
        write(cfg_path, json.dumps(cfg))
        self.base = commit_all(self.repo, "new default core")
        self.branch()
        write(self.repo / "CLAUDE.md", "# notes\n@.claude/rules/git-workflow.md\nmore\n")
        write(self.repo / "AGENTS.md", "agents\n")
        r = self.evaluate(commit_all(self.repo))
        self.assertTrue(r.ok, r.reason)

    def test_core_change_requires_approval(self):
        self.branch()
        write(self.repo / ".github/workflows/ci.yml", "on: pull_request\n")
        head = commit_all(self.repo)
        r = self.evaluate(head)
        self.assertFalse(r.ok)
        self.assertIn("人間の承認待ち", r.reason)
        self.assertTrue(self.evaluate(head, approved=True).ok)

    def test_core_from_base_config_not_head(self):
        # base の config で .claude/settings.json をコアにしておく
        cfg_path = self.repo / ".kiro/orchestration/config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["auto_merge"]["protected_paths"] = [".claude/settings.json"]
        write(cfg_path, json.dumps(cfg))
        self.base = commit_all(self.repo, "config")
        self.branch()
        cfg["auto_merge"]["protected_paths"] = []
        write(cfg_path, json.dumps(cfg))
        write(self.repo / ".claude/settings.json", "{}\n")
        r = self.evaluate(commit_all(self.repo))
        self.assertFalse(r.ok)
        self.assertIn(".claude/settings.json", "\n".join(r.details))

    def test_floor_core_applies_even_if_config_drops_it(self):
        cfg_path = self.repo / ".kiro/orchestration/config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["auto_merge"]["protected_paths"] = []
        write(cfg_path, json.dumps(cfg))
        self.base = commit_all(self.repo, "config")
        self.branch()
        write(self.repo / ".github/workflows/x.yml", "x\n")
        self.assertFalse(self.evaluate(commit_all(self.repo)).ok)

    def test_owned_edit_in_consumer_fails_even_with_approval(self):
        self.branch()
        f = self.repo / ".claude/rules/git-workflow.md"
        write(f, f.read_text(encoding="utf-8") + "\nlocal tweak\n")
        r = self.evaluate(commit_all(self.repo), approved=True)
        self.assertFalse(r.ok)
        self.assertIn("配布物", r.reason)

    def test_invariant_violation_fails(self):
        self.branch()
        f = self.repo / ".claude/skills/linear-worker/SKILL.md"
        write(f, re.sub(r"force[-_ ]?push", "push", f.read_text(encoding="utf-8"), flags=re.I))
        r = self.evaluate(commit_all(self.repo), approved=True)
        self.assertFalse(r.ok)
        self.assertIn("不変条件", r.reason)
        self.assertTrue(any("no-force-push" in d for d in r.details), r.details)

    def test_invariants_cannot_be_weakened_by_head_without_approval(self):
        self.branch()
        write(self.repo / ".harness/invariants.json", "[]\n")
        f = self.repo / ".claude/skills/linear-worker/SKILL.md"
        write(f, "nothing\n")
        r = self.evaluate(commit_all(self.repo))
        self.assertFalse(r.ok)
        self.assertIn("不変条件", r.reason)

    def test_missing_invariants_file_is_skipped(self):
        git(self.repo, "rm", "-q", ".harness/invariants.json")
        self.base = commit_all(self.repo, "no invariants")
        self.branch()
        write(self.repo / "src/app.py", "x\n")
        r = self.evaluate(commit_all(self.repo))
        self.assertTrue(r.ok)
        self.assertTrue(any("スキップ" in d for d in r.details))

    def test_verified_sync_pr_passes_without_approval(self):
        self.branch("chore/harness-sync-x")
        write(self.repo / ".harness/version", "{}\n")
        f = self.repo / ".claude/rules/git-workflow.md"
        write(f, f.read_text(encoding="utf-8") + "\nnew rules\n")
        r = self.evaluate(commit_all(self.repo), sync_result=(True, ["一致"]))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(self.approvals, 0)
        self.assertEqual(self.verifies, 1)

    def test_unverified_sync_pr_fails(self):
        self.branch("chore/harness-sync-x")
        write(self.repo / ".harness/version", "{}\n")
        write(self.repo / ".claude/rules/git-workflow.md", "new rules\n")
        r = self.evaluate(commit_all(self.repo), approved=True, sync_result=(False, ["不一致"]))
        self.assertFalse(r.ok)

    def test_sync_pr_outside_allowed_paths_is_not_verified(self):
        self.branch("chore/harness-sync-x")
        write(self.repo / ".harness/version", "{}\n")
        write(self.repo / "src/app.py", "evil\n")
        r = self.evaluate(commit_all(self.repo), sync_result=(True, ["一致"]))
        self.assertEqual(self.verifies, 0)
        self.assertFalse(r.ok)  # .harness/ はコア → 承認が必要

    def test_distributor_requires_approval_for_owned(self):
        repo = self.tmp / "dist"
        git(self.harness, "clone", "-q", str(self.harness), str(repo))
        for k, v in (("core.autocrlf", "false"), ("user.name", "t"), ("user.email", "t@e")):
            git(repo, "config", k, v)
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "feature/doc")
        f = repo / ".claude/skills/linear-worker/SKILL.md"
        write(f, f.read_text(encoding="utf-8") + "\n追記\n")
        head = commit_all(repo, "doc")
        r = pg.evaluate(repo, base, head, approval_fn=lambda: (False, "なし"),
                        sync_verify_fn=lambda: self.fail("配布元では同期検証しない"))
        self.assertFalse(r.ok)
        self.assertIn("人間の承認待ち", r.reason)
        r = pg.evaluate(repo, base, head, approval_fn=lambda: (True, "承認: alice"),
                        sync_verify_fn=lambda: (False, []))
        self.assertTrue(r.ok, r.reason)

        # 配布の定義・適用スクリプトもコア
        git(repo, "checkout", "-q", "-b", "feature/sync-script", base)
        f = repo / "scripts/sync/apply_sync.py"
        write(f, f.read_text(encoding="utf-8") + "\n# tweak\n")
        head = commit_all(repo, "script")
        r = pg.evaluate(repo, base, head, approval_fn=lambda: (False, "なし"), sync_verify_fn=lambda: (False, []))
        self.assertFalse(r.ok)
        self.assertIn("scripts/sync/apply_sync.py", "\n".join(r.details))


class SyncVerificationIntegrationTest(unittest.TestCase):
    """policy_gate → 配布元 clone の verify_harness_sync.py → apply_sync を実際に通す。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.harness, self.sha = make_harness(self.tmp / "harness")
        self.repo, self.base = make_consumer(self.tmp / "consumer", minimal_config())

    def tearDown(self):
        self._tmp.cleanup()

    def run_gate(self, head):
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), unittest.mock.patch("sys.stdout", new=out), \
                unittest.mock.patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": "", "GITHUB_STEP_SUMMARY": ""}):
            code = pg.main(["--repo-dir", str(self.repo), "--base", self.base, "--head", head,
                            "--harness-repo", HARNESS_REPO, "--harness-url", str(self.harness)])
        return code, out.getvalue()

    def test_first_sync_pr_passes_and_tampered_fails(self):
        git(self.repo, "checkout", "-q", "-b", "chore/harness-sync-1", self.base)
        with redirect_stderr(io.StringIO()):
            apply_sync.apply_sync(self.harness, self.repo, self.sha, HARNESS_REPO, "main")
        head = commit_all(self.repo, "sync")
        code, out = self.run_gate(head)
        self.assertEqual(code, 0, out)
        self.assertIn("同期 PR", out)

        f = self.repo / ".claude/skills/linear-worker/SKILL.md"
        write(f, f.read_text(encoding="utf-8") + "\nextra\n")
        head2 = commit_all(self.repo, "tamper")
        code, out = self.run_gate(head2)
        self.assertEqual(code, 1, out)


if __name__ == "__main__":
    unittest.main()
