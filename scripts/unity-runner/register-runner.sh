#!/usr/bin/env bash
# リポジトリ単位の GitHub Actions ランナーを登録し、user systemd で常駐させる(冪等)
# 使い方: register-runner.sh <repo-name> [registration-token] [owner]
#   登録トークンは `gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token`
#   (1 時間で失効)。トークンを渡すと、既存の登録(.runner)があっても捨てて登録し直す
#   (GitHub 側で削除・無効化された古い登録を上書きするため)。渡さなければ既存の登録のまま
#   サービスだけ整える。手順と前提は docs/onprem-unity-runner.md
set -euo pipefail
REPO="$1"; TOKEN="${2:-}"
OWNER="${3:-Hidano-Dev}"
SLUG="$(echo "$REPO" | tr '[:upper:]' '[:lower:]')"
DIR="$HOME/runners/$SLUG"
NAME="unity-ci-ubuntu-$SLUG"
SVC="actions-runner-$SLUG"

mkdir -p "$HOME/runners"
if [ ! -x "$DIR/config.sh" ]; then
  mkdir -p "$DIR"
  cp -a "$HOME/actions-runner/." "$DIR/"
fi

cd "$DIR"
if [ -n "$TOKEN" ]; then
  if [ -f .runner ]; then
    # 古い登録を捨てて登録し直す。config.sh は .runner があると拒否するので、サービスを止めて
    # ローカルの登録情報だけ消す(GitHub 側は --replace で同名ランナーを置き換える)
    echo "re-registering: discarding local registration $(python3 -c 'import json;print(json.load(open(".runner"))["agentName"])')"
    systemctl --user stop "$SVC.service" 2>/dev/null || true
    rm -f .runner .credentials .credentials_rsaparams
  fi
  ./config.sh --unattended \
    --url "https://github.com/$OWNER/$REPO" \
    --token "$TOKEN" \
    --name "$NAME" \
    --labels "unity,linux-unity" \
    --work _work \
    --replace
elif [ -f .runner ]; then
  echo "already configured: $(python3 -c 'import json;print(json.load(open(".runner"))["agentName"])') (pass a registration token to re-register)"
else
  echo "not configured and no registration token given" >&2
  exit 1
fi

# ジョブに渡す環境(ランナー起動時に .env / .path を読む)
cat > .env <<EOF
DOTNET_ROOT=$HOME/.local/opt/dotnet
DOTNET_CLI_TELEMETRY_OPTOUT=1
UNITY_EDITORS_ROOT=$HOME/Unity/Hub/Editor
UNITY_6000_3_19F1=$HOME/Unity/Hub/Editor/6000.3.19f1/Editor/Unity
UNITY_6000_0_36F1=$HOME/Unity/Hub/Editor/6000.0.36f1/Editor/Unity
LANG=C.UTF-8
EOF
echo "$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" > .path

# user systemd サービス(sudo 不要、linger 有効化済み)
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SVC.service" <<EOF
[Unit]
Description=GitHub Actions runner ($OWNER/$REPO)
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/run.sh
Restart=always
RestartSec=10
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=DOTNET_ROOT=$HOME/.local/opt/dotnet

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now "$SVC.service"
sleep 5
systemctl --user --no-pager --lines=5 status "$SVC.service" | sed -n '1,12p'
