#!/usr/bin/env python3
"""取り込み側リポジトリの設定ファイル(orchestration config)の置き場所の解決。

設定は `.claude/orchestration.json` に置く。2026-11-26 までの置き場所 `.kiro/orchestration/config.json`
も互換のため読む(Kiro / SDD を使わないリポジトリから `.kiro/` を丸ごと消せるようにするための移動)。

- 新パスがあれば新パス、無ければ旧パス。両方あれば新パスを使い、警告を出す
- どちらも無ければ新パス(存在しないパス = 既定値で動く / 新しく作る場所)
- Harness Sync(scripts/sync/apply_sync.py)は旧パスだけのリポジトリの config を新パスへ移す

設定のパスを知る必要のあるスクリプトはこのモジュールを使う。ただし単独のファイルとして
取り出して実行されるもの(policy_gate.py / resolve_conflicts.py)は import できないので
`choose_config_rel` を写している(tests/test_harness_config.py が一致を検査する)。
ワークフロー(templates/consumer/*.yml)のシェルも同じ優先順位で読む。

標準ライブラリのみ使用。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

CONFIG_REL = ".claude/orchestration.json"
LEGACY_CONFIG_REL = ".kiro/orchestration/config.json"
# 読む順(先にあるものを優先する)
CONFIG_CANDIDATES = (CONFIG_REL, LEGACY_CONFIG_REL)


def _warn_stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def choose_config_rel(exists: Callable[[str], bool], warn: Callable[[str], None] | None = None) -> str | None:
    """存在する config の相対パス(新パス優先)。どちらも無ければ None。

    exists はリポジトリ直下からの相対パスを受け取り、そのファイルがあるかを返す
    (作業ツリーなら Path.is_file、git のコミットなら `git cat-file -e` など)。
    """
    new, old = exists(CONFIG_REL), exists(LEGACY_CONFIG_REL)
    if new and old and warn is not None:
        warn(f"::warning::{CONFIG_REL} と {LEGACY_CONFIG_REL} の両方があります。{CONFIG_REL} を使います"
             f"(旧パスのファイルは削除してください)")
    if new:
        return CONFIG_REL
    if old:
        return LEGACY_CONFIG_REL
    return None


def resolve_config_path(root: str | Path = ".", *, warn: bool = True) -> Path:
    """作業ツリー root で使う config のパス。どちらも無ければ新パス(存在しないパス)を返す。"""
    root = Path(root)
    rel = choose_config_rel(lambda p: (root / p).is_file(), _warn_stderr if warn else None)
    return root / (rel or CONFIG_REL)
