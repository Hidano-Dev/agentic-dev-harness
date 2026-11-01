#!/usr/bin/env python3
"""同期 PR の内容が「配布元の指定コミットを apply_sync で適用した結果」と完全に一致するかを検証する。

取り込み側の policy-gate(policy_gate.py)が、head の `.harness/version` が指す配布元を clone し、
その clone にあるこのスクリプトで呼ぶ。PR の作者には依存せず内容だけで判定する
(誰が作った PR でも、人間がレビュー済みの配布元 ref に含まれるコミットを適用した結果と
一致すれば、取り込み側の人間の承認なしで安全とみなせる)。

判定:
1. head の `.harness/version` を読む。repo が --harness-repo と一致し、ref が --harness-ref と一致すること
2. その sha が配布元の --harness-ref(人間レビュー済みのブランチ)に含まれること
3. base 側(merge-base)に `.harness/version` があれば、新しい sha がその子孫であること(巻き戻し禁止)
4. merge-base のツリーを一時 worktree に展開して apply_sync を実行し、結果のツリーが
   head のツリーと全ファイル(モード含む)一致すること

終了コード: 0 = 一致、1 = 不一致・条件違反、2 = 実行エラー。
標準ライブラリのみ使用。
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_sync import DEFAULT_HARNESS_REPO, DEFAULT_REF, VERSION_REL, SyncError, apply_sync  # noqa: E402

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class VerifyError(Exception):
    """検証そのものが実行できない(終了コード 2)。"""


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise VerifyError(f"git {' '.join(args)} に失敗: {r.stderr.strip()}")
    return r


def show(repo: Path, rev: str, path: str) -> str | None:
    r = git(repo, "show", f"{rev}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def parse_version(text: str | None) -> dict | None:
    if text is None:
        return None
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def clone_harness(url: str, dest: Path) -> None:
    r = subprocess.run(["git", "clone", "--quiet", "--no-checkout", url, str(dest)],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise VerifyError(f"配布元を clone できない({url}): {r.stderr.strip()}")


def ref_commit(harness: Path, ref: str) -> str | None:
    for cand in (f"refs/remotes/origin/{ref}", f"refs/heads/{ref}"):
        r = git(harness, "rev-parse", "--verify", "--quiet", cand + "^{commit}", check=False)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def check_harness_commit(repo_dir: Path, base: str, head: str, harness: Path, sha: str,
                         harness_repo: str, harness_ref: str) -> str | None:
    """sha が配布元の <harness_ref> に含まれ、merge-base(base, head) の `.harness/version` から
    前進していることを確認する。問題があればその説明を返す。

    policy_gate.check_harness_commit と同じ判定(policy_gate は取り込み側の配布物として単独で動き、
    検証前の配布元のコードを import できないので複製している。tests/test_policy_gate.py で一致を確かめる)。
    """
    tip = ref_commit(harness, harness_ref)
    if tip is None:
        return f"配布元に {harness_ref} ブランチが無い"
    if git(harness, "cat-file", "-e", sha + "^{commit}", check=False).returncode != 0:
        return f"配布元に {sha} が無い"
    if not is_ancestor(harness, sha, tip):
        return f"{sha} は配布元の {harness_ref} に含まれない(レビュー前のコミットは同期に使えない)"
    mb = git(repo_dir, "merge-base", base, head, check=False)
    prev = parse_version(show(repo_dir, mb.stdout.strip(), VERSION_REL)) if mb.returncode == 0 else None
    if prev and prev.get("repo") == harness_repo and isinstance(prev.get("sha"), str):
        if git(harness, "cat-file", "-e", prev["sha"] + "^{commit}", check=False).returncode != 0 \
                or not is_ancestor(harness, prev["sha"], sha):
            return f"同期前の {prev['sha']} から {sha} への更新ではない(巻き戻し・分岐)"
    return None


def verify(repo_dir: Path, base: str, head: str, harness_repo: str = DEFAULT_HARNESS_REPO,
           harness_ref: str = DEFAULT_REF, harness_dir: Path | None = None,
           harness_url: str | None = None) -> tuple[bool, list[str]]:
    """戻り値: (一致したか, 理由・差分のメッセージ)。実行できなければ VerifyError。"""
    repo_dir = Path(repo_dir).resolve()
    msgs: list[str] = []
    head_sha = git(repo_dir, "rev-parse", "--verify", head + "^{commit}").stdout.strip()
    git(repo_dir, "rev-parse", "--verify", base + "^{commit}")
    mb = git(repo_dir, "merge-base", base, head_sha).stdout.strip()

    ver = parse_version(show(repo_dir, head_sha, VERSION_REL))
    if ver is None:
        return False, [f"head に {VERSION_REL} が無い(または JSON として不正)"]
    if ver.get("repo") != harness_repo:
        return False, [f"{VERSION_REL} の repo が想定外: {ver.get('repo')!r}(想定: {harness_repo!r})"]
    if ver.get("ref") != harness_ref:
        return False, [f"{VERSION_REL} の ref が想定外: {ver.get('ref')!r}(想定: {harness_ref!r})"]
    sha = ver.get("sha")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        return False, [f"{VERSION_REL} の sha が不正: {sha!r}"]

    tmp_root = Path(tempfile.mkdtemp(prefix="harness-verify-"))
    try:
        if harness_dir is None:
            harness_dir = tmp_root / "harness"
            clone_harness(harness_url or f"https://github.com/{harness_repo}.git", harness_dir)
        harness_dir = Path(harness_dir).resolve()
        problem = check_harness_commit(repo_dir, base, head_sha, harness_dir, sha, harness_repo, harness_ref)
        if problem:
            return False, [problem]

        wt = tmp_root / "worktree"
        git(repo_dir, "worktree", "add", "--quiet", "--detach", str(wt), mb)
        try:
            try:
                apply_sync(harness_dir, wt, sha, harness_repo, harness_ref)
            except SyncError as e:
                return False, [f"apply_sync が失敗した: {e}"]
            git(wt, "add", "-A")
            expected = git(wt, "write-tree").stdout.strip()
            actual = git(repo_dir, "rev-parse", head_sha + "^{tree}").stdout.strip()
            if expected == actual:
                msgs.append(f"head のツリーが {harness_repo}@{sha[:7]} の適用結果と一致")
                return True, msgs
            diff = git(repo_dir, "diff", "--no-renames", "--name-status", expected, actual).stdout
            msgs.append(f"head のツリーが {harness_repo}@{sha[:7]} の適用結果と一致しない(期待 → head):")
            msgs += [f"  {line}" for line in diff.splitlines()]
            return False, msgs
        finally:
            git(repo_dir, "worktree", "remove", "--force", str(wt), check=False)
            git(repo_dir, "worktree", "prune", check=False)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="PR の base(コミット)")
    ap.add_argument("--head", required=True, help="PR の head(コミット)")
    ap.add_argument("--repo-dir", default=".", help="取り込み側リポジトリ(base / head を含む)")
    ap.add_argument("--harness-repo", default=os.environ.get("HARNESS_REPO") or DEFAULT_HARNESS_REPO)
    ap.add_argument("--harness-ref", default=os.environ.get("HARNESS_REF") or DEFAULT_REF)
    ap.add_argument("--harness", help="clone 済みの配布元(省略時は --harness-url から clone)")
    ap.add_argument("--harness-url", help="配布元の clone URL(既定: https://github.com/<harness-repo>.git)")
    a = ap.parse_args(argv)
    try:
        ok, msgs = verify(Path(a.repo_dir), a.base, a.head, a.harness_repo, a.harness_ref,
                          Path(a.harness) if a.harness else None, a.harness_url)
    except VerifyError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    for m in msgs:
        print(m)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
