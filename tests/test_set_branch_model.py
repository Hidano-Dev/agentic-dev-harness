"""set_branch_model.py / render_config.py のブランチ設定のテスト。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sbm = load("set_branch_model", "scripts/sync/set_branch_model.py")
rc = load("render_config", "scripts/onboard/render_config.py")
TEMPLATE = json.loads((ROOT / ".claude/skills/linear-worker/templates/orchestration-config.json").read_text(encoding="utf-8"))


class SetBranchModelTest(unittest.TestCase):
    def test_sets_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".kiro/orchestration/config.json"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"github": {"repo": "o/r", "default_branch": "main"}}), encoding="utf-8")
            self.assertEqual(sbm.main(["--target", d, "--default-branch", "develop", "--release-branch", "main"]), 0)
            cfg = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(cfg["github"], {"repo": "o/r", "default_branch": "develop", "release_branch": "main"})
            self.assertFalse(sbm.set_branch_model(cfg, "develop", "main"))

    def test_same_branch_means_single(self):
        cfg = {"github": {}}
        sbm.set_branch_model(cfg, "main", "main")
        self.assertIsNone(cfg["github"]["release_branch"])

    def test_no_config_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sbm.main(["--target", d, "--default-branch", "develop"]), 0)


class RenderConfigBranchTest(unittest.TestCase):
    def test_single_branch_by_default(self):
        cfg = rc.render(TEMPLATE, repo="o/r", default_branch="main", team="T", review_bot="codex")
        self.assertEqual(cfg["github"]["default_branch"], "main")
        self.assertIsNone(cfg["github"]["release_branch"])
        self.assertEqual(cfg["review"]["provider"], "action")

    def test_two_tier(self):
        cfg = rc.render(TEMPLATE, repo="o/r", default_branch="develop", team="T", review_bot="none",
                        release_branch="main")
        self.assertEqual(cfg["github"]["release_branch"], "main")
        self.assertIsNone(cfg["review"]["provider"])
        self.assertIsNone(cfg["review"]["bot"])


if __name__ == "__main__":
    unittest.main()
