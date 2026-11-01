#!/usr/bin/env python3
"""`registry/repos.yaml` から Harness Sync の配布対象(owner/name)を JSON 配列で出力する。

対象 = `sync: workflow` かつ harness.status が active / pilot のエントリ。
--repos(空白・カンマ区切り。name でも owner/name でも可)を指定するとその中に絞る。
対象外・未登録のリポジトリを指定したら失敗する(何もせず成功したように見せない)。
YAML ライブラリに依存せず、scripts/onboard/registry.py と同じ前提(台帳の書式)で読む。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onboard"))
from registry import ENTRY_RE, GITHUB_RE  # noqa: E402

SYNC_RE = re.compile(r"^    sync: (?P<v>[^\s#]+)", re.M)
STATUS_RE = re.compile(r"^      status: (?P<v>[^\s#]+)", re.M)
ACTIVE = {"active", "pilot"}


def entries(text: str) -> list[dict]:
    starts = list(ENTRY_RE.finditer(text))
    out = []
    for i, m in enumerate(starts):
        block = text[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        g = GITHUB_RE.search(block)
        s = SYNC_RE.search(block)
        st = STATUS_RE.search(block)
        out.append({"name": m.group("name"), "github": g.group("github") if g else None,
                    "sync": s.group("v") if s else None, "status": st.group("v") if st else None})
    return out


def targets(text: str, wanted: list[str]) -> list[str]:
    all_entries = entries(text)
    eligible = [e for e in all_entries if e["github"] and e["sync"] == "workflow" and e["status"] in ACTIVE]
    if not wanted:
        return [e["github"] for e in eligible]
    picked, bad = [], []
    for w in wanted:
        hit = next((e for e in eligible if w in (e["name"], e["github"])), None)
        if hit is None:
            bad.append(w)
        elif hit["github"] not in picked:
            picked.append(hit["github"])
    if bad:
        raise SystemExit("::error::配布対象(sync: workflow かつ status: active / pilot)ではない: " + ", ".join(bad))
    return picked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="registry/repos.yaml")
    ap.add_argument("--repos", default="", help="絞り込み(空白・カンマ区切り。空なら全対象)")
    a = ap.parse_args(argv)
    wanted = [w for w in re.split(r"[\s,]+", a.repos) if w]
    print(json.dumps(targets(Path(a.file).read_text(encoding="utf-8"), wanted)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
