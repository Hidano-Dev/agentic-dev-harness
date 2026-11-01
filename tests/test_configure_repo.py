from __future__ import annotations

import base64
import copy
import io
import json
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

from _helpers import ROOT

import configure_repo as cr
import registry_targets

REPO = "owner/app"
SHA = "c" * 40


def b64(text: str) -> dict:
    return {"content": base64.b64encode(text.encode("utf-8")).decode("ascii")}


class FakeGitHub:
    """パス(クエリ込み)→ 応答の辞書で GitHub API を模す。書き込みは記録して状態に反映する。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if path not in self.routes:
                raise cr.ApiError(404, "Not Found")
            return copy.deepcopy(self.routes[path])
        if method == "POST" and path.endswith("/git/refs"):
            self.routes[f"/repos/{REPO}/branches/develop"] = {"name": "develop"}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def base_routes(default="main", develop=False, allow_auto_merge=False, harness_workflows=True) -> dict:
    routes = {
        f"/repos/{REPO}": {"default_branch": default, "allow_auto_merge": allow_auto_merge, "allow_merge_commit": True},
        f"/repos/{REPO}/branches/main": {"name": "main"},
        f"/repos/{REPO}/git/ref/heads/main": {"object": {"sha": SHA}},
        f"/repos/{REPO}/commits/{default}": {"sha": SHA},
        f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100": {"workflow_runs": [
            {"check_suite_id": 1, "path": ".github/workflows/ci.yml"},
            {"check_suite_id": 2, "path": ".github/workflows/harness-sync.yml"},
            {"check_suite_id": 3, "path": ".github/workflows/unity.yml"},
            {"check_suite_id": 4, "path": "dynamic/github-code-scanning/codeql"},
        ]},
        f"/repos/{REPO}/commits/{SHA}/check-runs?per_page=100": {"check_runs": [
            {"name": "test", "check_suite": {"id": 1}, "app": {"slug": "github-actions"}},
            {"name": "lint", "check_suite": {"id": 1}, "app": {"slug": "github-actions"}},
            {"name": "sync", "check_suite": {"id": 2}, "app": {"slug": "github-actions"}},
            {"name": "unity-tests", "check_suite": {"id": 3}, "app": {"slug": "github-actions"}},
            {"name": "Analyze", "check_suite": {"id": 4}, "app": {"slug": "github-actions"}},
            {"name": "external", "check_suite": {"id": 9}, "app": {"slug": "some-ci"}},
        ]},
        f"/repos/{REPO}/contents/.github/workflows/ci.yml?ref={SHA}": b64("on:\n  pull_request:\n  push:\n"),
        f"/repos/{REPO}/contents/.github/workflows/harness-sync.yml?ref={SHA}": b64("on:\n  push:\n  schedule: []\n"),
        f"/repos/{REPO}/contents/.github/workflows/unity.yml?ref={SHA}": b64("on: [pull_request]\n"),
        f"/repos/{REPO}/contents/.kiro/orchestration/config.json?ref={default}":
            b64(json.dumps({"checks": {"unity_ci": {"workflow": "unity.yml"}}})),
    }
    if develop:
        routes[f"/repos/{REPO}/branches/develop"] = {"name": "develop"}
    if harness_workflows:
        for ref in ("main", "develop"):
            for wf in cr.HARNESS_CHECK_WORKFLOWS.values():
                routes[f"/repos/{REPO}/contents/{wf}?ref={ref}"] = b64("on: pull_request_target\n")
    return routes


def run(gh, *args):
    out = io.StringIO()
    with redirect_stderr(io.StringIO()), unittest.mock.patch("sys.stdout", new=out):
        code = cr.main(["--repo", REPO, *args], gh=gh)
    return code, out.getvalue()


class ConfigureRepoTest(unittest.TestCase):
    def test_dry_run_is_default_and_writes_nothing(self):
        gh = FakeGitHub(base_routes())
        code, out = run(gh, "--create-develop")
        self.assertEqual(code, 0)
        self.assertEqual(gh.writes(), [])
        self.assertIn("--dry-run", out)
        self.assertIn("develop を main", out)

    def test_plan_contents(self):
        gh = FakeGitHub(base_routes())
        plan = cr.build_plan(gh, REPO, create_develop=True)
        by_path = {a.path: a for a in plan.actions}
        self.assertEqual(by_path[f"/repos/{REPO}/git/refs"].body, {"ref": "refs/heads/develop", "sha": SHA})
        self.assertEqual(by_path[f"/repos/{REPO}"].body, {"default_branch": "develop", "allow_auto_merge": True})
        dev = by_path[f"/repos/{REPO}/branches/develop/protection"].body
        self.assertEqual(dev["required_status_checks"],
                         {"strict": False, "contexts": ["aggregate-docs", "codex-review", "lint", "policy-gate", "test"]})
        self.assertTrue(dev["required_conversation_resolution"])
        self.assertFalse(dev["allow_force_pushes"])
        self.assertFalse(dev["allow_deletions"])
        main = by_path[f"/repos/{REPO}/branches/main/protection"].body
        self.assertEqual(main["required_pull_request_reviews"]["required_approving_review_count"], 1)
        self.assertFalse(main["allow_force_pushes"])
        notes = "\n".join(plan.notes)
        self.assertIn("unity-tests", notes)   # オンプレ Unity CI は必須にしない
        self.assertIn("sync", notes)          # push でしか走らない

    def test_apply_executes_in_order(self):
        gh = FakeGitHub(base_routes())
        code, _ = run(gh, "--create-develop", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual([(m, p) for m, p, _ in gh.writes()], [
            ("POST", f"/repos/{REPO}/git/refs"),
            ("PATCH", f"/repos/{REPO}"),
            ("PUT", f"/repos/{REPO}/branches/develop/protection"),
            ("PUT", f"/repos/{REPO}/branches/main/protection"),
        ])

    def test_missing_develop_without_flag_aborts(self):
        gh = FakeGitHub(base_routes())
        with self.assertRaises(SystemExit):
            run(gh)
        self.assertEqual(gh.writes(), [])

    def test_idempotent_when_already_configured(self):
        routes = base_routes(default="develop", develop=True, allow_auto_merge=True)
        routes[f"/repos/{REPO}/branches/develop/protection"] = {
            "required_status_checks": {"strict": False, "contexts": ["aggregate-docs", "codex-review", "lint",
                                                                     "policy-gate", "test"]},
            "enforce_admins": {"enabled": False},
            "required_conversation_resolution": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
        }
        routes[f"/repos/{REPO}/branches/main/protection"] = {
            "required_pull_request_reviews": {"required_approving_review_count": 1},
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
        }
        gh = FakeGitHub(routes)
        code, out = run(gh, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("変更なし", out)
        self.assertEqual(gh.writes(), [])

    def test_keeps_existing_settings(self):
        routes = base_routes(default="develop", develop=True, allow_auto_merge=True)
        routes[f"/repos/{REPO}/branches/develop/protection"] = {
            "required_status_checks": {"strict": True, "contexts": ["human-added"]},
            "required_pull_request_reviews": {"required_approving_review_count": 2},
            "enforce_admins": {"enabled": True},
        }
        routes[f"/repos/{REPO}/branches/main/protection"] = {
            "required_status_checks": {"strict": True, "contexts": ["release-check"]},
            "required_pull_request_reviews": {"required_approving_review_count": 2},
            "allow_force_pushes": {"enabled": True},
        }
        plan = cr.build_plan(FakeGitHub(routes), REPO, create_develop=False, required_checks=[])
        by_path = {a.path: a.body for a in plan.actions}
        dev = by_path[f"/repos/{REPO}/branches/develop/protection"]
        self.assertIn("human-added", dev["required_status_checks"]["contexts"])
        self.assertFalse(dev["required_status_checks"]["strict"])
        self.assertEqual(dev["required_pull_request_reviews"]["required_approving_review_count"], 2)
        self.assertTrue(dev["enforce_admins"])
        main = by_path[f"/repos/{REPO}/branches/main/protection"]
        self.assertEqual(main["required_status_checks"]["contexts"], ["release-check"])
        self.assertEqual(main["required_pull_request_reviews"]["required_approving_review_count"], 2)
        self.assertFalse(main["allow_force_pushes"])

    def test_explicit_checks_and_exclusions(self):
        gh = FakeGitHub(base_routes())
        plan = cr.build_plan(gh, REPO, create_develop=True, required_checks=["build"],
                             extra_checks=["e2e"], exclude_checks=["aggregate-docs"])
        dev = next(a.body for a in plan.actions if a.path.endswith("/develop/protection"))
        self.assertEqual(dev["required_status_checks"]["contexts"], ["build", "codex-review", "e2e", "policy-gate"])


class HarnessWorkflowPresenceTest(unittest.TestCase):
    def test_harness_checks_skipped_when_workflows_absent(self):
        gh = FakeGitHub(base_routes(harness_workflows=False))
        plan = cr.build_plan(gh, REPO, create_develop=True)
        dev = next(a.body for a in plan.actions if a.path.endswith("/develop/protection"))
        self.assertEqual(dev["required_status_checks"]["contexts"], ["lint", "test"])
        notes = "\n".join(plan.notes)
        for name in cr.HARNESS_CHECKS:
            self.assertIn(name, notes)

    def test_only_present_workflows_become_required(self):
        routes = base_routes(harness_workflows=False)
        routes[f"/repos/{REPO}/contents/.github/workflows/docs-policy.yml?ref=main"] = b64("on: pull_request\n")
        plan = cr.build_plan(FakeGitHub(routes), REPO, create_develop=True, required_checks=[])
        dev = next(a.body for a in plan.actions if a.path.endswith("/develop/protection"))
        self.assertEqual(dev["required_status_checks"]["contexts"], ["aggregate-docs"])

    def test_result_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.json"
            routes = base_routes(default="master")
            routes[f"/repos/{REPO}/branches/master"] = {"name": "master"}
            routes[f"/repos/{REPO}/git/ref/heads/master"] = {"object": {"sha": SHA}}
            routes[f"/repos/{REPO}/contents/.kiro/orchestration/config.json?ref=master"] = b64("{}")
            for wf in cr.HARNESS_CHECK_WORKFLOWS.values():
                routes[f"/repos/{REPO}/contents/{wf}?ref=master"] = b64("x")
            code, _ = run(FakeGitHub(routes), "--create-develop", "--result", str(out))
            self.assertEqual(code, 0)
            r = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((r["previous_default_branch"], r["default_branch"], r["release_branch"]),
                         ("master", "develop", "master"))
        self.assertFalse(r["applied"])


class ProtectionOnlyTest(unittest.TestCase):
    def routes(self, contexts, harness_workflows=True):
        routes = base_routes(default="develop", develop=True, allow_auto_merge=True,
                             harness_workflows=harness_workflows)
        routes[f"/repos/{REPO}/branches/develop/protection"] = {
            "required_status_checks": {"strict": False, "contexts": contexts},
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "required_conversation_resolution": {"enabled": True},
        }
        return routes

    def test_adds_only_missing_harness_checks(self):
        gh = FakeGitHub(self.routes(["test", "aggregate-docs"]))
        code, out = run(gh, "--protection-only", "--apply")
        self.assertEqual(code, 0, out)
        writes = gh.writes()
        self.assertEqual([(m, p) for m, p, _ in writes], [("PUT", f"/repos/{REPO}/branches/develop/protection")])
        body = writes[0][2]
        self.assertEqual(body["required_status_checks"]["contexts"],
                         ["aggregate-docs", "codex-review", "policy-gate", "test"])
        self.assertTrue(body["required_conversation_resolution"])
        self.assertEqual(body["required_pull_request_reviews"]["required_approving_review_count"], 0)

    def test_idempotent(self):
        gh = FakeGitHub(self.routes(["aggregate-docs", "codex-review", "policy-gate"]))
        code, out = run(gh, "--protection-only", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("変更なし", out)
        self.assertEqual(gh.writes(), [])

    def test_waits_for_missing_workflows(self):
        routes = self.routes(["test"], harness_workflows=False)
        routes[f"/repos/{REPO}/contents/.github/workflows/policy-gate.yml?ref=develop"] = b64("x")
        gh = FakeGitHub(routes)
        run(gh, "--protection-only", "--apply")
        self.assertEqual(gh.writes()[0][2]["required_status_checks"]["contexts"], ["policy-gate", "test"])

    def test_noop_without_two_tier_setup(self):
        gh = FakeGitHub(base_routes())  # 既定ブランチが main のまま
        code, out = run(gh, "--protection-only", "--apply")
        self.assertEqual((code, gh.writes()), (0, []))
        routes = base_routes(default="develop", develop=True)  # develop に保護が無い
        gh = FakeGitHub(routes)
        code, _ = run(gh, "--protection-only", "--apply")
        self.assertEqual((code, gh.writes()), (0, []))

    def test_dry_run_default(self):
        gh = FakeGitHub(self.routes(["test"]))
        run(gh, "--protection-only")
        self.assertEqual(gh.writes(), [])


class RegistryTargetsTest(unittest.TestCase):
    def test_real_registry(self):
        text = (ROOT / "registry/repos.yaml").read_text(encoding="utf-8")
        t = registry_targets.targets(text, [])
        self.assertIn("Hidano-Dev/unity-renderer", t)
        self.assertNotIn("Hidano-Dev/unity-sdd-kit", t)
        self.assertNotIn("Hidano-Dev/unity-project-template", t)

    def test_filter_and_unknown(self):
        text = (ROOT / "registry/repos.yaml").read_text(encoding="utf-8")
        self.assertEqual(registry_targets.targets(text, ["oscdesk"]), ["Hidano-Dev/oscdesk"])
        with self.assertRaises(SystemExit):
            registry_targets.targets(text, ["unity-sdd-kit"])


if __name__ == "__main__":
    unittest.main()
