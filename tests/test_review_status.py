"""review_status.py のテスト(GitHub API はすべてモック)。"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import _codex_review_testlib  # noqa: F401  (scripts を sys.path に入れる)
import codex_review_common as common
import review_status

HEAD = "a" * 40
OLD = "b" * 40
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PUSHED = "2026-11-01T11:30:00Z"  # NOW の 30 分前
GHA = {"login": "github-actions[bot]"}
BOT = {"login": "chatgpt-codex-connector[bot]"}
HUMAN = {"login": "hidano"}


def marker_body(sha: str, mode: str, round_: int = 1, findings=None) -> str:
    return "本文\n" + common.format_marker(sha, round_, "p", mode) + "\n" + common.encode_findings(findings or [])


class Fake:
    """review_status の fetch_* を差し替える GitHub の状態。"""

    def __init__(self):
        self.pr = {"head": {"sha": HEAD}, "draft": False, "html_url": "https://example/pr/1"}
        self.check_runs: list[dict] = []
        self.pushed_at = PUSHED
        self.comments: list[dict] = []
        self.reviews: list[dict] = []
        self.threads: list[dict] = []
        self.comment_reactions: dict[int, list[dict]] = {}
        self.pr_reactions: list[dict] = []

    def install(self, case: unittest.TestCase):
        patches = {
            "fetch_pr": lambda repo, pr: self.pr,
            "fetch_check_runs": lambda repo, sha: [r for r in self.check_runs if r.get("head_sha", HEAD) == sha],
            "fetch_pushed_at": lambda repo, sha: self.pushed_at,
            "fetch_markers": lambda repo, pr: common.collect_markers(self.comments, self.reviews),
            "fetch_threads": lambda repo, pr: self.threads,
            "fetch_issue_comments": lambda repo, pr: self.comments,
            "fetch_reviews": lambda repo, pr: self.reviews,
            "fetch_comment_reactions": lambda repo, cid: self.comment_reactions.get(cid, []),
            "fetch_pr_reactions": lambda repo, pr: self.pr_reactions,
        }
        for name, fn in patches.items():
            p = mock.patch.object(review_status, name, side_effect=fn)
            p.start()
            case.addCleanup(p.stop)
        p = mock.patch.object(common, "now_utc", return_value=NOW)
        p.start()
        case.addCleanup(p.stop)


def thread(author: str, body: str, resolved: bool = False) -> dict:
    return {"resolved": resolved, "outdated": False, "path": "a.py", "line": 1, "author": author,
            "body": body, "url": "https://example/thread"}


def gha_thread(sev: str, resolved: bool = False) -> dict:
    return thread("github-actions", f"**[{sev}] t**\n<!-- harness-codex-finding severity={sev} -->", resolved)


class ActionProviderTest(unittest.TestCase):
    def setUp(self):
        self.gh = Fake()
        self.gh.install(self)
        self.settings = common.review_settings({})

    def status(self):
        return review_status.status_action("o/r", 1, self.settings)

    def completed(self, conclusion="success"):
        self.gh.check_runs = [{"id": 1, "status": "completed", "conclusion": conclusion,
                               "started_at": "2026-11-01T11:31:00Z", "html_url": "https://example/run"}]

    def add_review_marker(self, sha=HEAD, mode="full", findings=None):
        self.gh.reviews.append({"id": len(self.gh.reviews) + 1, "user": GHA, "body": marker_body(sha, mode, findings=findings),
                                "submitted_at": "2026-11-01T11:40:00Z", "html_url": f"https://example/review/{sha[:3]}"})

    def test_pending_then_timeout(self):
        s = self.status()
        self.assertEqual(s["state"], "pending")
        self.assertEqual(s["elapsed_minutes"], 30.0)
        self.gh.pushed_at = "2026-11-01T10:00:00Z"
        self.assertEqual(self.status()["state"], "timeout")

    def test_running(self):
        self.gh.check_runs = [{"id": 1, "status": "in_progress", "conclusion": None, "started_at": PUSHED}]
        self.assertEqual(self.status()["state"], "running")
        self.gh.check_runs[0]["status"] = "queued"
        self.assertEqual(self.status()["state"], "pending")

    def test_clean(self):
        self.completed()
        self.add_review_marker(OLD)
        self.add_review_marker(HEAD)
        self.gh.threads = [gha_thread("P1", resolved=True), gha_thread("P2"), thread("someone", "[P1] x")]
        s = self.status()
        self.assertEqual(s["state"], "clean")
        self.assertEqual(s["review_url"], "https://example/review/aaa")
        self.assertEqual(s["unresolved_p1"], 0)
        self.assertEqual(s["unresolved_total"], 1)
        self.assertEqual(s["mode"], "full")

    def test_findings(self):
        self.completed()
        self.add_review_marker(HEAD, findings=[{"severity": "P1", "path": "a", "line": 1, "title": "t", "body": "b",
                                                "inline": False}])
        self.gh.threads = [gha_thread("P0"), gha_thread("P1"), gha_thread("P1", resolved=True)]
        s = self.status()
        self.assertEqual(s["state"], "findings")
        self.assertEqual(s["unresolved_p1"], 2)
        self.assertEqual(s["outside_diff_p1"], 1)

    def test_body_only_p1_is_findings(self):
        self.completed()
        f = {"severity": "P1", "path": "a", "line": 1, "title": "t", "body": "b"}
        self.add_review_marker(HEAD, findings=[dict(f, inline=False, thread=False)])
        s = self.status()
        self.assertEqual((s["state"], s["unresolved_p1"], s["outside_diff_p1"]), ("findings", 0, 1))
        self.assertIn("スレッドにできなかった", s["reason"])
        # ファイル単位のスレッドにでき、それが resolve 済みなら clean
        self.gh.reviews = []
        self.add_review_marker(HEAD, findings=[dict(f, inline=False, thread=True)])
        self.gh.threads = [gha_thread("P1", resolved=True)]
        s = self.status()
        self.assertEqual((s["state"], s["outside_diff_p1"]), ("clean", 0))

    def test_skip_marker_and_old_threads(self):
        self.completed()
        self.add_review_marker(OLD)
        self.gh.comments.append({"id": 9, "user": GHA, "body": marker_body(HEAD, "skip"),
                                 "created_at": "2026-11-01T11:45:00Z", "html_url": "https://example/c/9"})
        s = self.status()
        self.assertEqual(s["state"], "skipped")
        self.assertEqual(s["mode"], "skip")
        self.gh.threads = [gha_thread("P1")]
        self.assertEqual(self.status()["state"], "findings")

    def test_capped(self):
        self.completed()
        self.gh.comments.append({"id": 9, "user": GHA, "body": marker_body(HEAD, "capped", 3),
                                 "created_at": "2026-11-01T11:45:00Z"})
        s = self.status()
        self.assertEqual(s["state"], "skipped")  # 人間が承認済みでチェックが成功
        self.assertIn("上限", s["reason"])
        self.completed("failure")  # 承認待ちでチェックが失敗
        s = self.status()
        self.assertEqual(s["state"], "capped")
        self.assertIn("Approve", s["reason"])

    def test_body_only_p1_with_failed_check_is_findings(self):
        # post_review.py はスレッドにできなかった P0 / P1 があるとチェックを失敗させる
        self.completed("failure")
        f = {"severity": "P1", "path": "a", "line": 1, "title": "t", "body": "b", "inline": False, "thread": False}
        self.add_review_marker(HEAD, findings=[f])
        s = self.status()
        self.assertEqual((s["state"], s["outside_diff_p1"]), ("findings", 1))

    def test_no_marker_is_skipped(self):
        self.completed()
        self.add_review_marker(OLD)
        self.assertEqual(self.status()["state"], "skipped")

    def test_forged_marker_ignored(self):
        self.completed()
        self.gh.comments.append({"id": 9, "user": HUMAN, "body": marker_body(HEAD, "full"),
                                 "created_at": "2026-11-01T11:45:00Z"})
        s = self.status()
        self.assertEqual(s["state"], "skipped")
        self.assertIsNone(s["review_url"])

    def test_unavailable(self):
        self.completed()
        self.gh.comments.append({"id": 9, "user": GHA, "body": marker_body(HEAD, "unavailable", 0),
                                 "created_at": "2026-11-01T11:45:00Z"})
        self.assertEqual(self.status()["state"], "unavailable")
        self.gh.comments = []
        self.completed("failure")
        s = self.status()
        self.assertEqual(s["state"], "unavailable")
        self.assertIn("failure", s["reason"])

    def test_missing_api_key_is_unavailable(self):
        body = marker_body(HEAD, "unavailable", 0) + "\n" + common.encode_reason(common.NO_API_KEY_REASON)
        for conclusion in ("success", "failure"):  # review.fail_open = true / false
            with self.subTest(conclusion=conclusion):
                self.completed(conclusion)
                self.gh.comments = [{"id": 9, "user": GHA, "body": body, "created_at": "2026-11-01T11:45:00Z",
                                     "html_url": "https://example/c/9"}]
                s = self.status()
                self.assertEqual(s["state"], "unavailable")
                self.assertEqual(s["reason"], common.NO_API_KEY_REASON)
                self.assertEqual(s["review_url"], "https://example/c/9")

    def test_sync_skip_reason(self):
        self.completed()
        reason = "配布元からの同期 PR(内容の検証は policy-gate が行う)"
        self.gh.comments.append({"id": 9, "user": GHA, "created_at": "2026-11-01T11:45:00Z",
                                 "body": marker_body(HEAD, "skip", 0) + "\n" + common.encode_reason(reason)})
        s = self.status()
        self.assertEqual((s["state"], s["reason"]), ("skipped", reason))

    def test_draft(self):
        self.gh.pr["draft"] = True
        self.assertEqual(self.status()["state"], "skipped")

    def test_check_for_other_sha_is_ignored(self):
        self.gh.check_runs = [{"id": 1, "status": "completed", "conclusion": "success", "head_sha": OLD}]
        self.assertEqual(self.status()["state"], "pending")


class AppProviderTest(unittest.TestCase):
    def setUp(self):
        self.gh = Fake()
        self.gh.install(self)
        self.settings = common.review_settings({"review": {"bot": {
            "summary_marker": "codex-pull-request-review-summary", "request_comment": "@codex review"}}})

    def status(self):
        return review_status.status_app("o/r", 1, self.settings)

    def request(self, cid=5, at="2026-11-01T11:35:00Z"):
        self.gh.comments.append({"id": cid, "user": HUMAN, "body": "@codex review", "created_at": at,
                                 "html_url": f"https://example/c/{cid}"})

    def test_no_request_pending(self):
        s = self.status()
        self.assertEqual(s["state"], "pending")
        self.assertIn("依頼コメント", s["reason"])

    def test_timeout_counts_from_later_of_push_and_request(self):
        self.gh.pushed_at = "2026-11-01T10:00:00Z"
        self.request(at="2026-11-01T11:30:00Z")
        s = self.status()
        self.assertEqual(s["state"], "pending")
        self.assertEqual(s["elapsed_minutes"], 30.0)
        self.gh.comments = []
        self.request(at="2026-11-01T10:30:00Z")
        self.assertEqual(self.status()["state"], "timeout")

    def test_old_request_ignored(self):
        self.request(at="2026-11-01T09:00:00Z")
        self.gh.comment_reactions[5] = [{"user": BOT, "content": "+1", "created_at": "2026-11-01T09:01:00Z"}]
        self.assertEqual(self.status()["state"], "pending")

    def test_review_for_head(self):
        self.request()
        self.gh.reviews = [{"id": 1, "user": BOT, "commit_id": OLD, "html_url": "old"}]
        self.assertEqual(self.status()["state"], "pending")
        self.gh.reviews.append({"id": 2, "user": BOT, "commit_id": HEAD, "html_url": "new"})
        self.gh.threads = [thread("chatgpt-codex-connector", "**![P2 Badge](x) t**")]
        s = self.status()
        self.assertEqual((s["state"], s["review_url"], s["unresolved_total"]), ("clean", "new", 1))
        self.gh.threads.append(thread("chatgpt-codex-connector", "**![P1 Badge](x) bug**"))
        s = self.status()
        self.assertEqual((s["state"], s["unresolved_p1"]), ("findings", 1))

    def test_summary_comment(self):
        self.request()
        self.gh.comments.append({"id": 6, "user": BOT, "created_at": "2026-11-01T11:50:00Z",
                                 "body": "<!-- codex-pull-request-review-summary --> Didn't find any major issues",
                                 "html_url": "sum"})
        s = self.status()
        self.assertEqual((s["state"], s["review_url"]), ("clean", "sum"))

    def test_thumbs_up_on_request_or_pr_body(self):
        self.request()
        self.gh.comment_reactions[5] = [{"user": BOT, "content": "eyes", "created_at": "2026-11-01T11:36:00Z"}]
        self.assertEqual(self.status()["state"], "running")
        self.gh.comment_reactions[5].append({"user": BOT, "content": "+1", "created_at": "2026-11-01T11:50:00Z"})
        self.assertEqual(self.status()["state"], "clean")
        self.gh.comment_reactions[5] = []
        self.gh.pr_reactions = [{"user": BOT, "content": "+1", "created_at": "2026-11-01T09:00:00Z"}]
        self.assertEqual(self.status()["state"], "pending")  # push より前の +1 は前のヘッドのもの
        self.gh.pr_reactions.append({"user": BOT, "content": "+1", "created_at": "2026-11-01T11:40:00Z"})
        self.assertEqual(self.status()["state"], "clean")

    def test_human_thumbs_up_ignored(self):
        self.request()
        self.gh.comment_reactions[5] = [{"user": HUMAN, "content": "+1", "created_at": "2026-11-01T11:50:00Z"}]
        self.assertEqual(self.status()["state"], "pending")

    def test_bot_null(self):
        settings = common.review_settings({"review": {"bot": None}})
        self.assertEqual(review_status.status_app("o/r", 1, settings)["state"], "unavailable")


class MainTest(unittest.TestCase):
    def test_main_and_provider_default(self):
        gh = Fake()
        gh.install(self)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(review_status.main(["--pr", "1", "--repo", "o/r", "--config", "missing.json"]), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual((out["provider"], out["state"]), ("action", "pending"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(review_status.main(["--pr", "1", "--repo", "o/r", "--provider", "app",
                                                 "--config", "missing.json"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["provider"], "app")

    def test_provider_null(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "cfg.json"
            cfg.write_text(json.dumps({"review": {"provider": None}}), encoding="utf-8")
            with mock.patch.object(review_status, "fetch_pr") as fp:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(review_status.main(["--pr", "1", "--repo", "o/r", "--config", str(cfg)]), 0)
            fp.assert_not_called()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["state"], "skipped")
        self.assertIn("review.provider=null", out["reason"])

    def test_error(self):
        with mock.patch.object(review_status, "fetch_pr", side_effect=common.GitHubError("nope")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(review_status.main(["--pr", "1", "--repo", "o/r", "--config", "missing.json"]), 2)
        self.assertEqual(json.loads(buf.getvalue())["state"], "error")


if __name__ == "__main__":
    unittest.main()
