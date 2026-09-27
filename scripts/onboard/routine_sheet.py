#!/usr/bin/env python3
"""Routine(claude.ai/code/routines)作成用の設定シートを Markdown で出力する。

Routine は API から作れないため、ここで必要な値(名前・cron・ソース・プロンプト本文・
コネクタ・許可ツール)を 1 枚にまとめ、ワークフローのジョブサマリに載せる。
プロンプト本文は `.claude/skills/linear-worker/templates/routine-prompt.md` の
`---` 以下をそのまま使う(置換箇所なし)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROMPT_TEMPLATE = Path(".claude/skills/linear-worker/templates/routine-prompt.md")
TOOLS = (
    "Bash / Read / Write / Edit / Glob / Grep / WebFetch / WebSearch に加え、"
    "`/code-review` `/kiro:validate-impl` `/kiro:spec-impl` の起動に必要な "
    "Skill / SlashCommand / Task(Agent)"
)


def prompt_body(harness: Path) -> str:
    lines = (harness / PROMPT_TEMPLATE).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1 :]).strip() + "\n"
    raise SystemExit(f"::error::{PROMPT_TEMPLATE} に '---' 区切りが無い")


def render(a: argparse.Namespace, result: dict | None) -> str:
    repo_name = a.repo.split("/")[-1]
    routine_name = a.routine_name or f"自律駆動ハーネス ({repo_name})"
    out: list[str] = []
    out.append(f"# Onboarding: `{a.repo}`\n")

    out.append("## 自動で行ったこと\n")
    if result:
        out.append(f"- 配布物の同期: {', '.join(result.get('assets_synced', [])) or '(なし)'}")
        sync_msg = {
            "written": "`.github/workflows/harness-sync.yml` を配置",
            "updated": "既存の同期ワークフロー(`harness-sync.yml` / `orchestration-sync.yml`)の同期対象から AGENTS.md を除外",
            "existing": "既存の `harness-sync.yml` を維持",
            "template-init": "unity-sdd-template 生成先(`orchestration-sync.yml` 既存)のため配置せず",
        }
        out.append(f"- 同期ワークフロー: {sync_msg.get(result.get('sync_workflow'), result.get('sync_workflow'))}")
        claude_md = {"created": "新規作成", "appended": "import 行を追記", "unchanged": "変更なし(import 済み)"}
        out.append(f"- `CLAUDE.md`: {claude_md.get(result.get('claude_md'), result.get('claude_md'))}")
        cfg = {
            "created": "生成",
            "exists": "既存のため変更なし(値は手で確認すること)",
            "skipped": "作成せず(SDD ワークフローのみ。linear-worker は起動しない)",
        }
        out.append(f"- `.kiro/orchestration/config.json`: {cfg.get(result.get('config'), result.get('config'))}")
    if a.pr_url:
        out.append(f"- 対象リポジトリへの PR: {a.pr_url}")
    elif a.pushed_to:
        out.append(f"- 対象リポジトリの `{a.pushed_to}` へ直接 push")
    else:
        out.append("- 対象リポジトリ: 差分なし(導入済み)")
    if a.linear_note and not a.sdd_only:
        out.append(f"- Linear: {a.linear_note}")
    out.append(f"- 台帳(`registry/repos.yaml`): {a.registry_note or '更新なし'}")
    out.append("")

    out.append("## 残りの手作業\n")
    if a.sdd_only:
        if a.pr_url:
            out.append("1. 対象リポジトリの PR をレビューしてマージする")
        out.append("")
        out.append(
            "SDD ワークフローのみの導入なので Routine は不要。後から linear-worker を動かす場合は "
            "Onboard Repository を `linear_team` 付きで再実行する(配布物は同期済みなので config と台帳だけ差分になる)。"
        )
        return "\n".join(out) + "\n"
    step = 1
    if a.pr_url:
        out.append(f"{step}. 対象リポジトリの PR をレビューしてマージする(`config.json` の値を確認。`checks.fast` が空なら CI 相当のコマンドを入れる)")
        step += 1
    out.append(f"{step}. GitHub 側: PR で CI が走ることを確認する。外部レビューボット(例: Codex)を使うなら GitHub App をインストールし、PR で動くことを 1 回確認する")
    step += 1
    out.append(f"{step}. 下の設定シートで Routine を作成し、1 回手動実行してログを確認する(`auto_merge.enabled` は最初は false のまま)")
    step += 1
    entry_name = a.entry_name or repo_name
    out.append(
        f"{step}. Routine の ID を台帳に記入する: Actions → **Registry Update** を "
        f"`name={entry_name}` `routine_id=<ID>` `config=true` で実行"
    )
    out.append("")

    out.append("## Routine 設定シート\n")
    out.append("| 項目 | 値 |")
    out.append("|---|---|")
    out.append(f"| 名前 | `{routine_name}` |")
    out.append(f"| cron(UTC) | `{a.cron}` |")
    out.append(f"| ソース | `https://github.com/{a.repo}`(デフォルトブランチ `{a.default_branch}`) |")
    out.append("| MCP コネクタ | Linear(必須)。Notion 等は任意 |")
    out.append(f"| 許可ツール | {TOOLS} |")
    out.append("| 通知 | push 通知を有効にすると空振り・駐機の報告が届く |")
    out.append("")
    out.append("プロンプト(そのまま貼る。置換箇所なし):\n")
    out.append("```text")
    out.append(prompt_body(Path(a.harness)).rstrip("\n"))
    out.append("```")
    out.append("")
    out.append("Claude Code からは `/schedule` でも作成できる(同じ値を渡す)。")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", default=".")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--default-branch", default="main")
    ap.add_argument("--routine-name", default="")
    ap.add_argument("--cron", default="52 * * * *")
    ap.add_argument("--result", help="bootstrap_target.py の結果 JSON")
    ap.add_argument("--pr-url", default="")
    ap.add_argument("--pushed-to", default="", help="直接 push したブランチ名")
    ap.add_argument("--linear-note", default="")
    ap.add_argument("--registry-note", default="")
    ap.add_argument("--sdd-only", action="store_true", help="linear-worker なし(Routine シートを出さない)")
    ap.add_argument("--entry-name", default="", help="台帳上の実際のエントリ名(省略時はリポジトリ名)")
    a = ap.parse_args(argv)

    result = json.loads(Path(a.result).read_text(encoding="utf-8")) if a.result else None
    sys.stdout.write(render(a, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
