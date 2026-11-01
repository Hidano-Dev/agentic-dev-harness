#!/usr/bin/env python3
"""取り込み側リポジトリの GitHub 設定を develop 既定 + main リリースの形に揃える(配布元専用・冪等)。

既定は --dry-run(予定を表示するだけ)。--apply を付けたときだけ変更する。
トークンは環境変数 GH_TOKEN(または GITHUB_TOKEN)。GitHub App のインストールトークンを想定
(Administration: write / Contents: write / Actions: read / Checks: read / Metadata: read)。

行うこと(すでにその状態なら何もしない):
1. develop が無ければ既定ブランチの先端から作成(--create-develop 指定時のみ。無指定で無ければ中止)
2. 既定ブランチを develop に変更、"Allow auto-merge" と merge commit を有効化
   (Harness Sync の同期 PR は `gh pr merge --auto --merge` で予約する)
3. develop の branch protection:
   必須チェック = codex-review / policy-gate / aggregate-docs(docs-policy.yml)+ 既存 CI
   (直近の既定ブランチのコミットの check runs のうち、pull_request で走る GitHub Actions の
   ワークフローのもの。config の checks.unity_ci.workflow は除く)。strict=false、
   conversation resolution 必須、force-push / 削除禁止。既存の必須チェックは残す
4. リリースブランチ(既定: 変更前の既定ブランチ。それが develop なら main)の protection:
   PR 必須・承認 1 以上、force-push / 削除禁止。既存の必須チェック等は残す

指定しない項目(develop の承認数・push 制限・管理者への適用など)は既存の設定を保つ。
標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

HARNESS_CHECKS = ["codex-review", "policy-gate", "aggregate-docs"]
DEVELOP = "develop"
CONFIG_REL = ".kiro/orchestration/config.json"
PR_TRIGGER_RE = re.compile(r"\bpull_request\b")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class GitHub:
    """最小限の REST クライアント。テストでは request を差し替える。"""

    def __init__(self, token: str, api: str = "https://api.github.com"):
        self.token = token
        self.api = api.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.api + path, data=data, method=method,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "harness-configure-repo",
                     **({"Content-Type": "application/json"} if data else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")) from e
        return json.loads(raw) if raw else None


def get_or_none(gh, path: str):
    try:
        return gh.request("GET", path)
    except ApiError as e:
        if e.status == 404:
            return None
        raise


@dataclass
class Action:
    description: str
    method: str
    path: str
    body: dict | None = None


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def enabled(d, key: str, default=False) -> bool:
    v = (d or {}).get(key)
    if isinstance(v, dict):
        return bool(v.get("enabled", default))
    return default if v is None else bool(v)


def protection_to_put(p: dict | None) -> dict:
    """GET の branch protection を PUT の形に変換する(無ければ空の保護)。"""
    p = p or {}
    rsc = p.get("required_status_checks")
    status = None
    if rsc:
        contexts = list(rsc.get("contexts") or [])
        contexts += [c["context"] for c in rsc.get("checks") or [] if c.get("context") not in contexts]
        status = {"strict": bool(rsc.get("strict")), "contexts": sorted(set(contexts))}
    rpr = p.get("required_pull_request_reviews")
    reviews = None
    if rpr:
        reviews = {
            "dismiss_stale_reviews": bool(rpr.get("dismiss_stale_reviews")),
            "require_code_owner_reviews": bool(rpr.get("require_code_owner_reviews")),
            "required_approving_review_count": int(rpr.get("required_approving_review_count") or 0),
            "require_last_push_approval": bool(rpr.get("require_last_push_approval")),
        }
    r = p.get("restrictions")
    restrictions = None
    if r:
        restrictions = {"users": sorted(u["login"] for u in r.get("users") or []),
                        "teams": sorted(t["slug"] for t in r.get("teams") or []),
                        "apps": sorted(a["slug"] for a in r.get("apps") or [])}
    return {
        "required_status_checks": status,
        "enforce_admins": enabled(p, "enforce_admins"),
        "required_pull_request_reviews": reviews,
        "restrictions": restrictions,
        "required_linear_history": enabled(p, "required_linear_history"),
        "allow_force_pushes": enabled(p, "allow_force_pushes"),
        "allow_deletions": enabled(p, "allow_deletions"),
        "required_conversation_resolution": enabled(p, "required_conversation_resolution"),
    }


def read_unity_ci_workflow(gh, repo: str, ref: str) -> str | None:
    c = get_or_none(gh, f"/repos/{repo}/contents/{CONFIG_REL}?ref={urllib.parse.quote(ref)}")
    if not c or "content" not in c:
        return None
    try:
        cfg = json.loads(base64.b64decode(c["content"]).decode("utf-8"))
        wf = ((cfg.get("checks") or {}).get("unity_ci") or {}).get("workflow")
    except (ValueError, AttributeError):
        return None
    return wf if isinstance(wf, str) and wf.strip() else None


def infer_ci_checks(gh, repo: str, branch: str) -> tuple[list[str], list[str]]:
    """既定ブランチ先端の check runs から、PR で走る既存 CI の check 名を推定する。戻り値: (名前, メモ)。"""
    notes: list[str] = []
    commit = get_or_none(gh, f"/repos/{repo}/commits/{urllib.parse.quote(branch)}")
    if not commit:
        return [], [f"{branch} のコミットを取得できないため既存 CI の推定をスキップ"]
    sha = commit["sha"]
    runs = (gh.request("GET", f"/repos/{repo}/actions/runs?head_sha={sha}&per_page=100") or {}).get("workflow_runs") or []
    suite_path = {r.get("check_suite_id"): r.get("path") or "" for r in runs}
    checks = (gh.request("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs") or []
    unity = read_unity_ci_workflow(gh, repo, branch)
    unity_base = unity.rsplit("/", 1)[-1] if unity else None
    pr_triggered: dict[str, bool] = {}
    names: set[str] = set()
    for cr in checks:
        if (cr.get("app") or {}).get("slug") != "github-actions":
            continue
        name = cr.get("name") or ""
        if not name or name in HARNESS_CHECKS:
            continue
        path = suite_path.get((cr.get("check_suite") or {}).get("id"), "")
        if not path.startswith(".github/workflows/"):
            continue
        if unity_base and path.rsplit("/", 1)[-1] == unity_base:
            notes.append(f"{name}({path})はオンプレ Unity CI(checks.unity_ci)のため必須にしない")
            continue
        if path not in pr_triggered:
            c = get_or_none(gh, f"/repos/{repo}/contents/{path}?ref={sha}")
            text = base64.b64decode(c["content"]).decode("utf-8", "replace") if c and "content" in c else ""
            pr_triggered[path] = bool(PR_TRIGGER_RE.search(text))
        if pr_triggered[path]:
            names.add(name)
        else:
            notes.append(f"{name}({path})は pull_request で走らないため必須にしない")
    return sorted(names), notes


def build_plan(gh, repo: str, *, create_develop: bool, release_branch: str | None = None,
               extra_checks: list[str] = (), exclude_checks: list[str] = (),
               required_checks: list[str] | None = None) -> Plan:
    plan = Plan()
    info = gh.request("GET", f"/repos/{repo}")
    default = info["default_branch"]
    release = release_branch or (default if default != DEVELOP else "main")
    develop = get_or_none(gh, f"/repos/{repo}/branches/{DEVELOP}")

    if develop is None:
        if not create_develop:
            raise SystemExit(f"::error::{repo} に {DEVELOP} ブランチが無い(--create-develop で既定ブランチから作成する)")
        src = gh.request("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(default)}")
        plan.actions.append(Action(f"{DEVELOP} を {default}({src['object']['sha'][:7]})から作成", "POST",
                                   f"/repos/{repo}/git/refs",
                                   {"ref": f"refs/heads/{DEVELOP}", "sha": src["object"]["sha"]}))

    patch = {}
    if default != DEVELOP:
        patch["default_branch"] = DEVELOP
    if not info.get("allow_auto_merge"):
        patch["allow_auto_merge"] = True
    if info.get("allow_merge_commit") is False:
        patch["allow_merge_commit"] = True
    if patch:
        plan.actions.append(Action("リポジトリ設定: " + ", ".join(f"{k}={v}" for k, v in patch.items()),
                                   "PATCH", f"/repos/{repo}", patch))

    # develop の保護
    if required_checks is not None:
        ci = list(required_checks)
    else:
        ci, notes = infer_ci_checks(gh, repo, default)
        plan.notes += notes
    current = get_or_none(gh, f"/repos/{repo}/branches/{DEVELOP}/protection") if develop else None
    cur = protection_to_put(current)
    want = json.loads(json.dumps(cur))
    contexts = set((cur["required_status_checks"] or {}).get("contexts") or [])
    contexts |= set(HARNESS_CHECKS) | set(ci) | set(extra_checks)
    contexts -= set(exclude_checks)
    want["required_status_checks"] = {"strict": False, "contexts": sorted(contexts)}
    want["required_conversation_resolution"] = True
    want["allow_force_pushes"] = False
    want["allow_deletions"] = False
    if want != cur or current is None:
        plan.actions.append(Action(f"{DEVELOP} の保護: 必須チェック {', '.join(sorted(contexts))}、"
                                   "conversation resolution 必須、force-push / 削除禁止",
                                   "PUT", f"/repos/{repo}/branches/{DEVELOP}/protection", want))

    # リリースブランチの保護
    if get_or_none(gh, f"/repos/{repo}/branches/{urllib.parse.quote(release)}") is None:
        plan.notes.append(f"リリースブランチ {release} が無いため保護をスキップ")
    else:
        current = get_or_none(gh, f"/repos/{repo}/branches/{urllib.parse.quote(release)}/protection")
        cur = protection_to_put(current)
        want = json.loads(json.dumps(cur))
        reviews = want["required_pull_request_reviews"] or {
            "dismiss_stale_reviews": False, "require_code_owner_reviews": False,
            "required_approving_review_count": 0, "require_last_push_approval": False}
        reviews["required_approving_review_count"] = max(1, reviews["required_approving_review_count"])
        want["required_pull_request_reviews"] = reviews
        want["allow_force_pushes"] = False
        want["allow_deletions"] = False
        if want != cur or current is None:
            plan.actions.append(Action(f"{release} の保護: PR 必須・承認 {reviews['required_approving_review_count']} 以上、"
                                       "force-push / 削除禁止", "PUT",
                                       f"/repos/{repo}/branches/{urllib.parse.quote(release)}/protection", want))
    return plan


def split_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


def main(argv: list[str] | None = None, gh=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--create-develop", action="store_true", help="develop が無ければ既定ブランチから作成する")
    ap.add_argument("--release-branch", help="リリースブランチ(既定: 変更前の既定ブランチ。develop なら main)")
    ap.add_argument("--extra-check", action="append", default=[], help="develop の必須チェックに足す名前(カンマ区切り可)")
    ap.add_argument("--exclude-check", action="append", default=[], help="develop の必須チェックから外す名前")
    ap.add_argument("--checks", help="既存 CI の check 名を推定せずこの一覧(カンマ区切り)にする")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="予定を表示するだけ(既定)")
    mode.add_argument("--apply", action="store_true", help="実際に変更する")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo):
        ap.error(f"--repo は owner/name 形式: {a.repo!r}")
    if gh is None:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("::error::GH_TOKEN が無い", file=sys.stderr)
            return 1
        gh = GitHub(token)
    try:
        plan = build_plan(gh, a.repo, create_develop=a.create_develop, release_branch=a.release_branch,
                          extra_checks=split_list(a.extra_check), exclude_checks=split_list(a.exclude_check),
                          required_checks=split_list([a.checks]) if a.checks is not None else None)
    except ApiError as e:
        print(f"::error::{a.repo} の設定を読めない: {e}", file=sys.stderr)
        return 1
    label = "実行" if a.apply else "予定(--dry-run。--apply で実行)"
    print(f"{a.repo}: {label}")
    for n in plan.notes:
        print(f"  メモ: {n}")
    if not plan.actions:
        print("  変更なし(設定済み)")
        return 0
    for act in plan.actions:
        print(f"  - {act.description}")
        if a.apply:
            try:
                gh.request(act.method, act.path, act.body)
            except ApiError as e:
                print(f"::error::{act.description} に失敗: {e}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
