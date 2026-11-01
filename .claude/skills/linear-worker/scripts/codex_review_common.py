#!/usr/bin/env python3
"""Codex レビュー(review_plan.py / post_review.py / review_status.py)の共通処理。

- config(`.kiro/orchestration/config.json`)の `review.*` の読み出しと既定値
- PR コメント / PR review に埋める隠しマーカーの書式と解析
  `<!-- harness-codex-review sha=<sha> round=<n> patch=<patch-id> mode=<mode> -->`
  `<!-- harness-codex-findings <base64(JSON)> -->`
  マーカーは github-actions[bot] が書いたものだけを信頼する
- `gh api` の薄いラッパー(テストではこのモジュールを使う側の関数を差し替える)

標準ライブラリのみ使用。`python3 -I`(スクリプトのディレクトリが sys.path に入らない)でも
各スクリプトが読み込めるよう、呼び出し側は自分のディレクトリを sys.path に足してから import する。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

CONFIG_REL = Path(".kiro/orchestration/config.json")
CHECK_NAME = "codex-review"  # ワークフローの job 名 = branch protection の必須チェック名
SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING = ("P0", "P1")  # スレッド化して未解決ならマージを止める重大度
# マーカーを信頼する author(REST は "github-actions[bot]"、GraphQL は "github-actions")
TRUSTED_AUTHORS = {"github-actions"}
DEFAULT_BOT_LOGIN = "chatgpt-codex-connector[bot]"
# mode: full / delta = Codex が実際にレビューした、skip = 前回の結果を引き継いだ、
# capped = 回数上限でレビューしなかった、unavailable = 実行できなかった(fail-open)
MODES = ("full", "delta", "skip", "capped", "unavailable")
REVIEWED_MODES = ("full", "delta", "skip")  # 次回の比較基準(prev)になり得るもの

REVIEW_MARKER = "harness-codex-review"
FINDINGS_MARKER = "harness-codex-findings"
FINDING_MARKER = "harness-codex-finding"  # インラインコメント 1 件ごとの重大度
MARKER_RE = re.compile(r"<!-- harness-codex-review((?:\s+[a-z_]+=[^\s<>]*)+)\s*-->")
FINDINGS_RE = re.compile(r"<!-- harness-codex-findings ([A-Za-z0-9+/=]*) -->")
FINDING_RE = re.compile(r"<!-- harness-codex-finding severity=(P[0-3]) -->")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


# --------------------------------------------------------------------------- config


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"{path} の最上位が JSON オブジェクトではない")
    return cfg


def _obj(value, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"config の {key} がオブジェクトではない")
    return value


def review_settings(cfg: dict) -> dict:
    """review.* の値(キーが無ければ既定値)。"""
    review = _obj(cfg.get("review"), "review")
    action = _obj(review.get("action"), "review.action")
    bot = review.get("bot")
    if bot is not None and not isinstance(bot, dict):
        raise ValueError("config の review.bot がオブジェクトではない")

    def positive_int(value, default, key):
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"config の {key} が 0 以上の整数ではない")
        return value

    def opt_str(value, key):
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"config の {key} が文字列ではない")
        return value

    provider = review.get("provider") or "action"
    if provider not in ("action", "app"):
        raise ValueError("config の review.provider は action / app のどちらか")
    return {
        "provider": provider,
        "max_rounds": positive_int(action.get("max_rounds"), 3, "review.action.max_rounds"),
        "model": opt_str(action.get("model"), "review.action.model"),
        "effort": opt_str(action.get("effort"), "review.action.effort"),
        "fail_open": review.get("fail_open", True) is not False,
        "wait_minutes": positive_int(review.get("wait_minutes"), 60, "review.wait_minutes"),
        "bot": None if bot is None and "bot" in review else {
            "login": opt_str((bot or {}).get("login"), "review.bot.login") or DEFAULT_BOT_LOGIN,
            "summary_marker": opt_str((bot or {}).get("summary_marker"), "review.bot.summary_marker"),
            "request_comment": opt_str(
                (bot or {}).get("request_comment") or (bot or {}).get("retrigger_comment"),
                "review.bot.request_comment"),
        },
    }


def resolve_repo(arg: str | None, cfg: dict) -> str:
    repo = arg or os.environ.get("GITHUB_REPOSITORY") or ((cfg.get("github") or {}).get("repo") or "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise ValueError("リポジトリ(owner/name)が分からない。--repo か GITHUB_REPOSITORY を指定する")
    return repo


# --------------------------------------------------------------------------- マーカー


def neutralize(text: str) -> str:
    """LLM の出力などをコメント本文へ入れる前に、HTML コメント(マーカーの偽装)を無効化する。"""
    return str(text).replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def format_marker(sha: str, round_: int, patch: str, mode: str) -> str:
    return f"<!-- {REVIEW_MARKER} sha={sha} round={int(round_)} patch={patch or 'none'} mode={mode} -->"


def encode_findings(findings: list[dict]) -> str:
    raw = json.dumps(findings, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"<!-- {FINDINGS_MARKER} {base64.b64encode(raw).decode('ascii')} -->"


def parse_marker(text: str | None) -> dict | None:
    """本文の最後のマーカー(と findings)を読む。壊れていれば None。"""
    if not text:
        return None
    found = list(MARKER_RE.finditer(text))
    if not found:
        return None
    attrs = dict(kv.split("=", 1) for kv in found[-1].group(1).split())
    sha = attrs.get("sha", "").lower()
    mode = attrs.get("mode", "full")  # mode 属性の無い書式(設計書どおり)は通常のレビュー扱い
    try:
        round_ = int(attrs.get("round", ""))
    except ValueError:
        return None
    if not SHA_RE.match(sha) or mode not in MODES or round_ < 0:
        return None
    findings: list[dict] = []
    fm = list(FINDINGS_RE.finditer(text))
    if fm:
        try:
            data = json.loads(base64.b64decode(fm[-1].group(1), validate=True).decode("utf-8"))
            if isinstance(data, list):
                findings = [f for f in data if isinstance(f, dict)]
        except (ValueError, UnicodeDecodeError):
            findings = []
    patch = attrs.get("patch", "none")
    return {"sha": sha, "round": round_, "patch": "" if patch == "none" else patch,
            "mode": mode, "findings": findings}


def login_of(user) -> str:
    login = ((user or {}).get("login") or "") if isinstance(user, dict) else ""
    return login[:-5] if login.endswith("[bot]") else login


def same_login(a: str, b: str) -> bool:
    strip = lambda s: (s or "").lower().removesuffix("[bot]")  # noqa: E731
    return strip(a) == strip(b)


def collect_markers(issue_comments: list[dict], reviews: list[dict]) -> list[dict]:
    """信頼できる author が書いたマーカーを古い順に並べる。"""
    out = []
    for c in issue_comments:
        if login_of(c.get("user")) not in TRUSTED_AUTHORS:
            continue
        m = parse_marker(c.get("body"))
        if m:
            m.update(at=c.get("created_at") or "", url=c.get("html_url") or "", source="comment",
                     order=int(c.get("id") or 0))
            out.append(m)
    for r in reviews:
        if login_of(r.get("user")) not in TRUSTED_AUTHORS:
            continue
        m = parse_marker(r.get("body"))
        if m:
            m.update(at=r.get("submitted_at") or "", url=r.get("html_url") or "", source="review",
                     order=int(r.get("id") or 0))
            out.append(m)
    out.sort(key=lambda m: (m["at"], m["source"], m["order"]))
    return out


# --------------------------------------------------------------------------- 時刻


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- GitHub API


class GitHubError(Exception):
    pass


def gh_api(path: str, method: str = "GET", body: dict | None = None):
    """`gh api` を 1 回呼ぶ(GH_TOKEN は環境変数)。応答 JSON を返す。"""
    cmd = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if body is not None:
        cmd += ["--input", "-"]
    proc = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise GitHubError(f"gh api {method} {path} に失敗: {(proc.stderr or proc.stdout).strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def gh_api_list(path: str, key: str | None = None, limit_pages: int = 50) -> list:
    """一覧系の API を per_page=100 でページ送りして連結する(key があれば応答のそのキー)。"""
    items: list = []
    sep = "&" if "?" in path else "?"
    for page in range(1, limit_pages + 1):
        data = gh_api(f"{path}{sep}per_page=100&page={page}")
        chunk = (data or {}).get(key, []) if key else (data or [])
        items.extend(chunk)
        if len(chunk) < 100:
            break
    return items


def gh_graphql(query: str, variables: dict) -> dict:
    data = gh_api("graphql", method="POST", body={"query": query, "variables": variables})
    if not isinstance(data, dict) or data.get("errors"):
        raise GitHubError(f"GraphQL エラー: {json.dumps((data or {}).get('errors'), ensure_ascii=False)}")
    return data["data"]


def enc(value: str) -> str:
    return quote(str(value), safe="")


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 1) { nodes { author { login } body url } }
        }
      }
    }
  }
}
"""


def fetch_review_threads(repo: str, pr: int) -> list[dict]:
    """PR のレビュースレッド(先頭コメントの author / body 付き)。"""
    owner, name = repo.split("/", 1)
    threads: list[dict] = []
    after = None
    for _ in range(50):
        data = gh_graphql(REVIEW_THREADS_QUERY, {"owner": owner, "name": name, "number": int(pr), "after": after})
        rt = data["repository"]["pullRequest"]["reviewThreads"]
        for node in rt["nodes"]:
            first = (node.get("comments") or {}).get("nodes") or [{}]
            threads.append({
                "resolved": bool(node.get("isResolved")),
                "outdated": bool(node.get("isOutdated")),
                "path": node.get("path"),
                "line": node.get("line"),
                "author": login_of((first[0] or {}).get("author")),
                "body": (first[0] or {}).get("body") or "",
                "url": (first[0] or {}).get("url") or "",
            })
        if not rt["pageInfo"]["hasNextPage"]:
            break
        after = rt["pageInfo"]["endCursor"]
    return threads


def fetch_markers(repo: str, pr: int) -> list[dict]:
    comments = gh_api_list(f"repos/{repo}/issues/{int(pr)}/comments")
    reviews = gh_api_list(f"repos/{repo}/pulls/{int(pr)}/reviews")
    return collect_markers(comments, reviews)
