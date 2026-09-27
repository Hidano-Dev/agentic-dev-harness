#!/usr/bin/env python3
"""`.kiro/orchestration/config.json` を雛形から生成する。

Onboard Repository ワークフロー(.github/workflows/onboard-repo.yml)から
bootstrap_target.py 経由で呼ばれる。雛形は
`.claude/skills/linear-worker/templates/orchestration-config.json`。
標準ライブラリのみ使用(ランナー・ローカルどちらでも動く)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TEMPLATE_REL = Path(".claude/skills/linear-worker/templates/orchestration-config.json")


def split_checks(raw: str) -> list[str]:
    """カンマ区切りの fast checks 文字列を配列にする(空要素は捨てる)。"""
    return [c.strip() for c in raw.split(",") if c.strip()]


def render(
    template: dict,
    *,
    repo: str,
    default_branch: str,
    team: str,
    project: str | None,
    needs_local: str,
    checks_fast: list[str],
    review_bot: str,
) -> dict:
    cfg = json.loads(json.dumps(template))  # deep copy(キー順は雛形どおり)
    cfg["$comment"] = (
        f"{repo} の harness 設定。Onboard Repository ワークフローが雛形から生成した。"
        "dev-orchestrator(confirmation_channel)と linear-worker(キュー・ゲート・自動マージ)が読む。"
        "キーの意味は .claude/skills/linear-worker/SKILL.md の「設定」節。"
    )
    cfg["linear"]["team"] = team
    cfg["linear"]["project"] = project or None
    cfg["linear"]["labels"]["needs_local"] = needs_local
    cfg["github"]["repo"] = repo
    cfg["github"]["default_branch"] = default_branch
    cfg["checks"]["fast"] = checks_fast
    if review_bot == "none":
        cfg["review"]["bot"] = None
    elif review_bot != "codex":
        raise ValueError(f"unknown review_bot: {review_bot}")
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", default=".", help="agentic-dev-harness のチェックアウト先")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--default-branch", default="main")
    ap.add_argument("--team", required=True)
    ap.add_argument("--project", default="")
    ap.add_argument("--needs-local", default="needs-local")
    ap.add_argument("--checks-fast", default="", help="カンマ区切り")
    ap.add_argument("--review-bot", choices=["codex", "none"], default="codex")
    ap.add_argument("--out", help="出力先(省略時は stdout)")
    a = ap.parse_args(argv)

    template = json.loads((Path(a.harness) / TEMPLATE_REL).read_text(encoding="utf-8"))
    cfg = render(
        template,
        repo=a.repo,
        default_branch=a.default_branch,
        team=a.team,
        project=a.project.strip() or None,
        needs_local=a.needs_local.strip() or "needs-local",
        checks_fast=split_checks(a.checks_fast),
        review_bot=a.review_bot,
    )
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
