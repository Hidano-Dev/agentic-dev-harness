"""release_notes.py のテスト。一時ディレクトリに main / develop を持つ実リポジトリを作って検証する。"""
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

SCRIPT = Path(__file__).resolve().parents[1] / ".claude/skills/linear-worker/scripts/release_notes.py"

spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout.decode()


class Repo:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="release-notes-"))
        git(self.dir, "init", "-q", "-b", "main")
        for k, v in (("core.autocrlf", "false"), ("user.name", "test"), ("user.email", "test@example.com"),
                     ("commit.gpgsign", "false")):
            git(self.dir, "config", k, v)
        self.write({"README.md": "# r\n"})
        self.commit("init")
        git(self.dir, "checkout", "-q", "-b", "develop")

    def write(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            p = self.dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, msg: str) -> None:
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", msg)

    def merge_pr(self, number: int, branch: str, title: str, files: dict[str, str], as_branch: str | None = None) -> None:
        git(self.dir, "checkout", "-q", "-b", branch)
        self.write(files)
        self.commit(f"work on {branch}")
        git(self.dir, "checkout", "-q", "develop")
        git(self.dir, "merge", "-q", "--no-ff", branch,
            "-m", f"Merge pull request #{number} from o/{as_branch or branch}", "-m", title)

    def run(self, *extra: str) -> tuple[int, str]:
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "--base", "main", "--head", "develop", *extra],
                           cwd=self.dir, capture_output=True)
        return r.returncode, r.stdout.decode("utf-8") + r.stderr.decode("utf-8")

    def cleanup(self) -> None:
        def onerror(func, path, _exc):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(self.dir, onerror=onerror)


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    ends = [i for i in (rest.find("\n### "), rest.find("\n## ")) if i >= 0]
    return rest[: min(ends)] if ends else rest


class ReleaseNotesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo()

    def tearDown(self) -> None:
        self.repo.cleanup()

    def build_history(self) -> None:
        r = self.repo
        r.merge_pr(10, "feature/hid-12-add-thing", "Add thing", {"docs/thing.md": "x\n"})
        r.merge_pr(11, "fix/hid-12-fix-thing", "Fix thing", {"src/a.py": "a = 1\n"})
        r.merge_pr(12, "claude/abc", "HID-30 Update rules", {"CLAUDE.md": "@x\n", ".claude/rules/r.md": "r\n"})
        r.merge_pr(13, "chore/ci-tweak", "Tweak CI (UTF-8 logs)", {".github/workflows/ci.yml": "on: push\n"})
        r.merge_pr(14, "feature/hid-40-worker", "Update worker",
                   {".claude/skills/linear-worker/scripts/x.py": "x\n", "templates/consumer/promote.yml": "x\n"})
        r.merge_pr(15, "back-merge/main-abc1234", "Back-merge main", {"hotfix.md": "h\n"})
        r.merge_pr(16, "hotfix-src", "Back-merge main directly", {"hotfix2.md": "h\n"}, as_branch="main")
        r.write({"direct.txt": "d\n"})
        r.commit("Direct commit on develop")
        r.write({"squashed.md": "s\n"})
        r.commit("Squashed feature HID-50 (#17)")

    def test_full_notes(self):
        self.build_history()
        self.repo.write({".kiro/orchestration/config.json": json.dumps({"github": {"release_branch": "main"}})})
        self.repo.commit("Add config (#18)")
        rc, out = self.repo.run()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith(release_notes.MARKER))
        # 要確認の変更が PR 一覧より前にある
        self.assertLess(out.index("## 要確認の変更"), out.index("## マージされた PR"))
        core = section(out, "### コア")
        self.assertIn("`.github/workflows/ci.yml`", core)
        self.assertIn("`.kiro/orchestration/config.json`", core)
        owned = section(out, "### 配布物")
        self.assertIn("`.claude/skills/linear-worker/scripts/x.py`", owned)
        self.assertIn("`templates/consumer/promote.yml`", owned)  # manifest が無いときの既定値に含まれる
        instr = section(out, "### 指示文")
        self.assertIn("`CLAUDE.md`", instr)
        self.assertIn("`.claude/rules/r.md`", instr)
        self.assertNotIn("src/a.py", core + owned + instr)

        hid12 = section(out, "### HID-12\n")
        self.assertIn("#10 Add thing", hid12)
        self.assertIn("#11 Fix thing", hid12)
        self.assertIn("#12 HID-30 Update rules", section(out, "### HID-30\n"))
        self.assertIn("#17 Squashed feature HID-50", section(out, "### HID-50\n"))
        no_issue = section(out, "### Issue なし")
        self.assertIn("#13 Tweak CI (UTF-8 logs)", no_issue)
        self.assertNotIn("### UTF-8", out)
        # back-merge は載せない
        self.assertNotIn("#15", out)
        self.assertNotIn("#16", out)
        self.assertIn("Direct commit on develop", section(out, "## PR を経ない変更"))
        self.assertIn("## 変更統計", out)
        self.assertIn("files changed", out)

    def test_manifest_and_config_override(self):
        self.build_history()
        self.repo.write({
            "sync-manifest.json": json.dumps({"owned": [".claude/skills/linear-worker"],
                                              "workflows": {"templates/consumer/promote.yml": ".github/workflows/promote.yml"}}),
        })
        self.repo.commit("Add manifest")
        cfg = self.repo.dir.parent / f"{self.repo.dir.name}-cfg.json"
        cfg.write_text(json.dumps({"auto_merge": {"protected_paths": ["src/"]}}), encoding="utf-8")
        try:
            rc, out = self.repo.run("--config", str(cfg))
        finally:
            cfg.unlink()
        self.assertEqual(rc, 0, out)
        self.assertIn("`templates/consumer/promote.yml`", section(out, "### 配布物"))
        core = section(out, "### コア")
        self.assertIn("`src/a.py`", core)
        self.assertNotIn(".github/workflows/ci.yml", core)
        # release_branch 未設定なので head=main の back-merge は載る。back-merge/ 接頭辞は常に除く
        self.assertIn("#16", out)
        self.assertNotIn("#15", out)

    def test_no_flagged_changes_and_output_file(self):
        self.repo.merge_pr(1, "feature/hid-1-doc", "Doc", {"docs/a.md": "a\n"})
        out_file = self.repo.dir.parent / f"{self.repo.dir.name}-notes.md"
        try:
            rc, out = self.repo.run("--output", str(out_file))
            text = out_file.read_text(encoding="utf-8")
        finally:
            out_file.unlink(missing_ok=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("コア・配布物・指示文の変更はありません。", text)
        self.assertIn("#1 Doc", section(text, "### HID-1\n"))

    def test_bad_ref_is_error(self):
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "--base", "nope", "--head", "develop"],
                           cwd=self.repo.dir, capture_output=True)
        self.assertEqual(r.returncode, 2)


class UnitTest(unittest.TestCase):
    def test_issue_ids(self):
        f = release_notes.issue_ids
        self.assertEqual(f("Add x", "feature/hid-13-mov-prores4444"), ["HID-13"])
        self.assertEqual(f("[ABC-2] Fix", "claude/xyz"), ["ABC-2"])
        self.assertEqual(f("Use SHA-256 and UTF-8", "fix/sha-256-thing"), [])
        self.assertEqual(f("HID-1 and HID-2", "chore/hid-1-x"), ["HID-1", "HID-2"])
        self.assertEqual(f("Plain", None), [])

    def test_path_matches(self):
        m = release_notes.path_matches
        self.assertTrue(m(".claude/skills/linear-worker/a", [".claude/skills/linear-worker"]))
        self.assertFalse(m(".claude/skills/linear-worker2/a", [".claude/skills/linear-worker"]))
        self.assertTrue(m(".github/x", [".github/"]))
        self.assertTrue(m("CLAUDE.md", ["CLAUDE.md"]))
        self.assertFalse(m("docs/CLAUDE.md", ["CLAUDE.md"]))


if __name__ == "__main__":
    unittest.main()
