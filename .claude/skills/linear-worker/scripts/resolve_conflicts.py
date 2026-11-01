#!/usr/bin/env python3
"""マージ途中のコンフリクトのうち、文書ファイルの「両側が同じ位置に追記しただけ」のものを解消する。

並列に動く PR が同じ文書の同じ見出しの末尾へそれぞれ箇条を足すと、内容としては両立するのに
git はコンフリクトとして止まる。これを機械的に「両方残す」で解消し、自動マージを止めないための
もの(conflict-sweeper ワークフローから呼ぶ。手元でも使える)。

- `git merge` がコンフリクトで止まった状態の作業ツリーで実行する。`git ls-files -u` に出る
  各ファイルについて判定する
- 対象: config の `conflicts.auto_resolve_extensions`(既定 .md / .markdown / .rst / .adoc /
  .asciidoc / .txt)に拡張子が一致し、バイナリでもシンボリックリンクでもなく、ours と theirs の
  両ステージがあるファイル(add/add は base を空として扱う)。コードファイルは対象外
- 判定は `git merge-file -p --diff3` の出力の衝突ブロックごとに行う:
  - base が空(両側が同じ位置に追記しただけ)→ ours → theirs の順で連結する。両側で同じ
    先頭行・末尾行は 1 回だけ残す(両側がまったく同じ追記なら 1 回だけ)
  - ours == base なら theirs、theirs == base なら ours、ours == theirs ならその内容
  - base の行を両側がそのまま保ってその前後に追記しただけ(末尾改行の無い最終行の後ろへの
    追記など。行末の違いは無視して比べる)なら、base 部分 + 両側の追記の連結
  - それ以外(同じ既存行を両側が書き換えた)は解消不可
- ファイル内の全ブロックが解消できたときだけ、そのファイルを書き込んで `git add` する。
  1 ブロックでも解消できなければそのファイルには触れない(コンフリクトマーカー入りのまま残る)

出力(標準出力、UTF-8 の JSON):
  {"resolved": [パス...], "unresolved": [パス...], "reasons": {パス: 理由}}
終了コード: 0 = 未解消のファイルなし / 1 = 未解消のファイルあり / 2 = エラー

標準ライブラリのみ使用(ランナー・ローカルどちらでも動く)。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_REL = Path(".kiro/orchestration/config.json")
DEFAULT_EXTENSIONS = [".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt"]
# 本文中のマーカー風の行(7 文字の <<<<<<< 等)と区別するため長めにする
MARKER_SIZE = 30
M_OURS = b"<" * MARKER_SIZE
M_BASE = b"|" * MARKER_SIZE
M_SEP = b"=" * MARKER_SIZE
M_THEIRS = b">" * MARKER_SIZE


class ResolveError(Exception):
    pass


def git(args: list[str], cwd: Path, *, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True)
    if check and r.returncode != 0:
        raise ResolveError(f"git {' '.join(args)} に失敗: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r


def load_extensions(config: Path) -> list[str]:
    if not config.is_file():
        return DEFAULT_EXTENSIONS
    cfg = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ResolveError(f"{config} の最上位が JSON オブジェクトではない")
    conflicts = cfg.get("conflicts") or {}
    if not isinstance(conflicts, dict):
        raise ResolveError(f"{config} の conflicts がオブジェクトではない")
    exts = conflicts.get("auto_resolve_extensions", DEFAULT_EXTENSIONS)
    if not isinstance(exts, list) or not all(isinstance(e, str) and e for e in exts):
        raise ResolveError(f"{config} の conflicts.auto_resolve_extensions が文字列の配列ではない")
    return [e.lower() for e in exts]


def unmerged_entries(root: Path) -> dict[str, dict[int, tuple[str, str]]]:
    """{パス: {ステージ番号: (mode, blob sha)}}"""
    out = git(["ls-files", "-u", "-z"], root).stdout
    entries: dict[str, dict[int, tuple[str, str]]] = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, sha, stage = meta.decode().split()
        entries.setdefault(path.decode("utf-8", "surrogateescape"), {})[int(stage)] = (mode, sha)
    return entries


def split_lines(data: bytes) -> list[bytes]:
    """行末(\\n。CRLF の \\r は行に含めたまま)を保持して行に分ける。"""
    lines = data.split(b"\n")
    tail = lines.pop()
    out = [line + b"\n" for line in lines]
    if tail:
        out.append(tail)
    return out


def is_marker(line: bytes, marker: bytes) -> bool:
    body = line.rstrip(b"\r\n")
    return body.startswith(marker) and (len(body) == MARKER_SIZE or body[MARKER_SIZE:MARKER_SIZE + 1] == b" ")


def same_lines(a: list[bytes], b: list[bytes]) -> bool:
    """行末(改行の有無・CRLF/LF)の違いを無視して比べる。"""
    return len(a) == len(b) and all(x.rstrip(b"\r\n") == y.rstrip(b"\r\n") for x, y in zip(a, b))


def resolve_hunk(ours: list[bytes], base: list[bytes], theirs: list[bytes]) -> list[bytes] | None:
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if base:
        # base の行を両側がそのまま保ち、その前後に追記しただけのもの(末尾改行の無い最終行の
        # 後ろへ追記すると、その行の改行の有無だけが変わって base 側に入る)は追記として扱う
        n = len(base)
        if len(ours) >= n and len(theirs) >= n:
            if same_lines(ours[:n], base) and same_lines(theirs[:n], base):
                rest = resolve_hunk(ours[n:], [], theirs[n:])
                return None if rest is None else ours[:n] + rest
            if same_lines(ours[len(ours) - n:], base) and same_lines(theirs[len(theirs) - n:], base):
                rest = resolve_hunk(ours[:len(ours) - n], [], theirs[:len(theirs) - n])
                return None if rest is None else rest + ours[len(ours) - n:]
        return None  # 同じ既存行を両側が書き換えた
    # 両側が同じ位置に追記しただけ。共通の先頭行・末尾行は 1 回だけ残す
    pre = 0
    while pre < len(ours) and pre < len(theirs) and ours[pre] == theirs[pre]:
        pre += 1
    suf = 0
    while (suf < len(ours) - pre and suf < len(theirs) - pre
           and ours[len(ours) - 1 - suf] == theirs[len(theirs) - 1 - suf]):
        suf += 1
    o_mid = ours[pre:len(ours) - suf]
    t_mid = theirs[pre:len(theirs) - suf]
    return ours[:pre] + o_mid + t_mid + ours[len(ours) - suf:]


def resolve_text(merged: bytes) -> bytes | None:
    """merge-file --diff3 の出力から衝突ブロックを解消した内容を返す。解消できなければ None。"""
    out: list[bytes] = []
    state = None  # None / "ours" / "base" / "theirs"
    ours: list[bytes] = []
    base: list[bytes] = []
    theirs: list[bytes] = []
    for line in split_lines(merged):
        if state is None:
            if is_marker(line, M_OURS):
                state, ours, base, theirs = "ours", [], [], []
            else:
                out.append(line)
        elif state == "ours":
            if is_marker(line, M_BASE):
                state = "base"
            elif is_marker(line, M_SEP):
                return None  # diff3 形式でない(base 部分が無い)
            else:
                ours.append(line)
        elif state == "base":
            if is_marker(line, M_SEP):
                state = "theirs"
            else:
                base.append(line)
        else:
            if is_marker(line, M_THEIRS):
                hunk = resolve_hunk(ours, base, theirs)
                if hunk is None:
                    return None
                out.extend(hunk)
                state = None
            else:
                theirs.append(line)
    if state is not None:
        raise ResolveError("merge-file の出力の衝突ブロックが閉じていない")
    return b"".join(out)


def blob(root: Path, sha: str) -> bytes:
    return git(["cat-file", "blob", sha], root).stdout


def try_resolve(root: Path, path: str, stages: dict[int, tuple[str, str]], exts: list[str]) -> tuple[bytes | None, str]:
    if not any(path.lower().endswith(e) for e in exts):
        return None, "対象外の拡張子"
    if 2 not in stages or 3 not in stages:
        return None, "片側で削除されている(modify/delete)"
    if any(mode == "120000" for mode, _ in stages.values()):
        return None, "シンボリックリンク"
    if any(mode == "160000" for mode, _ in stages.values()):
        return None, "submodule"
    if (root / path).is_symlink():
        return None, "作業ツリー上のパスがシンボリックリンク"
    ours = blob(root, stages[2][1])
    theirs = blob(root, stages[3][1])
    base = blob(root, stages[1][1]) if 1 in stages else b""
    if any(b"\0" in c for c in (ours, base, theirs)):
        return None, "バイナリ"
    with tempfile.TemporaryDirectory() as d:
        files = []
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(content)
            files.append(p)
        r = git(["merge-file", "-p", "--diff3", f"--marker-size={MARKER_SIZE}",
                 "-L", "ours", "-L", "base", "-L", "theirs", *files], root, check=False)
    if r.returncode < 0 or r.returncode > 127:
        raise ResolveError(f"git merge-file に失敗({path}): {r.stderr.decode('utf-8', 'replace').strip()}")
    resolved = resolve_text(r.stdout)
    if resolved is None:
        return None, "両側が同じ既存行を書き換えている"
    return resolved, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help=f"設定ファイル(既定: リポジトリ直下の {CONFIG_REL.as_posix()})")
    ap.add_argument("--dry-run", action="store_true", help="判定だけ行い、ファイルの書き込み・git add をしない")
    a = ap.parse_args(argv)
    # JSON は機械が読むので、端末のコードページ(Windows の cp932 等)によらず UTF-8 で出す
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")

    try:
        top = git(["rev-parse", "--show-toplevel"], Path.cwd()).stdout.decode().strip()
        root = Path(top)
        exts = load_extensions(Path(a.config) if a.config else root / CONFIG_REL)
        resolved: list[str] = []
        unresolved: list[str] = []
        reasons: dict[str, str] = {}
        for path, stages in sorted(unmerged_entries(root).items()):
            content, reason = try_resolve(root, path, stages, exts)
            if content is None:
                unresolved.append(path)
                reasons[path] = reason
                continue
            if not a.dry_run:
                (root / path).write_bytes(content)
                git(["add", "--", path], root)
            resolved.append(path)
    except (ResolveError, OSError, ValueError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        print(f"::error::{e}", file=sys.stderr)
        return 2
    print(json.dumps({"resolved": resolved, "unresolved": unresolved, "reasons": reasons}, ensure_ascii=False))
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
