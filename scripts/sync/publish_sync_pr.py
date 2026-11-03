#!/usr/bin/env python3
"""apply_sync 済みの作業ツリーから同期 PR を作る(配布元専用。Harness Sync / Rollout が呼ぶ)。

1. 差分が無ければ何もしない
2. ブランチ `chore/harness-sync-<sha7>` に commit して push(同名のリモートブランチが PR 無しで
   残っていれば、同期専用ブランチなので消してから push する。force-push はしない)
3. ラベル harness-sync 付きで PR を作成(同じブランチの open PR があれば作らない)
4. 古い同期 PR(harness-sync ラベル・`chore/harness-sync-` ブランチ)をクローズしてブランチを削除
5. `gh pr merge --auto --merge` で自動マージを予約(失敗は warning。必須チェックは policy-gate が
   完全一致検証で判定する)

`gh` CLI を使う(環境変数 GH_TOKEN に GitHub App のトークン。GITHUB_TOKEN で push すると
ワークフローの更新も CI の起動もできない)。標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "harness-sync"
BRANCH_PREFIX = "chore/harness-sync-"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise SystemExit(f"::error::{' '.join(cmd)} に失敗: {r.stderr.strip() or r.stdout.strip()}")
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="apply_sync 済みの取り込み側作業ツリー")
    ap.add_argument("--repo", required=True, help="取り込み側 owner/name")
    ap.add_argument("--base", required=True, help="PR の base(取り込み側の既定ブランチ)")
    ap.add_argument("--sha", required=True, help="適用した配布元のコミット")
    ap.add_argument("--harness-repo", required=True)
    ap.add_argument("--run-url", default="", help="PR 本文に載せる実行 URL")
    a = ap.parse_args(argv)
    t = Path(a.target)
    sha7 = a.sha[:7]
    branch = BRANCH_PREFIX + sha7

    run(["git", "add", "-A"], t)
    if run(["git", "diff", "--cached", "--quiet"], t, check=False).returncode == 0:
        print(f"差分なし({a.harness_repo}@{sha7} は適用済み)")
        return 0

    prs = json.loads(run(["gh", "pr", "list", "--repo", a.repo, "--state", "open", "--label", LABEL,
                          "--limit", "100", "--json", "number,headRefName,url"]).stdout or "[]")
    mine = [p for p in prs if p["headRefName"] == branch]
    if mine:
        print(f"同期 PR が既にある: {mine[0]['url']}")
        return 0

    run(["git", "checkout", "-q", "-B", branch], t)
    run(["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q", "-m",
         f"chore: sync harness assets ({a.harness_repo}@{sha7})"], t)
    if run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], t, check=False).returncode == 0:
        print(f"::notice::PR の無い同期ブランチ {branch} が残っていたため削除して作り直します")
        run(["git", "push", "-q", "origin", "--delete", branch], t)
    run(["git", "push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}"], t)

    run(["gh", "label", "create", LABEL, "--repo", a.repo, "--color", "ededed",
         "--description", "agentic-dev-harness の配布物の同期", "--force"], check=False)
    body = "\n".join([
        f"[{a.harness_repo}](https://github.com/{a.harness_repo}) の "
        f"[`{sha7}`](https://github.com/{a.harness_repo}/commit/{a.sha}) を取り込みます(Harness Sync が自動作成)。",
        "",
        "- 配布物(`.claude/skills/linear-worker` 等)・配布ワークフロー・`CLAUDE.md` の import・config の移行・"
        "`.harness/version` を更新します",
        "- `policy-gate` が「配布元の指定コミットを適用した結果と完全一致」を検証します(一致すれば人間の承認は不要)",
        "- 配布物はこのリポジトリで直接編集しないでください(配布元で修正して同期します)",
    ] + (["", f"実行: {a.run_url}"] if a.run_url else []))
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body + "\n")
        body_file = f.name
    url = run(["gh", "pr", "create", "--repo", a.repo, "--base", a.base, "--head", branch,
               "--title", f"chore: sync harness assets ({sha7})", "--body-file", body_file,
               "--label", LABEL]).stdout.strip()
    Path(body_file).unlink(missing_ok=True)
    print(f"PR: {url}")

    for p in prs:
        if p["headRefName"].startswith(BRANCH_PREFIX) and p["headRefName"] != branch:
            r = run(["gh", "pr", "close", str(p["number"]), "--repo", a.repo, "--delete-branch",
                     "--comment", f"新しい同期 PR {url} に置き換えたためクローズします。"], check=False)
            if r.returncode != 0:
                print(f"::warning::古い同期 PR #{p['number']} をクローズできませんでした: {r.stderr.strip()}")

    r = run(["gh", "pr", "merge", url, "--auto", "--merge"], check=False)
    if r.returncode != 0:
        print(f"::warning::自動マージを予約できませんでした(Allow auto-merge が無効?): {r.stderr.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
