"""resolve_conflicts.py のテスト。一時ディレクトリに実リポジトリを作り、実際に git merge でコンフリクトさせる。"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".claude/skills/linear-worker/scripts/resolve_conflicts.py"


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=check)


class ConflictRepo:
    """main に base を置き、ours ブランチ(main)と theirs ブランチで別々に変更してマージする。"""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="resolve-conflicts-"))
        git(self.dir, "init", "-q", "-b", "main")
        for k, v in (("core.autocrlf", "false"), ("core.safecrlf", "false"), ("user.name", "test"),
                     ("user.email", "test@example.com"), ("commit.gpgsign", "false"), ("merge.conflictstyle", "merge")):
            git(self.dir, "config", k, v)
        (self.dir / ".gitkeep").write_bytes(b"")
        self.commit("init")

    def write(self, files: dict[str, bytes | None]) -> None:
        for name, content in files.items():
            p = self.dir / name
            if content is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)

    def commit(self, msg: str) -> None:
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", msg)

    def setup(self, base: dict, ours: dict, theirs: dict) -> None:
        if base:
            self.write(base)
            self.commit("base")
        git(self.dir, "checkout", "-q", "-b", "theirs")
        self.write(theirs)
        self.commit("theirs")
        git(self.dir, "checkout", "-q", "main")
        self.write(ours)
        self.commit("ours")
        r = git(self.dir, "merge", "--no-ff", "--no-edit", "theirs", check=False)
        assert r.returncode != 0, "コンフリクトが起きなかった"

    def run(self, *extra: str) -> tuple[int, dict]:
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), *extra], cwd=self.dir, capture_output=True)
        return r.returncode, json.loads(r.stdout.decode("utf-8"))

    def read(self, name: str) -> bytes:
        return (self.dir / name).read_bytes()

    def unmerged(self) -> set[str]:
        out = git(self.dir, "ls-files", "-u").stdout.decode()
        return {line.split("\t", 1)[1] for line in out.splitlines() if line}

    def cleanup(self) -> None:
        def onerror(func, path, _exc):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(self.dir, onerror=onerror)


class ResolveConflictsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = ConflictRepo()

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_append_to_same_heading_keeps_both(self):
        base = b"# Notes\n\n## A\n\n- one\n\n## B\n\n- x\n"
        self.repo.setup(
            {"doc.md": base},
            {"doc.md": base.replace(b"- one\n", b"- one\n- ours item\n")},
            {"doc.md": base.replace(b"- one\n", b"- one\n- theirs item\n")},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["resolved"], ["doc.md"])
        self.assertEqual(out["unresolved"], [])
        self.assertEqual(
            self.repo.read("doc.md"),
            b"# Notes\n\n## A\n\n- one\n- ours item\n- theirs item\n\n## B\n\n- x\n",
        )
        self.assertEqual(self.repo.unmerged(), set())

    def test_identical_append_kept_once(self):
        base = b"# T\n\n- a\n"
        self.repo.setup(
            {"doc.md": base},
            {"doc.md": base + b"- same\n- ours\n"},
            {"doc.md": base + b"- same\n- theirs\n"},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.repo.read("doc.md"), b"# T\n\n- a\n- same\n- ours\n- theirs\n")

    def test_table_row_rewritten_on_both_sides_is_untouched(self):
        base = b"| k | v |\n|---|---|\n| a | 1 |\n| b | 2 |\n"
        self.repo.setup(
            {"table.md": base},
            {"table.md": base.replace(b"| a | 1 |", b"| a | 10 |")},
            {"table.md": base.replace(b"| a | 1 |", b"| a | 100 |")},
        )
        before = self.repo.read("table.md")
        rc, out = self.repo.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["resolved"], [])
        self.assertEqual(out["unresolved"], ["table.md"])
        self.assertEqual(self.repo.read("table.md"), before)
        self.assertIn(b"<<<<<<<", before)
        self.assertEqual(self.repo.unmerged(), {"table.md"})

    def test_mixed_only_resolvable_file_is_added(self):
        doc = b"# D\n\n- a\n"
        table = b"| a | 1 |\n"
        self.repo.setup(
            {"doc.md": doc, "table.md": table},
            {"doc.md": doc + b"- ours\n", "table.md": b"| a | 2 |\n"},
            {"doc.md": doc + b"- theirs\n", "table.md": b"| a | 3 |\n"},
        )
        table_before = self.repo.read("table.md")
        rc, out = self.repo.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["resolved"], ["doc.md"])
        self.assertEqual(out["unresolved"], ["table.md"])
        self.assertEqual(self.repo.unmerged(), {"table.md"})
        self.assertEqual(self.repo.read("doc.md"), b"# D\n\n- a\n- ours\n- theirs\n")
        self.assertEqual(self.repo.read("table.md"), table_before)

    def test_add_add_new_file(self):
        self.repo.setup(
            {},
            {"new.md": b"# New\n\n- ours\n"},
            {"new.md": b"# New\n\n- theirs\n"},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["resolved"], ["new.md"])
        self.assertEqual(self.repo.read("new.md"), b"# New\n\n- ours\n- theirs\n")
        self.assertEqual(self.repo.unmerged(), set())

    def test_code_file_is_not_touched(self):
        base = b"x = 1\n"
        self.repo.setup(
            {"mod.py": base},
            {"mod.py": base + b"y = 2\n"},
            {"mod.py": base + b"z = 3\n"},
        )
        before = self.repo.read("mod.py")
        rc, out = self.repo.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["unresolved"], ["mod.py"])
        self.assertEqual(out["reasons"]["mod.py"], "対象外の拡張子")
        self.assertEqual(self.repo.read("mod.py"), before)
        self.assertEqual(self.repo.unmerged(), {"mod.py"})

    def test_crlf_file(self):
        base = b"# C\r\n\r\n- a\r\n\r\n## Next\r\n"
        self.repo.setup(
            {"crlf.md": base},
            {"crlf.md": base.replace(b"- a\r\n", b"- a\r\n- ours\r\n")},
            {"crlf.md": base.replace(b"- a\r\n", b"- a\r\n- theirs\r\n")},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.repo.read("crlf.md"), b"# C\r\n\r\n- a\r\n- ours\r\n- theirs\r\n\r\n## Next\r\n")

    def test_no_trailing_newline(self):
        base = b"# E\n\n- a"
        self.repo.setup(
            {"eof.md": base},
            {"eof.md": b"# E\n\n- a\n- ours"},
            {"eof.md": b"# E\n\n- a\n- theirs"},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 0, out)
        content = self.repo.read("eof.md")
        self.assertNotIn(b"<<<", content)
        lines = content.decode().splitlines()
        self.assertEqual(lines, ["# E", "", "- a", "- ours", "- theirs"])

    def test_config_extensions_override(self):
        base = b"x = 1\n"
        self.repo.setup(
            {"mod.py": base},
            {"mod.py": base + b"y = 2\n"},
            {"mod.py": base + b"z = 3\n"},
        )
        cfg = self.repo.dir.parent / f"{self.repo.dir.name}-config.json"
        cfg.write_text(json.dumps({"conflicts": {"auto_resolve_extensions": [".py"]}}), encoding="utf-8")
        try:
            rc, out = self.repo.run("--config", str(cfg))
        finally:
            cfg.unlink()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.repo.read("mod.py"), b"x = 1\ny = 2\nz = 3\n")

    def test_txt_not_resolved_by_default(self):
        base = b"a\n"
        self.repo.setup({"notes.txt": base}, {"notes.txt": base + b"o\n"}, {"notes.txt": base + b"t\n"})
        rc, out = self.repo.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["reasons"]["notes.txt"], "対象外の拡張子")

    def test_non_doc_basenames_never_resolved(self):
        names = ["requirements-dev.txt", "sub/CMakeLists.txt", "CLAUDE.md", "docs/AGENTS.md", "x/SKILL.md"]
        self.repo.setup(
            {n: b"a\n" for n in names},
            {n: b"a\no\n" for n in names},
            {n: b"a\nt\n" for n in names},
        )
        cfg = self.repo.dir.parent / f"{self.repo.dir.name}-config.json"
        cfg.write_text(json.dumps({"conflicts": {"auto_resolve_extensions": [".md", ".txt"]}}), encoding="utf-8")
        try:
            rc, out = self.repo.run("--config", str(cfg))
        finally:
            cfg.unlink()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["resolved"], [])
        self.assertEqual(sorted(out["unresolved"]), sorted(names))
        self.assertEqual(self.repo.unmerged(), set(names))

    def test_non_doc_basename_matches_classify_change(self):
        scripts = SCRIPT.parent
        mods = {}
        for name in ("resolve_conflicts", "classify_change"):
            spec = importlib.util.spec_from_file_location(f"_t_{name}", scripts / f"{name}.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod  # dataclass が sys.modules を参照するため
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.modules.pop(spec.name, None)
            mods[name] = mod
        a = mods["resolve_conflicts"].NON_DOC_BASENAME
        b = mods["classify_change"].NON_DOC_BASENAME
        self.assertEqual((a.pattern, a.flags), (b.pattern, b.flags))
        self.assertEqual(mods["resolve_conflicts"].NON_DOC_DIRS, mods["classify_change"].NON_DOC_DIRS)
        self.assertEqual(mods["resolve_conflicts"].DEFAULT_TEXT_EXCLUDE_PATHS,
                         mods["classify_change"].DEFAULT_TEXT_EXCLUDE_PATHS)

    def test_agent_dirs_exclude_paths_and_owned_never_resolved(self):
        refused = {
            ".claude/rules/git-workflow.md": "依存・ビルド定義またはエージェントへの指示",
            "sub/.github/doc.md": "依存・ビルド定義またはエージェントへの指示",
            ".cursor/rules/x.md": "依存・ビルド定義またはエージェントへの指示",
            "Assets/readme.md": "除外パス(fast_track.text_exclude_paths)",
            "docs/owned/guide.md": "配布物(.harness/version の owned)",
            "owned-file.md": "配布物(.harness/version の owned)",
        }
        version = json.dumps({"owned": ["docs/owned", "owned-file.md"], "repo": "x/y", "ref": "main", "sha": "0"})
        base = {n: b"a\n" for n in [*refused, "docs/ok.md"]}
        base[".harness/version"] = version.encode()
        self.repo.setup(
            base,
            {n: b"a\no\n" for n in [*refused, "docs/ok.md"]},
            {n: b"a\nt\n" for n in [*refused, "docs/ok.md"]},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["resolved"], ["docs/ok.md"])
        self.assertEqual(out["reasons"], refused)
        self.assertEqual(self.repo.unmerged(), set(refused))

    def test_dry_run_does_not_write(self):
        base = b"# D\n"
        self.repo.setup({"doc.md": base}, {"doc.md": base + b"- o\n"}, {"doc.md": base + b"- t\n"})
        before = self.repo.read("doc.md")
        rc, out = self.repo.run("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["resolved"], ["doc.md"])
        self.assertEqual(self.repo.read("doc.md"), before)
        self.assertEqual(self.repo.unmerged(), {"doc.md"})

    def test_modify_delete_is_unresolved(self):
        self.repo.setup(
            {"gone.md": b"# G\n- a\n"},
            {"gone.md": b"# G\n- a\n- ours\n"},
            {"gone.md": None},
        )
        rc, out = self.repo.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["unresolved"], ["gone.md"])


if __name__ == "__main__":
    unittest.main()
