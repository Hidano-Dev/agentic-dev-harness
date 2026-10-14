#!/usr/bin/env python3
"""集約型ドキュメント(README.md 等)をリリース用 PR 以外で変更していないか検査する。

`git-workflow.md` §7 の「ドキュメントの置き場所」を機械的に守らせるためのもの。
並列に動く複数の PR が同じ集約型ドキュメントへ追記するとマージコンフリクトが連鎖し、
自律ワーカーの自動進行が止まる。開発途中の記録は Linear に置き、集約型ドキュメントは
リリース用 PR だけがまとめて更新する。

- 呼び出し元: 取り込み側の CI(templates/consumer/docs-policy.yml)と、linear-worker の
  一次ゲート(SKILL.md §4 手順 1)
- `.kiro/orchestration/config.json` が無い・`linear.team` が無いリポジトリは対象外
  (記録先の Linear が無いため)。何もせず成功する
- 対象パスは config の `docs.aggregate_paths`(無ければ既定値)。`/` で終わる値は
  ディレクトリ接頭辞、それ以外は完全一致。空配列ならこの検査を無効にする
- リリース用 PR(ヘッドブランチが `docs.release_branch_prefix` で始まる、または
  ラベル `docs.release_label` が付いている)は対象パスを変更してよい

標準ライブラリのみ使用(ランナー・ローカルどちらでも動く)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CONFIG_REL = Path(".kiro/orchestration/config.json")
# SKILL.md 設定表の既定値。キーが無い config ではこれを使う
DEFAULT_AGGREGATE_PATHS = ["README.md", "CHANGELOG.md", "docs/backlog.md"]
DEFAULT_RELEASE_BRANCH_PREFIX = "release/"
DEFAULT_RELEASE_LABEL = "release-docs"


def load_policy(config: Path) -> dict | None:
    """検査に使う設定を返す。対象外のリポジトリなら None。"""
    if not config.is_file():
        return None
    cfg = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"{config} の最上位が JSON オブジェクトではない")
    team = (cfg.get("linear") or {}).get("team")
    if not isinstance(team, str) or not team.strip():
        return None
    docs = cfg.get("docs") or {}
    if not isinstance(docs, dict):
        raise ValueError(f"{config} の docs がオブジェクトではない")
    paths = docs.get("aggregate_paths", DEFAULT_AGGREGATE_PATHS)
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError(f"{config} の docs.aggregate_paths が文字列の配列ではない")
    return {
        "paths": paths,
        "release_branch_prefix": docs.get("release_branch_prefix") or DEFAULT_RELEASE_BRANCH_PREFIX,
        "release_label": docs.get("release_label") or DEFAULT_RELEASE_LABEL,
    }


def matches(path: str, patterns: list[str]) -> bool:
    return any(path.startswith(p) if p.endswith("/") else path == p for p in patterns)


def changed_files(base: str, head: str) -> list[str]:
    # リネームは削除 + 追加として両方の名前を出す(集約型ドキュメントの移動も検出する)
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(CONFIG_REL))
    ap.add_argument("--base", required=True, help="比較元(PR のベース SHA、または origin/<default_branch>)")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--head-ref", default="", help="PR のヘッドブランチ名")
    ap.add_argument("--labels-json", default="[]", help="PR のラベル名の JSON 配列")
    a = ap.parse_args(argv)

    try:
        policy = load_policy(Path(a.config))
    except (ValueError, json.JSONDecodeError) as e:
        print(f"::error::{e}")
        return 1
    if policy is None:
        print("::notice::Linear 連携の設定が無いため、ドキュメントの置き場所の検査は対象外")
        return 0
    if not policy["paths"]:
        print("::notice::docs.aggregate_paths が空のため、ドキュメントの置き場所の検査は無効")
        return 0

    labels = json.loads(a.labels_json or "[]")
    if a.head_ref.startswith(policy["release_branch_prefix"]) or policy["release_label"] in labels:
        print("::notice::リリース用 PR のため、集約型ドキュメントの変更を許可")
        return 0

    try:
        files = changed_files(a.base, a.head)
    except subprocess.CalledProcessError as e:
        print(f"::error::git diff に失敗: {e.stderr.strip()}")
        return 1
    hits = [f for f in files if matches(f, policy["paths"])]
    if not hits:
        return 0
    for f in hits:
        print(f"::error file={f}::{f} は集約型ドキュメント(docs.aggregate_paths)です。")
    print(
        "集約型ドキュメントはリリース用 PR(ブランチ接頭辞 "
        f"`{policy['release_branch_prefix']}` またはラベル `{policy['release_label']}`)でだけ更新します。"
        "反映したい内容は Linear Issue の「ドキュメント反映メモ」に書いてください"
        "(.claude/rules/git-workflow.md §7)。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
