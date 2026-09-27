#!/usr/bin/env python3
"""対象リポジトリのチェックアウトに harness を導入する(冪等)。

Onboard Repository ワークフローが、対象リポジトリを `target/` に checkout した
状態で呼ぶ。行うこと:

1. 配布物(.claude .codex .kiro .agents [AGENTS.md])を上書きコピー
2. 同期ワークフロー(templates/consumer/harness-sync.yml)を配置
   - 既に harness-sync.yml / orchestration-sync.yml(unity-sdd-template 生成先)が
     あれば置かない
3. ルート CLAUDE.md に `@.claude/rules/sdd-workflow.md` を保証
4. `.kiro/orchestration/config.json` を生成(既にあれば触らない。`--linear-team` が
   空なら SDD ワークフローのみの導入とみなし作らない)

結果を JSON で `--result` に書く(ワークフローが PR 本文・サマリに使う)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_config import TEMPLATE_REL, render, split_checks  # noqa: E402

IMPORT_LINE = "@.claude/rules/sdd-workflow.md"
ASSET_DIRS = [".claude", ".codex", ".kiro", ".agents"]
SYNC_TEMPLATE = Path("templates/consumer/harness-sync.yml")
SYNC_DEST = Path(".github/workflows/harness-sync.yml")
TEMPLATE_INIT_SYNC = Path(".github/workflows/orchestration-sync.yml")
CONFIG_DEST = Path(".kiro/orchestration/config.json")
SYNC_PATHS_FULL = 'SYNC_PATHS: ".claude .codex .kiro .agents AGENTS.md"'
SYNC_PATHS_NO_AGENTS = 'SYNC_PATHS: ".claude .codex .kiro .agents"'
# unity-sdd-template 生成先の orchestration-sync.yml は同期パスをループに直書きしている
TEMPLATE_INIT_LOOP_FULL = "for p in .claude .codex .kiro .agents AGENTS.md; do"
TEMPLATE_INIT_LOOP_NO_AGENTS = "for p in .claude .codex .kiro .agents; do"

CLAUDE_MD_NEW = f"""<!--
  SDD ワークフローと Git 運用ルールは agentic-dev-harness から配布される
  .claude/rules/ にある。プロジェクト固有のメモはこのファイルに追記する。
-->

{IMPORT_LINE}
"""


def copy_path(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def sync_assets(harness: Path, target: Path, sync_agents_md: bool) -> list[str]:
    paths = ASSET_DIRS + (["AGENTS.md"] if sync_agents_md else [])
    synced: list[str] = []
    for p in paths:
        src = harness / p
        if not src.exists():
            print(f"::warning::{p} は配布元に存在しないためスキップ", file=sys.stderr)
            continue
        copy_path(src, target / p)
        synced.append(p)
    return synced


def exclude_agents_md(workflow: Path, full_line: str, no_agents_line: str) -> str:
    """既存の同期ワークフローから AGENTS.md を外す。戻り値: updated / existing。"""
    text = workflow.read_text(encoding="utf-8")
    if full_line in text:
        workflow.write_text(text.replace(full_line, no_agents_line), encoding="utf-8", newline="\n")
        return "updated"
    if no_agents_line in text:
        return "existing"
    raise RuntimeError(
        f"{workflow.name} の同期パス指定が想定と異なるため AGENTS.md を除外できない。"
        f"手動で AGENTS.md を同期対象から外してから再実行すること(期待した行: {full_line!r})"
    )


def place_sync_workflow(harness: Path, target: Path, sync_agents_md: bool) -> tuple[str, str]:
    """戻り値: (sync 方式, 今回の操作)。

    方式は registry の sync フィールドに対応(workflow / template-init)。
    操作は written(新規配置) / updated(既存の同期ワークフローから AGENTS.md を除外) /
    existing(既存を維持) / template-init(生成先の orchestration-sync.yml をそのまま使用)。
    """
    # 既存の同期ワークフローが AGENTS.md を同期し続けると、今回維持した独自 AGENTS.md が
    # 次回の同期で上書きされるので、sync_agents_md=false なら同期対象からも除外する。
    # 除外できない(行が想定と異なる)場合は成功扱いにせず失敗させ、手動修正を求める
    if (target / TEMPLATE_INIT_SYNC).exists():
        if sync_agents_md:
            return "template-init", "template-init"
        action = exclude_agents_md(target / TEMPLATE_INIT_SYNC, TEMPLATE_INIT_LOOP_FULL, TEMPLATE_INIT_LOOP_NO_AGENTS)
        return "template-init", ("updated" if action == "updated" else "template-init")
    dest = target / SYNC_DEST
    if dest.exists():
        if sync_agents_md:
            return "workflow", "existing"
        return "workflow", exclude_agents_md(dest, SYNC_PATHS_FULL, SYNC_PATHS_NO_AGENTS)
    text = (harness / SYNC_TEMPLATE).read_text(encoding="utf-8")
    if not sync_agents_md:
        if SYNC_PATHS_FULL not in text:
            raise RuntimeError("harness-sync.yml の SYNC_PATHS 行が想定と異なる")
        text = text.replace(SYNC_PATHS_FULL, SYNC_PATHS_NO_AGENTS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="\n")
    return "workflow", "written"


def ensure_claude_md(target: Path) -> str:
    f = target / "CLAUDE.md"
    if not f.exists():
        f.write_text(CLAUDE_MD_NEW, encoding="utf-8", newline="\n")
        return "created"
    text = f.read_text(encoding="utf-8")
    if any(line.strip() == IMPORT_LINE for line in text.splitlines()):
        return "unchanged"
    sep = "" if text.endswith("\n") else "\n"
    f.write_text(f"{text}{sep}\n{IMPORT_LINE}\n", encoding="utf-8", newline="\n")
    return "appended"


def write_config(harness: Path, target: Path, a: argparse.Namespace) -> str:
    if not a.linear_team.strip():
        return "skipped"  # SDD ワークフローのみ(linear-worker なし)
    dest = target / CONFIG_DEST
    if dest.exists():
        return "exists"
    template = json.loads((harness / TEMPLATE_REL).read_text(encoding="utf-8"))
    cfg = render(
        template,
        repo=a.repo,
        default_branch=a.default_branch,
        team=a.linear_team,
        project=a.linear_project.strip() or None,
        needs_local=a.needs_local.strip() or "needs-local",
        checks_fast=split_checks(a.checks_fast),
        review_bot=a.review_bot,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return "created"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", default=".")
    ap.add_argument("--target", required=True)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--default-branch", default="main")
    ap.add_argument("--linear-team", default="", help="空なら config.json を作らない(SDD のみ)")
    ap.add_argument("--linear-project", default="")
    ap.add_argument("--needs-local", default="needs-local")
    ap.add_argument("--checks-fast", default="")
    ap.add_argument("--review-bot", choices=["codex", "none"], default="codex")
    ap.add_argument("--sync-agents-md", default="true", help="true / false")
    ap.add_argument("--result", help="結果 JSON の書き出し先")
    a = ap.parse_args(argv)

    harness = Path(a.harness).resolve()
    target = Path(a.target).resolve()
    if not (harness / TEMPLATE_REL).exists():
        print(f"::error::{a.harness} は agentic-dev-harness ではない({TEMPLATE_REL} が無い)", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"::error::対象ディレクトリが無い: {target}", file=sys.stderr)
        return 1
    sync_agents_md = str(a.sync_agents_md).lower() == "true"

    result = {
        "assets_synced": sync_assets(harness, target, sync_agents_md),
    }
    try:
        result["sync_mode"], result["sync_workflow"] = place_sync_workflow(harness, target, sync_agents_md)
    except RuntimeError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    result["claude_md"] = ensure_claude_md(target)
    result["config"] = write_config(harness, target, a)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if a.result:
        Path(a.result).write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
