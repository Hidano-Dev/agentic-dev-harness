#!/usr/bin/env bash
# Unity CI サーバー: sudo 不要のユーザーローカル環境構築(冪等)
# 対象: gh / git-lfs / pwsh / node+corepack(pnpm) / .NET 8 SDK / actions-runner 本体
set -euo pipefail

OPT="$HOME/.local/opt"
BIN="$HOME/.local/bin"
mkdir -p "$OPT" "$BIN" "$HOME/ci-setup"
export PATH="$BIN:$PATH"

# 注意: `curl | grep -m1` は grep が先に閉じて curl が 23 を返し pipefail で落ちるので、python3 で全量読む
latest_tag() { curl -fsSL "https://api.github.com/repos/$1/releases/latest" | python3 -c 'import sys,json;print(json.load(sys.stdin)["tag_name"])'; }

echo "=== gh"
if ! command -v gh >/dev/null; then
  v=$(latest_tag cli/cli); v=${v#v}
  curl -fsSL "https://github.com/cli/cli/releases/download/v${v}/gh_${v}_linux_amd64.tar.gz" | tar xz -C "$OPT"
  ln -sfn "$OPT/gh_${v}_linux_amd64/bin/gh" "$BIN/gh"
fi
gh --version | head -1

echo "=== git-lfs"
if ! command -v git-lfs >/dev/null; then
  v=$(latest_tag git-lfs/git-lfs); v=${v#v}
  mkdir -p "$OPT/git-lfs"
  curl -fsSL "https://github.com/git-lfs/git-lfs/releases/download/v${v}/git-lfs-linux-amd64-v${v}.tar.gz" | tar xz -C "$OPT/git-lfs" --strip-components=1
  ln -sfn "$OPT/git-lfs/git-lfs" "$BIN/git-lfs"
fi
git lfs install --skip-repo
git lfs version

echo "=== pwsh (7.4 LTS)"
if ! command -v pwsh >/dev/null; then
  v=$(curl -fsSL "https://api.github.com/repos/PowerShell/PowerShell/releases?per_page=50" | python3 -c 'import sys,json,re;print(next(r["tag_name"][1:] for r in json.load(sys.stdin) if re.fullmatch(r"v7\.4\.\d+", r["tag_name"])))')
  mkdir -p "$OPT/powershell"
  curl -fsSL "https://github.com/PowerShell/PowerShell/releases/download/v${v}/powershell-${v}-linux-x64.tar.gz" | tar xz -C "$OPT/powershell"
  chmod +x "$OPT/powershell/pwsh"
  ln -sfn "$OPT/powershell/pwsh" "$BIN/pwsh"
fi
pwsh -NoLogo -Command '$PSVersionTable.PSVersion.ToString()'

echo "=== node 22 LTS + corepack/pnpm"
# PATH 上の既存 node(distro パッケージ・nvm 等)は版も置き場所もまちまちで、ランナーのジョブ PATH
# (~/.local/bin + 標準ディレクトリ)から見えない場合があるため再利用せず、常に $OPT/node の 22 を使う
if [ ! -x "$OPT/node/bin/node" ]; then
  v=$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c 'import sys,json;print(next(r["version"] for r in json.load(sys.stdin) if r["version"].startswith("v22.")))')
  mkdir -p "$OPT/node"
  curl -fsSL "https://nodejs.org/dist/${v}/node-${v}-linux-x64.tar.xz" | tar xJ -C "$OPT/node" --strip-components=1
  fi
for b in node npm npx corepack; do ln -sfn "$OPT/node/bin/$b" "$BIN/$b"; done
node --version
corepack enable --install-directory "$BIN"
corepack pnpm --version

echo "=== .NET 8 SDK"
# 同上: 既存の dotnet(ランタイムのみ・別メジャーの SDK)は再利用せず、常に $OPT/dotnet の 8.x SDK を使う
if ! "$OPT/dotnet/dotnet" --list-sdks 2>/dev/null | grep -q '^8\.'; then
  curl -fsSL https://dot.net/v1/dotnet-install.sh -o "$HOME/ci-setup/dotnet-install.sh"
  bash "$HOME/ci-setup/dotnet-install.sh" --channel 8.0 --install-dir "$OPT/dotnet"
fi
ln -sfn "$OPT/dotnet/dotnet" "$BIN/dotnet"
export DOTNET_ROOT="$OPT/dotnet"
dotnet --version

echo "=== actions-runner"
if [ ! -x "$HOME/actions-runner/run.sh" ]; then
  v=$(latest_tag actions/runner); v=${v#v}
  mkdir -p "$HOME/actions-runner"
  curl -fsSL "https://github.com/actions/runner/releases/download/v${v}/actions-runner-linux-x64-${v}.tar.gz" | tar xz -C "$HOME/actions-runner"
fi
ls "$HOME/actions-runner/config.sh" >/dev/null && echo "runner extracted: $(cat "$HOME/actions-runner/bin/Runner.Listener.deps.json" >/dev/null 2>&1; ls "$HOME/actions-runner" | head -3 | tr '\n' ' ')"

echo "=== PATH / env for non-interactive shells"
MARK="# >>> unity-ci userlocal >>>"
if ! grep -q "$MARK" "$HOME/.bashrc"; then
  tmp=$(mktemp)
  {
    echo "$MARK"
    echo 'export PATH="$HOME/.local/bin:$PATH"'
    echo 'export DOTNET_ROOT="$HOME/.local/opt/dotnet"'
    echo 'export DOTNET_CLI_TELEMETRY_OPTOUT=1'
    echo 'export UNITY_EDITORS_ROOT="$HOME/Unity/Hub/Editor"'
    echo "# <<< unity-ci userlocal <<<"
    cat "$HOME/.bashrc"
  } > "$tmp" && mv "$tmp" "$HOME/.bashrc"
fi
echo "done"
