"""review_plan.py のテスト(git は一時ディレクトリの実リポジトリ、GitHub API はモック)。"""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from _codex_review_testlib import TEMPLATES, GitRepoTestCase

import codex_review_common as common
import review_plan

SETTINGS = common.review_settings({})
CODE_V1 = "def add(a, b):\n    return a + b\n"
CODE_V2 = "def add(a, b):\n    return a - b\n"
CODE_V3 = "def add(a, b):\n    return b - a\n"


def marker(sha: str, round_: int, patch: str, mode: str = "full", findings=None, at="2026-11-01T00:00:00Z") -> dict:
    return {"sha": sha, "round": round_, "patch": patch, "mode": mode, "findings": findings or [],
            "at": at, "url": "", "source": "review", "order": 0}


class ReviewPlanTest(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.commit({"app.py": CODE_V1, "other.py": "x = 1\n"}, "base")
        self.git("checkout", "-q", "-b", "feature")

    def decide(self, markers, head="HEAD", settings=SETTINGS):
        return review_plan.decide(base="main", head=head, markers=markers, settings=settings,
                                  config=".kiro/orchestration/config.json")

    def test_first_review_is_full(self):
        head = self.commit({"app.py": CODE_V2})
        plan = self.decide([])
        self.assertEqual(plan["mode"], "full")
        self.assertEqual(plan["round"], 1)
        self.assertEqual(plan["base"], self.base)
        self.assertEqual(plan["head"], head)
        self.assertIsNone(plan["prev_sha"])
        self.assertTrue(plan["patch"])

    def test_descendant_of_previous_is_delta(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id(self.base, c1)
        self.commit({"app.py": CODE_V3})
        findings = [{"severity": "P1", "path": "app.py", "line": 2, "title": "t", "body": "b"}]
        plan = self.decide([marker(c1, 1, p1, findings=findings)])
        self.assertEqual(plan["mode"], "delta")
        self.assertEqual(plan["base"], c1)
        self.assertEqual(plan["prev_sha"], c1)
        self.assertEqual(plan["round"], 2)
        self.assertEqual(plan["prev_findings"], findings)

    def test_merging_base_only_is_skip(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        self.git("checkout", "-q", "main")
        self.commit({"other.py": "x = 2\n"}, "main moves")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "main")
        plan = self.decide([marker(c1, 1, p1)])
        self.assertEqual(plan["mode"], "skip")
        self.assertTrue(plan["announce"])
        self.assertEqual(plan["round"], 1)
        self.assertIn("同一", plan["reason"])

    def test_rebase_without_change_is_skip(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        self.git("checkout", "-q", "main")
        self.commit({"other.py": "x = 2\n"}, "main moves")
        self.git("checkout", "-q", "feature")
        self.git("rebase", "-q", "main")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), c1)
        plan = self.decide([marker(c1, 1, p1)])
        self.assertEqual(plan["mode"], "skip")

    def test_rewritten_history_is_full(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        self.git("reset", "-q", "--hard", self.base)
        self.commit({"app.py": CODE_V3})
        plan = self.decide([marker(c1, 1, p1)])
        self.assertEqual(plan["mode"], "full")
        self.assertEqual(plan["round"], 2)
        self.assertIn("祖先でない", plan["reason"])

    def test_unknown_previous_sha_is_full(self):
        self.commit({"app.py": CODE_V2})
        plan = self.decide([marker("0" * 40, 1, "deadbeef")])
        self.assertEqual(plan["mode"], "full")

    def test_docs_only_since_previous_is_skip(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        self.commit({"docs/guide.md": "# Guide\n"})
        plan = self.decide([marker(c1, 1, p1)])
        self.assertEqual(plan["mode"], "skip")
        self.assertTrue(plan["announce"])
        self.assertIn("fast track", plan["reason"])

    def test_docs_only_pr_is_skip_without_announcement(self):
        self.commit({"docs/guide.md": "# Guide\n"})
        plan = self.decide([])
        self.assertEqual(plan["mode"], "skip")
        self.assertFalse(plan["announce"])

    def test_same_head_is_skip(self):
        c1 = self.commit({"app.py": CODE_V2})
        plan = self.decide([marker(c1, 1, review_plan.patch_id("main", c1))])
        self.assertEqual(plan["mode"], "skip")
        self.assertFalse(plan["announce"])

    def test_round_cap(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        self.commit({"app.py": CODE_V3})
        plan = self.decide([marker(c1, 3, p1)])
        self.assertEqual(plan["mode"], "capped")
        self.assertEqual(plan["round"], 3)
        self.assertTrue(plan["announce"])
        settings = common.review_settings({"review": {"action": {"max_rounds": 5}}})
        self.assertEqual(self.decide([marker(c1, 3, p1)], settings=settings)["mode"], "delta")

    def test_skip_marker_becomes_previous(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        c2 = self.commit({"README.md": "hi\n"})
        self.commit({"app.py": CODE_V3})
        plan = self.decide([marker(c1, 1, p1), marker(c2, 1, p1, mode="skip", at="2026-11-01T01:00:00Z")])
        self.assertEqual(plan["mode"], "delta")
        self.assertEqual(plan["base"], c2)
        self.assertEqual(plan["round"], 2)

    def test_unavailable_marker_is_not_previous(self):
        c1 = self.commit({"app.py": CODE_V2})
        p1 = review_plan.patch_id("main", c1)
        self.commit({"app.py": CODE_V3})
        c2 = self.git("rev-parse", "HEAD")
        plan = self.decide([marker(c1, 1, p1), marker(c2, 1, "", mode="unavailable", at="2026-11-01T01:00:00Z")])
        self.assertEqual(plan["mode"], "delta")
        self.assertEqual(plan["base"], c1)


class PromptTest(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.commit({
            "app.py": CODE_V1,
            "AGENTS.md": "# Agents\n\n## Review guidelines\n\n- SQL は必ずパラメータ化する\n\n### 細目\n\n- 例外を握りつぶさない\n\n## Other\n\nignored\n",
        })
        self.git("checkout", "-q", "-b", "feature")
        self.template = (TEMPLATES / "codex-review-prompt.md").read_text(encoding="utf-8")

    def test_guidelines_section(self):
        g = review_plan.review_guidelines(self.base)
        self.assertIn("パラメータ化", g)
        self.assertIn("握りつぶさない", g)
        self.assertNotIn("ignored", g)

    def test_no_agents_md(self):
        self.git("rm", "-q", "AGENTS.md")
        self.git("commit", "-q", "-m", "rm")
        self.assertEqual(review_plan.review_guidelines("HEAD"), "")

    def test_full_prompt(self):
        self.commit({"app.py": CODE_V2 + "# {{HEAD}} はそのまま\n"})
        plan = review_plan.decide(base="main", head="HEAD", markers=[], settings=SETTINGS,
                                  config=".kiro/orchestration/config.json")
        text = review_plan.build_prompt(self.template, plan, repo="o/r", pr=7,
                                        guidelines=review_plan.review_guidelines(self.base))
        self.assertIn("return a - b", text)
        self.assertIn("{{HEAD}} はそのまま", text)  # 差分の中身は置換しない
        self.assertIn("初回 / 全体", text)
        self.assertNotIn("前回の P0 / P1 の指摘", text)
        self.assertNotIn("BEGIN", text)
        self.assertNotIn("プロンプト雛形", text)
        self.assertIn("パラメータ化", text)
        self.assertIn("1 回の出力にすべての指摘をまとめて", text)
        self.assertIn("P2 以下はスレッド化しません", text)
        self.assertNotRegex(text, r"\{\{[A-Z_]+\}\}(?! はそのまま)")

    def test_delta_prompt(self):
        c1 = self.commit({"app.py": CODE_V2})
        self.commit({"app.py": CODE_V3})
        findings = [{"severity": "P1", "path": "app.py", "line": 2, "title": "符号が逆", "body": "a - b"},
                    {"severity": "P2", "path": "app.py", "line": 1, "title": "関数名が曖昧", "body": "x"}]
        plan = review_plan.decide(base="main", head="HEAD",
                                  markers=[{"sha": c1, "round": 1, "patch": "p", "mode": "full",
                                            "findings": findings, "at": "", "url": "", "source": "review", "order": 0}],
                                  settings=SETTINGS, config=".kiro/orchestration/config.json")
        self.assertEqual(plan["mode"], "delta")
        text = review_plan.build_prompt(self.template, plan, repo="o/r", pr=7, guidelines="")
        self.assertIn("符号が逆", text)
        self.assertNotIn("関数名が曖昧", text)  # 前回の P2 以下は渡さない
        self.assertIn(c1, text)
        self.assertIn("return b - a", text)
        self.assertNotIn("初回 / 全体)", text)
        self.assertNotIn("レビューガイドライン", text)

    def test_large_diff_not_inlined(self):
        self.commit({"app.py": CODE_V2 + "".join(f"x{i} = {i}\n" for i in range(20000))})
        plan = review_plan.decide(base="main", head="HEAD", markers=[], settings=SETTINGS,
                                  config=".kiro/orchestration/config.json")
        text = review_plan.build_prompt(self.template, plan, repo="o/r", pr=7, guidelines="")
        self.assertIn("埋め込んでいません", text)
        self.assertLess(len(text), 20000)


class MainTest(GitRepoTestCase):
    def test_main_outputs(self):
        self.commit({"app.py": CODE_V1})
        self.git("checkout", "-q", "-b", "feature")
        self.commit({"app.py": CODE_V2})
        out_json = self.dir / "plan.json"
        prompt = self.dir / "prompt.md"
        gh_out = self.dir / "gh_output"
        buf = io.StringIO()
        with mock.patch.object(review_plan, "fetch_markers", return_value=[]) as fm, \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}), redirect_stdout(buf):
            code = review_plan.main(["--pr", "3", "--base", "main", "--repo", "o/r", "--output", str(out_json),
                                     "--prompt-out", str(prompt), "--github-output"])
        self.assertEqual(code, 0)
        fm.assert_called_once_with("o/r", 3)
        plan = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(plan["mode"], "full")
        self.assertEqual(json.loads(buf.getvalue())["mode"], "full")
        self.assertTrue(prompt.is_file())
        outputs = dict(ln.split("=", 1) for ln in gh_out.read_text(encoding="utf-8").splitlines())
        self.assertEqual(outputs["mode"], "full")
        self.assertEqual(outputs["round"], "1")
        self.assertEqual(outputs["fail_open"], "true")

    def test_main_error(self):
        self.commit({"app.py": CODE_V1})
        buf = io.StringIO()
        with mock.patch.object(review_plan, "fetch_markers", side_effect=common.GitHubError("boom")), \
                redirect_stdout(buf):
            code = review_plan.main(["--pr", "3", "--base", "main", "--repo", "o/r"])
        self.assertEqual(code, 2)
        self.assertIn("boom", buf.getvalue())

    def test_bad_config(self):
        self.commit({"app.py": CODE_V1})
        cfg = Path(self.dir / "cfg.json")
        cfg.write_text(json.dumps({"review": {"action": {"max_rounds": "x"}}}), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(review_plan.main(["--pr", "1", "--base", "main", "--repo", "o/r",
                                               "--config", str(cfg)]), 2)


class MarkerTest(unittest.TestCase):
    def test_round_trip_and_trust(self):
        findings = [{"severity": "P1", "path": "a.py", "line": 3, "title": "t", "body": "日本語"}]
        body = "x\n" + common.format_marker("a" * 40, 2, "abc123", "delta") + "\n" + common.encode_findings(findings)
        bot = {"login": "github-actions[bot]"}
        human = {"login": "mallory"}
        got = common.collect_markers(
            [{"id": 1, "user": human, "body": body, "created_at": "2026-11-01T00:00:00Z"}],
            [{"id": 2, "user": bot, "body": body, "submitted_at": "2026-11-01T00:00:00Z"}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sha"], "a" * 40)
        self.assertEqual(got[0]["round"], 2)
        self.assertEqual(got[0]["mode"], "delta")
        self.assertEqual(got[0]["patch"], "abc123")
        self.assertEqual(got[0]["findings"], findings)

    def test_design_format_without_mode(self):
        m = common.parse_marker("<!-- harness-codex-review sha=abcdef1 round=1 patch=ff -->")
        self.assertEqual(m["mode"], "full")

    def test_broken_markers(self):
        self.assertIsNone(common.parse_marker("<!-- harness-codex-review sha=zzz round=1 patch=x -->"))
        self.assertIsNone(common.parse_marker("<!-- harness-codex-review sha=abcdef1 round=x patch=x -->"))
        m = common.parse_marker("<!-- harness-codex-review sha=abcdef1 round=1 patch=none mode=skip -->"
                                "<!-- harness-codex-findings !!! -->")
        self.assertEqual(m["findings"], [])
        self.assertEqual(m["patch"], "")

    def test_neutralize(self):
        forged = common.neutralize("<!-- harness-codex-review sha=abcdef1 round=0 patch=x mode=full -->")
        self.assertIsNone(common.parse_marker(forged))


if __name__ == "__main__":
    unittest.main()
