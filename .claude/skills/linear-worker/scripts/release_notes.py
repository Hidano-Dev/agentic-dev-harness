#!/usr/bin/env python3
"""昇格 PR(Release PR。例: develop → main)の本文を生成する。

promote ワークフロー(templates/consumer/promote.yml)が、既定ブランチへの push のたびに
Release PR の本文をこの出力で作成・更新する。人間はこの PR でまとめてレビューするので、
挙動を変える変更(コア・配布物・指示文)を先頭で強調する。

- 対象: `<base>..<head>` の first-parent 上のコミット(base = リリースブランチ、head = 既定ブランチ)
  - `Merge pull request #N from <owner>/<branch>` のマージコミット → PR(本文 1 行目をタイトルとする)
  - `<タイトル> (#N)` のコミット(squash マージ)→ PR
  - それ以外のマージコミット(`Merge branch ...` 等)は載せない。その他の通常コミットは
    「PR を経ない変更」として載せる
  - リリースブランチ自身・`back-merge/` で始まるブランチからの PR(back-merge)は載せない
- PR は Linear Issue ID ごとにまとめる。ID はブランチ名(`<type>/<id>-<topic>`)と PR タイトルの
  `[A-Z]+-\\d+` から取る(`UTF-8` のような規格名は除く)
- 変更ファイルの分類(`git diff --name-only <base>...<head>`。上から順に最初に当たったもの):
  1. 配布物: sync manifest(リポジトリ直下の `sync-manifest.json`、無ければ `.harness/sync-manifest.json`)の
     `owned` と `workflows` のキー・値。manifest が無ければ(取り込み側)`<head>:.harness/version` の
     `owned`(あれば `workflows` も)、それも無ければ既定値(配布元の sync-manifest.json と同じ)
  2. コア: config の `auto_merge.protected_paths`(無ければ既定値)
  3. 指示文: 任意の階層の `CLAUDE.md` / `AGENTS.md` / `SKILL.md`、`.claude/` `.agents/` `.cursor/` 配下
- パスの一致: `/` で終わる値はディレクトリ接頭辞、それ以外は完全一致またはその配下

出力: Markdown(標準出力、または `--output` のファイル。UTF-8)。先頭に隠しマーカー
`<!-- harness-release-notes -->` を置く。
終了コード: 0 = 成功 / 2 = エラー(git の失敗・設定の不正)

標準ライブラリのみ使用(ランナー・ローカルどちらでも動く)。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CONFIG_REL = Path(".kiro/orchestration/config.json")
MANIFEST_CANDIDATES = [Path("sync-manifest.json"), Path(".harness/sync-manifest.json")]
MARKER = "<!-- harness-release-notes -->"
DEFAULT_PROTECTED_PATHS = [
    ".github/", ".harness/", ".kiro/orchestration/", ".kiro/settings/", ".claude/settings.json", ".codex/",
]
# sync manifest が無いときの既定(配布元の sync-manifest.json の owned + workflows のキー・値)
DEFAULT_OWNED = [".claude/skills/linear-worker", ".claude/rules/git-workflow.md", ".harness/invariants.json"]
DEFAULT_WORKFLOWS = {
    f"templates/consumer/{name}.yml": f".github/workflows/{name}.yml"
    for name in ("harness-sync", "docs-policy", "codex-review", "policy-gate", "conflict-sweeper", "promote",
                 "policy-gate-recheck", "policy-gate-relabel")
}
VERSION_REL = ".harness/version"
INSTRUCTION_NAMES = {"CLAUDE.md", "AGENTS.md", "SKILL.md"}
INSTRUCTION_PREFIXES = [".claude/", ".agents/", ".cursor/"]
BACK_MERGE_PREFIX = "back-merge/"
# Issue ID と紛らわしい規格名など
NON_ISSUE_PREFIXES = {"UTF", "SHA", "ISO", "RFC", "MD", "AES", "RSA", "HTTP", "TLS", "SSL", "PEP", "CP", "WIN", "IEC", "ECMA"}
TITLE_ID_RE = re.compile(r"\b([A-Z]+)-(\d+)\b")
BRANCH_ID_RE = re.compile(r"^(?:[^/]+/)?([A-Za-z]+)-(\d+)(?:-|$)")
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from ([^/\s]+)/(\S+)$")
SQUASH_PR_RE = re.compile(r"^(.*\S)\s+\(#(\d+)\)$")
MAX_FILES_PER_SECTION = 100
MAX_STAT_LINES = 300


class NotesError(Exception):
    pass


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True)
    if r.returncode != 0:
        raise NotesError(f"git {' '.join(args)} に失敗: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace")


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise NotesError(f"{path} の最上位が JSON オブジェクトではない")
    return data


def str_list(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise NotesError(f"{where} が文字列の配列ではない")
    return value


def version_at(ref: str) -> dict | None:
    """`<ref>:.harness/version` の JSON。無ければ None。"""
    r = subprocess.run(["git", "show", f"{ref}:{VERSION_REL}"], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NotesError(f"{ref}:{VERSION_REL} が JSON として不正")
    if not isinstance(data, dict):
        raise NotesError(f"{ref}:{VERSION_REL} の最上位が JSON オブジェクトではない")
    return data


def load_rules(config: Path, manifest: Path | None, head: str | None = None) -> dict:
    cfg = load_json(config) or {}
    am = cfg.get("auto_merge") or {}
    protected = str_list(am.get("protected_paths", DEFAULT_PROTECTED_PATHS), f"{config} の auto_merge.protected_paths")
    release_branch = (cfg.get("github") or {}).get("release_branch")

    if manifest is None:
        manifest = next((p for p in MANIFEST_CANDIDATES if p.is_file()), None)
    man = load_json(manifest) if manifest else None
    if man is None:
        owned, workflows = list(DEFAULT_OWNED), DEFAULT_WORKFLOWS
        # 取り込み側: 同期が書いた head の .harness/version の owned(あれば workflows も)を使う
        ver = version_at(head) if head else None
        if ver is not None:
            if "owned" in ver:
                owned = list(str_list(ver["owned"], f"{head}:{VERSION_REL} の owned"))
            if "workflows" in ver:
                workflows = ver["workflows"]
                if not isinstance(workflows, dict):
                    raise NotesError(f"{head}:{VERSION_REL} の workflows がオブジェクトではない")
    else:
        owned = list(str_list(man.get("owned", []), f"{manifest} の owned"))
        workflows = man.get("workflows") or {}
        if not isinstance(workflows, dict):
            raise NotesError(f"{manifest} の workflows がオブジェクトではない")
    owned += [p for kv in workflows.items() for p in kv if isinstance(p, str) and p]
    return {"protected": protected, "owned": owned, "release_branch": release_branch}


def path_matches(path: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False


def classify(path: str, rules: dict) -> str | None:
    if path_matches(path, rules["owned"]):
        return "owned"
    if path_matches(path, rules["protected"]):
        return "core"
    if path.rsplit("/", 1)[-1] in INSTRUCTION_NAMES or path_matches(path, INSTRUCTION_PREFIXES):
        return "instructions"
    return None


def issue_ids(title: str, branch: str | None) -> list[str]:
    ids: list[str] = []
    if branch:
        m = BRANCH_ID_RE.match(branch)
        if m and m.group(1).upper() not in NON_ISSUE_PREFIXES:
            ids.append(f"{m.group(1).upper()}-{m.group(2)}")
    for m in TITLE_ID_RE.finditer(title):
        if m.group(1) not in NON_ISSUE_PREFIXES:
            ident = f"{m.group(1)}-{m.group(2)}"
            if ident not in ids:
                ids.append(ident)
    return ids


def collect_commits(base: str, head: str, release_branch: str | None) -> tuple[list[dict], list[dict]]:
    out = git("log", "--first-parent", "--format=%H%x1f%P%x1f%s%x1f%b%x1e", f"{base}..{head}")
    prs: list[dict] = []
    others: list[dict] = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, subject, body = (rec.split("\x1f") + ["", "", ""])[:4]
        is_merge = len(parents.split()) > 1
        m = MERGE_PR_RE.match(subject)
        if is_merge and m:
            branch = m.group(3)
            if branch == release_branch or branch.startswith(BACK_MERGE_PREFIX):
                continue
            title = next((line.strip() for line in body.splitlines() if line.strip()), branch)
            prs.append({"number": int(m.group(1)), "title": title, "branch": branch, "sha": sha})
            continue
        if is_merge:
            continue  # Merge branch ... 等(PR 単位の変更ではない)
        m = SQUASH_PR_RE.match(subject)
        if m:
            prs.append({"number": int(m.group(2)), "title": m.group(1), "branch": None, "sha": sha})
        else:
            others.append({"sha": sha, "subject": subject})
    for pr in prs:
        pr["issues"] = issue_ids(pr["title"], pr["branch"])
    prs.sort(key=lambda p: p["number"])
    others.reverse()  # 古い順
    return prs, others


def file_section(title: str, note: str, files: list[str]) -> list[str]:
    lines = [f"### {title}({len(files)} 件)", "", note, ""]
    if not files:
        return lines[:1] + ["", "なし", ""]
    for f in files[:MAX_FILES_PER_SECTION]:
        lines.append(f"- `{f}`")
    if len(files) > MAX_FILES_PER_SECTION:
        lines.append(f"- …ほか {len(files) - MAX_FILES_PER_SECTION} 件")
    lines.append("")
    return lines


def pr_line(pr: dict) -> str:
    branch = f"(`{pr['branch']}`)" if pr["branch"] else ""
    return f"- #{pr['number']} {pr['title']}{branch}"


def render(base: str, head: str, rules: dict) -> str:
    base_sha = git("rev-parse", "--short", base).strip()
    head_sha = git("rev-parse", "--short", head).strip()
    files = [f for f in git("diff", "--name-only", "--no-renames", f"{base}...{head}").splitlines() if f]
    groups: dict[str, list[str]] = {"owned": [], "core": [], "instructions": []}
    for f in files:
        kind = classify(f, rules)
        if kind:
            groups[kind].append(f)
    prs, others = collect_commits(base, head, rules["release_branch"])

    lines = [MARKER, f"`{base}`({base_sha})← `{head}`({head_sha})の昇格 PR です。", ""]
    flagged = sum(len(v) for v in groups.values())
    lines += ["## 要確認の変更", ""]
    if flagged:
        lines += [f"> [!IMPORTANT]\n> 挙動を変えるファイルが {flagged} 件変更されています。マージ前に確認してください。", ""]
    else:
        lines += ["コア・配布物・指示文の変更はありません。", ""]
    lines += file_section("コア", "人間の承認が必要なパス(`auto_merge.protected_paths`)。", groups["core"])
    lines += file_section("配布物", "ハーネスが配布するパス(sync manifest の owned / workflows)。", groups["owned"])
    lines += file_section("指示文", "エージェントへの指示(CLAUDE.md / AGENTS.md / SKILL.md / `.claude/` 等)。", groups["instructions"])

    lines += [f"## マージされた PR({len(prs)} 件)", ""]
    by_issue: dict[str, list[dict]] = {}
    no_issue: list[dict] = []
    for pr in prs:
        if pr["issues"]:
            by_issue.setdefault(pr["issues"][0], []).append(pr)
        else:
            no_issue.append(pr)
    if not prs:
        lines += ["なし", ""]

    def issue_key(ident: str) -> tuple[str, int]:
        prefix, _, num = ident.partition("-")
        return prefix, int(num)

    for ident in sorted(by_issue, key=issue_key):
        lines += [f"### {ident}", ""] + [pr_line(p) for p in by_issue[ident]] + [""]
    if no_issue:
        lines += ["### Issue なし", ""] + [pr_line(p) for p in no_issue] + [""]
    if others:
        lines += [f"## PR を経ない変更({len(others)} 件)", ""]
        lines += [f"- {o['sha'][:7]} {o['subject']}" for o in others] + [""]

    stat = git("diff", "--stat=120", "--no-renames", f"{base}...{head}").rstrip("\n").splitlines()
    summary = stat[-1].strip() if stat else "変更なし"
    if len(stat) > MAX_STAT_LINES:
        stat = stat[:MAX_STAT_LINES - 1] + [" …(省略)", stat[-1]]
    lines += ["## 変更統計", "", f"<details><summary>{summary}</summary>", "", "```", *stat, "```", "", "</details>", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="リリースブランチ(例: origin/main)")
    ap.add_argument("--head", required=True, help="昇格元の既定ブランチ(例: origin/develop)")
    ap.add_argument("--config", default=str(CONFIG_REL))
    ap.add_argument("--manifest", default=None, help="sync manifest(既定: sync-manifest.json → .harness/sync-manifest.json)")
    ap.add_argument("--output", default=None, help="書き出すファイル(既定: 標準出力)")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    try:
        rules = load_rules(Path(a.config), Path(a.manifest) if a.manifest else None, a.head)
        text = render(a.base, a.head, rules)
    except (NotesError, OSError, ValueError) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
