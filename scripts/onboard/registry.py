#!/usr/bin/env python3
"""`registry/repos.yaml`(実行リポジトリ台帳)への追記・更新。

YAML ライブラリに依存せず、台帳の既存書式(コメント付き)を保ったまま
テキストとして編集する。

  add    … エントリを末尾に追加(同名エントリがあれば何もしない)
  update … 既存エントリの harness.config / routine.id / status を書き換える
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ENTRY_RE = re.compile(r"^  - name: (?P<name>\S+)\s*$", re.M)


def yaml_str(s: str) -> str:
    """値をダブルクォートの YAML スカラーにする(日本語・記号を安全に)。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_entry(a: argparse.Namespace) -> str:
    project = yaml_str(a.project) if a.project.strip() else "null"
    sync_comment = {
        "workflow": ".github/workflows/harness-sync.yml",
        "template-init": "unity-sdd-template 生成先(orchestration-sync.yml で追従)",
        "manual": "手動コピー",
    }[a.sync]
    config_comment = {
        "true": "",
        "pending": "   # 導入 PR マージ後に Registry Update で true にする",
        "false": "",
    }[a.config_state]
    notes = f"{a.date} に Onboard Repository ワークフローで導入"
    details = []
    if a.sdd_only:
        details.append("SDD ワークフローのみ。linear-worker なし")
    if a.pr_url:
        details.append(f"PR: {a.pr_url}")
    if details:
        notes += "(" + "。".join(details) + ")"
    notes += "。"
    if a.notes:
        notes += " " + a.notes.strip()
    if a.sdd_only:
        return f"""
  - name: {a.name}
    github: {a.github}
    role: execution
    sync: {a.sync}   # {sync_comment}
    harness: null   # linear-worker を動かす場合は Onboard Repository を linear_team 付きで再実行
    notes: >-
      {notes}
"""
    return f"""
  - name: {a.name}
    github: {a.github}
    role: execution
    sync: {a.sync}   # {sync_comment}
    harness:
      config: {a.config_state}{config_comment}
      linear:
        team: {yaml_str(a.team)}
        project: {project}
        labels:
          needs_human: {yaml_str(a.needs_human)}
          needs_local: {yaml_str(a.needs_local)}
      routine:
        name: {yaml_str(a.routine_name)}
        cron: {yaml_str(a.cron)}   # UTC
        id: null   # Routine 作成後に Registry Update ワークフローで記入
        connectors: [Linear]
      status: pilot
    notes: >-
      {notes}
"""


def find_block(text: str, name: str) -> tuple[int, int]:
    """名前が一致するエントリの [start, end) 文字位置を返す。"""
    starts = [(m.start(), m.group("name")) for m in ENTRY_RE.finditer(text)]
    for i, (pos, n) in enumerate(starts):
        if n == name:
            end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
            return pos, end
    raise SystemExit(f"::error::台帳にエントリ '{name}' が無い")


def cmd_add(a: argparse.Namespace) -> int:
    path = Path(a.file)
    text = path.read_text(encoding="utf-8")
    if any(m.group("name") == a.name for m in ENTRY_RE.finditer(text)):
        s, e = find_block(text, a.name)
        block = text[s:e]
        # SDD のみ(harness: null)で登録済みのリポジトリに linear-worker を足す再実行なら
        # エントリを harness 付きへ置き換える。それ以外の既存エントリは触らない
        if not a.sdd_only and re.search(r"^    harness: null\b", block, re.M):
            trail = block[len(block.rstrip("\n")) :]  # 元の末尾改行(次エントリとの空行)を保つ
            new_block = render_entry(a).strip("\n") + (trail or "\n")
            path.write_text(text[:s] + new_block + text[e:], encoding="utf-8", newline="\n")
            print(f"registry: '{a.name}' を SDD のみ → linear-worker 付きに更新")
            return 0
        print(f"registry: '{a.name}' は登録済み(変更なし)")
        return 0
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + render_entry(a), encoding="utf-8", newline="\n")
    print(f"registry: '{a.name}' を追加")
    return 0


def replace_in_block(block: str, key_re: str, new_value: str, label: str) -> str:
    """ブロック内の `<indent><key>: <value>[   # comment]` 行の値を置き換える(1 行目のみ)。"""
    pat = re.compile(rf"^(?P<head>\s+{key_re}: )(?P<val>[^\n#]*?)(?P<tail>\s*(#.*)?)$", re.M)
    m = pat.search(block)
    if not m:
        raise SystemExit(f"::error::エントリ内に {label} の行が見つからない")
    return block[: m.start("val")] + new_value + block[m.end("val") :]


def cmd_update(a: argparse.Namespace) -> int:
    path = Path(a.file)
    text = path.read_text(encoding="utf-8")
    s, e = find_block(text, a.name)
    block = text[s:e]
    changed = []
    if a.routine_id:
        block = replace_in_block(block, r"id", a.routine_id, "routine.id")
        # 「Routine 作成後に記入」の案内コメントは役目を終えるので落とす
        block = re.sub(r"^(\s+id: \S+)\s+# Routine 作成後に.*$", r"\1", block, flags=re.M)
        changed.append(f"routine.id={a.routine_id}")
    if a.status and a.status != "keep":
        block = replace_in_block(block, r"status", a.status, "status")
        changed.append(f"status={a.status}")
    if a.config and a.config != "keep":
        block = replace_in_block(block, r"config", a.config, "harness.config")
        if a.config == "true":
            block = re.sub(r"^(\s+config: true)\s+# 導入 PR マージ後に.*$", r"\1", block, flags=re.M)
        changed.append(f"config={a.config}")
    if not changed:
        print("registry: 変更項目が指定されていない(変更なし)")
        return 0
    path.write_text(text[:s] + block + text[e:], encoding="utf-8", newline="\n")
    print(f"registry: '{a.name}' を更新 → " + ", ".join(changed))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="registry/repos.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--github", required=True, help="owner/name")
    add.add_argument("--sync", choices=["workflow", "template-init", "manual"], default="workflow")
    add.add_argument("--config-state", choices=["true", "pending", "false"], default="pending")
    add.add_argument("--sdd-only", action="store_true", help="linear-worker なし(harness: null)で登録")
    add.add_argument("--team", default="")
    add.add_argument("--project", default="")
    add.add_argument("--needs-human", default="needs-human")
    add.add_argument("--needs-local", default="needs-local")
    add.add_argument("--routine-name", default="")
    add.add_argument("--cron", default="52 * * * *")
    add.add_argument("--date", required=True, help="YYYY-MM-DD")
    add.add_argument("--pr-url", default="")
    add.add_argument("--notes", default="")
    add.set_defaults(fn=cmd_add)

    upd = sub.add_parser("update")
    upd.add_argument("--name", required=True)
    upd.add_argument("--routine-id", default="")
    upd.add_argument("--status", choices=["keep", "pilot", "active", "paused"], default="keep")
    upd.add_argument("--config", choices=["keep", "true", "false", "pending"], default="keep")
    upd.set_defaults(fn=cmd_update)

    a = ap.parse_args(argv)
    if a.cmd == "add" and not a.sdd_only and not (a.team.strip() and a.routine_name.strip()):
        ap.error("add には --team と --routine-name が必要(linear-worker なしなら --sdd-only)")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
