#!/usr/bin/env python3
"""取り込み側リポジトリの設定ファイル(orchestration config)の置き場所の解決。

設定は `.claude/orchestration.json` に置く。2026-11-26 までの置き場所 `.kiro/orchestration/config.json`
も互換のため読む(Kiro / SDD を使わないリポジトリから `.kiro/` を丸ごと消せるようにするための移動)。

- 新パスがあれば新パス、無ければ旧パス。両方あれば新パスを使い、警告を出す
- どちらも無ければ新パス(存在しないパス = 既定値で動く / 新しく作る場所)
- Harness Sync(scripts/sync/apply_sync.py)は旧パスだけのリポジトリの config を新パスへ移す。
  両方あれば同期を失敗させて人間に回す
- ゲートの判定(policy_gate.py・codex-review.yml・conflict-sweeper.yml)は、両方あれば
  `strictest_config` で厳しい側に倒した config を使う。片方だけを正とすると、保護されていない側の
  ファイルを置く・書き換えるだけで制約を外せるため

設定のパスを知る必要のあるスクリプトはこのモジュールを使う。ただし単独のファイルとして
取り出して実行されるもの(policy_gate.py / resolve_conflicts.py)は import できないので
`choose_config_rel` と `strictest_config` を写している(tests/test_harness_config.py が一致を検査する)。
ワークフロー(templates/consumer/*.yml)のシェルも同じ優先順位で読む。

CLI: `python3 harness_config.py strictest <新パスの内容> <旧パスの内容>` は 2 つの config を
`strictest_config` でまとめた JSON を標準出力に書く(JSON として読めない側は無視する。
両方読めなければ終了コード 1)。codex-review.yml が使う。

標準ライブラリのみ使用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

CONFIG_REL = ".claude/orchestration.json"
LEGACY_CONFIG_REL = ".kiro/orchestration/config.json"
# 読む順(先にあるものを優先する)
CONFIG_CANDIDATES = (CONFIG_REL, LEGACY_CONFIG_REL)
# auto_merge.protected_paths が無い側に使う既定(policy_gate.py の DEFAULT_CORE と同じ。テストが一致を検査する)
DEFAULT_PROTECTED_PATHS = [".github/", ".harness/", ".kiro/orchestration/", ".kiro/settings/",
                           ".claude/settings.json", ".codex/", CONFIG_REL]


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


def strictest_config(new: dict, old: dict, default_protected: list[str]) -> dict:
    """新旧両方の config があるときに使う、厳しい側に倒した config。

    - auto_merge.protected_paths: 両方の和集合(文字列の配列でない側は default_protected を使う)
    - auto_merge.enabled: 両方 true のときだけ true。auto_merge.merge_parked: どちらかが false なら false
    - review.fail_open: どちらかが false なら false。review.provider: 食い違えば action(必須チェックを出す側)
    - fast_track / conflicts: 食い違えば fast_track.enabled / conflicts.auto_resolve を false
    - github: 食い違えば release_branch を null(Release PR として緩めた判定をしない)
    - それ以外のキーは新パスの値
    """
    def obj(cfg: dict, key: str) -> dict:
        value = cfg.get(key)
        return value if isinstance(value, dict) else {}

    def protected(am: dict) -> list:
        paths = am.get("protected_paths")
        if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
            return list(paths)
        return list(default_protected)

    merged = json.loads(json.dumps(new))
    am_new, am_old = obj(new, "auto_merge"), obj(old, "auto_merge")
    am = dict(am_new)
    am["protected_paths"] = list(dict.fromkeys(protected(am_new) + protected(am_old)))
    am["enabled"] = am_new.get("enabled") is True and am_old.get("enabled") is True
    if am_new.get("merge_parked") is False or am_old.get("merge_parked") is False:
        am["merge_parked"] = False
    merged["auto_merge"] = am
    rv_new, rv_old = obj(new, "review"), obj(old, "review")
    if rv_new or rv_old:
        rv = dict(rv_new)
        if rv_new.get("fail_open") is False or rv_old.get("fail_open") is False:
            rv["fail_open"] = False
        if rv_new.get("provider", "action") != rv_old.get("provider", "action"):
            rv["provider"] = "action"
        merged["review"] = rv
    for key, flag in (("fast_track", "enabled"), ("conflicts", "auto_resolve")):
        if obj(new, key) != obj(old, key):
            merged[key] = {**obj(new, key), flag: False}
    if obj(new, "github") != obj(old, "github"):
        merged["github"] = {**obj(new, "github"), "release_branch": None}
    return merged


def resolve_config_path(root: str | Path = ".", *, warn: bool = True) -> Path:
    """作業ツリー root で使う config のパス。どちらも無ければ新パス(存在しないパス)を返す。"""
    root = Path(root)
    rel = choose_config_rel(lambda p: (root / p).is_file(), _warn_stderr if warn else None)
    return root / (rel or CONFIG_REL)


def _load_dict(path: str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "strictest":
        print("usage: harness_config.py strictest <新パスの config> <旧パスの config>", file=sys.stderr)
        return 2
    new, old = _load_dict(args[1]), _load_dict(args[2])
    if new is None and old is None:
        print("::error::どちらの config も JSON オブジェクトとして読めません", file=sys.stderr)
        return 1
    if new is not None and old is not None:
        out = strictest_config(new, old, DEFAULT_PROTECTED_PATHS)
    else:
        out = new if new is not None else old
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
