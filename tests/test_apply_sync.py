from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

from _helpers import (CONFIG_REL as NEW, HARNESS_REPO, LEGACY_CONFIG_REL as LEGACY, commit_all, git, make_consumer,
                      make_harness, minimal_config, write)

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
        before = json.loads((self.target / LEGACY).read_text(encoding="utf-8"))
        r = self.apply()
        after = json.loads((self.target / NEW).read_text(encoding="utf-8"))
        self.assertEqual(r["config"], "migrated")
        self.assertNotEqual(before, after)
        self.assertIn(".kiro/orchestration/", after["auto_merge"]["protected_paths"])

    # ------------------------------------------------------------ config の置き場所の移動

    def test_legacy_only_config_moves_to_new_path(self):
        # .kiro/ の他の中身(SDD の資産)には触れない
        write(self.target / ".kiro/specs/feature-a/requirements.md", "# spec\n")
        commit_all(self.target, "spec")
        before = json.loads((self.target / LEGACY).read_text(encoding="utf-8"))
        r = self.apply()
        self.assertEqual(r["config_moved"], f"{LEGACY} -> {NEW}")
        self.assertFalse((self.target / LEGACY).exists())
        self.assertFalse((self.target / ".kiro/orchestration").exists())
        self.assertTrue((self.target / ".kiro/specs/feature-a/requirements.md").is_file())
        after = json.loads((self.target / NEW).read_text(encoding="utf-8"))
        self.assertEqual(after["github"]["repo"], before["github"]["repo"])  # 中身はそのまま(移行だけ適用)
        self.assertIn(NEW, after["auto_merge"]["protected_paths"])
        # git から見ると旧パスの削除と新パスの追加(git mv 相当。移行で中身も変わるので R になるとは限らない)
        git(self.target, "add", "-A")
        status = git(self.target, "diff", "--cached", "--name-status", "--no-renames", "--", LEGACY, NEW)
        self.assertEqual(sorted(status.splitlines()), sorted([f"A\t{NEW}", f"D\t{LEGACY}"]))

    def test_empty_kiro_dir_is_removed(self):
        self.apply()
        self.assertFalse((self.target / ".kiro").exists())

    def test_move_is_idempotent(self):
        self.apply()
        commit_all(self.target, "sync")
        tree1 = git(self.target, "rev-parse", "HEAD^{tree}")
        r = self.apply()
        self.assertNotIn("config_moved", r)
        git(self.target, "add", "-A")
        self.assertEqual(git(self.target, "write-tree"), tree1)

    def test_move_is_deferred_while_old_policy_gate_is_installed(self):
        # 同期前の policy_gate.py(base 側で同期 PR を判定する)が新パスを知らなければ、今回は移さない
        write(self.target / ".claude/skills/linear-worker/scripts/policy_gate.py",
              'CONFIG_REL = ".kiro/orchestration/config.json"\n')
        commit_all(self.target, "old harness")
        r = self.apply()
        self.assertNotIn("config_moved", r)
        self.assertTrue((self.target / LEGACY).is_file())
        self.assertFalse((self.target / NEW).exists())
        self.assertIn("次回の同期", self.stderr)
        self.assertEqual(r["config"], "migrated")  # 旧パスのまま移行は適用する
        # 次の同期(新しい policy_gate.py が入った後)で移す
        commit_all(self.target, "sync 1")
        r = self.apply()
        self.assertIn("config_moved", r)
        self.assertTrue((self.target / NEW).is_file())

    def test_move_is_skipped_while_other_files_reference_legacy_path(self):
        write(self.target / ".claude/skills/dev-orchestrator/SKILL.md",
              "confirmation_channel は .kiro/orchestration/config.json から読む\n")
        commit_all(self.target, "sdd")
        r = self.apply()
        self.assertNotIn("config_moved", r)
        self.assertTrue((self.target / LEGACY).is_file())
        self.assertIn(".claude/skills/dev-orchestrator/SKILL.md", self.stderr)

    def test_move_is_skipped_when_new_path_is_gitignored(self):
        write(self.target / ".gitignore", ".claude/*.json\n")
        commit_all(self.target, "ignore")
        r = self.apply()
        self.assertNotIn("config_moved", r)
        self.assertTrue((self.target / LEGACY).is_file())
        self.assertIn(".gitignore", self.stderr)

    def test_new_path_only_is_used(self):
        os.remove(self.target / LEGACY)
        write(self.target / NEW, json.dumps(minimal_config()) + "\n")
        commit_all(self.target, "new path")
        r = self.apply()
        self.assertNotIn("config_moved", r)
        self.assertEqual(r["config"], "migrated")
        self.assertFalse((self.target / LEGACY).exists())

    def test_both_paths_abort_without_writing(self):
        # 旧版のゲートは新パスをコアと見ないので、新パスはエージェントの PR で承認なしに入り得る。
        # 同期がそれを正として採用・移行しないよう、何も書かずに止めて人間に回す
        legacy_text = (self.target / LEGACY).read_text(encoding="utf-8")
        new_text = json.dumps(minimal_config()) + "\n"
        write(self.target / NEW, new_text)
        commit_all(self.target, "both")
        with self.assertRaises(SyncError) as cm:
            self.apply()
        self.assertIn("両方", str(cm.exception))
        self.assertIn("削除", str(cm.exception))
        self.assertEqual((self.target / LEGACY).read_text(encoding="utf-8"), legacy_text)
        self.assertEqual((self.target / NEW).read_text(encoding="utf-8"), new_text)
        self.assertFalse((self.target / ".harness/version").exists())
        self.assertFalse((self.target / ".claude/skills/linear-worker").exists())

    def test_both_paths_cli_reports_error(self):
        write(self.target / NEW, json.dumps(minimal_config()) + "\n")
        commit_all(self.target, "both")
        with redirect_stderr(io.StringIO()) as err, unittest.mock.patch("sys.stdout", new=io.StringIO()):
            code = apply_sync.main(["--harness", str(self.harness), "--target", str(self.target),
                                    "--harness-repo", HARNESS_REPO, "--ref", "main"])
        self.assertEqual(code, 1)
        self.assertIn("::error::", err.getvalue())

    def test_move_is_skipped_while_any_tracked_file_references_legacy_path(self):
        write(self.target / "tools/deploy.sh", "cat .kiro/orchestration/config.json\n")
        write(self.target / "docs/ops.md", "設定は `.kiro/orchestration/config.json`\n")
        commit_all(self.target, "own references")
        r = self.apply()
        self.assertNotIn("config_moved", r)
        self.assertTrue((self.target / LEGACY).is_file())
        self.assertIn("tools/deploy.sh", self.stderr)
        self.assertIn("docs/ops.md", self.stderr)

    def test_untracked_and_binary_files_do_not_block_move(self):
        (self.target / "blob.bin").write_bytes(b"\0.kiro/orchestration/config.json")
        commit_all(self.target, "binary")
        write(self.target / "scratch.txt", "cat .kiro/orchestration/config.json\n")  # 追跡していない
        r = self.apply()
        self.assertIn("config_moved", r)

    def test_no_config_is_absent(self):
        os.remove(self.target / LEGACY)
        commit_all(self.target, "no config")
        r = self.apply()
        self.assertEqual(r["config"], "absent")
        self.assertFalse((self.target / NEW).exists())

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
        for rel in (LEGACY, NEW):
            with self.subTest(rel=rel):
                if (self.target / LEGACY).exists():
                    os.remove(self.target / LEGACY)
                write(self.target / rel, "{ broken")
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
