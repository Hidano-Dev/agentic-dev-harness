"""migrate_config.py の 2026-11-01 以降の移行のテスト。"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".claude/skills/linear-worker/scripts/migrate_config.py"
TEMPLATE = ROOT / ".claude/skills/linear-worker/templates/orchestration-config.json"

spec = importlib.util.spec_from_file_location("migrate_config", SCRIPT)
mc = importlib.util.module_from_spec(spec)
sys.modules["migrate_config"] = mc
spec.loader.exec_module(mc)

OLD_PROTECTED = [".claude/", ".github/", ".kiro/settings/", ".kiro/orchestration/",
                 "CLAUDE.md", "AGENTS.md", ".agents/", ".codex/"]
OLD_APPLIED = ["2026-10-01-auto-merge-enabled", "2026-10-01-protect-orchestration-config",
               "2026-10-02-protect-root-instructions"]


def old_config(**over):
    cfg = {
        "github": {"repo": "o/r", "default_branch": "main"},
        "review": {"bot": {"name": "codex", "request_comment": "@codex review"}, "wait_minutes": 60},
        "auto_merge": {"enabled": True, "protected_paths": list(OLD_PROTECTED)},
        "applied_migrations": list(OLD_APPLIED),
    }
    cfg.update(over)
    return cfg


class MigrationTest(unittest.TestCase):
    def test_template_lists_all_migrations(self):
        tpl = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(tpl["applied_migrations"]), sorted(m for m, _ in mc.MIGRATIONS))

    def test_template_is_fixed_point(self):
        tpl = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        before = json.dumps(tpl, sort_keys=True)
        tpl["applied_migrations"] = []
        mc.migrate(tpl)
        tpl.pop("applied_migrations")
        orig = json.loads(before)
        orig.pop("applied_migrations")
        self.assertEqual(tpl, orig)

    def test_old_config_is_tiered(self):
        cfg = old_config()
        log, warnings = mc.migrate(cfg)
        self.assertEqual(warnings, [])
        self.assertEqual(cfg["review"]["provider"], "action")
        self.assertIsNone(cfg["github"]["release_branch"])
        self.assertEqual(cfg["github"]["default_branch"], "main")
        paths = cfg["auto_merge"]["protected_paths"]
        for p in ["CLAUDE.md", "AGENTS.md", ".claude/", ".agents/"]:
            self.assertNotIn(p, paths)
        for p in mc.CORE_PROTECTED_PATHS:
            self.assertIn(p, paths)
        # config の新しい置き場所も保護する。旧パスも残す(旧パスで動くリポジトリのため)
        self.assertIn(".claude/orchestration.json", paths)
        self.assertIn(".kiro/orchestration/", paths)
        self.assertEqual(len(log), 4)

    def test_custom_paths_are_kept(self):
        cfg = old_config(auto_merge={"enabled": True, "protected_paths": OLD_PROTECTED + ["Assets/Secret/"]})
        mc.migrate(cfg)
        self.assertIn("Assets/Secret/", cfg["auto_merge"]["protected_paths"])

    def test_no_review_bot_means_no_provider(self):
        cfg = old_config(review={"bot": None})
        mc.migrate(cfg)
        self.assertIsNone(cfg["review"]["provider"])

    def test_idempotent(self):
        cfg = old_config()
        mc.migrate(cfg)
        snapshot = json.dumps(cfg, sort_keys=True)
        log, _ = mc.migrate(cfg)
        self.assertEqual(log, [])
        self.assertEqual(json.dumps(cfg, sort_keys=True), snapshot)

    def test_enable_auto_merge_after_tiering(self):
        cfg = {"auto_merge": {"enabled": False, "protected_paths": list(mc.CORE_PROTECTED_PATHS)},
               "applied_migrations": []}
        _, warnings = mc.migrate(cfg)
        self.assertEqual(warnings, [])
        self.assertTrue(cfg["auto_merge"]["enabled"])


class ProtectClaudeOrchestrationTest(unittest.TestCase):
    MID = "2026-11-26-protect-claude-orchestration-config"

    def run_one(self, cfg):
        cfg.setdefault("applied_migrations", [m for m, _ in mc.MIGRATIONS if m != self.MID])
        log, warnings = mc.migrate(cfg)
        self.assertEqual(warnings, [])
        self.assertIn(self.MID, cfg["applied_migrations"])
        return log

    def test_adds_new_path_and_keeps_legacy(self):
        cfg = {"auto_merge": {"protected_paths": list(mc.CORE_PROTECTED_PATHS) + ["Assets/X/"]}}
        log = self.run_one(cfg)
        paths = cfg["auto_merge"]["protected_paths"]
        self.assertEqual(paths, list(mc.CORE_PROTECTED_PATHS) + ["Assets/X/", ".claude/orchestration.json"])
        self.assertEqual(len(log), 1)

    def test_already_covered_by_claude_dir(self):
        cfg = {"auto_merge": {"protected_paths": [".claude/", ".github/"]}}
        log = self.run_one(cfg)
        self.assertEqual(cfg["auto_merge"]["protected_paths"], [".claude/", ".github/"])
        self.assertEqual(log, [])

    def test_missing_key_writes_core_default(self):
        cfg = {"auto_merge": {}}
        self.run_one(cfg)
        self.assertEqual(cfg["auto_merge"]["protected_paths"],
                         list(mc.CORE_PROTECTED_PATHS) + [".claude/orchestration.json"])

    def test_not_a_list_is_retried(self):
        cfg = {"auto_merge": {"protected_paths": "x"},
               "applied_migrations": [m for m, _ in mc.MIGRATIONS if m != self.MID]}
        _, warnings = mc.migrate(cfg)
        self.assertTrue(any(self.MID in w for w in warnings))
        self.assertNotIn(self.MID, cfg["applied_migrations"])


class MainResolvesConfigPathTest(unittest.TestCase):
    """引数なしの migrate_config.py は新パス優先・無ければ旧パスの config を移行する。"""

    def run_main(self, root: Path, *rels: str):
        for rel in rels:
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"auto_merge": {"protected_paths": [".github/"]}, "applied_migrations": []}),
                         encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(mc.main([]), 0)
        finally:
            os.chdir(cwd)
        return err.getvalue()

    def migrated(self, f: Path) -> bool:
        return bool(json.loads(f.read_text(encoding="utf-8"))["applied_migrations"])

    def test_legacy_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.run_main(root, ".kiro/orchestration/config.json")
            self.assertTrue(self.migrated(root / ".kiro/orchestration/config.json"))
            self.assertFalse((root / ".claude/orchestration.json").exists())

    def test_new_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.run_main(root, ".claude/orchestration.json")
            self.assertTrue(self.migrated(root / ".claude/orchestration.json"))

    def test_both_prefers_new_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            err = self.run_main(root, ".claude/orchestration.json", ".kiro/orchestration/config.json")
            self.assertTrue(self.migrated(root / ".claude/orchestration.json"))
            self.assertFalse(self.migrated(root / ".kiro/orchestration/config.json"))
            self.assertIn("::warning::", err)


if __name__ == "__main__":
    unittest.main()
