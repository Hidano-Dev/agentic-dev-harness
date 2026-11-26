#!/usr/bin/env python3
"""配布元(agentic-dev-harness)の指定コミットを取り込み側リポジトリの作業ツリーへ適用する(配布元専用)。

取り込み側の Harness Sync(templates/consumer/harness-sync.yml)・配布元の Rollout・
同期 PR の検証(verify_harness_sync.py)が、clone した配布元のこのスクリプトを呼ぶ。

行うこと(sync-manifest.json に従う):

1. 書き込み先の事前検査: 途中のディレクトリを含めてシンボリックリンク・submodule(gitlink)が
   無いこと。リンク先への書き込みや、superproject に乗らない書き込みを防ぐ(旧 harness-sync.yml の
   検査を移植)。1 つでもあれば何も書かずに中止する
2. 配布物(owned)をコピー。ディレクトリは丸ごと置き換える(配布元で消したファイルは消える)。
   前回の `.harness/version` にあって今回の manifest から外れたパスは削除する
3. ワークフロー雛形(workflows)を配置先へコピー。配布元に無い雛形は warning でスキップ
4. ルート CLAUDE.md に import 行(claude_md_import)を保証(無ければ末尾に追記。他の内容は触らない)
5. config を旧パス `.kiro/orchestration/config.json` だけに置いているリポジトリは新パス
   `.claude/orchestration.json` へ移す(空になった `.kiro/orchestration/`・`.kiro/` は消す。`.kiro/` の
   他の中身には触れない)。ただし次のときは移さない(旧パスのままでも動く):
   - 配布物以外のファイル(SDD の dev-orchestrator など)が旧パスを参照している
   - 新パスが取り込み側の .gitignore で除外されている
   - 同期前の policy_gate.py が新パスを知らない(同期 PR の検証は base 側の policy_gate.py が行うので、
     新パスへの移動は「同期の範囲外」として人間の承認待ちになる)。この同期で新しい policy_gate.py が
     入るので、次の同期で移す
   両方のパスにあれば新パスを使い、warning を出す(どちらも消さない)。
   その config を JSON として検査し(不正なら中止、雛形のプレースホルダが残っていれば warning)、
   配布元の migrate_config.py を適用する
6. `.harness/version`(JSON: repo / ref / sha / owned)を書く
7. 書いたファイルが取り込み側の .gitignore で除外されていないことを確認する(除外されると
   コミットされず、同期したつもりで欠ける)

配布物は配布元の作業ツリーではなく git のオブジェクト(`git archive <sha>`)から読むので、
同じ (配布元 sha, 取り込み側のツリー) からは常に同じ結果になる(決定的)。
標準ライブラリのみ使用。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

# config の置き場所の解決は配布物の harness_config.py(同じ clone の中にある)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "linear-worker" / "scripts"))
from harness_config import CONFIG_REL, LEGACY_CONFIG_REL, choose_config_rel  # noqa: E402

DEFAULT_HARNESS_REPO = "Hidano-Dev/agentic-dev-harness"
DEFAULT_REF = "main"
MANIFEST_REL = "sync-manifest.json"
VERSION_REL = ".harness/version"
POLICY_GATE_REL = ".claude/skills/linear-worker/scripts/policy_gate.py"
MIGRATE_REL = ".claude/skills/linear-worker/scripts/migrate_config.py"
CLAUDE_MD_REL = "CLAUDE.md"


class SyncError(Exception):
    """同期を中止すべき状態(何も書かずに止めるのが原則)。"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def run_git(cwd: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=True,
            text=not binary,
            **({} if binary else {"encoding": "utf-8"}),
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else e.stderr
        raise SyncError(f"git {' '.join(args)} に失敗: {err.strip()}") from e


def is_git_repo(path: Path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def check_rel(rel: str, what: str) -> str:
    """manifest 由来の相対パスを検査して正規化する(ルート外・.git への書き込みを防ぐ)。"""
    if not isinstance(rel, str) or not rel.strip():
        raise SyncError(f"manifest の {what} に空のパスがある")
    p = PurePosixPath(rel.strip().rstrip("/"))
    if p.is_absolute() or "\\" in rel or any(part in ("", ".", "..", ".git") for part in p.parts):
        raise SyncError(f"manifest の {what} に不正なパスがある: {rel!r}")
    return str(p)


def load_manifest(root: Path) -> dict:
    f = root / MANIFEST_REL
    if not f.is_file():
        raise SyncError(f"配布元に {MANIFEST_REL} が無い")
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SyncError(f"{MANIFEST_REL} が JSON として不正: {e}") from e
    owned = m.get("owned")
    workflows = m.get("workflows") or {}
    imp = m.get("claude_md_import")
    if not isinstance(owned, list) or not isinstance(workflows, dict):
        raise SyncError(f"{MANIFEST_REL} の owned は配列、workflows はオブジェクトであること")
    if imp is not None and (not isinstance(imp, str) or not imp.strip() or "\n" in imp):
        raise SyncError(f"{MANIFEST_REL} の claude_md_import は 1 行の文字列であること")
    return {
        "owned": [check_rel(p, "owned") for p in owned],
        "workflows": {check_rel(k, "workflows(雛形)"): check_rel(v, "workflows(配置先)") for k, v in workflows.items()},
        "claude_md_import": imp.strip() if imp else None,
    }


def resolve_sha(harness: Path, sha: str | None) -> str:
    rev = (sha or "HEAD") + "^{commit}"
    return run_git(harness, "rev-parse", "--verify", "--quiet", rev).stdout.strip()


def export_tree(harness: Path, sha: str, dest: Path) -> set[str]:
    """配布元の sha のツリーを dest に展開する。戻り値: シンボリックリンク(展開しない)の相対パス。"""
    # 改行変換(core.autocrlf)は実行環境で変わるので無効にし、コミットされた内容そのものを使う
    data = run_git(harness, "-c", "core.autocrlf=false", "archive", "--format=tar", sha, binary=True).stdout
    links: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for m in tar.getmembers():
            name = m.name.rstrip("/")
            if not name or name == "pax_global_header":
                continue
            parts = PurePosixPath(name).parts
            if PurePosixPath(name).is_absolute() or ".." in parts:
                raise SyncError(f"配布元のアーカイブに不正なパスがある: {name!r}")
            out = dest.joinpath(*parts)
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                out.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(m)
                out.write_bytes(fobj.read() if fobj else b"")
                os.chmod(out, 0o755 if m.mode & 0o111 else 0o644)
            elif m.issym() or m.islnk():
                links.add(name)
    return links


def gitlinks(target: Path) -> set[str]:
    if not is_git_repo(target):
        return set()
    out = run_git(target, "ls-files", "--stage", "-z").stdout
    result = set()
    for entry in out.split("\0"):
        if entry.startswith("160000 "):
            result.add(entry.split("\t", 1)[1])
    return result


def problems_at(target: Path, rel: str, links: set[str]) -> list[str]:
    """target/rel への書き込みが通常のファイル・ディレクトリに閉じない理由を列挙する。"""
    bad: list[str] = []
    cur = target
    acc: list[str] = []
    for part in PurePosixPath(rel).parts:
        cur = cur / part
        acc.append(part)
        p = "/".join(acc)
        if cur.is_symlink():
            return [f"{p}(シンボリックリンク)"]
        if p in links:
            return [f"{p}(submodule)"]
    if cur.is_dir():
        for dirpath, dirnames, filenames in os.walk(cur, followlinks=False):
            for n in dirnames + filenames:
                full = Path(dirpath) / n
                if full.is_symlink():
                    bad.append(f"{full.relative_to(target).as_posix()}(シンボリックリンク)")
    prefix = rel + "/"
    bad += [f"{g}(submodule)" for g in sorted(links) if g.startswith(prefix)]
    return bad


def under(rel: str, roots: set[str]) -> bool:
    return any(rel == r or rel.startswith(r + "/") or r.startswith(rel + "/") for r in roots)


def remove_path(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        def retry(func, path, _exc):  # 読み取り専用ファイル(Windows)も消す
            os.chmod(path, stat.S_IWRITE)
            func(path)

        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=retry)
        else:
            shutil.rmtree(p, onerror=retry)
    elif p.exists() or p.is_symlink():
        p.unlink()


def copy_into(src: Path, dst: Path) -> None:
    """src を dst へ置き換えコピーする(ディレクトリは丸ごと)。"""
    if dst.exists() or dst.is_symlink():
        remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=False)
    else:
        shutil.copyfile(src, dst)
        shutil.copymode(src, dst)


def ensure_claude_md(target: Path, import_line: str) -> str:
    f = target / CLAUDE_MD_REL
    if not f.exists():
        f.write_bytes((import_line + "\n").encode("utf-8"))
        return "created"
    text = f.read_bytes().decode("utf-8")
    if any(line.rstrip("\r") == import_line for line in text.split("\n")):
        return "unchanged"
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    f.write_bytes((text + import_line + "\n").encode("utf-8"))
    return "appended"


def _mentions_legacy_config(target: Path, owned: list[str]) -> list[str]:
    """配布物以外で旧パスの config を参照しているファイル(`.claude/` `.kiro/` の下だけを見る)。

    SDD(unity-sdd-kit)の dev-orchestrator は旧パスの config の confirmation_channel を読む。
    これを移すと dev-orchestrator が設定を見失うので、参照が残っている間は移さない。
    """
    needle = LEGACY_CONFIG_REL.encode("utf-8")
    hits: list[str] = []
    for top in (".claude", ".kiro"):
        base = target / top
        if not base.is_dir() or base.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            for n in sorted(filenames):
                full = Path(dirpath) / n
                rel = full.relative_to(target).as_posix()
                if rel in (LEGACY_CONFIG_REL, CONFIG_REL) or under(rel, set(owned)) or full.is_symlink():
                    continue
                try:
                    if full.stat().st_size > 1_000_000:
                        continue
                    if needle in full.read_bytes():
                        hits.append(rel)
                except OSError:
                    continue
    return hits


def plan_config_move(target: Path, owned: list[str]) -> tuple[str, str]:
    """旧パスの config を新パスへ移すか。戻り値: (判定, 理由)。判定は move / keep / none。

    配布物をコピーする前(同期前の policy_gate.py が残っているうち)に呼ぶ。
    """
    new, old = (target / CONFIG_REL), (target / LEGACY_CONFIG_REL)
    if not old.is_file() or new.exists() or new.is_symlink():
        return "none", ""
    refs = _mentions_legacy_config(target, owned)
    if refs:
        return "keep", (f"{', '.join(refs)} が旧パス {LEGACY_CONFIG_REL} を参照しているため、config を移さず"
                        f"旧パスのまま使います(参照を新パス {CONFIG_REL} に直すと次回の同期で移します)")
    # 新パスが .gitignore で除外されていると、移した config がコミットされず旧パスの削除だけが残る
    if is_git_repo(target) and subprocess.run(
            ["git", "-C", str(target), "check-ignore", "-q", "--no-index", CONFIG_REL],
            capture_output=True).returncode == 0:
        return "keep", (f"新パス {CONFIG_REL} が .gitignore で除外されているため、config を移さず旧パス "
                        f"{LEGACY_CONFIG_REL} のまま使います(除外を外すと次回の同期で移します)")
    gate = target / POLICY_GATE_REL
    if gate.is_file() and CONFIG_REL.encode("utf-8") not in gate.read_bytes():
        return "keep", (f"同期前の policy_gate.py が新パス {CONFIG_REL} を知らないため、config の移動は"
                        f"次回の同期で行います(この同期 PR を policy-gate が自動で通せるようにするため)")
    return "move", ""


def move_legacy_config(target: Path) -> None:
    """旧パスの config を新パスへ移し、空になった `.kiro/orchestration/`・`.kiro/` を消す。"""
    src, dst = target / LEGACY_CONFIG_REL, target / CONFIG_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    for d in (src.parent, src.parent.parent):  # .kiro/orchestration/ → .kiro/
        try:
            d.rmdir()  # 空のときだけ消える(他の中身があれば OSError で残す)
        except OSError:
            break
    log(f"::notice::config を {LEGACY_CONFIG_REL} から {CONFIG_REL} へ移しました")


def check_and_migrate_config(target: Path, export: Path) -> str:
    rel = choose_config_rel(lambda p: (target / p).is_file(), log)
    if rel is None:
        log(f"::notice::{CONFIG_REL}(旧 {LEGACY_CONFIG_REL})が無いため linear-worker は起動しません(Git 運用ルールのみ)")
        return "absent"
    f = target / rel
    text = f.read_text(encoding="utf-8")
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        raise SyncError(f"{rel} が JSON として不正: {e}") from e
    if re.search(r'"<[^"]*>"', text):
        log(f"::warning::{rel} に雛形のプレースホルダ(<...>)が残っています")
    migrate = export / MIGRATE_REL
    if not migrate.is_file():
        return "checked"
    r = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(migrate), str(f)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for stream in (r.stdout, r.stderr):
        if stream and stream.strip():
            log(stream.rstrip())
    if r.returncode != 0:
        raise SyncError(f"migrate_config.py が失敗した(終了コード {r.returncode})")
    return "migrated" if f.read_text(encoding="utf-8") != text else "checked"


def read_version(target: Path) -> dict | None:
    f = target / VERSION_REL
    if not f.is_file():
        return None
    try:
        v = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def version_text(repo: str, ref: str, sha: str, owned: list[str]) -> str:
    return json.dumps({"owned": owned, "ref": ref, "repo": repo, "sha": sha}, indent=2, sort_keys=True) + "\n"


def apply_sync(harness: Path, target: Path, sha: str | None = None,
               harness_repo: str = DEFAULT_HARNESS_REPO, ref: str = DEFAULT_REF) -> dict:
    harness = Path(harness).resolve()
    target = Path(target).resolve()
    if not target.is_dir():
        raise SyncError(f"対象ディレクトリが無い: {target}")
    full_sha = resolve_sha(harness, sha)
    with tempfile.TemporaryDirectory(prefix="harness-export-") as tmp:
        export = Path(tmp)
        src_links = export_tree(harness, full_sha, export)
        manifest = load_manifest(export)
        owned = manifest["owned"]
        workflows = manifest["workflows"]
        import_line = manifest["claude_md_import"]

        prev = read_version(target)
        stale: list[str] = []
        if prev and prev.get("repo") == harness_repo and isinstance(prev.get("owned"), list):
            for p in prev["owned"]:
                try:
                    rel = check_rel(p, "前回の owned")
                except SyncError:
                    continue
                if rel not in owned and not under(rel, set(owned)):
                    stale.append(rel)

        # 1. 事前検査(全部見てから書く: 途中で失敗して半端な状態を残さない)
        dests = list(owned) + list(workflows.values()) + stale + [VERSION_REL, CONFIG_REL, LEGACY_CONFIG_REL]
        if import_line:
            dests.append(CLAUDE_MD_REL)
        links = gitlinks(target)
        bad: list[str] = []
        for rel in dests:
            bad += problems_at(target, rel, links)
        if bad:
            raise SyncError(
                "同期先にシンボリックリンクまたは submodule があるため同期を中止した"
                "(通常のファイル・ディレクトリにしてから再実行すること): " + ", ".join(dict.fromkeys(bad))
            )
        for rel in owned + list(workflows):
            hits = [l for l in sorted(src_links) if l == rel or l.startswith(rel + "/")]
            if hits:
                raise SyncError(f"配布元の {rel} にシンボリックリンクがある(配布できない): {', '.join(hits)}")

        # config の移動は、同期前の policy_gate.py を見て決めるので配布物のコピーより前に判定する
        move, move_reason = plan_config_move(target, owned)

        result: dict = {"sha": full_sha, "ref": ref, "repo": harness_repo,
                        "owned": [], "workflows": [], "removed": []}
        # 2. 配布物
        for rel in stale:
            if (target / rel).exists():
                remove_path(target / rel)
                result["removed"].append(rel)
                log(f"::notice::{rel} は配布物から外れたため削除しました")
        for rel in owned:
            src = export / rel
            if not src.exists():
                log(f"::warning::{rel} は配布元に存在しないためスキップしました")
                continue
            copy_into(src, target / rel)
            result["owned"].append(rel)
        # 3. ワークフロー
        for tpl, dest in workflows.items():
            src = export / tpl
            if not src.is_file():
                log(f"::warning::{tpl} は配布元に存在しないためスキップしました")
                continue
            copy_into(src, target / dest)
            result["workflows"].append(dest)
        # 4. CLAUDE.md
        result["claude_md"] = ensure_claude_md(target, import_line) if import_line else "skipped"
        # 5. config(旧パスだけなら新パスへ移してから検査・移行する)
        if move == "move":
            move_legacy_config(target)
            result["config_moved"] = f"{LEGACY_CONFIG_REL} -> {CONFIG_REL}"
        elif move_reason:
            log(f"::notice::{move_reason}")
        result["config"] = check_and_migrate_config(target, export)
        # 6. version
        vf = target / VERSION_REL
        vf.parent.mkdir(parents=True, exist_ok=True)
        vf.write_bytes(version_text(harness_repo, ref, full_sha, owned).encode("utf-8"))

    # 7. .gitignore で落ちるファイルが無いこと
    if is_git_repo(target):
        roots = result["owned"] + result["workflows"] + [VERSION_REL]
        if import_line:
            roots.append(CLAUDE_MD_REL)
        ignored = run_git(target, "ls-files", "--others", "--ignored", "--exclude-standard", "--", *roots).stdout
        if ignored.strip():
            raise SyncError(
                "取り込み側の .gitignore が配布ファイルを除外している(コミットされない): "
                + " ".join(ignored.split()) + " — 除外を外してから再実行すること"
            )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness", required=True, help="配布元の clone(git リポジトリ)")
    ap.add_argument("--target", required=True, help="取り込み側リポジトリの作業ツリー")
    ap.add_argument("--sha", help="適用する配布元のコミット(既定: --harness の HEAD)")
    ap.add_argument("--harness-repo", default=os.environ.get("HARNESS_REPO") or DEFAULT_HARNESS_REPO,
                    help="`.harness/version` に記録する配布元(owner/name)")
    ap.add_argument("--ref", default=os.environ.get("HARNESS_REF") or DEFAULT_REF,
                    help="`.harness/version` に記録する配布元のブランチ")
    ap.add_argument("--result", help="結果 JSON の書き出し先")
    a = ap.parse_args(argv)
    try:
        result = apply_sync(Path(a.harness), Path(a.target), a.sha, a.harness_repo, a.ref)
    except SyncError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if a.result:
        Path(a.result).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
