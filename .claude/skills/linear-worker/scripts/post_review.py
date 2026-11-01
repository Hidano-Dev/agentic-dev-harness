#!/usr/bin/env python3
"""Codex レビューの結果を PR に投稿する(codex-review.yml から呼ぶ)。

- `--kind review`(既定): Codex の出力 JSON(codex-review-schema.json)を **1 件の PR review
  (event=COMMENT)** として投稿する。
  - P0 / P1 は該当行へのインラインコメント(= 解決が必要なスレッド)。行が PR の差分外なら
    ファイル単位のコメント(subject_type=file)にする。ファイル自体が差分外なら PR の最初の変更ファイルに
    付け、実際の場所を本文の先頭に書く。GitHub がそれも拒否した時だけ本文に載せる
    (review_status.py は、スレッドの無い P0 / P1 が残る間 state=findings を返す)
  - P2 以下は本文の折りたたみ一覧(スレッドを作らない)
  - 指摘ゼロでも「指摘なし」の review を必ず投稿する
  - 本文の末尾に隠しマーカー(review_plan.py / review_status.py が読む)
- `--kind skip|capped|unavailable`: 前回の結果の引き継ぎ・回数上限・レビュー不可(fail-open)を
  マーカー付きの PR コメントとして投稿する
- `--check-result FILE`: Codex の出力がスキーマどおりか検査するだけ(0 = 正常、1 = 不正)

同じヘッド・同じ mode のマーカーがすでにあれば投稿しない(ジョブの再実行で重複させない)。
終了コード: 0 = 投稿した / 投稿不要、1 = 結果が不正(--check-result / --kind review)、2 = エラー。
標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_review_common as common  # noqa: E402

BODY_LIMIT = 60_000       # GitHub の本文上限(65536 文字)より余裕をもって
FINDINGS_EMBED_LIMIT = 20_000
ITEM_BODY_LIMIT = 1_500


class InvalidResult(Exception):
    pass


# --------------------------------------------------------------------------- 結果の検査


def normalize_path(path: str) -> str:
    p = str(path).strip().replace("\\", "/")
    for prefix in ("./", "a/", "b/"):
        if p.startswith(prefix) and len(p) > len(prefix):
            p = p[len(prefix):]
            break
    return p.lstrip("/")


def load_result(path: Path) -> dict:
    """Codex の出力を読み、スキーマどおりか検査して正規化する。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise InvalidResult(f"結果ファイルを読めない: {e}") from e
    if not text:
        raise InvalidResult("結果ファイルが空")
    # コードフェンスで囲まれて返ってきた場合に備える
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidResult(f"JSON として読めない: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) \
            or not isinstance(data.get("findings"), list):
        raise InvalidResult("summary(文字列)/ findings(配列)が無い")
    findings = []
    for i, f in enumerate(data["findings"]):
        if not isinstance(f, dict):
            raise InvalidResult(f"findings[{i}] がオブジェクトではない")
        sev = f.get("severity")
        line = f.get("line")
        if sev not in common.SEVERITIES:
            raise InvalidResult(f"findings[{i}].severity が P0〜P3 ではない: {sev!r}")
        if isinstance(line, bool) or not isinstance(line, int):
            raise InvalidResult(f"findings[{i}].line が整数ではない")
        for key in ("path", "title", "body"):
            if not isinstance(f.get(key), str):
                raise InvalidResult(f"findings[{i}].{key} が文字列ではない")
        findings.append({"severity": sev, "path": normalize_path(f["path"]), "line": line,
                         "title": f["title"].strip() or "(無題)", "body": f["body"].strip()})
    findings.sort(key=lambda f: (common.SEVERITIES.index(f["severity"]), f["path"], f["line"]))
    return {"summary": data["summary"].strip(), "findings": findings}


# --------------------------------------------------------------------------- 差分(コメント可能な行)


_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unquote_path(p: str) -> str:
    """git が引用符で囲んだパス(core.quotepath=false なので非 ASCII はそのまま。\\" \\\\ 等だけ戻す)。"""
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), p[1:-1])
    return p


def commentable_lines(base: str, head: str) -> dict[str, set[int]]:
    """GitHub の PR 差分でインラインコメントを付けられる行(ヘッド側の行番号。追加行 + 文脈行)。"""
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
         "-M", "--src-prefix=a/", "--dst-prefix=b/", base, head],
        check=True, capture_output=True).stdout.decode("utf-8", errors="replace")
    lines: dict[str, set[int]] = {}
    current: set[int] | None = None
    n = 0
    in_hunk = False
    for ln in out.split("\n"):
        if ln.startswith("diff --git "):
            current, in_hunk = None, False
            continue
        if not in_hunk and ln.startswith("+++ "):
            target = _unquote_path(ln[4:].rstrip("\t"))
            current = lines.setdefault(target[2:], set()) if target.startswith("b/") else None
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
        if m:
            n, in_hunk = int(m.group(1)), True
            continue
        if not in_hunk or current is None:
            continue
        if ln.startswith((" ", "+")):  # "-" と "\ No newline" はヘッド側の行を進めない
            current.add(n)
            n += 1
    return lines


# --------------------------------------------------------------------------- 本文


def _clip(text: str, limit: int) -> str:
    text = common.neutralize(text)
    return text if len(text) <= limit else text[:limit] + "…(省略)"


def _loc(f: dict) -> str:
    return f"`{common.neutralize(f['path'])}:{f['line']}`"


def inline_body(f: dict) -> str:
    return (f"**[{f['severity']}] {_clip(f['title'], 300)}**\n\n{_clip(f['body'], 20_000)}\n\n"
            f"<sub>Codex レビュー。修正したら返信して resolve、対応不要なら理由を返信して resolve してください。</sub>\n"
            f"<!-- {common.FINDING_MARKER} severity={f['severity']} -->")


def embed_findings(findings: list[dict]) -> str:
    data = [dict(f) for f in findings]
    text = common.encode_findings(data)
    if len(text) > FINDINGS_EMBED_LIMIT:
        data = [f for f in data if f["severity"] in common.BLOCKING]
        text = common.encode_findings(data)
    if len(text) > FINDINGS_EMBED_LIMIT:
        data = [dict(f, body=f["body"][:300]) for f in data]
        text = common.encode_findings(data)
    while len(text) > FINDINGS_EMBED_LIMIT and data:
        data = data[:-1]
        text = common.encode_findings(data)
    return text


def file_comment_body(f: dict, target: str) -> str:
    """差分外の P0 / P1 をファイル単位のスレッドにする時の本文(実際の場所を先頭に書く)。"""
    if target == f["path"]:
        where = f"場所: {_loc(f)}(PR の差分外の行のため、ファイル単位のスレッドにしています)"
    else:
        where = (f"実際の場所: {_loc(f)}(このファイルは PR の差分に含まれないため、"
                 f"PR の最初の変更ファイル `{common.neutralize(target)}` にスレッドを作成しています)")
    return f"{where}\n\n" + inline_body(f)


def review_body(plan: dict, result: dict, inline: list[dict], filed: list[dict], body_only: list[dict],
                minor: list[dict]) -> str:
    mode_label = {"full": "初回 / 全体", "delta": "再レビュー / 前回からの差分"}.get(plan["mode"], plan["mode"])
    head, base = plan["head"], plan["base"]
    parts = [f"### Codex レビュー(round {plan['round']} / {mode_label})",
             f"対象: `{head[:7]}`(差分: `{base[:7]}..{head[:7]}`)", "",
             _clip(result["summary"], 5_000) or "(summary なし)", ""]
    blocking = len(inline) + len(filed) + len(body_only)
    if blocking == 0 and not minor:
        parts.append("**指摘なし**")
    elif blocking == 0:
        parts.append(f"**P0 / P1 の指摘なし**(P2 以下 {len(minor)} 件。スレッドは作りません)")
    else:
        parts.append(f"**P0 / P1: {blocking} 件**(該当行 {len(inline)} 件・ファイル単位 {len(filed)} 件のスレッドを作成。"
                     f"修正または理由付きの返信のうえ resolve してください)")
    if filed:
        parts += ["", "#### 差分外の行への P0 / P1(ファイル単位のスレッドを作成)"]
        for f in filed:
            parts.append(f"- **[{f['severity']}] {_clip(f['title'], 300)}** — {_loc(f)}")
    if body_only:
        parts += ["", "#### スレッドにできなかった P0 / P1(マージ前に必ず対応し、修正を push してください)"]
        for f in body_only:
            parts.append(f"- **[{f['severity']}] {_clip(f['title'], 300)}** — {_loc(f)}<br>{_clip(f['body'], ITEM_BODY_LIMIT)}")
    if minor:
        parts += ["", f"<details><summary>P2 以下の指摘 {len(minor)} 件(スレッドは作りません。対応は任意)</summary>", ""]
        for f in minor:
            body = _clip(f["body"], ITEM_BODY_LIMIT).replace("\n", "\n  > ")
            parts.append(f"- **[{f['severity']}] {_clip(f['title'], 300)}** — {_loc(f)}\n  > {body}")
        parts += ["", "</details>"]
    return "\n".join(parts)


def with_markers(body: str, plan: dict, mode: str, round_: int, findings: list[dict]) -> str:
    tail = "\n\n" + common.format_marker(plan["head"], round_, plan.get("patch") or "", mode) + "\n" + embed_findings(findings)
    if len(body) + len(tail) > BODY_LIMIT:
        body = body[:BODY_LIMIT - len(tail) - 40] + "\n\n…(長すぎるため省略)"
    return body + tail


def split_findings(findings: list[dict], commentable: dict[str, set[int]]):
    inline, outside, minor = [], [], []
    for f in findings:
        if f["severity"] not in common.BLOCKING:
            minor.append(f)
        elif f["line"] in commentable.get(f["path"], set()):
            inline.append(f)
        else:
            outside.append(f)
    return inline, outside, minor


def notice_body(kind: str, plan: dict, reason: str) -> str:
    head = plan["head"][:7]
    if kind == "skip":
        prev = (plan.get("prev_sha") or "")[:7]
        text = (f"### Codex レビュー: 前回の結果を引き継ぎ(`{head}`)\n\n"
                f"{common.neutralize(reason or plan.get('reason') or '')}。"
                + (f"`{prev}` のレビュー結果(未解決のスレッドを含む)を引き継ぎます。" if prev else "レビューは不要です。"))
    elif kind == "capped":
        text = (f"### Codex レビュー: 回数上限に達しました(`{head}`)\n\n"
                f"レビュー回数が上限(`review.action.max_rounds` = {plan.get('max_rounds')})に達したため、"
                f"このヘッドは Codex でレビューしていません。未解決のスレッドは引き続き解決が必要です。"
                f"追加の確認は人間のレビュー(develop → main の Release PR を含む)に委ねます。")
    else:
        text = (f"### Codex レビュー: レビュー不可(fail-open)(`{head}`)\n\n"
                f"Codex レビューを実行できませんでした({common.neutralize(reason or '理由不明')})。"
                f"`review.fail_open` が有効なため、レビューなしでチェックを成功させています。"
                f"必要ならワークフローを再実行してください。")
    return text


# --------------------------------------------------------------------------- GitHub


def fetch_markers(repo: str, pr: int) -> list[dict]:
    return common.fetch_markers(repo, pr)


def create_review(repo: str, pr: int, payload: dict) -> dict:
    return common.gh_api(f"repos/{repo}/pulls/{int(pr)}/reviews", method="POST", body=payload) or {}


def create_comment(repo: str, pr: int, body: str) -> dict:
    return common.gh_api(f"repos/{repo}/issues/{int(pr)}/comments", method="POST", body={"body": body}) or {}


def create_file_comment(repo: str, pr: int, head: str, path: str, body: str) -> dict:
    """ファイル単位のレビューコメント(subject_type=file)。解決が必要なスレッドになる。"""
    return common.gh_api(f"repos/{repo}/pulls/{int(pr)}/comments", method="POST",
                         body={"commit_id": head, "path": path, "subject_type": "file", "body": body}) or {}


def fetch_review_comments(repo: str, pr: int) -> list[dict]:
    return common.gh_api_list(f"repos/{repo}/pulls/{int(pr)}/comments")


# --------------------------------------------------------------------------- 投稿


def already_posted(markers: list[dict], head: str, mode: str) -> dict | None:
    for m in reversed(markers):
        if m["sha"] == head and m["mode"] == mode:
            return m
    return None


def post(kind: str, plan: dict, *, repo: str, pr: int, result: dict | None = None, reason: str = "",
         dry_run: bool = False) -> dict:
    mode = plan["mode"] if kind == "review" else kind
    if kind == "review" and mode not in ("full", "delta"):
        raise ValueError(f"plan の mode が {mode} なのでレビューは投稿しない")
    existing = already_posted(fetch_markers(repo, pr), plan["head"], mode)
    if existing:
        return {"posted": False, "reason": "同じヘッド・mode のマーカーが既にある", "url": existing.get("url")}

    if kind != "review":
        findings = (plan.get("prev_findings") or []) if kind == "skip" else []
        round_ = int(plan.get("prev_round", plan.get("round", 0)))
        body = with_markers(notice_body(kind, plan, reason), plan, mode, round_, findings)
        if dry_run:
            return {"posted": False, "dry_run": True, "body": body}
        res = create_comment(repo, pr, body)
        return {"posted": True, "kind": kind, "url": res.get("html_url")}

    commentable = commentable_lines(plan["merge_base"], plan["head"])
    inline, outside, minor = split_findings(result["findings"], commentable)
    filed: list[dict] = []  # ファイル単位のスレッドにできた差分外の P0 / P1(残りの outside は本文のみ)

    def payload_for(inline_: list[dict]) -> dict:
        recorded = [dict(f, inline=f in inline_, thread=f in inline_ or f in filed) for f in result["findings"]]
        body_only = [f for f in outside if f not in filed]
        body = with_markers(review_body(plan, result, inline_, filed, body_only, minor),
                            plan, mode, plan["round"], recorded)
        return {"commit_id": plan["head"], "event": "COMMENT", "body": body,
                "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT", "body": inline_body(f)}
                             for f in inline_]}

    if dry_run:
        return {"posted": False, "dry_run": True, "payload": payload_for(inline)}
    existing_bodies = {(c.get("path"), c.get("body")) for c in fetch_review_comments(repo, pr)
                       if common.login_of(c.get("user")) in common.TRUSTED_AUTHORS
                       and c.get("commit_id") == plan["head"]}

    def file_thread(f: dict) -> bool:
        """差分外の P0 / P1 をファイル単位のレビューコメント(= 解決が必要なスレッド)にする。"""
        target = f["path"] if f["path"] in commentable else next(iter(commentable), None)
        if target is None:
            return False
        body = file_comment_body(f, target)
        if (target, body) in existing_bodies:  # ジョブの再実行で同じスレッドを重ねない
            return True
        try:
            create_file_comment(repo, pr, plan["head"], target, body)
        except common.GitHubError as e:
            print(f"::warning::ファイル単位のコメントを投稿できなかったため本文に載せる({f['path']}): {e}",
                  file=sys.stderr)
            return False
        return True

    filed.extend(f for f in outside if file_thread(f))
    try:
        res = create_review(repo, pr, payload_for(inline))
    except common.GitHubError as e:
        if not inline:
            raise
        # 行の対応が GitHub の差分とずれていた(422 等)。ファイル単位のスレッドに切り替えて投稿し直す
        print(f"::warning::インラインコメント付きの review を投稿できなかったためファイル単位のコメントにする: {e}",
              file=sys.stderr)
        outside = outside + inline
        filed.extend(f for f in inline if file_thread(f))
        inline = []
        res = create_review(repo, pr, payload_for(inline))
    return {"posted": True, "kind": "review", "url": res.get("html_url"), "inline": len(inline),
            "file_level": len(filed), "outside_diff": len(outside), "body_only": len(outside) - len(filed),
            "minor": len(minor)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-result", metavar="FILE", help="Codex の出力を検査するだけ")
    ap.add_argument("--kind", choices=("review", "skip", "capped", "unavailable"), default="review")
    ap.add_argument("--plan", help="review_plan.py の出力 JSON")
    ap.add_argument("--result", help="Codex の出力 JSON(--kind review)")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--repo")
    ap.add_argument("--config", default=str(common.CONFIG_REL))
    ap.add_argument("--reason", default="", help="コメントに添える理由(skip / unavailable)")
    ap.add_argument("--dry-run", action="store_true", help="投稿せず内容を出力する")
    a = ap.parse_args(argv)

    if a.check_result:
        try:
            load_result(Path(a.check_result))
        except InvalidResult as e:
            print(f"::warning::Codex の出力が不正: {e}")
            return 1
        print("ok")
        return 0
    if not a.plan or a.pr is None:
        ap.error("--plan と --pr が必要")
    try:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        if a.kind == "review" and not a.result:
            raise ValueError("--kind review には --result が必要")
        result = load_result(Path(a.result)) if a.kind == "review" else None
        repo = common.resolve_repo(a.repo, common.load_config(Path(a.config)))
        out = post(a.kind, plan, repo=repo, pr=a.pr, result=result, reason=a.reason, dry_run=a.dry_run)
    except InvalidResult as e:
        print(f"::error::Codex の出力が不正: {e}")
        return 1
    except (ValueError, OSError, KeyError, json.JSONDecodeError, common.GitHubError) as e:
        print(f"::error::{e}")
        return 2
    except subprocess.CalledProcessError as e:
        print(f"::error::git に失敗: {(e.stderr or b'').decode(errors='replace').strip()}")
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
