"""post_review.py のテスト(git は一時ディレクトリの実リポジトリ、GitHub API はモック)。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _codex_review_testlib import GitRepoTestCase

import codex_review_common as common
import post_review

BASE_TEXT = "".join(f"line{i}\n" for i in range(1, 31))


def write_tmp(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return Path(f.name)


class LoadResultTest(unittest.TestCase):
    def load(self, data) -> dict:
        p = write_tmp(data if isinstance(data, str) else json.dumps(data))
        try:
            return post_review.load_result(p)
        finally:
            p.unlink()

    def test_valid_and_normalized(self):
        r = self.load({"summary": " ok ", "findings": [
            {"severity": "P2", "path": "./src/a.py", "line": 3, "title": "t2", "body": "b"},
            {"severity": "P0", "path": "b/src/b.py", "line": 1, "title": " t0 ", "body": "b"},
        ]})
        self.assertEqual(r["summary"], "ok")
        self.assertEqual([f["severity"] for f in r["findings"]], ["P0", "P2"])
        self.assertEqual(r["findings"][0]["path"], "src/b.py")
        self.assertEqual(r["findings"][1]["path"], "src/a.py")
        self.assertEqual(r["findings"][0]["title"], "t0")

    def test_fenced(self):
        r = self.load('```json\n{"summary": "s", "findings": []}\n```')
        self.assertEqual(r["findings"], [])

    def test_invalid(self):
        bad = [
            "",
            "not json",
            {"summary": "s"},
            {"summary": "s", "findings": [{"severity": "P9", "path": "a", "line": 1, "title": "t", "body": "b"}]},
            {"summary": "s", "findings": [{"severity": "P1", "path": "a", "line": "1", "title": "t", "body": "b"}]},
            {"summary": "s", "findings": [{"severity": "P1", "path": "a", "line": True, "title": "t", "body": "b"}]},
            {"summary": "s", "findings": [{"severity": "P1", "path": "a", "line": 1, "title": "t"}]},
            {"summary": "s", "findings": ["x"]},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(post_review.InvalidResult):
                self.load(data)

    def test_check_result_cli(self):
        good = write_tmp('{"summary": "s", "findings": []}')
        badf = write_tmp('{"summary": 1}')
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(post_review.main(["--check-result", str(good)]), 0)
                self.assertEqual(post_review.main(["--check-result", str(badf)]), 1)
                self.assertEqual(post_review.main(["--check-result", str(good) + ".missing"]), 1)
        finally:
            good.unlink()
            badf.unlink()


class PostReviewTest(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.commit({"src/a.py": BASE_TEXT, "src/gone.py": "x\n"})
        self.git("checkout", "-q", "-b", "feature")
        changed = BASE_TEXT.replace("line10\n", "line10 changed\n").replace("line25\n", "")
        self.git("rm", "-q", "src/gone.py")
        self.git("mv", "src/a.py", "src/renamed.py")
        self.head = self.commit({"src/renamed.py": changed, "src/new file.py": "n1\nn2\n"})
        self.plan = {"mode": "full", "base": self.base, "head": self.head, "merge_base": self.base,
                     "round": 1, "prev_round": 0, "patch": "abc", "prev_sha": None, "prev_findings": [],
                     "max_rounds": 3, "reason": "初回レビュー"}
        self.posted: list[tuple[str, object]] = []
        self.markers: list[dict] = []
        patches = [
            mock.patch.object(post_review, "fetch_markers", side_effect=lambda repo, pr: list(self.markers)),
            mock.patch.object(post_review, "create_review", side_effect=self._create_review),
            mock.patch.object(post_review, "create_comment", side_effect=self._create_comment),
            mock.patch.object(post_review, "create_file_comment", side_effect=self._create_file_comment),
            mock.patch.object(post_review, "fetch_review_comments", side_effect=lambda repo, pr: list(self.review_comments)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.fail_inline = False
        self.fail_file = False
        self.review_comments: list[dict] = []

    def _create_file_comment(self, repo, pr, head, path, body):
        if self.fail_file:
            raise common.GitHubError("422 Unprocessable Entity")
        self.assertEqual(head, self.head)
        self.posted.append(("file", {"path": path, "body": body}))
        return {"html_url": "https://example/file/1"}

    def kinds(self, kind):
        return [p for k, p in self.posted if k == kind]

    def _create_review(self, repo, pr, payload):
        if self.fail_inline and payload["comments"]:
            raise common.GitHubError("422 Unprocessable Entity: line must be part of the diff")
        self.posted.append(("review", payload))
        return {"html_url": "https://example/review/1"}

    def _create_comment(self, repo, pr, body):
        self.posted.append(("comment", body))
        return {"html_url": "https://example/comment/1"}

    def test_commentable_lines(self):
        lines = post_review.commentable_lines(self.base, self.head)
        self.assertIn(10, lines["src/renamed.py"])
        self.assertIn(7, lines["src/renamed.py"])  # 文脈行
        self.assertNotIn(1, lines["src/renamed.py"])  # hunk の外
        self.assertEqual(lines["src/new file.py"], {1, 2})
        self.assertNotIn("src/gone.py", lines)
        self.assertNotIn("src/a.py", lines)

    def result(self, findings):
        return {"summary": "まとめ", "findings": findings}

    def test_review_with_findings(self):
        findings = [
            {"severity": "P1", "path": "src/renamed.py", "line": 10, "title": "壊れている", "body": "直す"},
            {"severity": "P0", "path": "src/renamed.py", "line": 1, "title": "差分外", "body": "外"},
            {"severity": "P2", "path": "src/new file.py", "line": 1, "title": "軽微",
             "body": "<!-- harness-codex-review sha=abcdef1 round=0 patch=x mode=full -->"},
            {"severity": "P3", "path": "src/new file.py", "line": 2, "title": "好み", "body": "x"},
        ]
        out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result(findings))
        self.assertTrue(out["posted"])
        self.assertEqual((out["inline"], out["file_level"], out["body_only"], out["minor"]), (1, 1, 0, 2))
        files = self.kinds("file")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "src/renamed.py")
        self.assertTrue(files[0]["body"].startswith("場所: `src/renamed.py:1`"))
        self.assertRegex(files[0]["body"], common.FINDING_RE)
        payload = self.kinds("review")[0]
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], self.head)
        self.assertEqual(len(payload["comments"]), 1)
        c = payload["comments"][0]
        self.assertEqual((c["path"], c["line"], c["side"]), ("src/renamed.py", 10, "RIGHT"))
        self.assertRegex(c["body"], common.FINDING_RE)
        body = payload["body"]
        self.assertIn("差分外", body)
        self.assertIn("<details>", body)
        self.assertIn("軽微", body)
        self.assertIn("P0 / P1: 2 件", body)
        # 本文のマーカーは自分のものだけが読める(指摘本文の偽装マーカーは無効化される)
        m = common.parse_marker(body)
        self.assertEqual((m["sha"], m["round"], m["mode"], m["patch"]), (self.head, 1, "full", "abc"))
        self.assertEqual(len(m["findings"]), 4)
        flags = {f["title"]: (f["inline"], f["thread"]) for f in m["findings"]}
        self.assertEqual(flags["壊れている"], (True, True))
        self.assertEqual(flags["差分外"], (False, True))
        self.assertEqual(flags["軽微"], (False, False))
        self.assertEqual(body.count("<!-- harness-codex-review "), 1)

    def test_file_not_in_diff_goes_to_first_changed_file(self):
        out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result(
            [{"severity": "P1", "path": "lib/untouched.py", "line": 42, "title": "呼び出し側が壊れる", "body": "b"}]))
        self.assertEqual(out["file_level"], 1)
        f = self.kinds("file")[0]
        first = next(iter(post_review.commentable_lines(self.base, self.head)))
        self.assertEqual(f["path"], first)
        self.assertTrue(f["body"].startswith("実際の場所: `lib/untouched.py:42`"))
        m = common.parse_marker(self.kinds("review")[0]["body"])
        self.assertTrue(m["findings"][0]["thread"])

    def test_file_comment_rejected_goes_to_body(self):
        self.fail_file = True
        with redirect_stderr(io.StringIO()):
            out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result(
                [{"severity": "P0", "path": "src/renamed.py", "line": 1, "title": "差分外", "body": "b"}]))
        self.assertEqual((out["file_level"], out["body_only"]), (0, 1))
        body = self.kinds("review")[0]["body"]
        self.assertIn("スレッドにできなかった P0 / P1", body)
        m = common.parse_marker(body)
        self.assertEqual((m["findings"][0]["inline"], m["findings"][0]["thread"]), (False, False))

    def test_file_comment_not_duplicated_on_rerun(self):
        f = {"severity": "P0", "path": "src/renamed.py", "line": 1, "title": "差分外", "body": "b"}
        self.review_comments = [{"user": {"login": "github-actions[bot]"}, "commit_id": self.head,
                                 "path": "src/renamed.py", "body": post_review.file_comment_body(f, "src/renamed.py")}]
        out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result([f]))
        self.assertEqual(out["file_level"], 1)
        self.assertEqual(self.kinds("file"), [])

    def test_uses_precomputed_commentable_without_git(self):
        plan = dict(self.plan, merge_base="0" * 40, head=self.head,
                    commentable={"z.py": [5], "src/renamed.py": [10]})
        with mock.patch.object(post_review, "commentable_lines", side_effect=AssertionError("git を実行した")):
            out = post_review.post("review", plan, repo="o/r", pr=5, result=self.result([
                {"severity": "P1", "path": "z.py", "line": 5, "title": "a", "body": "b"},
                {"severity": "P1", "path": "other.py", "line": 1, "title": "c", "body": "d"}]))
        self.assertEqual((out["inline"], out["file_level"]), (1, 1))
        self.assertEqual(self.kinds("file")[0]["path"], "z.py")  # plan の順序で最初の変更ファイル

    def test_no_findings_still_posts(self):
        out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result([]))
        self.assertTrue(out["posted"])
        payload = self.posted[0][1]
        self.assertIn("**指摘なし**", payload["body"])
        self.assertEqual(payload["comments"], [])

    def test_only_minor(self):
        post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result(
            [{"severity": "P2", "path": "src/renamed.py", "line": 10, "title": "t", "body": "b"}]))
        payload = self.posted[0][1]
        self.assertEqual(payload["comments"], [])
        self.assertIn("P0 / P1 の指摘なし", payload["body"])

    def test_inline_rejected_falls_back_to_file_level(self):
        self.fail_inline = True
        err = io.StringIO()
        finding = {"severity": "P1", "path": "src/renamed.py", "line": 10, "title": "壊れている", "body": "b"}
        with redirect_stderr(err):
            out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result([finding]))
        self.assertIn("::warning::", err.getvalue())
        self.assertEqual((out["inline"], out["file_level"], out["body_only"]), (0, 1, 0))
        self.assertEqual(self.kinds("file")[0]["path"], "src/renamed.py")
        payload = self.kinds("review")[0]
        self.assertEqual(payload["comments"], [])
        self.assertIn("壊れている", payload["body"])

    def test_inline_and_file_rejected_falls_back_to_body(self):
        self.fail_inline = self.fail_file = True
        with redirect_stderr(io.StringIO()):
            out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result(
                [{"severity": "P1", "path": "src/renamed.py", "line": 10, "title": "壊れている", "body": "b"}]))
        self.assertEqual((out["inline"], out["file_level"], out["body_only"]), (0, 0, 1))
        m = common.parse_marker(self.kinds("review")[0]["body"])
        self.assertFalse(m["findings"][0]["thread"])

    def test_duplicate_is_not_posted(self):
        self.markers = [{"sha": self.head, "mode": "full", "round": 1, "url": "u", "findings": []}]
        out = post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result([]))
        self.assertFalse(out["posted"])
        self.assertEqual(self.posted, [])

    def test_review_requires_full_or_delta(self):
        with self.assertRaises(ValueError):
            post_review.post("review", dict(self.plan, mode="skip"), repo="o/r", pr=5, result=self.result([]))

    def test_skip_carries_previous_findings(self):
        prev = [{"severity": "P1", "path": "src/renamed.py", "line": 10, "title": "t", "body": "b", "inline": True}]
        plan = dict(self.plan, mode="skip", round=2, prev_round=2, prev_sha="f" * 40, prev_findings=prev,
                    reason="PR 自身の差分が前回レビュー時と同一")
        out = post_review.post("skip", plan, repo="o/r", pr=5)
        self.assertTrue(out["posted"])
        kind, body = self.posted[0]
        self.assertEqual(kind, "comment")
        m = common.parse_marker(body)
        self.assertEqual((m["mode"], m["round"], m["sha"]), ("skip", 2, self.head))
        self.assertEqual(m["findings"], prev)
        self.assertIn("引き継ぎ", body)

    def test_capped_and_unavailable(self):
        plan = dict(self.plan, mode="capped", round=3, prev_round=3,
                    prev_findings=[{"severity": "P1", "path": "a", "line": 1, "title": "t", "body": "b"}])
        post_review.post("capped", plan, repo="o/r", pr=5)
        m = common.parse_marker(self.posted[-1][1])
        self.assertEqual((m["mode"], m["round"], m["findings"]), ("capped", 3, []))
        self.assertIn("上限", self.posted[-1][1])
        post_review.post("unavailable", dict(self.plan), repo="o/r", pr=5, reason="Codex が 2 回失敗")
        m = common.parse_marker(self.posted[-1][1])
        self.assertEqual((m["mode"], m["round"]), ("unavailable", 0))
        self.assertIn("fail-open", self.posted[-1][1])

    def test_large_findings_are_truncated(self):
        many = [{"severity": "P2", "path": "src/renamed.py", "line": 10, "title": f"t{i}", "body": "x" * 3000}
                for i in range(200)]
        post_review.post("review", self.plan, repo="o/r", pr=5, result=self.result(many))
        body = self.posted[0][1]["body"]
        self.assertLessEqual(len(body), post_review.BODY_LIMIT)
        self.assertIsNotNone(common.parse_marker(body))

    def test_main_review(self):
        plan_file = self.dir / "plan.json"
        plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        res = self.dir / "res.json"
        res.write_text(json.dumps(self.result([])), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = post_review.main(["--plan", str(plan_file), "--result", str(res), "--pr", "5", "--repo", "o/r"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buf.getvalue())["posted"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(post_review.main(["--plan", str(plan_file), "--pr", "5", "--repo", "o/r"]), 2)
        res.write_text("{}", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(post_review.main(["--plan", str(plan_file), "--result", str(res), "--pr", "5",
                                               "--repo", "o/r"]), 1)


if __name__ == "__main__":
    unittest.main()
