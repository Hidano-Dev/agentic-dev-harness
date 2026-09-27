#!/usr/bin/env python3
"""対象リポジトリのチェックアウトに harness を導入する(冪等)。

Onboard Repository ワークフローが、対象リポジトリを `target/` に checkout した
状態で呼ぶ。行うこと:

1. 配布物(.claude .codex .kiro .agents AGENTS.md)を上書きコピー
   - AGENTS.md は、対象に既にあってマーカー(AGENTS_MD_MARKER)を含まないものは
     独自ファイルとみなし触らない(配布版にはマーカーが入っている)
2. 同期ワークフロー(templates/consumer/harness-sync.yml)を配置
   - 既に harness-sync.yml / orchestration-sync.yml(unity-sdd-template 生成先)が
     あれば置かない
3. ルート CLAUDE.md に `@.claude/rules/sdd-workflow.md` を保証
4. `.kiro/orchestration/config.json` を生成(既にあれば触らない。`--linear-team` が
   空なら SDD ワークフローのみの導入とみなし作らない)。`linear.project` はリポジトリ名

結果を JSON で `--result` に書く(ワークフローが PR 本文・サマリに使う)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_config import TEMPLATE_REL, project_name, render  # noqa: E402

IMPORT_LINE = "@.claude/rules/sdd-workflow.md"
ASSET_DIRS = [".claude", ".codex", ".kiro", ".agents"]
AGENTS_MD = "AGENTS.md"
# 配布版 AGENTS.md の先頭コメントに含まれる文字列。templates/consumer/harness-sync.yml の
# AGENTS_MD_MARKER と同じ値にする(同期ワークフロー側も同じ判定で上書きを避ける)
AGENTS_MD_MARKER = "managed-by: agentic-dev-harness"
SYNC_TEMPLATE = Path("templates/consumer/harness-sync.yml")
SYNC_DEST = Path(".github/workflows/harness-sync.yml")
TEMPLATE_INIT_SYNC = Path(".github/workflows/orchestration-sync.yml")
CONFIG_DEST = Path(".kiro/orchestration/config.json")
# 既存の同期ワークフローを「本 harness の同期経路」と認める SYNC_PATHS 指定(新旧両方)
SYNC_PATHS_FULL = 'SYNC_PATHS: ".claude .codex .kiro .agents AGENTS.md"'
SYNC_PATHS_NO_AGENTS = 'SYNC_PATHS: ".claude .codex .kiro .agents"'
# unity-sdd-template 生成先の orchestration-sync.yml は同期パスをループに直書きしている
TEMPLATE_INIT_LOOP_FULL = "for p in .claude .codex .kiro .agents AGENTS.md; do"
TEMPLATE_INIT_LOOP_NO_AGENTS = "for p in .claude .codex .kiro .agents; do"

CLAUDE_MD_NEW = f"""<!--
  SDD ワークフローと Git 運用ルールは agentic-dev-harness から配布される
  .claude/rules/ にある。プロジェクト固有のメモはこのファイルに追記する。
-->

{IMPORT_LINE}
"""


def find_symlinks(dst: Path) -> list[Path]:
    """同期先(とその配下)にあるシンボリックリンクを列挙する。"""
    if dst.is_symlink():
        return [dst]
    if dst.is_dir():
        return [p for p in dst.rglob("*") if p.is_symlink()]
    return []


def assert_regular_dest(target: Path, rel: Path) -> None:
    """target/rel への書き込みが対象ルート内の通常ファイルに閉じることを保証する。

    rel の各構成要素(途中のディレクトリを含む)がシンボリックリンクなら中止する。
    リンクが無ければ書き込み先は固定の相対パスなので対象ルートの外には出ない。
    """
    cur = target
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise RuntimeError(
                f"{cur.relative_to(target)} がシンボリックリンクのため書き込みを中止した。"
                "リンクを外してから再実行すること"
            )


def copy_path(src: Path, dst: Path) -> None:
    # 同期先にシンボリックリンクがあると copy2 / copytree はリンク先へ書き込むため、
    # 対象ルートの外や無関係な追跡ファイルを上書きしかねない。リンクは拒否する
    links = find_symlinks(dst)
    if links:
        shown = ", ".join(str(p.relative_to(dst.parent)) for p in links[:5])
        raise RuntimeError(
            f"同期先 {dst.name} にシンボリックリンクがあるため上書きコピーを中止した: {shown}"
            f"{' …' if len(links) > 5 else ''}。リンクを外してから再実行すること"
        )
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def has_agents_md_marker(path: Path) -> bool:
    return AGENTS_MD_MARKER in path.read_text(encoding="utf-8", errors="replace")


def sync_assets(harness: Path, target: Path) -> tuple[list[str], str]:
    """戻り値: (同期したパス, AGENTS.md の扱い: synced / kept)。"""
    paths = ASSET_DIRS + [AGENTS_MD]
    # 配布元 AGENTS.md にマーカーが無いと、配布した先で次回から「独自ファイル」と誤認されて
    # 更新が届かなくなる。配布元側の欠落はここで止める
    if (harness / AGENTS_MD).exists() and not has_agents_md_marker(harness / AGENTS_MD):
        raise RuntimeError(f"配布元の {AGENTS_MD} にマーカー({AGENTS_MD_MARKER!r})が無い。先頭のマーカー行を戻すこと")
    # 途中で失敗して半端な状態を残さないよう、コピー前に全同期先を検査する
    for p in paths:
        links = find_symlinks(target / p)
        if links:
            shown = ", ".join(str(x.relative_to(target)) for x in links[:5])
            raise RuntimeError(
                f"同期先にシンボリックリンクがあるため中止した: {shown}{' …' if len(links) > 5 else ''}。"
                "リンクを外してから再実行すること"
            )
    synced: list[str] = []
    agents_md = "synced"
    for p in paths:
        src = harness / p
        if not src.exists():
            print(f"::warning::{p} は配布元に存在しないためスキップ", file=sys.stderr)
            continue
        if p == AGENTS_MD and (target / p).exists() and not has_agents_md_marker(target / p):
            # 独自の AGENTS.md(マーカー無し)は尊重する。同期ワークフローも同じ判定で上書きしない
            print(
                f"::notice::対象の {AGENTS_MD} にマーカー({AGENTS_MD_MARKER!r})が無いため独自ファイルとみなし、上書きしない",
                file=sys.stderr,
            )
            agents_md = "kept"
            continue
        copy_path(src, target / p)
        synced.append(p)
    return synced, agents_md


def is_template_init_sync(path: Path) -> bool:
    """unity-sdd-template 生成先の同期ワークフローか(同名の無関係なファイルを誤認しない)。

    生成先の orchestration-sync.yml は配布元として agentic-dev-harness を参照し、
    配布物を固定ループでコピーする。どちらも無ければ別物とみなし、harness-sync.yml を置く。
    """
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if "agentic-dev-harness" not in text:
        print(
            f"::warning::{path.name} は agentic-dev-harness を参照していないため生成先の同期ワークフローとは"
            "みなさず、harness-sync.yml を配置します",
            file=sys.stderr,
        )
        return False
    if TEMPLATE_INIT_LOOP_FULL not in text and TEMPLATE_INIT_LOOP_NO_AGENTS not in text:
        print(
            f"::warning::{path.name} に想定の同期ループが無いため生成先の同期ワークフローとはみなさず、"
            "harness-sync.yml を配置します",
            file=sys.stderr,
        )
        return False
    return True


def warn_if_legacy_agents_md_sync(workflow: Path, full_line: str) -> None:
    """AGENTS.md をマーカー判定なしで上書きする旧版の同期ワークフローなら警告する。

    旧版は SYNC_PATHS(またはループ)に AGENTS.md を含み、マーカーの文字列を持たない。
    独自の AGENTS.md を持つリポジトリで旧版が動くと上書きされるので差し替えを促す
    (ワークフロー本体はユーザーの編集があり得るため、ここでは書き換えない)。
    """
    text = workflow.read_text(encoding="utf-8")
    if full_line in text and AGENTS_MD_MARKER not in text:
        print(
            f"::warning::{workflow.name} は AGENTS.md をマーカー判定なしで上書きする旧版です。"
            "独自の AGENTS.md を保護するには templates/consumer/harness-sync.yml の新版に差し替えてください",
            file=sys.stderr,
        )


def place_sync_workflow(harness: Path, target: Path) -> tuple[str, str]:
    """戻り値: (sync 方式, 今回の操作)。

    方式は registry の sync フィールドに対応(workflow / template-init)。
    操作は written(新規配置) / existing(既存を維持) /
    template-init(生成先の orchestration-sync.yml をそのまま使用)。
    """
    assert_regular_dest(target, SYNC_DEST)
    dest = target / SYNC_DEST
    has_harness_sync = False
    if dest.exists():
        # 同名の無関係なワークフロー・古い不完全なワークフローを正規の同期経路と誤認しない
        # (上書きもしない)。配布元の参照と、既知の SYNC_PATHS 指定のどちらかを必須にする
        text = dest.read_text(encoding="utf-8")
        if "agentic-dev-harness" not in text or (SYNC_PATHS_FULL not in text and SYNC_PATHS_NO_AGENTS not in text):
            raise RuntimeError(
                f"{SYNC_DEST} が既に存在するが、agentic-dev-harness の現行の同期ワークフローではない"
                f"(配布元の参照が無い、または SYNC_PATHS が {SYNC_PATHS_FULL!r} / {SYNC_PATHS_NO_AGENTS!r} の"
                "いずれでもない)。templates/consumer/harness-sync.yml で置き換えてから再実行すること"
            )
        warn_if_legacy_agents_md_sync(dest, SYNC_PATHS_FULL)
        has_harness_sync = True
    if is_template_init_sync(target / TEMPLATE_INIT_SYNC):
        warn_if_legacy_agents_md_sync(target / TEMPLATE_INIT_SYNC, TEMPLATE_INIT_LOOP_FULL)
        return "template-init", "template-init"
    if has_harness_sync:
        return "workflow", "existing"
    text = (harness / SYNC_TEMPLATE).read_text(encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="\n")
    return "workflow", "written"


def ensure_claude_md(target: Path) -> str:
    assert_regular_dest(target, Path("CLAUDE.md"))
    f = target / "CLAUDE.md"
    if not f.exists():
        f.write_text(CLAUDE_MD_NEW, encoding="utf-8", newline="\n")
        return "created"
    text = f.read_text(encoding="utf-8")
    if any(line.strip() == IMPORT_LINE for line in text.splitlines()):
        return "unchanged"
    sep = "" if text.endswith("\n") else "\n"
    f.write_text(f"{text}{sep}\n{IMPORT_LINE}\n", encoding="utf-8", newline="\n")
    return "appended"


def check_existing_config_identity(target: Path, repo: str, default_branch: str) -> None:
    """既存 config.json の github.repo / default_branch が対象と一致することを確認する。

    別リポジトリからコピーした config や既定ブランチ変更前の config を再利用すると、
    ワーカーが別リポジトリ・存在しないブランチを向くので、何も変更する前に止める。
    """
    dest = target / CONFIG_DEST
    if not dest.exists():
        return
    try:
        cfg = json.loads(dest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CONFIG_DEST} が JSON として不正: {e}。修正してから再実行すること") from e
    gh = cfg.get("github", {}) if isinstance(cfg, dict) else {}
    mismatches = []
    if gh.get("repo") != repo:
        mismatches.append(f"github.repo: config={gh.get('repo')!r} / 対象={repo!r}")
    if gh.get("default_branch") != default_branch:
        mismatches.append(f"github.default_branch: config={gh.get('default_branch')!r} / 対象={default_branch!r}")
    if mismatches:
        raise RuntimeError(
            f"既存の {CONFIG_DEST} が対象リポジトリと一致しない: " + "; ".join(mismatches)
            + "。config を修正してから再実行すること"
        )
    # linear.team は linear-worker の必須設定であり、台帳登録にも使う。空のまま先へ進むと
    # 対象だけ変更して台帳登録で失敗するので、コピー前にここで止める
    linear = (cfg.get("linear") or {}) if isinstance(cfg, dict) else {}
    team = linear.get("team")
    if not isinstance(team, str) or not team.strip():
        raise RuntimeError(
            f"既存の {CONFIG_DEST} に linear.team が無い(または空)。config を修正してから再実行すること"
        )
    # ラベル名は linear-worker が実値をそのまま読むため、空・前後空白付きの値は
    # (台帳や Linear 側だけ正規化しても食い違うので)config 側の修正を求める
    for key, value in (linear.get("labels") or {}).items():
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise RuntimeError(
                f"既存の {CONFIG_DEST} の linear.labels.{key} が不正({value!r}: 空、または前後に空白)。"
                "config を修正してから再実行すること"
            )


def effective_config(target: Path) -> dict:
    """config.json の実値のうち台帳・Linear 準備に使う項目(team / project / labels)を返す。"""
    cfg = json.loads((target / CONFIG_DEST).read_text(encoding="utf-8"))
    linear = cfg.get("linear") or {}
    labels = linear.get("labels") or {}
    # ラベル名が欠落・空なら SKILL.md の既定値に正規化する(空名を台帳や Linear に流さない)
    return {
        "team": linear.get("team"),
        "project": linear.get("project") or None,
        "needs_human": (labels.get("needs_human") or "").strip() or "needs-human",
        "needs_local": (labels.get("needs_local") or "").strip() or "needs-local",
    }


def write_config(harness: Path, target: Path, a: argparse.Namespace) -> str:
    if not a.linear_team.strip():
        return "skipped"  # SDD ワークフローのみ(linear-worker なし)
    assert_regular_dest(target, CONFIG_DEST)
    dest = target / CONFIG_DEST
    if dest.exists():
        # 既存 config は変更しない。台帳にはフォーム入力ではなく既存 config の実値を載せるので、
        # 食い違いは警告して知らせる(直したければ config を編集して再実行)。
        # ラベル名は既存 config の値をそのまま使う(フォームでは決めない)ので比較しない
        eff = effective_config(target)
        wanted = {"team": a.linear_team.strip(), "project": project_name(a.repo)}
        diffs = [f"{k}: config={eff.get(k)!r} / 規約={v!r}" for k, v in wanted.items() if eff.get(k) != v]
        if diffs:
            print(
                "::warning::既存の config.json とフォーム入力・規約が一致しません(config を優先し、台帳にも config の"
                "値を登録します): " + "; ".join(diffs),
                file=sys.stderr,
            )
        return "exists"
    template = json.loads((harness / TEMPLATE_REL).read_text(encoding="utf-8"))
    cfg = render(
        template,
        repo=a.repo,
        default_branch=a.default_branch,
        team=a.linear_team.strip(),
        review_bot=a.review_bot,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return "created"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", default=".")
    ap.add_argument("--target", required=True)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--default-branch", default="main")
    ap.add_argument("--linear-team", default="", help="空なら config.json を作らない(SDD のみ)")
    ap.add_argument("--review-bot", choices=["codex", "none"], default="codex")
    ap.add_argument("--result", help="結果 JSON の書き出し先")
    a = ap.parse_args(argv)

    harness = Path(a.harness).resolve()
    target = Path(a.target).resolve()
    if not (harness / TEMPLATE_REL).exists():
        print(f"::error::{a.harness} は agentic-dev-harness ではない({TEMPLATE_REL} が無い)", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"::error::対象ディレクトリが無い: {target}", file=sys.stderr)
        return 1

    try:
        # 書き込み先の事前検査(コピー前に全部見る: 途中で失敗して半端な状態を残さない)
        for rel in (Path("CLAUDE.md"), CONFIG_DEST, SYNC_DEST, TEMPLATE_INIT_SYNC):
            assert_regular_dest(target, rel)
        # 既に config.json がある(= linear-worker が動き得る)対象を SDD のみとして扱うと、
        # 台帳(harness: null)と実態が食い違うので拒否する
        if not a.linear_team.strip() and (target / CONFIG_DEST).exists():
            raise RuntimeError(
                f"{CONFIG_DEST} が既に存在するため SDD のみ(sdd_only)としては導入できない。"
                "sdd_only を外して再実行すること"
            )
        check_existing_config_identity(target, a.repo, a.default_branch)
        result: dict = {}
        result["assets_synced"], result["agents_md"] = sync_assets(harness, target)
        result["sync_mode"], result["sync_workflow"] = place_sync_workflow(harness, target)
        result["claude_md"] = ensure_claude_md(target)
        result["config"] = write_config(harness, target, a)
        # 台帳に載せる実値(新規生成でも既存でも config.json から読む)
        result["effective"] = effective_config(target) if result["config"] in ("created", "exists") else None
    except RuntimeError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if a.result:
        Path(a.result).write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
