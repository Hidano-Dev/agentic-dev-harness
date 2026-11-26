#!/usr/bin/env python3
"""取り込み側の config(`.claude/orchestration.json`。旧 `.kiro/orchestration/config.json` も読む)に 2 段構成のブランチ設定を書く(配布元専用)。

Harness Rollout(.github/workflows/rollout.yml)が `configure_settings` 付きで動いたとき、
configure_repo.py が develop を作って既定ブランチを切り替えた後に呼ぶ。apply_sync.py(同期の
検証にも使う決定的な処理)とは分け、ここでだけ `github.default_branch` / `github.release_branch` を
変える。config はコア(auto_merge.protected_paths)なので、この変更を含む同期 PR は
policy-gate が人間の承認を求める(初回導入時に 1 度だけ人間が確認する)。

- config が無いリポジトリでは何もしない(Linear 連携なし = harness-sync だけの導入)
- すでに同じ値なら書かない(冪等)
標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "linear-worker" / "scripts"))
from harness_config import CONFIG_REL, LEGACY_CONFIG_REL, choose_config_rel  # noqa: E402


def set_branch_model(cfg: dict, default_branch: str, release_branch: str | None) -> bool:
    gh = cfg.setdefault("github", {})
    if not isinstance(gh, dict):
        raise ValueError("github がオブジェクトではない")
    release = release_branch if release_branch and release_branch != default_branch else None
    changed = gh.get("default_branch") != default_branch or gh.get("release_branch", "__missing__") != release
    gh["default_branch"] = default_branch
    gh["release_branch"] = release
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="取り込み側リポジトリの作業ツリー")
    ap.add_argument("--default-branch", required=True, help="作業ブランチ(例: develop)")
    ap.add_argument("--release-branch", default="", help="リリースブランチ(例: main。空なら単一ブランチ運用)")
    a = ap.parse_args(argv)

    target = Path(a.target)
    rel = choose_config_rel(lambda p: (target / p).is_file(), print)
    if rel is None:
        print(f"::notice::{CONFIG_REL}(旧 {LEGACY_CONFIG_REL})が無いためブランチ設定は書きません")
        return 0
    path = target / rel
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        print(f"::error::{rel} の最上位が JSON オブジェクトではありません")
        return 1
    try:
        changed = set_branch_model(cfg, a.default_branch, a.release_branch or None)
    except ValueError as e:
        print(f"::error::{rel}: {e}")
        return 1
    if changed:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"::notice::{rel}: github.default_branch={a.default_branch} / "
              f"release_branch={cfg['github']['release_branch']} に設定")
    return 0


if __name__ == "__main__":
    sys.exit(main())
