#!/usr/bin/env python3
"""PR の現在のヘッドに対する Codex レビューの状態を 1 回だけ調べて JSON で出す(読み取り専用)。

エージェントがセッション内で待つ・巡回で判定するためのもの。待機はしない
(待つ側が Monitor / ScheduleWakeup 等で繰り返し呼ぶ)。

  review_status.py --pr N [--provider action|app] [--repo owner/name] [--config ...]

出力: {state, head, review_url, unresolved_p1, unresolved_total, provider, reason, ...}
  state:
    pending     まだ始まっていない(チェック未作成・キュー待ち / 依頼への反応なし)
    running     レビュー中(チェックが in_progress / ボットの eyes リアクション)
    clean       現在のヘッドのレビューが完了し、未解決の P0 / P1 スレッドが無い
    findings    レビューが完了し(または引き継ぎ / 上限)、未解決の P0 / P1 スレッドが残っている
                (action: スレッドにできず本文にだけ載った P0 / P1 = outside_diff_p1 が現在のヘッドにある場合も)
    skipped     レビュー不要・引き継ぎ・回数上限・draft(未解決の P0 / P1 スレッドは無い)
    unavailable レビューを実行できなかった(チェック失敗・fail-open・review.bot が null)
    timeout     現在のヘッドの push(app は依頼との遅い方)から review.wait_minutes を超えても完了しない

provider:
  action  codex-review.yml(GitHub Actions)。チェック `codex-review` の status / conclusion と
          post_review.py のマーカーから判定する
  app     従来の Codex GitHub App(後方互換)。依頼コメント以降のボットの review(commit_id == head)・
          summary コメント・+1(clean)/ eyes(running)リアクションから判定する
既定は config の review.provider(キーが無ければ action)。null(外部レビューなし)なら
API を呼ばずに state=skipped を返す。

終了コード: 0 = 判定できた(state は JSON を見る)、2 = エラー(GitHub API / config)。
標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_review_common as common  # noqa: E402

APP_SEVERITY_RE = re.compile(r"!\[(P[0-3]) Badge\]|\[(P[0-3])\]|\b(P[0-3])\b")


# --------------------------------------------------------------------------- GitHub(テストで差し替える)


def fetch_pr(repo: str, pr: int) -> dict:
    return common.gh_api(f"repos/{repo}/pulls/{int(pr)}") or {}


def fetch_check_runs(repo: str, sha: str) -> list[dict]:
    return common.gh_api_list(
        f"repos/{repo}/commits/{sha}/check-runs?check_name={common.enc(common.CHECK_NAME)}&filter=latest",
        key="check_runs")


def fetch_pushed_at(repo: str, sha: str) -> str | None:
    """ヘッドが push された時刻の近似: そのコミットの check suite の最初の作成時刻(無ければコミット時刻)。"""
    suites = common.gh_api_list(f"repos/{repo}/commits/{sha}/check-suites", key="check_suites")
    times = sorted(s.get("created_at") for s in suites if s.get("created_at"))
    if times:
        return times[0]
    commit = common.gh_api(f"repos/{repo}/commits/{sha}") or {}
    return ((commit.get("commit") or {}).get("committer") or {}).get("date")


def fetch_markers(repo: str, pr: int) -> list[dict]:
    return common.fetch_markers(repo, pr)


def fetch_threads(repo: str, pr: int) -> list[dict]:
    return common.fetch_review_threads(repo, pr)


def fetch_issue_comments(repo: str, pr: int) -> list[dict]:
    return common.gh_api_list(f"repos/{repo}/issues/{int(pr)}/comments")


def fetch_reviews(repo: str, pr: int) -> list[dict]:
    return common.gh_api_list(f"repos/{repo}/pulls/{int(pr)}/reviews")


def fetch_comment_reactions(repo: str, comment_id: int) -> list[dict]:
    return common.gh_api_list(f"repos/{repo}/issues/comments/{int(comment_id)}/reactions")


def fetch_pr_reactions(repo: str, pr: int) -> list[dict]:
    return common.gh_api_list(f"repos/{repo}/issues/{int(pr)}/reactions")


# --------------------------------------------------------------------------- 共通


def elapsed_minutes(since: str | None) -> float | None:
    t = common.parse_time(since)
    return None if t is None else round((common.now_utc() - t).total_seconds() / 60, 1)


def waiting_state(state: str, since: str | None, wait_minutes: int) -> tuple[str, float | None]:
    """pending / running のまま待機期限を過ぎていれば timeout。"""
    el = elapsed_minutes(since)
    if el is not None and el > wait_minutes:
        return "timeout", el
    return state, el


def _later(a: str | None, b: str | None) -> str | None:
    ta, tb = common.parse_time(a), common.parse_time(b)
    if ta is None:
        return b
    if tb is None:
        return a
    return a if ta >= tb else b


# --------------------------------------------------------------------------- provider = action


def action_unresolved(threads: list[dict]) -> tuple[int, int, list[str]]:
    """github-actions[bot] が作った未解決スレッド(P0 / P1 の数, 全体, P0 / P1 の URL)。"""
    p1 = total = 0
    urls = []
    for t in threads:
        if t["resolved"] or t["author"] not in common.TRUSTED_AUTHORS:
            continue
        m = common.FINDING_RE.search(t["body"])
        if not m:
            continue
        total += 1
        if m.group(1) in common.BLOCKING:
            p1 += 1
            urls.append(t["url"])
    return p1, total, urls


def status_action(repo: str, pr: int, settings: dict) -> dict:
    info = fetch_pr(repo, pr)
    head = (info.get("head") or {}).get("sha") or ""
    out = {"provider": "action", "head": head, "review_url": None, "unresolved_p1": 0, "unresolved_total": 0}
    if info.get("draft"):
        return dict(out, state="skipped", reason="draft の PR(codex-review は ready_for_review で走る)")

    runs = fetch_check_runs(repo, head)
    runs.sort(key=lambda r: (r.get("started_at") or "", r.get("id") or 0))
    run = runs[-1] if runs else None
    pushed_at = fetch_pushed_at(repo, head)
    out["pushed_at"] = pushed_at
    if run:
        out.update(check_status=run.get("status"), check_conclusion=run.get("conclusion"),
                   check_url=run.get("html_url"))

    if run is None or run.get("status") != "completed":
        base = "running" if run and run.get("status") == "in_progress" else "pending"
        state, el = waiting_state(base, pushed_at, settings["wait_minutes"])
        reason = {"pending": "チェック codex-review がまだ始まっていない", "running": "レビュー中",
                  "timeout": f"push から {settings['wait_minutes']} 分を超えても完了しない"}[state]
        return dict(out, state=state, reason=reason, elapsed_minutes=el)

    p1, total, urls = action_unresolved(fetch_threads(repo, pr))
    out.update(unresolved_p1=p1, unresolved_total=total, unresolved_p1_urls=urls)
    conclusion = run.get("conclusion")
    if conclusion not in ("success", "neutral", "skipped"):
        return dict(out, state="unavailable", reason=f"チェック codex-review が {conclusion}")

    mine = [m for m in fetch_markers(repo, pr) if m["sha"] == head]
    marker = mine[-1] if mine else None
    if marker:
        # スレッドにできなかった(本文にだけ載った)P0 / P1。thread が無い古い記録は inline で代用する
        out.update(review_url=marker.get("url"), mode=marker["mode"], round=marker["round"],
                   outside_diff_p1=sum(1 for f in marker["findings"]
                                       if f.get("severity") in common.BLOCKING
                                       and f.get("thread", f.get("inline")) is False))
    if marker and marker["mode"] == "unavailable":
        return dict(out, state="unavailable", reason="Codex を実行できなかった(fail-open でチェックは成功)")
    if p1:
        return dict(out, state="findings", reason=f"未解決の P0 / P1 スレッドが {p1} 件")
    if out.get("outside_diff_p1"):
        return dict(out, state="findings",
                    reason=f"スレッドにできなかった P0 / P1 が {out['outside_diff_p1']} 件(レビュー本文を参照。修正を push する)")
    if marker is None:
        return dict(out, state="skipped", reason="このヘッドのマーカーなし(fast track / OPENAI_API_KEY なし / 同じヘッドはレビュー済み)")
    if marker["mode"] in ("full", "delta"):
        return dict(out, state="clean", reason="レビュー完了・未解決の P0 / P1 なし")
    return dict(out, state="skipped",
                reason={"skip": "前回のレビュー結果を引き継ぎ", "capped": "レビュー回数の上限"}[marker["mode"]])


# --------------------------------------------------------------------------- provider = app


def app_severity(body: str) -> str | None:
    m = APP_SEVERITY_RE.search(body or "")
    return next((g for g in m.groups() if g), None) if m else None


def status_app(repo: str, pr: int, settings: dict) -> dict:
    bot = settings["bot"]
    info = fetch_pr(repo, pr)
    head = (info.get("head") or {}).get("sha") or ""
    out = {"provider": "app", "head": head, "review_url": None, "unresolved_p1": 0, "unresolved_total": 0}
    if bot is None:
        return dict(out, state="unavailable", reason="review.bot が null(外部レビューなし)")
    if info.get("draft"):
        return dict(out, state="skipped", reason="draft の PR")
    login = bot["login"]
    pushed_at = fetch_pushed_at(repo, head)
    t_push = common.parse_time(pushed_at)

    def after_push(ts: str | None) -> bool:
        t = common.parse_time(ts)
        return t is not None and (t_push is None or t >= t_push)

    comments = fetch_issue_comments(repo, pr)
    request = (bot.get("request_comment") or "").strip()
    requests = [c for c in comments if request and request in (c.get("body") or "") and after_push(c.get("created_at"))
                and not common.same_login(common.login_of(c.get("user")), login)]
    first_request = min((c["created_at"] for c in requests), key=common.parse_time, default=None)
    since = _later(pushed_at, first_request)
    out.update(pushed_at=pushed_at, requested_at=first_request)

    threads = fetch_threads(repo, pr)
    p1 = total = 0
    urls = []
    for t in threads:
        if t["resolved"] or not common.same_login(t["author"], login):
            continue
        total += 1
        if app_severity(t["body"]) in common.BLOCKING:
            p1 += 1
            urls.append(t["url"])
    out.update(unresolved_p1=p1, unresolved_total=total, unresolved_p1_urls=urls)

    def done(url: str | None, how: str) -> dict:
        if p1:
            return dict(out, state="findings", review_url=url, reason=f"{how}。未解決の P0 / P1 スレッドが {p1} 件")
        return dict(out, state="clean", review_url=url, reason=f"{how}。未解決の P0 / P1 なし")

    # (1) ボットの review(現在のヘッドに対するもの)
    reviews = [r for r in fetch_reviews(repo, pr)
               if common.same_login(common.login_of(r.get("user")), login) and r.get("commit_id") == head]
    if reviews:
        return done(reviews[-1].get("html_url"), "ボットのレビューが現在のヘッドに対して完了")
    # (2) ボットの summary コメント(push 以降)
    marker = bot.get("summary_marker") or ""
    for c in reversed(comments):
        if common.same_login(common.login_of(c.get("user")), login) and after_push(c.get("created_at")) \
                and (not marker or marker in (c.get("body") or "")):
            return done(c.get("html_url"), "ボットの summary コメントあり")
    # (3)(4) 依頼コメント・PR 本文へのボットのリアクション
    reactions: list[tuple[dict, str | None]] = []
    for c in requests:
        reactions += [(r, c.get("html_url")) for r in fetch_comment_reactions(repo, c["id"])]
    reactions += [(r, info.get("html_url")) for r in fetch_pr_reactions(repo, pr) if after_push(r.get("created_at"))]
    bot_reactions = [(r, url) for r, url in reactions if common.same_login(common.login_of(r.get("user")), login)]
    for r, url in bot_reactions:
        if r.get("content") == "+1":
            return done(url, "ボットの +1 リアクション(指摘なし)")
    running = any(r.get("content") == "eyes" for r, _ in bot_reactions)
    state, el = waiting_state("running" if running else "pending", since, settings["wait_minutes"])
    reason = {"running": "ボットの eyes リアクション(レビュー中)",
              "pending": "依頼への反応なし" if requests else "レビュー依頼コメントが現在のヘッドに対して無い",
              "timeout": f"push / 依頼の遅い方から {settings['wait_minutes']} 分を超えても完了しない"}[state]
    return dict(out, state=state, reason=reason, elapsed_minutes=el)


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--provider", choices=("action", "app"), help="既定: config の review.provider(無ければ action)")
    ap.add_argument("--repo")
    ap.add_argument("--config", default=str(common.CONFIG_REL))
    a = ap.parse_args(argv)
    try:
        cfg = common.load_config(Path(a.config))
        settings = common.review_settings(cfg)
        repo = common.resolve_repo(a.repo, cfg)
        provider = a.provider or settings["provider"]
        if provider is None:  # review.provider = null: 外部レビューなし(GitHub API も呼ばない)
            result = {"provider": None, "state": "skipped", "head": None, "review_url": None,
                      "unresolved_p1": 0, "unresolved_total": 0,
                      "reason": common.provider_skip_reason(None)}
        else:
            result = (status_action if provider == "action" else status_app)(repo, a.pr, settings)
    except (ValueError, OSError, KeyError, json.JSONDecodeError, common.GitHubError) as e:
        print(json.dumps({"state": "error", "reason": str(e)}, ensure_ascii=False))
        return 2
    result["checked_at"] = common.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
