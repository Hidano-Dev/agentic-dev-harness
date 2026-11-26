"""config の置き場所の解決(harness_config.py)と、その写し・ワークフローとの一致のテスト。"""
from __future__ import annotations

import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _helpers import ROOT, WORKER_SCRIPTS

import harness_config as hc
import policy_gate as pg
import resolve_conflicts as rc

NEW = ".claude/orchestration.json"
LEGACY = ".kiro/orchestration/config.json"
CASES = {  # (新パスあり, 旧パスあり) -> 使うパス
    (True, True): NEW,
    (True, False): NEW,
    (False, True): LEGACY,
    (False, False): None,
}


def write(root: Path, rel: str, text: str = "{}\n") -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


class ChooseConfigRelTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(hc.CONFIG_REL, NEW)
        self.assertEqual(hc.LEGACY_CONFIG_REL, LEGACY)
        self.assertEqual(hc.CONFIG_CANDIDATES, (NEW, LEGACY))

    def test_priority(self):
        for (new, old), want in CASES.items():
            with self.subTest(new=new, old=old):
                present = {NEW: new, LEGACY: old}
                warnings: list[str] = []
                self.assertEqual(hc.choose_config_rel(present.__getitem__, warnings.append), want)
                self.assertEqual(len(warnings), 1 if (new and old) else 0)

    def test_warn_is_optional(self):
        self.assertEqual(hc.choose_config_rel(lambda p: True), NEW)

    def test_resolve_config_path(self):
        for (new, old), want in CASES.items():
            with self.subTest(new=new, old=old), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                if new:
                    write(root, NEW)
                if old:
                    write(root, LEGACY)
                with redirect_stderr(io.StringIO()) as err:
                    got = hc.resolve_config_path(root)
                # どちらも無ければ新パス(存在しないパス = 既定値で動く)
                self.assertEqual(got, root / (want or NEW))
                self.assertEqual("::warning::" in err.getvalue(), new and old)

    def test_resolve_config_path_can_be_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, NEW)
            write(root, LEGACY)
            with redirect_stderr(io.StringIO()) as err:
                hc.resolve_config_path(root, warn=False)
            self.assertEqual(err.getvalue(), "")


class CopiesMatchTest(unittest.TestCase):
    """単独で取り出して実行される policy_gate.py / resolve_conflicts.py は harness_config を写している。"""

    def test_constants_match(self):
        for mod in (pg, rc):
            with self.subTest(mod=mod.__name__):
                self.assertEqual(mod.CONFIG_REL, hc.CONFIG_REL)
                self.assertEqual(mod.LEGACY_CONFIG_REL, hc.LEGACY_CONFIG_REL)

    def test_function_body_matches(self):
        def body(fn):
            src = inspect.getsource(fn)
            src = re.sub(r'(?s)^(def [^\n]*\n)\s*""".*?"""\n', r"\1", src)  # docstring は違ってよい
            return src

        for mod in (pg, rc):
            with self.subTest(mod=mod.__name__):
                self.assertEqual(body(mod.choose_config_rel), body(hc.choose_config_rel))

    def test_behaviour_matches(self):
        for mod in (pg, rc):
            for (new, old), want in CASES.items():
                with self.subTest(mod=mod.__name__, new=new, old=old):
                    present = {NEW: new, LEGACY: old}
                    self.assertEqual(mod.choose_config_rel(present.__getitem__), want)


class NoHardcodedLegacyPathTest(unittest.TestCase):
    """設定を読むスクリプトは旧パスを直接書かず、harness_config(またはその写し)を使う。"""

    ALLOWED = {
        "harness_config.py",  # 定義そのもの
        "policy_gate.py", "resolve_conflicts.py",  # 写し(CopiesMatchTest が一致を検査する)
    }
    # 文字列リテラルとしての旧パス(説明文の `...` は対象外)
    LITERAL = re.compile(r"""["']\.kiro/orchestration/config\.json["']""")

    def test_scripts(self):
        roots = [WORKER_SCRIPTS, ROOT / "scripts" / "sync", ROOT / "scripts" / "onboard"]
        offenders = []
        for d in roots:
            for f in sorted(d.glob("*.py")):
                if f.name in self.ALLOWED:
                    continue
                if self.LITERAL.search(f.read_text(encoding="utf-8")):
                    offenders.append(f.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])


class WorkflowsReadBothPathsTest(unittest.TestCase):
    """設定を読むワークフローは新パス → 旧パスの順で探す。"""

    FILES = ["codex-review.yml", "conflict-sweeper.yml", "promote.yml"]

    def test_order(self):
        for d in (ROOT / "templates" / "consumer", ROOT / ".github" / "workflows"):
            for name in self.FILES:
                with self.subTest(path=f"{d.name}/{name}"):
                    text = (d / name).read_text(encoding="utf-8")
                    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
                    code = "\n".join(lines)
                    self.assertIn(NEW, code)
                    self.assertIn(LEGACY, code)
                    self.assertLess(code.index(NEW), code.index(LEGACY))

    def test_promote_snippet_resolves(self):
        """promote.yml の設定読み出し(埋め込みの Python)を実際に動かして優先順位を確かめる。"""
        text = (ROOT / "templates" / "consumer" / "promote.yml").read_text(encoding="utf-8")
        m = re.search(r"python3 -I - <<'PY' >> \"\$GITHUB_OUTPUT\"\n(.*?)\n\s*PY\n", text, re.S)
        self.assertIsNotNone(m)
        lines = m.group(1).splitlines()
        indent = min(len(ln) - len(ln.lstrip()) for ln in lines if ln.strip())
        script = "\n".join(ln[indent:] for ln in lines)
        for (new, old), want in CASES.items():
            with self.subTest(new=new, old=old), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                if new:
                    write(root, NEW, json.dumps({"github": {"default_branch": "dev", "release_branch": "new"}}))
                if old:
                    write(root, LEGACY, json.dumps({"github": {"default_branch": "dev", "release_branch": "old"}}))
                env = {**os.environ, "EVENT_NAME": "workflow_dispatch", "REF_NAME": "dev", "REPO_DEFAULT": "dev"}
                out = subprocess.run([sys.executable, "-I", "-"], input=script, cwd=root, env=env,
                                     capture_output=True, text=True, encoding="utf-8").stdout
                expected = {NEW: "release=new", LEGACY: "release=old", None: "release="}[want]
                self.assertIn(expected + "\n", out + "\n")


class ScriptDefaultsTest(unittest.TestCase):
    """--config を省いたスクリプトは作業ディレクトリの新パス(無ければ旧パス)を読む。"""

    def run_in(self, root: Path, fn):
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                code = fn()
        finally:
            os.chdir(cwd)
        return code, out.getvalue()

    def test_check_docs_policy(self):
        import check_docs_policy as cdp
        cfg = {"linear": {"team": "T"}, "docs": {"aggregate_paths": []}}
        for rel in (NEW, LEGACY):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                write(root, rel, json.dumps(cfg))
                code, out = self.run_in(root, lambda: cdp.main(["--base", "HEAD"]))
                self.assertEqual(code, 0)
                # Linear 連携ありとして読めている(空の aggregate_paths で無効化された)
                self.assertIn("docs.aggregate_paths が空", out)

    def test_release_notes_rules_from_resolved_config(self):
        import release_notes as rn
        for rel in (NEW, LEGACY):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                write(root, rel, json.dumps({"auto_merge": {"protected_paths": ["Secret/"]}}))
                rules = rn.load_rules(hc.resolve_config_path(root), None)
                self.assertIn("Secret/", rules["protected"])

    def test_release_notes_default_protects_new_path(self):
        import release_notes as rn
        self.assertIn(NEW, rn.DEFAULT_PROTECTED_PATHS)
        self.assertIn(".kiro/orchestration/", rn.DEFAULT_PROTECTED_PATHS)

    def test_set_branch_model_writes_resolved_config(self):
        import set_branch_model as sbm
        for paths, want in (((NEW,), NEW), ((LEGACY,), LEGACY), ((NEW, LEGACY), NEW)):
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for rel in paths:
                    write(root, rel, json.dumps({"github": {"default_branch": "main"}}))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(sbm.main(["--target", d, "--default-branch", "develop",
                                               "--release-branch", "main"]), 0)
                for rel in paths:
                    gh = json.loads((root / rel).read_text(encoding="utf-8"))["github"]
                    self.assertEqual(gh["default_branch"], "develop" if rel == want else "main", rel)
                self.assertFalse((root / NEW).exists() and NEW not in paths)


class ConfigureRepoReadsBothPathsTest(unittest.TestCase):
    def test_unity_ci_workflow(self):
        import base64

        import configure_repo as cr

        def b64(cfg):
            return {"content": base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")}

        class Fake:
            def __init__(self, routes):
                self.routes = routes

            def request(self, method, path, body=None):
                if path not in self.routes:
                    raise cr.ApiError(404, "Not Found")
                return self.routes[path]

        def route(rel):
            return f"/repos/o/r/contents/{rel}?ref=main"

        new = b64({"checks": {"unity_ci": {"workflow": "new.yml"}}})
        old = b64({"checks": {"unity_ci": {"workflow": "old.yml"}}})
        for routes, want in (({route(NEW): new}, "new.yml"), ({route(LEGACY): old}, "old.yml"),
                             ({route(NEW): new, route(LEGACY): old}, "new.yml"), ({}, None)):
            with self.subTest(want=want):
                self.assertEqual(cr.read_unity_ci_workflow(Fake(routes), "o/r", "main"), want)


class BootstrapTargetConfigPathTest(unittest.TestCase):
    """オンボーディングは新パスに config を作る。旧パスの既存 config はそのまま使う。"""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts" / "onboard"))
        import bootstrap_target
        self.bt = bootstrap_target

    def run_bootstrap(self, target: Path) -> dict:
        result = target.parent / "result.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = self.bt.main(["--harness", str(ROOT), "--target", str(target), "--repo", "o/r",
                                 "--default-branch", "main", "--linear-team", "T", "--result", str(result)])
        self.assertEqual(code, 0)
        return json.loads(result.read_text(encoding="utf-8"))

    def test_creates_new_path(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "t"
            target.mkdir()
            r = self.run_bootstrap(target)
            self.assertEqual(r["config"], "created")
            self.assertEqual(r["config_path"], NEW)
            cfg = json.loads((target / NEW).read_text(encoding="utf-8"))
            self.assertIn(NEW, cfg["auto_merge"]["protected_paths"])
            self.assertFalse((target / ".kiro").exists())

    def test_existing_legacy_config_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "t"
            write(target, LEGACY, json.dumps({"github": {"repo": "o/r", "default_branch": "main"},
                                              "linear": {"team": "T", "project": "r"}}))
            r = self.run_bootstrap(target)
            self.assertEqual(r["config"], "exists")
            self.assertEqual(r["config_path"], LEGACY)
            self.assertFalse((target / NEW).exists())
            self.assertEqual(r["effective"]["team"], "T")


if __name__ == "__main__":
    unittest.main()
