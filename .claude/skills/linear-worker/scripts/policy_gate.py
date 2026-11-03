#!/usr/bin/env python3
"""PR のポリシーゲート(必須チェック `policy-gate` の本体)。

templates/consumer/policy-gate.yml が base 側のこのスクリプトを呼ぶ(PR が判定ロジック自体を
書き換えて通すのを防ぐ)。PR の差分(`git diff --name-only base...head`)に対して次の順に判定する:

1. 不変条件: `.harness/invariants.json`(`[{id, file, pattern, message}]`)の各 file が
   head に存在し、pattern(正規表現)を含むこと。満たさなければ失敗。invariants.json が
   無ければスキップ。使う invariants.json は base 側のもの(同期 PR として検証できた場合と、
   人間が承認した PR が invariants.json 自体を変えている場合は head 側)
2. 同期 PR: `.harness/version` を変更していて、変更が配布物・`.harness/`・ワークフロー・
   CLAUDE.md・config.json に収まり、配布元の verify_harness_sync.py が「指定 sha の適用結果と
   完全一致」と判定すれば成功(人間の承認は不要)。検証スクリプトを実行する前に、その sha が
   配布元の --harness-ref に含まれ、base 側の記録から前進していることをこのスクリプト自身が確かめる。
   同期 PR の形だが完全一致を確かめられなかったものは 4 と同じく人間の承認で通す
3. 同期 PR の形でないのに取り込み側で配布物(harness owned paths)を直接変更している →
   失敗(配布元で直す)
4. コアパス(config の auto_merge.protected_paths。無ければ既定値)または配布物に触れている →
   現在 head に対する人間の APPROVED review があれば成功、無ければ失敗(人間の承認待ち)
5. それ以外 → 成功

人間の承認 = レビュアーごとの最新の判断(APPROVED / CHANGES_REQUESTED / DISMISSED)が
APPROVED で commit_id が head、user.type が User(Bot でない)、author_association が
OWNER / MEMBER / COLLABORATOR。CHANGES_REQUESTED が残っていれば承認なし扱い。

Release PR(head ブランチ = config の github.default_branch、base ブランチ = github.release_branch。
--base-ref / --head-ref で渡す)では 2・3 を行わず、コア・配布物に触れていれば 4 と同じく人間の承認を求める。

配布元(base のリポジトリ直下に sync-manifest.json がある)では 2・3 を行わず、配布物と
sync-manifest.json・scripts/sync/・templates/consumer/ はコアとして人間の承認を求める。

結果は標準出力と $GITHUB_STEP_SUMMARY に出す。終了コード: 0 = 成功、1 = 失敗、2 = 実行エラー。
標準ライブラリのみ・単独ファイルで動く(base から取り出して実行されるため、他のファイルを import しない)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_HARNESS_REPO = "Hidano-Dev/agentic-dev-harness"
DEFAULT_HARNESS_REF = "main"
CONFIG_REL = ".kiro/orchestration/config.json"
VERSION_REL = ".harness/version"
INVARIANTS_REL = ".harness/invariants.json"
MANIFEST_REL = "sync-manifest.json"
VERIFY_REL = "scripts/sync/verify_harness_sync.py"
# コア(人間の承認が必須)の既定。config の auto_merge.protected_paths があればそちらを使う
DEFAULT_CORE = [".github/", ".harness/", ".kiro/orchestration/", ".kiro/settings/", ".claude/settings.json", ".codex/"]
# config に関わらず常にコアとして扱う(ゲート自身・同期記録・config を守る)。sync-manifest.json と
# scripts/sync/ は取り込み側にも置かせない(足すと配布元とみなされ、配布物の保護が外れるため)
FLOOR_CORE = [".github/", ".harness/", ".kiro/orchestration/", "sync-manifest.json", "scripts/sync/"]
# 配布元でだけ追加するコア
DISTRIBUTOR_CORE = [MANIFEST_REL, "scripts/sync/", "templates/consumer/"]
# `.harness/version` に owned が無いときの配布物(配布元の sync-manifest.json と揃える)
DEFAULT_OWNED = [".claude/skills/linear-worker", ".claude/rules/git-workflow.md", ".harness/invariants.json"]
# 同期 PR が変更してよいパス(この範囲に収まる PR だけ完全一致検証を試す)
SYNC_EXTRA = [".harness/", ".github/workflows/", "CLAUDE.md", CONFIG_REL]
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


class GateError(Exception):
    """判定を実行できない(終了コード 2)。"""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise GateError(f"git {' '.join(args)} に失敗: {r.stderr.strip()}")
    return r


def show(repo: Path, rev: str, path: str) -> str | None:
    r = git(repo, "show", f"{rev}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def load_json(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def matches(path: str, prefixes: list[str]) -> bool:
    """path が prefixes のいずれか(ディレクトリ接頭辞、またはファイルそのもの)に該当するか。"""
    for p in prefixes:
        q = p.rstrip("/")
        if not q:
            continue
        if path == q or path.startswith(q + "/"):
            return True
    return False


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}").stdout
    return sorted(p for p in out.split("\0") if p)


# ---------------------------------------------------------------- reviews


def human_approval(reviews: list[dict], head: str) -> tuple[bool, str]:
    """head に対する人間の承認があるか。戻り値: (承認あり, 説明)。"""
    latest: dict[str, dict] = {}
    for r in reviews:
        user = r.get("user") or {}
        login = user.get("login") or ""
        if user.get("type") != "User" or login.endswith("[bot]"):
            continue
        if r.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        if r.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        latest[login] = r
    approvers = sorted(l for l, r in latest.items() if r["state"] == "APPROVED" and r.get("commit_id") == head)
    blockers = sorted(l for l, r in latest.items() if r["state"] == "CHANGES_REQUESTED")
    if blockers:
        return False, f"変更要求あり({', '.join(blockers)})"
    if approvers:
        return True, f"承認: {', '.join(approvers)}"
    stale = sorted(l for l, r in latest.items() if r["state"] == "APPROVED")
    if stale:
        return False, f"承認が古いコミットに対するもの({', '.join(stale)})"
    return False, "人間の承認なし"


def fetch_reviews(repo: str, pr: int, token: str, api: str = "https://api.github.com") -> list[dict]:
    """PR のレビューを提出順に全件取る。"""
    reviews: list[dict] = []
    page = 1
    while True:
        req = urllib.request.Request(
            f"{api}/repos/{repo}/pulls/{pr}/reviews?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "harness-policy-gate"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            raise GateError(f"PR #{pr} のレビューを取得できない: {e}") from e
        reviews += batch
        if len(batch) < 100:
            return reviews
        page += 1


# ---------------------------------------------------------------- sync verification


def check_harness_commit(repo_dir: Path, base: str, head: str, harness: Path, sha: str,
                         harness_repo: str, harness_ref: str) -> str | None:
    """sha が配布元の origin/<harness_ref> に含まれ、base 側の記録から前進していることを確認する。
    問題があればその説明を返す。"""
    # ref の引き方は verify_harness_sync.check_harness_commit と揃える(テストで一致を確かめている)
    tip = None
    for cand in (f"refs/remotes/origin/{harness_ref}", f"refs/heads/{harness_ref}"):
        r = git(harness, "rev-parse", "--verify", "--quiet", cand + "^{commit}", check=False)
        if r.returncode == 0:
            tip = r.stdout.strip()
            break
    if tip is None:
        return f"配布元に {harness_ref} ブランチが無い"
    if git(harness, "cat-file", "-e", sha + "^{commit}", check=False).returncode != 0:
        return f"配布元に {sha} が無い"
    if git(harness, "merge-base", "--is-ancestor", sha, tip, check=False).returncode != 0:
        return f"{sha} は配布元の {harness_ref} に含まれない(レビュー前のコミットは同期に使えない)"
    mb = git(repo_dir, "merge-base", base, head, check=False)
    prev = load_json(show(repo_dir, mb.stdout.strip(), VERSION_REL)) if mb.returncode == 0 else None
    if isinstance(prev, dict) and prev.get("repo") == harness_repo and isinstance(prev.get("sha"), str):
        p = prev["sha"]
        if git(harness, "cat-file", "-e", p + "^{commit}", check=False).returncode != 0 \
                or git(harness, "merge-base", "--is-ancestor", p, sha, check=False).returncode != 0:
            return f"同期前の {p} から {sha} への更新ではない(巻き戻し・分岐)"
    return None


def verify_sync_with_harness(repo_dir: Path, base: str, head: str, harness_repo: str, harness_ref: str,
                             harness_url: str | None = None) -> tuple[bool, list[str]]:
    """head の `.harness/version` が指す配布元を clone し、その verify_harness_sync.py で検証する。"""
    ver = load_json(show(repo_dir, head, VERSION_REL))
    if not isinstance(ver, dict):
        return False, [f"head の {VERSION_REL} が読めない"]
    if ver.get("repo") != harness_repo:
        return False, [f"{VERSION_REL} の repo が想定外: {ver.get('repo')!r}(想定: {harness_repo!r})"]
    if ver.get("ref") != harness_ref:
        return False, [f"{VERSION_REL} の ref が想定外: {ver.get('ref')!r}(想定: {harness_ref!r})"]
    sha = ver.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False, [f"{VERSION_REL} の sha が不正: {sha!r}"]
    url = harness_url or f"https://github.com/{harness_repo}.git"
    tmp = Path(tempfile.mkdtemp(prefix="policy-gate-harness-"))
    try:
        h = tmp / "harness"
        r = subprocess.run(["git", "clone", "--quiet", "--no-checkout", url, str(h)],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            return False, [f"配布元を clone できない({url}): {r.stderr.strip()}"]
        # その sha のコード(verify_harness_sync.py)を実行する前に、人間レビュー済みの ref に
        # 含まれる前進であることをここで確かめる(未レビューのブランチの検証スクリプトで通させない)
        problem = check_harness_commit(repo_dir, base, head, h, sha, harness_repo, harness_ref)
        if problem:
            return False, [problem]
        if git(h, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", sha,
               check=False).returncode != 0:
            return False, [f"配布元に {sha} が無い"]
        script = h / VERIFY_REL
        if not script.is_file():
            return False, [f"配布元の {sha[:7]} に {VERIFY_REL} が無い"]
        r = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(script), "--repo-dir", str(repo_dir), "--base", base,
             "--head", head, "--harness", str(h), "--harness-repo", harness_repo, "--harness-ref", harness_ref],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        lines = [ln for ln in ((r.stdout or "") + (r.stderr or "")).splitlines() if ln.strip()]
        return r.returncode == 0, lines
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- invariants


def check_invariants(repo_dir: Path, head: str, invariants) -> list[str]:
    """不変条件を head のファイルに対して検査し、違反メッセージを返す。"""
    if not isinstance(invariants, list):
        return [f"{INVARIANTS_REL} が配列ではない"]
    failures: list[str] = []
    cache: dict[str, str | None] = {}
    for inv in invariants:
        if not isinstance(inv, dict) or not all(isinstance(inv.get(k), str) for k in ("id", "file", "pattern")):
            failures.append(f"{INVARIANTS_REL} に不正な項目がある: {inv!r}")
            continue
        f = inv["file"]
        if f not in cache:
            cache[f] = show(repo_dir, head, f)
        text = cache[f]
        msg = inv.get("message") or ""
        if text is None:
            failures.append(f"[{inv['id']}] {f} が無い。{msg}")
            continue
        try:
            found = re.search(inv["pattern"], text) is not None
        except re.error as e:
            failures.append(f"[{inv['id']}] pattern が正規表現として不正: {e}")
            continue
        if not found:
            failures.append(f"[{inv['id']}] {f} に必要な記述が見つからない。{msg}")
    return failures


# ---------------------------------------------------------------- evaluation


@dataclass
class Result:
    ok: bool
    reason: str
    details: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        head = "policy-gate: 成功" if self.ok else "policy-gate: 失敗"
        lines = [f"## {head}", "", self.reason]
        if self.details:
            lines += [""] + [f"- {d}" for d in self.details]
        return "\n".join(lines) + "\n"


def core_paths(config) -> list[str]:
    am = config.get("auto_merge") if isinstance(config, dict) else None
    paths = am.get("protected_paths") if isinstance(am, dict) else None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        paths = list(DEFAULT_CORE)
    return list(dict.fromkeys(paths + FLOOR_CORE))


def evaluate(repo_dir: Path, base: str, head: str, *,
             approval_fn: Callable[[], tuple[bool, str]],
             sync_verify_fn: Callable[[], tuple[bool, list[str]]],
             config_path: Path | None = None, base_ref: str | None = None,
             head_ref: str | None = None, head_repo: str | None = None,
             repo: str | None = None) -> Result:
    repo_dir = Path(repo_dir)
    head = git(repo_dir, "rev-parse", "--verify", head + "^{commit}").stdout.strip()
    base = git(repo_dir, "rev-parse", "--verify", base + "^{commit}").stdout.strip()
    changed = changed_files(repo_dir, base, head)

    if config_path is not None:
        config = load_json(config_path.read_text(encoding="utf-8")) if config_path.is_file() else None
    else:
        # PR 自身が書き換えた config で保護を外せないよう、base 側の config を使う
        config = load_json(show(repo_dir, base, CONFIG_REL))
    core = core_paths(config)
    gh_cfg = config.get("github") if isinstance(config, dict) and isinstance(config.get("github"), dict) else {}
    # Release PR(作業ブランチ → リリースブランチの昇格 PR)。同期の変更と通常の変更がまとめて入るので
    # 同期の判定・配布物の直接編集の失敗は行わず、コア・配布物に触れていれば人間の承認を求める
    # (どのみち人間がマージする)。ブランチ名は base 側の config とイベントの ref で判定する
    release_pr = bool(
        isinstance(gh_cfg.get("release_branch"), str) and gh_cfg.get("release_branch")
        and isinstance(gh_cfg.get("default_branch"), str) and gh_cfg.get("default_branch")
        and base_ref == gh_cfg["release_branch"] and head_ref == gh_cfg["default_branch"]
        and (not head_repo or not repo or head_repo == repo)
    )

    manifest = load_json(show(repo_dir, base, MANIFEST_REL))
    distributor = isinstance(manifest, dict)
    if distributor:
        owned = [p for p in manifest.get("owned") or [] if isinstance(p, str)]
        # 配布の定義・適用スクリプト・配布ワークフロー雛形も取り込み側の挙動を変えるのでコア
        workflows = manifest.get("workflows") if isinstance(manifest.get("workflows"), dict) else {}
        core = list(dict.fromkeys(core + DISTRIBUTOR_CORE + [k for k in workflows if isinstance(k, str)]))
    else:
        ver = load_json(show(repo_dir, base, VERSION_REL))
        owned = ver.get("owned") if isinstance(ver, dict) and isinstance(ver.get("owned"), list) else DEFAULT_OWNED
    owned = [p for p in owned if isinstance(p, str)]

    approval_cache: list[tuple[bool, str]] = []

    def approval() -> tuple[bool, str]:
        if not approval_cache:
            approval_cache.append(approval_fn())
        return approval_cache[0]

    # 2. 同期 PR の検証(不変条件より先に行い、検証できたら head の invariants を使う)
    sync_ok, sync_msgs, sync_candidate = False, [], False
    if not distributor and not release_pr and VERSION_REL in changed:
        head_ver = load_json(show(repo_dir, head, VERSION_REL))
        head_owned = head_ver.get("owned") if isinstance(head_ver, dict) and isinstance(head_ver.get("owned"), list) else []
        allowed = owned + [p for p in head_owned if isinstance(p, str)] + SYNC_EXTRA
        outside = [f for f in changed if not matches(f, allowed)]
        if outside:
            sync_msgs = ["同期 PR の範囲外の変更がある: " + ", ".join(outside)]
        else:
            sync_candidate = True
            sync_ok, sync_msgs = sync_verify_fn()

    # 1. 不変条件
    inv_rev = base
    if sync_ok:
        inv_rev = head
    elif INVARIANTS_REL in changed and approval()[0]:
        inv_rev = head
    inv_text = show(repo_dir, inv_rev, INVARIANTS_REL)
    if inv_text is None and inv_rev == base:
        inv_text = show(repo_dir, head, INVARIANTS_REL)  # 導入 PR(base にまだ無い)
    details: list[str] = []
    if inv_text is not None:
        invariants = load_json(inv_text)
        failures = check_invariants(repo_dir, head, invariants) if invariants is not None \
            else [f"{INVARIANTS_REL} が JSON として不正"]
        if failures:
            return Result(False, "不変条件を満たしていない(方針文書から必須の記述が消えている)。", failures)
        details.append(f"不変条件: {len(invariants)} 件すべて満たす")
    else:
        details.append(f"不変条件: {INVARIANTS_REL} が無いためスキップ")

    if sync_ok:
        return Result(True, "同期 PR: 配布元の指定コミットの適用結果と完全に一致(人間の承認は不要)。",
                      details + sync_msgs)

    touched_owned = [f for f in changed if matches(f, owned)]
    touched_core = [f for f in changed if matches(f, core)]
    if sync_msgs:
        details += ["同期 PR として検証できなかった: "] + [f"  {m}" for m in sync_msgs]
    # 同期 PR の形(.harness/version と同期の範囲だけを変更)で完全一致を確かめられなかったものは、
    # 人間が確認して承認すれば通す(配布元の不具合・手動同期の救済)。それ以外の配布物の直接編集は失敗
    if release_pr:
        details.append(f"Release PR({head_ref} → {base_ref}): 同期の判定と配布物の直接編集の検査は行わない")
    if touched_owned and not distributor and not sync_candidate and not release_pr:
        return Result(False, "配布物(harness owned paths)を直接変更している。配布物は配布元で修正し、"
                             "Harness Sync で取り込むこと。", details + [f"配布物: {f}" for f in touched_owned])
    if touched_core or touched_owned:
        ok, why = approval()
        hits = sorted(set(touched_core) | set(touched_owned))
        listing = [f"コア: {f}" for f in hits]
        if ok:
            if release_pr:
                return Result(True, f"Release PR のコア・配布物の変更に人間の承認がある({why})。", details + listing)
            if sync_candidate:
                return Result(True, f"同期 PR の完全一致検証は失敗したが、人間の承認がある({why})。", details + listing)
            return Result(True, f"コアパスの変更に人間の承認がある({why})。", details + listing)
        if release_pr:
            return Result(False, f"人間の承認待ち: Release PR がコア・配布物を含むため、現在の head に対する人間の承認が必要({why})。",
                          details + listing)
        if sync_candidate:
            return Result(False, f"人間の承認待ち: 同期 PR の完全一致検証に失敗したため、現在の head に対する人間の承認が必要({why})。",
                          details + listing)
        return Result(False, f"人間の承認待ち: コアパスを変更しているため、現在の head に対する人間の承認が必要({why})。",
                      details + listing)
    return Result(True, "コアパス・配布物の変更なし。", details)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="PR の base(コミット)")
    ap.add_argument("--head", required=True, help="PR の head(コミット)")
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name(レビュー取得用)")
    ap.add_argument("--pr", type=int, help="PR 番号(レビュー取得用)")
    ap.add_argument("--config", help="config.json(既定: base 側の .kiro/orchestration/config.json)")
    ap.add_argument("--harness-repo", default=os.environ.get("HARNESS_REPO") or DEFAULT_HARNESS_REPO)
    ap.add_argument("--harness-ref", default=os.environ.get("HARNESS_REF") or DEFAULT_HARNESS_REF)
    ap.add_argument("--harness-url", help="配布元の clone URL(既定: https://github.com/<harness-repo>.git)")
    ap.add_argument("--base-ref", default="", help="PR の base ブランチ名(Release PR の判定用)")
    ap.add_argument("--head-ref", default="", help="PR の head ブランチ名(Release PR の判定用)")
    ap.add_argument("--head-repo", default="", help="PR の head リポジトリ(owner/name。fork の判定用)")
    a = ap.parse_args(argv)
    repo_dir = Path(a.repo_dir).resolve()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

    def approval_fn() -> tuple[bool, str]:
        if not (a.repo and a.pr and token):
            return False, "レビューを取得できない(--repo / --pr / GH_TOKEN が無い)"
        return human_approval(fetch_reviews(a.repo, a.pr, token), a.head_full)

    try:
        a.head_full = git(repo_dir, "rev-parse", "--verify", a.head + "^{commit}").stdout.strip()
        result = evaluate(
            repo_dir, a.base, a.head,
            approval_fn=approval_fn,
            sync_verify_fn=lambda: verify_sync_with_harness(repo_dir, a.base, a.head_full, a.harness_repo,
                                                            a.harness_ref, a.harness_url),
            config_path=Path(a.config) if a.config else None,
            base_ref=a.base_ref or None, head_ref=a.head_ref or None,
            head_repo=a.head_repo or None, repo=a.repo or None,
        )
    except GateError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    md = result.markdown()
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    if not result.ok:
        print(f"::error::{result.reason}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
