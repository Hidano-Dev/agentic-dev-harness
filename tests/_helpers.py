"""テスト共通: 一時ディレクトリに実 git リポジトリ(配布元・取り込み側)を作る。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPTS = ROOT / "scripts" / "sync"
WORKER_SCRIPTS = ROOT / ".claude" / "skills" / "linear-worker" / "scripts"
for p in (SYNC_SCRIPTS, WORKER_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

HARNESS_REPO = "Hidano-Dev/agentic-dev-harness"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       encoding="utf-8", env=GIT_ENV)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    for k, v in (("core.autocrlf", "false"), ("core.eol", "lf"), ("commit.gpgsign", "false"),
                 ("user.name", "test"), ("user.email", "test@example.com")):
        git(path, "config", k, v)
    return path


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def commit_all(repo: Path, msg: str = "commit") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


HARNESS_FILES = [
    "sync-manifest.json",
    ".claude/skills/linear-worker",
    ".claude/rules/git-workflow.md",
    ".harness/invariants.json",
    "templates/consumer",
    "scripts/sync",
]


def make_harness(path: Path) -> tuple[Path, str]:
    """本リポジトリの配布物をコピーした配布元リポジトリを作る。戻り値: (パス, main の sha)。"""
    init_repo(path)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in HARNESS_FILES:
        src = ROOT / rel
        dst = path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=ignore)
        else:
            shutil.copyfile(src, dst)
    return path, commit_all(path, "harness")


def minimal_config() -> dict:
    tpl = json.loads((WORKER_SCRIPTS.parent / "templates" / "orchestration-config.json").read_text(encoding="utf-8"))
    return {"github": {"repo": "owner/consumer", "default_branch": "develop"},
            "auto_merge": {"enabled": False, "protected_paths": [".claude/", ".github/"]},
            "applied_migrations": [m for m in tpl.get("applied_migrations", []) if "auto-merge-enabled" in m]}


def make_consumer(path: Path, config: dict | None = None) -> tuple[Path, str]:
    init_repo(path, "develop")
    write(path / "README.md", "# consumer\n")
    write(path / "CLAUDE.md", "# project notes\n")
    write(path / "src" / "app.py", "print('hi')\n")
    if config is not None:
        write(path / ".kiro/orchestration/config.json", json.dumps(config, indent=2) + "\n")
    return path, commit_all(path, "initial")
