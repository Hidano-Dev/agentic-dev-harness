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

from _helpers import ROOT, WORKER_SCRIPTS, commit_all, git, init_repo

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


class StrictestConfigTest(unittest.TestCase):
    DEFAULT = ["D/"]

    def merge(self, new, old):
        return hc.strictest_config(new, old, self.DEFAULT)

    def test_protected_paths_union(self):
        m = self.merge({"auto_merge": {"protected_paths": ["A/", "B/"]}},
                       {"auto_merge": {"protected_paths": ["B/", "C/"]}})
        self.assertEqual(m["auto_merge"]["protected_paths"], ["A/", "B/", "C/"])

    def test_missing_side_uses_default(self):
        m = self.merge({"auto_merge": {"protected_paths": []}}, {})
        self.assertEqual(m["auto_merge"]["protected_paths"], ["D/"])
        m = self.merge({"auto_merge": {"protected_paths": "x"}}, {"auto_merge": {"protected_paths": ["C/"]}})
        self.assertEqual(m["auto_merge"]["protected_paths"], ["D/", "C/"])

    def test_auto_merge_enabled_only_if_both(self):
        for a, b, want in ((True, True, True), (True, False, False), (False, True, False), (True, None, False)):
            with self.subTest(a=a, b=b):
                old = {"auto_merge": {"enabled": b}} if b is not None else {}
                self.assertIs(self.merge({"auto_merge": {"enabled": a}}, old)["auto_merge"]["enabled"], want)

    def test_merge_parked_and_fail_open(self):
        m = self.merge({"auto_merge": {"merge_parked": True}, "review": {"fail_open": True}},
                       {"auto_merge": {"merge_parked": False}, "review": {"fail_open": False}})
        self.assertIs(m["auto_merge"]["merge_parked"], False)
        self.assertIs(m["review"]["fail_open"], False)
        m = self.merge({"review": {"fail_open": True}}, {})
        self.assertIs(m["review"]["fail_open"], True)

    def test_provider_disagreement_means_action(self):
        self.assertEqual(self.merge({"review": {"provider": None}}, {"review": {"provider": "app"}})["review"]["provider"],
                         "action")
        self.assertIsNone(self.merge({"review": {"provider": None}}, {"review": {"provider": None}})["review"]["provider"])

    def test_disagreeing_features_are_disabled(self):
        m = self.merge({"fast_track": {"enabled": True}, "conflicts": {"auto_resolve": True},
                        "github": {"default_branch": "develop", "release_branch": "main"}},
                       {"fast_track": {"enabled": True, "require_ci": False}, "conflicts": {"auto_resolve": True},
                        "github": {"default_branch": "develop", "release_branch": None}})
        self.assertIs(m["fast_track"]["enabled"], False)
        self.assertIs(m["conflicts"]["auto_resolve"], True)  # 一致していれば新パスのまま
        self.assertIsNone(m["github"]["release_branch"])
        self.assertEqual(m["github"]["default_branch"], "develop")

    def test_inputs_are_not_modified(self):
        new = {"auto_merge": {"protected_paths": ["A/"]}}
        snapshot = json.dumps(new)
        self.merge(new, {"auto_merge": {"protected_paths": ["B/"]}})
        self.assertEqual(json.dumps(new), snapshot)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            n, o, broken = Path(d) / "n.json", Path(d) / "o.json", Path(d) / "b.json"
            n.write_text(json.dumps({"auto_merge": {"enabled": True, "protected_paths": ["A/"]}}), encoding="utf-8")
            o.write_text(json.dumps({"auto_merge": {"enabled": False, "protected_paths": ["B/"]}}), encoding="utf-8")
            broken.write_text("{ broken", encoding="utf-8")
            script = WORKER_SCRIPTS / "harness_config.py"

            def run(*args):
                return subprocess.run([sys.executable, "-I", str(script), "strictest", *map(str, args)],
                                      capture_output=True, text=True, encoding="utf-8")

            r = run(n, o)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["auto_merge"]["protected_paths"], ["A/", "B/"])
            self.assertIs(out["auto_merge"]["enabled"], False)
            r = run(broken, o)  # 読めない側は無視する
            self.assertEqual(json.loads(r.stdout)["auto_merge"]["protected_paths"], ["B/"])
            self.assertEqual(run(broken, broken).returncode, 1)


class ResolveConflictsBothConfigsTest(unittest.TestCase):
    """--config なしの resolve_conflicts.py は、新旧の config が食い違えば自動解消しない。"""

    def make_conflict(self, root: Path) -> None:
        init_repo(root)
        write(root, "notes.md", "# notes\n")
        commit_all(root, "base")
        git(root, "checkout", "-q", "-b", "other")
        write(root, "notes.md", "# notes\n- other\n")
        commit_all(root, "other")
        git(root, "checkout", "-q", "main")
        write(root, "notes.md", "# notes\n- main\n")
        commit_all(root, "main")
        subprocess.run(["git", "-C", str(root), "merge", "-q", "--no-commit", "other"], capture_output=True)

    def run_main(self, root: Path):
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = rc.main(["--dry-run"])
        finally:
            os.chdir(cwd)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_only_new_config_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, NEW, json.dumps({"conflicts": {"auto_resolve_extensions": [".md"]}}))
            self.make_conflict(root)
            code, out, _ = self.run_main(root)
            self.assertEqual((code, out["resolved"]), (0, ["notes.md"]))

    def test_disagreeing_configs_do_not_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, NEW, json.dumps({"conflicts": {"auto_resolve_extensions": [".md"]}}))
            write(root, LEGACY, json.dumps({"conflicts": {"auto_resolve_extensions": [".rst"]}}))
            self.make_conflict(root)
            code, out, err = self.run_main(root)
            self.assertEqual((code, out["unresolved"]), (1, ["notes.md"]))
            self.assertIn("::warning::", err)


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
            for name in ("choose_config_rel", "strictest_config"):
                with self.subTest(mod=mod.__name__, fn=name):
                    self.assertEqual(body(getattr(mod, name)), body(getattr(hc, name)))

    def test_default_protected_matches_policy_gate(self):
        self.assertEqual(hc.DEFAULT_PROTECTED_PATHS, pg.DEFAULT_CORE)

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
