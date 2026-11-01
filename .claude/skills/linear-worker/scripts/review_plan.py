#!/usr/bin/env python3
"""PR の Codex レビュー(codex-review.yml)で、今回なにをするかを決める。

出力(標準出力に JSON。`--output` でファイルにも書く):
  {mode, base, head, prev_sha, round, reason, ...}
  - mode:
    - skip: レビューしない(前回の結果を引き継ぐ / レビュー不要)。次のいずれか
      (-) review.provider が null(外部レビューなし)/ app(Codex GitHub App がレビューする)。何も投稿しない
      (-) Harness Sync の同期 PR(.harness/version を変更し、変更が配布物 + 同期の範囲だけ)。
          配布元との一致は policy-gate が検証するので、ここでは形だけを見る
      (a) fast track(PR 全体、または前回レビュー済みヘッドからの追加分が `classify_change.py` で 0)
      (b) 前回レビュー済みのヘッドと同じ SHA
      (c) PR 自身の差分(merge-base からの `git diff -U0` の `git patch-id --stable`)が前回と一致
          (base の取り込み・同じ内容への rebase だけ)
    - capped: これまでのレビュー回数が `review.action.max_rounds`(既定 3)に達した
    - delta: 前回レビュー済み SHA がヘッドの祖先 → 前回からの追加分だけを見る
    - full: 初回(前回レビュー無し、または前回 SHA がヘッドの祖先でない)
  - base: Codex に見せる差分の起点(full = merge-base、delta = 前回 SHA。skip / capped は merge-base)
  - round: 今回レビューした場合の回数(これまでの回数 + 1。skip / capped は増やさない)
  - announce: post_review.py で PR にコメントすべきか(skip で前回の結果を引き継ぐ時・capped)
  - prev_findings: 前回の指摘(skip の引き継ぎコメントに埋め直す)

前回の情報は PR のコメント / review に post_review.py が埋めた隠しマーカー
`<!-- harness-codex-review sha=... round=... patch=... mode=... -->` から読む
(author が github-actions[bot] のものだけを信頼する)。

`--prompt-out` を付けると、mode が full / delta の時に Codex へ渡すプロンプトを
`templates/codex-review-prompt.md` から組み立てて書き出す(base の `AGENTS.md` に
"Review guidelines" 節があれば追記する)。`--github-output` を付けると主要な値を
`$GITHUB_OUTPUT` にも書く。

終了コード: 0 = 判定できた、2 = エラー(git / GitHub API / config)。
標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_review_common as common  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
CLASSIFY = SCRIPT_DIR / "classify_change.py"
TEMPLATE = SCRIPT_DIR.parent / "templates" / "codex-review-prompt.md"
INLINE_DIFF_LIMIT = 150_000  # これより大きい差分はプロンプトに埋めず、git diff を実行させる
PREV_BODY_LIMIT = 800


# --------------------------------------------------------------------------- git


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def rev_parse(rev: str) -> str:
    return git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def is_commit(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0


def merge_base(a: str, b: str) -> str:
    return git("merge-base", a, b).strip()


DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
             "--src-prefix=a/", "--dst-prefix=b/")


def patch_id(base: str, head: str) -> str:
    """PR 自身の差分(merge-base(base, head) からの -U0 diff)の patch-id。差分が無ければ空文字列。"""
    mb = merge_base(base, head)
    diff = subprocess.run(["git", "diff", "-U0", "--binary", *DIFF_OPTS, mb, head],
                          check=True, capture_output=True).stdout
    if not diff.strip():
        return ""
    out = subprocess.run(["git", "patch-id", "--stable"], input=diff, check=True, capture_output=True).stdout
    return out.decode("ascii", errors="replace").split()[0] if out.strip() else ""


def is_fast_track(base: str, head: str, config: str) -> bool:
    """classify_change.py の終了コードが 0(fast track)か。"""
    proc = subprocess.run([sys.executable, str(CLASSIFY), "--base", base, "--head", head, "--config", config],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode == 0


# --------------------------------------------------------------------------- 同期 PR

VERSION_REL = ".harness/version"
MANIFEST_REL = "sync-manifest.json"
# policy_gate.py と揃える(.harness/version に owned が無いときの配布物 / 同期 PR が変更してよい範囲)
DEFAULT_OWNED = [".claude/skills/linear-worker", ".claude/rules/git-workflow.md", ".harness/invariants.json"]
SYNC_EXTRA = [".harness/", ".github/workflows/", "CLAUDE.md", ".kiro/orchestration/config.json"]
SYNC_REASON = "配布元からの同期 PR(内容の検証は policy-gate が行う)"


def _under(path: str, prefixes: list[str]) -> bool:
    for p in prefixes:
        q = p.rstrip("/")
        if q and (path == q or path.startswith(q + "/")):
            return True
    return False


def _show_json(rev: str, path: str):
    proc = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def is_harness_sync_pr(base: str, head: str) -> bool:
    """取り込み側の Harness Sync の PR の形か(.harness/version を変更し、変更が同期の範囲だけ)。

    内容が配布元と一致するかは検証しない(policy-gate が必須チェックとして検証する)。配布物は
    PR ヘッドの .harness/version ではなく base のものから取る(PR が範囲を広げられないように)。
    配布元(base に sync-manifest.json がある)では同期 PR は起きないので対象外。
    """
    if subprocess.run(["git", "cat-file", "-e", f"{base}:{MANIFEST_REL}"], capture_output=True).returncode == 0:
        return False
    out = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
                         check=True, capture_output=True).stdout.decode("utf-8", errors="replace")
    changed = [p for p in out.split("\0") if p]
    if VERSION_REL not in changed:
        return False
    ver = _show_json(base, VERSION_REL)
    owned = ver.get("owned") if isinstance(ver, dict) and isinstance(ver.get("owned"), list) else DEFAULT_OWNED
    allowed = [p for p in owned if isinstance(p, str)] + SYNC_EXTRA
    return all(_under(p, allowed) for p in changed)


# --------------------------------------------------------------------------- GitHub


def fetch_markers(repo: str, pr: int) -> list[dict]:
    return common.fetch_markers(repo, pr)


# --------------------------------------------------------------------------- 判定


def decide(*, base: str, head: str, markers: list[dict], settings: dict, config: str) -> dict:
    head = rev_parse(head)
    pr_base = rev_parse(base)
    mb = merge_base(pr_base, head)
    patch = patch_id(pr_base, head)
    reviewed = [m for m in markers if m["mode"] in common.REVIEWED_MODES]
    prev = reviewed[-1] if reviewed else None
    prev_round = max((m["round"] for m in markers), default=0)
    plan = {
        "mode": "", "base": mb, "head": head, "prev_sha": prev["sha"] if prev else None,
        "round": prev_round + 1, "prev_round": prev_round, "reason": "",
        "pr_base": pr_base, "merge_base": mb, "patch": patch, "announce": False,
        "max_rounds": settings["max_rounds"], "model": settings["model"], "effort": settings["effort"],
        "fail_open": settings["fail_open"],
        "prev_findings": prev["findings"] if prev else [],
    }

    def skip(reason: str, announce: bool) -> dict:
        plan.update(mode="skip", round=prev_round, reason=reason, announce=announce)
        return plan

    provider_reason = common.provider_skip_reason(settings["provider"])
    if provider_reason:  # Codex は呼ばず、PR にも何も投稿しない
        return skip(provider_reason, announce=False)
    if is_harness_sync_pr(pr_base, head):
        return skip(SYNC_REASON, announce=True)
    if is_fast_track(pr_base, head, config):
        return skip("PR 全体が fast track(テキスト / コメントのみ)", announce=prev is not None)
    if prev is None:
        prev_ok = False
    else:
        if prev["sha"] == head or head.startswith(prev["sha"]):
            return skip("このヘッドはレビュー済み", announce=False)
        if prev["patch"] and prev["patch"] == patch:
            return skip("PR 自身の差分が前回レビュー時と同一(base の取り込み / rebase のみ)", announce=True)
        prev_ok = is_commit(prev["sha"]) and is_ancestor(prev["sha"], head)
        if prev_ok and is_fast_track(prev["sha"], head, config):
            return skip(f"前回レビュー済み {prev['sha'][:7]} からの追加分が fast track", announce=True)

    if prev_round >= settings["max_rounds"]:
        plan.update(mode="capped", round=prev_round, announce=True,
                    reason=f"レビュー回数が上限(review.action.max_rounds = {settings['max_rounds']})に達した")
        return plan
    if prev_ok:
        plan.update(mode="delta", base=prev["sha"], reason=f"前回レビュー済み {prev['sha'][:7]} からの追加分")
    else:
        plan.update(mode="full", reason="初回レビュー" if prev is None else
                    f"前回レビュー済み {prev['sha'][:7]} がヘッドの祖先でない(force-push / rebase)")
    return plan


# --------------------------------------------------------------------------- プロンプト


def review_guidelines(rev: str) -> str:
    """rev の AGENTS.md の "Review guidelines" 節(見出しから同じ以上のレベルの次の見出しまで)。"""
    proc = subprocess.run(["git", "show", f"{rev}:AGENTS.md"], capture_output=True)
    if proc.returncode != 0:
        return ""
    text = proc.stdout.decode("utf-8", errors="replace")
    lines = text.splitlines()
    out: list[str] = []
    level = 0
    for ln in lines:
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", ln)
        if level == 0:
            if m and m.group(2).strip().lower() == "review guidelines":
                level = len(m.group(1))
            continue
        if m and len(m.group(1)) <= level:
            break
        out.append(ln)
    return "\n".join(out).strip()


def format_prev_findings(findings: list[dict]) -> str:
    blocking = [f for f in findings if f.get("severity") in common.BLOCKING]
    if not blocking:
        return "(前回の P0 / P1 の指摘はありません)"
    lines = []
    for f in blocking:
        body = str(f.get("body", "")).strip().replace("\n", " ")
        if len(body) > PREV_BODY_LIMIT:
            body = body[:PREV_BODY_LIMIT] + "…"
        lines.append(f"- [{f.get('severity')}] `{f.get('path')}:{f.get('line')}` {f.get('title', '')}: {body}")
    return "\n".join(lines)


def build_prompt(template: str, plan: dict, *, repo: str, pr: int, guidelines: str) -> str:
    mode = plan["mode"]
    template = re.sub(r"\A\s*<!--.*?-->\s*", "", template, flags=re.DOTALL)
    for m in ("full", "delta"):
        pattern = rf"<!-- BEGIN {m} -->\n?(.*?)<!-- END {m} -->\n?"
        template = re.sub(pattern, (lambda x: x.group(1)) if m == mode else "", template, flags=re.DOTALL)

    base, head = plan["base"], plan["head"]
    files = git("-c", "core.quotepath=false", "diff", "--name-status", *DIFF_OPTS, base, head).strip()
    diff = git("-c", "core.quotepath=false", "diff", *DIFF_OPTS, base, head)
    if not diff.strip():
        diff_block = "(差分はありません)"
    elif len(diff) > INLINE_DIFF_LIMIT:
        diff_block = (f"差分が大きい({len(diff)} 文字)ため埋め込んでいません。"
                      f"`git diff {base} {head}` を実行して確認してください。")
    else:
        fence = "````" if "```" in diff else "```"
        diff_block = f"差分:\n\n{fence}diff\n{diff}{'' if diff.endswith(chr(10)) else chr(10)}{fence}"
    values = {
        "REPO": repo, "PR": str(pr), "ROUND": str(plan["round"]),
        "MODE": {"full": "初回 / 全体", "delta": "再レビュー / 前回からの差分"}[mode],
        "BASE": base, "HEAD": head, "PREV_SHA": plan.get("prev_sha") or "",
        "FILES": "```\n" + (files or "(なし)") + "\n```",
        "DIFF": diff_block,
        "PREV_FINDINGS": format_prev_findings(plan.get("prev_findings") or []),
        "GUIDELINES": ("## このリポジトリのレビューガイドライン(AGENTS.md より)\n\n" + guidelines + "\n")
        if guidelines else "",
    }
    # 1 回の置換で済ませる(差分などの埋め込み内容に {{...}} があっても再置換しない)
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


# --------------------------------------------------------------------------- main


def write_github_output(plan: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    keys = ("mode", "round", "base", "head", "prev_sha", "model", "effort", "announce", "fail_open")
    with open(path, "a", encoding="utf-8") as fh:
        for k in keys:
            v = plan.get(k)
            v = "" if v is None else (str(v).lower() if isinstance(v, bool) else str(v))
            fh.write(f"{k}={v.replace(chr(10), ' ')}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--base", required=True, help="PR の base(SHA または origin/<branch>)")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", help="owner/name(既定: $GITHUB_REPOSITORY / config の github.repo)")
    ap.add_argument("--config", default=str(common.CONFIG_REL))
    ap.add_argument("--template", default=str(TEMPLATE))
    ap.add_argument("--output", help="判定結果の JSON を書き出すファイル")
    ap.add_argument("--prompt-out", help="mode が full / delta の時にプロンプトを書き出すファイル")
    ap.add_argument("--github-output", action="store_true", help="$GITHUB_OUTPUT にも書く")
    a = ap.parse_args(argv)

    try:
        cfg = common.load_config(Path(a.config))
        settings = common.review_settings(cfg)
        repo = common.resolve_repo(a.repo, cfg)
        markers = fetch_markers(repo, a.pr) if settings["provider"] == "action" else []
        plan = decide(base=a.base, head=a.head, markers=markers, settings=settings, config=a.config)
        if plan["mode"] in ("full", "delta"):
            # post_review.py が Codex の実行後に PR ヘッドの作業ツリーで git を実行しなくて済むよう、
            # インラインコメントを付けられる行をここで計算しておく(キーの順序 = 差分の順序)
            plan["commentable"] = {p: sorted(v) for p, v in
                                   common.commentable_lines(plan["merge_base"], plan["head"]).items()}
        if a.prompt_out and plan["mode"] in ("full", "delta"):
            template = Path(a.template).read_text(encoding="utf-8")
            prompt = build_prompt(template, plan, repo=repo, pr=a.pr, guidelines=review_guidelines(plan["pr_base"]))
            Path(a.prompt_out).write_text(prompt, encoding="utf-8")
    except (ValueError, OSError, json.JSONDecodeError, common.GitHubError) as e:
        print(f"::error::{e}")
        return 2
    except subprocess.CalledProcessError as e:
        print(f"::error::git に失敗: {(e.stderr if isinstance(e.stderr, str) else (e.stderr or b'').decode(errors='replace')).strip()}")
        return 2

    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if a.output:
        Path(a.output).write_text(text + "\n", encoding="utf-8")
    if a.github_output:
        write_github_output(plan)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
