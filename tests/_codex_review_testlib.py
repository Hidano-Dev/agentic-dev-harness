"""test_review_plan / test_post_review / test_review_status の共通部品。"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude" / "skills" / "linear-worker" / "scripts"
TEMPLATES = ROOT / ".claude" / "skills" / "linear-worker" / "templates"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _rm_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class GitRepoTestCase(unittest.TestCase):
    """一時ディレクトリに実リポジトリを作り、カレントディレクトリをそこへ移す。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self.dir = Path(tempfile.mkdtemp(prefix="codex-review-test-"))
        os.chdir(self.dir)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.dir, onerror=_rm_readonly)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                              encoding="utf-8").stdout.strip()

    def write(self, rel: str, text: str) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, files: dict[str, str], msg: str = "c") -> str:
        for rel, text in files.items():
            self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")
