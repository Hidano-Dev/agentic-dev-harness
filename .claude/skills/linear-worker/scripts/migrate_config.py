#!/usr/bin/env python3
"""`.kiro/orchestration/config.json` に配布元の設定変更を移行として適用する。

取り込み側の Harness Sync(templates/consumer/harness-sync.yml)が配布物のコピー後に呼ぶ。
config.json は同期でコピーされない(リポジトリ固有の値を持つ)ため、雛形の既定値を
変えただけでは既存リポジトリに届かない。届けたい変更をここに移行として足す。

- 各移行は 1 度だけ適用し、ID を config の `applied_migrations` に記録する。適用後に
  人間が値を戻しても、次の同期で再び上書きしない
- config の形が想定外で適用できない移行は記録せずに警告し、修正後の同期で再試行する
- 雛形(templates/orchestration-config.json)の `applied_migrations` には全移行の ID を
  入れておく。雛形から生成した新しい config は移行済みとして扱われ、導入時に人間が
  決めた値を同期が書き換えない
- 対象は `auto_merge` のみ。それ以外のキーを書き換える移行は足さない
標準ライブラリのみ使用(ランナー・ローカルどちらでも動く)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONFIG_REL = Path(".kiro/orchestration/config.json")
# SKILL.md 設定表の既定値。キーが無い config ではワーカーがこれを使う
DEFAULT_PROTECTED_PATHS = [".claude/", ".github/", ".kiro/settings/"]


class NotApplicable(Exception):
    """config の形が想定外で適用できない。ID を記録せず、次回の同期で再試行する。"""


def _auto_merge(cfg: dict) -> dict:
    am = cfg.setdefault("auto_merge", {})
    if not isinstance(am, dict):
        raise NotApplicable("auto_merge がオブジェクトではない")
    return am


def enable_auto_merge(cfg: dict) -> str | None:
    """雛形の既定を true に変えた(2026-10-01)のに合わせ、既存 config も true にする。"""
    am = _auto_merge(cfg)
    if am.get("enabled") is True:
        return None
    am["enabled"] = True
    return "auto_merge.enabled を true に変更"


def protect_orchestration(cfg: dict) -> str | None:
    """config 自体を変える PR が自動マージされないよう、protected_paths に追加する。"""
    am = _auto_merge(cfg)
    paths = am.get("protected_paths")
    if paths is None:
        # キーが無い = 既定値で動いている。追加分だけ書くと既定の保護が外れるので既定ごと書く
        paths = list(DEFAULT_PROTECTED_PATHS)
    elif not isinstance(paths, list):
        raise NotApplicable("auto_merge.protected_paths が配列ではない")
    if ".kiro/orchestration/" in paths:
        return None
    am["protected_paths"] = paths + [".kiro/orchestration/"]
    return "auto_merge.protected_paths に .kiro/orchestration/ を追加"


# (ID, 適用関数)。適用順に並べ、ID は変えない。追加したら雛形の applied_migrations にも足す
MIGRATIONS = [
    ("2026-10-01-auto-merge-enabled", enable_auto_merge),
    ("2026-10-01-protect-orchestration-config", protect_orchestration),
]


def migrate(cfg: dict) -> tuple[list[str], list[str]]:
    """未適用の移行を適用し、(適用ログ, 適用できなかった移行の警告) を返す。cfg を直接書き換える。"""
    applied = cfg.get("applied_migrations")
    if not isinstance(applied, list):
        applied = []
    log, warnings = [], []
    for mid, fn in MIGRATIONS:
        if mid in applied:
            continue
        try:
            msg = fn(cfg)
        except NotApplicable as e:
            warnings.append(f"{mid}: {e}ため適用できません。修正すると次回の同期で適用します")
            continue
        if msg:
            log.append(f"{mid}: {msg}")
        applied.append(mid)
    if applied or "applied_migrations" in cfg:
        cfg["applied_migrations"] = applied
    return log, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", nargs="?", default=str(CONFIG_REL))
    a = ap.parse_args(argv)

    path = Path(a.config)
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8")
    cfg = json.loads(text)
    if not isinstance(cfg, dict):
        print(f"::error::{path} の最上位が JSON オブジェクトではありません")
        return 1
    log, warnings = migrate(cfg)
    if json.loads(text) != cfg:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for line in log:
        print(f"::notice::{path}: {line}")
    for line in warnings:
        print(f"::warning::{path}: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
