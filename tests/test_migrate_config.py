"""migrate_config.py の 2026-11-01 移行のテスト。"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
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
        self.assertEqual(len(log), 3)

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


if __name__ == "__main__":
    unittest.main()
