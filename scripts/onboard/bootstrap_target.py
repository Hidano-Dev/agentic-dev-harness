#!/usr/bin/env python3
"""対象リポジトリのチェックアウトに harness を導入する(冪等)。

Onboard Repository ワークフローが、対象リポジトリを `target/` に checkout した
状態で呼ぶ。行うこと:

1. 配布物(ASSET_PATHS: linear-worker と Git 運用ルール)をコピー
   - ディレクトリは丸ごと harness 管理なので、配布元に無いファイルは削除して揃える
2. 同期ワークフロー(templates/consumer/harness-sync.yml)を配置
   - 既に agentic-dev-harness の harness-sync.yml があれば配布版に差し替える
     (旧版は SDD 一式も同期していた)。同名の無関係なファイルなら中止する
   - unity-sdd-template 生成先の orchestration-sync.yml(SDD の旧同期経路)には触れない。
     SDD は unity-sdd-kit の同期ワークフローへ移行する(docs/changelog.md)
3. ルート CLAUDE.md に `@.claude/rules/git-workflow.md` を保証
4. `.kiro/orchestration/config.json` を生成(既にあれば触らない)。`linear.project` はリポジトリ名

SDD ワークフロー(kiro commands / dev-orchestrator / AGENTS.md 等)は本ハーネスに含まれない。
必要なら unity-sdd-kit を別途導入する。

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

IMPORT_LINE = "@.claude/rules/git-workflow.md"
# harness が所有・配布するパス。templates/consumer/harness-sync.yml の SYNC_PATHS と揃える
ASSET_PATHS = [".claude/skills/linear-worker", ".claude/rules/git-workflow.md"]
SYNC_TEMPLATE = Path("templates/consumer/harness-sync.yml")
SYNC_DEST = Path(".github/workflows/harness-sync.yml")
CONFIG_DEST = Path(".kiro/orchestration/config.json")

CLAUDE_MD_NEW = f"""<!--
  Git 運用ルール(Linear 駆動の自律ワーカー linear-worker への入口を含む)は
  agentic-dev-harness から配布される .claude/rules/ にある。
  プロジェクト固有のメモはこのファイルに追記する。
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
    """src を dst へコピーする。ディレクトリは配布元に無いファイルを残さないよう丸ごと置き換える。"""
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, symlinks=False)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def sync_assets(harness: Path, target: Path) -> list[str]:
    """戻り値: 同期したパス。"""
    # 途中で失敗して半端な状態を残さないよう、コピー前に全同期先を検査する
    for p in ASSET_PATHS:
        assert_regular_dest(target, Path(p))
        links = find_symlinks(target / p)
        if links:
            shown = ", ".join(str(x.relative_to(target)) for x in links[:5])
            raise RuntimeError(
                f"同期先にシンボリックリンクがあるため中止した: {shown}{' …' if len(links) > 5 else ''}。"
                "リンクを外してから再実行すること"
            )
    synced: list[str] = []
    for p in ASSET_PATHS:
        src = harness / p
        if not src.exists():
            print(f"::warning::{p} は配布元に存在しないためスキップ", file=sys.stderr)
            continue
        copy_path(src, target / p)
        synced.append(p)
    return synced


def place_sync_workflow(harness: Path, target: Path) -> tuple[str, str]:
    """戻り値: (sync 方式, 今回の操作)。

    方式は registry の sync フィールドに対応(常に workflow)。
    操作は written(新規配置) / existing(配布版と同一) / updated(旧版を配布版に差し替え)。
    """
    assert_regular_dest(target, SYNC_DEST)
    dest = target / SYNC_DEST
    text = (harness / SYNC_TEMPLATE).read_text(encoding="utf-8")
    if dest.exists():
        current = dest.read_text(encoding="utf-8")
        # 同名の無関係なワークフローを正規の同期経路と誤認して上書きしない
        if "agentic-dev-harness" not in current:
            raise RuntimeError(
                f"{SYNC_DEST} が既に存在するが、agentic-dev-harness の同期ワークフローではない"
                "(配布元の参照が無い)。退避するか templates/consumer/harness-sync.yml で置き換えてから再実行すること"
            )
        if current == text:
            return "workflow", "existing"
        dest.write_text(text, encoding="utf-8", newline="\n")
        return "workflow", "updated"
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
    ap.add_argument("--linear-team", required=True, help="config の linear.team")
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
    if not a.linear_team.strip():
        print("::error::--linear-team が空(linear-worker の必須設定)", file=sys.stderr)
        return 1

    try:
        # 書き込み先の事前検査(コピー前に全部見る: 途中で失敗して半端な状態を残さない)
        for rel in (Path("CLAUDE.md"), CONFIG_DEST, SYNC_DEST):
            assert_regular_dest(target, rel)
        check_existing_config_identity(target, a.repo, a.default_branch)
        result: dict = {}
        result["assets_synced"] = sync_assets(harness, target)
        result["sync_mode"], result["sync_workflow"] = place_sync_workflow(harness, target)
        result["claude_md"] = ensure_claude_md(target)
        result["config"] = write_config(harness, target, a)
        # 台帳に載せる実値(新規生成でも既存でも config.json から読む)
        result["effective"] = effective_config(target)
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
