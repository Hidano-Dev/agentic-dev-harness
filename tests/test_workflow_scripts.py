"""ワークフローの run: スクリプトが YAML として読んだ結果でも bash の構文として正しいことのテスト。

素のスカラー(`run: echo "PR #$N"`)では " #" 以降が YAML のコメントになり、スクリプトが途中で
切れる(2026-11-03 に policy-gate-recheck で発生)。ソースの見た目ではなく、パース後の文字列を検査する。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted([*ROOT.glob(".github/workflows/*.yml"), *ROOT.glob("templates/consumer/*.yml")])

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


@unittest.skipIf(yaml is None, "PyYAML が無い")
@unittest.skipIf(shutil.which("bash") is None, "bash が無い")
class RunScriptSyntaxTest(unittest.TestCase):
    def test_bash_syntax(self):
        for f in FILES:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                for i, step in enumerate(job.get("steps") or []):
                    script = step.get("run")
                    if not script or step.get("shell") not in (None, "bash"):
                        continue
                    with self.subTest(file=f.name, job=job_name, step=step.get("name") or i):
                        proc = subprocess.run(["bash", "-n"], input=script.encode("utf-8"), capture_output=True)
                        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
