#!/usr/bin/env python3
"""PR の差分が「レビュー不要の軽微な変更(fast track)」かを判定する。

`git-workflow.md` §4 / linear-worker `SKILL.md` §4 の「軽微な変更の高速マージ」で使う。
差分が次のどちらか(または両方の組み合わせ)だけなら fast track とし、外部レビューの
依頼・待機を省いてマージ判定へ進める。それ以外はすべて「レビュー要」。

- **テキストのみ**: ソースコードではない文書ファイル(`fast_track.text_extensions`。既定
  `.md` `.txt` 等)の追加・変更・削除。ただし `fast_track.text_exclude_paths`(既定 `.kiro/`
  `Assets/` `Packages/` — spec 文書や、実行時に読まれ得る Unity の TextAsset)は除く
- **コメントのみ**: 対応言語のソースファイルで、コメントと空白(行頭・行末・空行・連続空白)
  以外が変わっていない変更。コメントを除いて正規化したコード(文字列リテラルは
  そのまま)を変更前後で比べる。ビルド・lint の挙動を変えるディレクティブ的なコメント
  (`//go:build` `// @ts-ignore` `# type:` `# noqa` 等)はコードとして扱う

判定は保守側に倒す: 未対応の拡張子・字句解析できないファイル・ファイルの追加/削除
(テキスト以外)・モード変更・シンボリックリンク・submodule・`auto_merge.protected_paths`
(エージェントへの指示・CI 定義・設定。文書であっても挙動を変える)に当たるファイルが
1 つでもあれば「レビュー要」。

- 比較は `git diff <base>...<head>`(merge-base 起点)。PR 全体なら `--base
  origin/<default_branch>`、前回レビュー済みのコミットからの追加分なら `--base <その SHA>`
- 終了コード: 0 = fast track、1 = レビュー要、2 = エラー(判定できない。レビュー要として扱う)
- config(`.kiro/orchestration/config.json`)が無いリポジトリでも既定値で判定する

標準ライブラリのみ使用(ランナー・ローカルどちらでも動く)。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CONFIG_REL = Path(".kiro/orchestration/config.json")
# SKILL.md 設定表の既定値。キーが無い config ではこれを使う
DEFAULT_TEXT_EXTENSIONS = [".md", ".markdown", ".txt", ".rst", ".adoc", ".asciidoc"]
DEFAULT_TEXT_EXCLUDE_PATHS = [".kiro/", "Assets/", "Packages/"]
DEFAULT_PROTECTED_PATHS = [
    ".claude/", ".github/", ".kiro/settings/", ".kiro/orchestration/",
    "CLAUDE.md", "AGENTS.md", ".agents/", ".codex/",
]
REGULAR_MODES = {"100644", "100755"}


class LexError(Exception):
    """字句解析できない(未終端の文字列など)。保守側に倒してレビュー要とする。"""


# --------------------------------------------------------------------------- config


def load_settings(config: Path) -> dict:
    cfg: dict = {}
    if config.is_file():
        cfg = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError(f"{config} の最上位が JSON オブジェクトではない")
    ft = cfg.get("fast_track") or {}
    am = cfg.get("auto_merge") or {}
    if not isinstance(ft, dict) or not isinstance(am, dict):
        raise ValueError(f"{config} の fast_track / auto_merge がオブジェクトではない")

    def str_list(value, default, key):
        if value is None:
            return list(default)
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"{config} の {key} が文字列の配列ではない")
        return value

    return {
        "enabled": ft.get("enabled", True) is not False,
        "text_extensions": [e.lower() for e in str_list(ft.get("text_extensions"), DEFAULT_TEXT_EXTENSIONS, "fast_track.text_extensions")],
        "text_exclude_paths": str_list(ft.get("text_exclude_paths"), DEFAULT_TEXT_EXCLUDE_PATHS, "fast_track.text_exclude_paths"),
        "protected_paths": str_list(am.get("protected_paths"), DEFAULT_PROTECTED_PATHS, "auto_merge.protected_paths"),
    }


def matches(path: str, patterns: list[str]) -> bool:
    return any(path.startswith(p) if p.endswith("/") else path == p for p in patterns)


# --------------------------------------------------------------------------- 字句解析

# ビルド・lint・型検査の挙動を変えるコメント。変わっていたらコードの変更として扱う
C_DIRECTIVE = re.compile(
    r"^(?:/\s*<(?:reference|amd-)|#\s*source(?:Mapping)?URL|\s*(?:go:|\+build|export\s|line\s|"
    r"@ts-|@jsx|@flow|eslint|jshint|tslint|biome-ignore|deno-|prettier-ignore|istanbul|c8\s|v8\s|"
    r"webpack|vite-|[#@]__(?:PURE|NO_SIDE_EFFECTS)__|NOLINT|nolint|clang-format|swiftlint|"
    r"noinspection|lint:|#?nosec|@formatter|ReSharper|@Suppress|@suppress))",
    re.IGNORECASE,
)
PY_DIRECTIVE = re.compile(
    r"^#(?:!|.*coding[:=]|\s*(?:type:|noqa|pragma|pylint:|mypy:|pyright:|fmt:|isort:|nosec|pyre-|ruff:))",
    re.IGNORECASE,
)
JS_REGEX_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
    "case", "do", "else", "yield", "await",
}
JS_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
CHAR_LITERAL = re.compile(r"'(?:\\[^'\n]{1,10}|[^\\'\n])'")
# C# の文字列の開始: $ の並び / @ / $ の並び / 引用符の並び
CSHARP_STRING = re.compile(r'(\$*)(@?)(\$*)("+)')


@dataclass(frozen=True)
class Spec:
    line: str | None = "//"
    block: bool = True
    nested: bool = False          # ブロックコメントの入れ子(Rust / Swift / Kotlin / Scala / Dart)
    single: str | None = "char"   # ' の扱い: "char"(1 文字リテラル)/ "string" / None
    double_newline: bool = False  # "..." の中で改行を許す(Rust)
    interp: str | None = None     # 文字列内の式埋め込み: "${" / "\\(" / None
    template: bool = False        # JS / TS のテンプレートリテラル
    regex: bool = False           # JS / TS の正規表現リテラル
    go_raw: bool = False          # Go の `...`
    triple: str = ""              # 三連引用符で始まる複数行文字列に使う引用符(例: '"' / '"\'')
    cpp_raw: bool = False         # C++ の R"d(...)d"
    rust_raw: bool = False        # Rust の r#"..."#
    swift_ext: bool = False       # Swift の #"..."#
    csharp: bool = False          # C# の @"..." / $"..." / """..."""
    dart_raw: bool = False        # Dart の r'...'


C_FAMILY = Spec(cpp_raw=True)
SPECS: dict[str, Spec] = {}
for _ext in (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".m", ".mm",
             ".shader", ".hlsl", ".cginc", ".compute", ".glsl"):
    SPECS[_ext] = C_FAMILY
SPECS[".cs"] = Spec(csharp=True)
SPECS[".java"] = Spec(triple='"')
for _ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"):
    SPECS[_ext] = Spec(single="string", template=True, regex=True)
SPECS[".go"] = Spec(go_raw=True)
SPECS[".rs"] = Spec(nested=True, double_newline=True, rust_raw=True)
SPECS[".swift"] = Spec(nested=True, single=None, interp="\\(", triple='"', swift_ext=True)
for _ext in (".kt", ".kts"):
    SPECS[_ext] = Spec(nested=True, interp="${", triple='"')
SPECS[".scala"] = Spec(nested=True, interp="${", triple='"')
SPECS[".dart"] = Spec(nested=True, single="string", interp="${", triple="\"'", dart_raw=True)
for _ext in (".css", ".uss"):
    SPECS[_ext] = Spec(line=None, single="string")
PYTHON_EXTS = {".py", ".pyi"}


def _ident(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


class Lexer:
    """コメントを取り除き、文字列リテラルを不透明なまま残してコードを正規化する。"""

    def __init__(self, src: str, spec: Spec):
        self.s = src
        self.spec = spec
        self.out: list[str] = []
        self.literals: list[str] = []

    # ---- 出力
    def _literal(self, text: str) -> None:
        self.out.append(f"\x00{len(self.literals)}\x00")
        self.literals.append(text)

    def _comment(self, text: str, body: str) -> None:
        if C_DIRECTIVE.match(body):
            self.out.append(text)  # ディレクティブはコードとして比較する
        else:
            # 改行を含むブロックコメントは改行に、それ以外は空白に置き換える
            # (`a/**/b` の連結や、JS の自動セミコロン挿入の違いを見逃さないため)
            self.out.append("\n" if "\n" in text else " ")

    def normalized(self) -> tuple[str, tuple[str, ...]]:
        self.scan(0)
        code = re.sub(r"[ \t\f\r\v]+", " ", "".join(self.out))
        lines = [ln.strip() for ln in code.split("\n")]
        return "\n".join(ln for ln in lines if ln), tuple(self.literals)

    # ---- 走査
    def scan(self, i: int, stop: str | None = None, emit: bool = True) -> int:
        """i からコードを走査する。stop(`}` / `)`)を与えると対応する閉じ括弧の直後を返す。"""
        s, n, sp = self.s, len(self.s), self.spec
        opener = {"}": "{", ")": "("}.get(stop or "")
        depth = 0
        prev: str | None = None  # 直前の有意なトークン: 記号 1 文字 / "W"(語)/ "L"(リテラル)
        word = ""
        in_word = False
        while i < n:
            c = s[i]
            if stop is not None:
                if c == opener:
                    depth += 1
                elif c == stop:
                    if depth == 0:
                        return i + 1
                    depth -= 1
            if sp.line and s.startswith(sp.line, i):
                j = s.find("\n", i)
                j = n if j < 0 else j
                if emit:
                    self._comment(s[i:j], s[i + len(sp.line):j])
                i, in_word = j, False
                continue
            if sp.block and s.startswith("/*", i):
                j = self._block_end(i)
                if emit:
                    self._comment(s[i:j], s[i + 2:j])
                i, in_word = j, False
                continue
            j = self._literal_end(i, prev, word)
            if j is not None:
                if emit:
                    self._literal(s[i:j])
                i, prev, in_word = j, "L", False
                continue
            if c == "\\":
                if emit:
                    self.out.append(s[i:i + 2])
                i, prev, in_word = i + 2, "\\", False
                continue
            if emit:
                self.out.append(c)
            if c.isspace():
                in_word = False
            elif _ident(c):
                word = word + c if in_word else c
                prev, in_word = "W", True
            else:
                prev, in_word = c, False
            i += 1
        if stop is not None:
            raise LexError("文字列内の式埋め込みが閉じていない")
        return i

    def _block_end(self, i: int) -> int:
        s, depth, j = self.s, 1, i + 2
        while j < len(s):
            if s.startswith("*/", j):
                depth -= 1
                j += 2
                if depth == 0:
                    return j
            elif self.spec.nested and s.startswith("/*", j):
                depth += 1
                j += 2
            else:
                j += 1
        raise LexError("ブロックコメントが閉じていない")

    def _string(self, i: int, close: str, *, escapes: bool = True, multiline: bool = False,
                interp: str | None = None, doubled: bool = False) -> int:
        """開き引用符の直後 i から閉じ引用符の直後までを返す。"""
        s, n = self.s, len(self.s)
        while i < n:
            if escapes and s[i] == "\\":
                if interp == "\\(" and s.startswith("\\(", i):
                    i = self.scan(i + 2, stop=")", emit=False)
                    continue
                i += 2
                continue
            if doubled and s.startswith(close * 2, i):
                i += 2
                continue
            if s.startswith(close, i):
                return i + len(close)
            if interp == "${" and s.startswith("${", i):
                i = self.scan(i + 2, stop="}", emit=False)
                continue
            if interp == "{":
                if s.startswith("{{", i):
                    i += 2
                    continue
                if s[i] == "{":
                    i = self.scan(i + 1, stop="}", emit=False)
                    continue
            if s[i] == "\n" and not multiline:
                raise LexError("文字列リテラルの途中で改行している")
            i += 1
        raise LexError("文字列リテラルが閉じていない")

    def _literal_end(self, i: int, prev: str | None, word: str) -> int | None:
        s, sp = self.s, self.spec
        c = s[i]
        at_word_start = i == 0 or not _ident(s[i - 1])

        if sp.csharp and (c == '"' or (at_word_start and c in "@$")):
            m = CSHARP_STRING.match(s, i)
            if m:
                interp = "{" if m.group(1) or m.group(3) else None
                start = m.start(4) + 1
                if m.group(2):  # 逐語的文字列 @"..."("" がエスケープ、改行可)
                    return self._string(start, '"', escapes=False, multiline=True, interp=interp, doubled=True)
                quotes = len(m.group(4))
                if quotes >= 3:  # raw 文字列リテラル """..."""
                    end = s.find('"' * quotes, m.end())
                    if end < 0:
                        raise LexError("raw 文字列リテラルが閉じていない")
                    return end + quotes
                return self._string(start, '"', interp=interp)
        if sp.cpp_raw and at_word_start:
            m = re.compile(r'(?:u8|u|U|L)?R"([^()\\\s]{0,16})\(').match(s, i)
            if m:
                close = ")" + m.group(1) + '"'
                end = s.find(close, m.end())
                if end < 0:
                    raise LexError("raw 文字列リテラルが閉じていない")
                return end + len(close)
        if sp.rust_raw and at_word_start:
            m = re.compile(r'(?:b|c)?r(#*)"').match(s, i)
            if m:
                close = '"' + m.group(1)
                end = s.find(close, m.end())
                if end < 0:
                    raise LexError("raw 文字列リテラルが閉じていない")
                return end + len(close)
        if sp.swift_ext and c == "#":
            m = re.compile(r'(#+)("""|")').match(s, i)
            if m:
                close = m.group(2) + m.group(1)
                end = s.find(close, m.end())
                if end < 0:
                    raise LexError("拡張デリミタの文字列リテラルが閉じていない")
                return end + len(close)
        if sp.dart_raw and at_word_start and c == "r":
            m = re.compile(r"r('''|\"\"\"|'|\")").match(s, i)
            if m:
                q = m.group(1)
                return self._string(m.end(), q, escapes=False, multiline=len(q) == 3)
        if sp.triple and c in sp.triple and s.startswith(c * 3, i):
            return self._string(i + 3, c * 3, multiline=True, interp=sp.interp)
        if c == '"':
            return self._string(i + 1, '"', multiline=sp.double_newline, interp=sp.interp)
        if c == "'":
            if sp.single == "string":
                return self._string(i + 1, "'", interp=sp.interp)
            if sp.single == "char":
                m = CHAR_LITERAL.match(s, i)
                return m.end() if m else None
            return None
        if c == "`":
            if sp.template:
                return self._string(i + 1, "`", multiline=True, interp="${")
            if sp.go_raw:
                end = s.find("`", i + 1)
                if end < 0:
                    raise LexError("raw 文字列リテラルが閉じていない")
                return end + 1
        if sp.regex and c == "/" and not s.startswith(("//", "/*"), i):
            regex_ok = prev is None or (prev == "W" and word in JS_REGEX_KEYWORDS) or (
                prev not in ("W", "L") and prev in JS_REGEX_PREV)
            if regex_ok:
                j, in_class = i + 1, False
                while j < len(s):
                    ch = s[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "\n":
                        return None
                    if in_class:
                        in_class = ch != "]"
                    elif ch == "[":
                        in_class = True
                    elif ch == "/":
                        j += 1
                        while j < len(s) and s[j].isalpha():
                            j += 1
                        return j
                    j += 1
                return None
        return None


def python_tokens(src: str) -> tuple:
    """Python はトークナイザで比較する。docstring は文字列なので変更はコード扱い。"""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NL:
                continue
            if tok.type == tokenize.COMMENT:
                if PY_DIRECTIVE.match(tok.string):
                    out.append((tok.type, tok.string.rstrip()))
                continue
            if tok.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                out.append((tok.type, ""))
                continue
            out.append((tok.type, tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError) as e:
        raise LexError(f"Python として字句解析できない: {e}") from e
    return tuple(out)


def code_signature(path: str, src: str):
    """コメントと空白を除いたコードの比較用表現。未対応の言語なら None。"""
    ext = PurePosixPath(path).suffix.lower()
    if ext in PYTHON_EXTS:
        return python_tokens(src)
    spec = SPECS.get(ext)
    if spec is None:
        return None
    if ext == ".go" and re.search(r'\bimport\s*(?:\(\s*[^)]*)?"C"', src):
        raise LexError("cgo のプリアンブル(コメント内の C コード)を含む")
    return Lexer(src, spec).normalized()


# --------------------------------------------------------------------------- 差分


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def blob_text(sha: str) -> str:
    data = subprocess.run(["git", "cat-file", "blob", sha], check=True, capture_output=True).stdout
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LexError("UTF-8 として読めない") from e


def changes(base: str, head: str) -> list[tuple[str, str, str, str, str, str]]:
    """(旧モード, 新モード, 旧 SHA, 新 SHA, 状態, パス) の一覧。リネームは削除 + 追加として扱う。"""
    raw = git("diff", "--raw", "--no-renames", "--no-abbrev", "-z", f"{base}...{head}")
    parts = raw.split("\0")
    out = []
    k = 0
    while k + 1 < len(parts) and parts[k]:
        meta = parts[k].lstrip(":").split()
        out.append((meta[0], meta[1], meta[2], meta[3], meta[4][0], parts[k + 1]))
        k += 2
    return out


def classify_file(change, settings) -> tuple[str, str]:
    """(区分, 説明)。区分は text / comment / review。"""
    old_mode, new_mode, old_sha, new_sha, status, path = change
    if matches(path, settings["protected_paths"]):
        return "review", "保護パス(auto_merge.protected_paths)"
    modes = {m for m in (old_mode, new_mode) if m != "000000"}
    if not modes <= REGULAR_MODES:
        return "review", "シンボリックリンク / submodule"
    if status == "M" and old_mode != new_mode:
        return "review", "ファイルモードの変更"
    ext = PurePosixPath(path).suffix.lower()
    if ext in settings["text_extensions"]:
        if matches(path, settings["text_exclude_paths"]):
            return "review", "テキストだが除外パス(fast_track.text_exclude_paths)"
        return "text", "テキスト"
    if status != "M":
        return "review", {"A": "ファイルの追加", "D": "ファイルの削除"}.get(status, f"状態 {status}")
    try:
        before = code_signature(path, blob_text(old_sha))
        if before is None:
            return "review", "コメントのみの判定に未対応の拡張子"
        after = code_signature(path, blob_text(new_sha))
    except LexError as e:
        return "review", f"字句解析できない({e})"
    if before != after:
        return "review", "コードの変更"
    return "comment", "コメント / 空白のみ"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(CONFIG_REL))
    ap.add_argument("--base", required=True, help="比較元(origin/<default_branch>、または前回レビュー済みの SHA)")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--json", action="store_true", help="結果を JSON で出力する")
    a = ap.parse_args(argv)

    try:
        settings = load_settings(Path(a.config))
        files = changes(a.base, a.head)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"::error::{e}")
        return 2
    except subprocess.CalledProcessError as e:
        print(f"::error::git に失敗: {(e.stderr or '').strip()}")
        return 2

    results = []
    for ch in files:
        try:
            kind, why = classify_file(ch, settings)
        except subprocess.CalledProcessError as e:
            kind, why = "review", f"blob を読めない: {(e.stderr or b'').decode(errors='replace').strip()}"
        results.append({"path": ch[5], "kind": kind, "reason": why})

    if not settings["enabled"]:
        verdict, summary = "review", "fast_track.enabled が false"
    elif not results:
        verdict, summary = "fast-track", "差分なし"
    elif any(r["kind"] == "review" for r in results):
        verdict, summary = "review", "コード等の変更を含む"
    else:
        kinds = {r["kind"] for r in results}
        summary = {frozenset({"text"}): "テキストのみ", frozenset({"comment"}): "コメントのみ"}.get(
            frozenset(kinds), "テキストとコメントのみ")
        verdict = "fast-track"

    if a.json:
        print(json.dumps({"verdict": verdict, "summary": summary, "files": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"  [{r['kind']}] {r['path']}: {r['reason']}")
        print(f"verdict: {verdict}({summary})")
    return 0 if verdict == "fast-track" else 1


if __name__ == "__main__":
    sys.exit(main())
