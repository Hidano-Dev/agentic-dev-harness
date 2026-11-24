#!/usr/bin/env bash
# Unity Hub を Xvfb 上で user systemd サービスとして常駐させる(冪等)
# 使い方: install-hub-keepalive.sh
#
# Hub ログインで有効化した Personal ライセンスは、オフラインで使える期間が約 30 日しかない。
# Hub が動いていてログイン情報(アクセストークン)を持っていないと更新されず、期限切れになると
# Unity が終了コード 198 で起動に失敗する(2026-11-07 に実際に起きた)。そこで Hub を常駐させる。
#
# Hub はログイン情報を Secret Service(GNOME キーリング)の既定コレクションに保存する。
# 既定の login キーリングはデスクトップにパスワードでログインしたときしか開かず、SSH や再起動の
# あとはロックされたままなので、常駐中の Hub が読めない。代わりにパスワード無しのキーリング
# `ci` を作って既定にする(login キーリングには触らない)。代償として、Unity アカウントの
# ログイン情報は暗号化されずに ~/.local/share/keyrings/ci.keyring に置かれる。
#
# 初回(とログインが切れたとき)は人が RDP で入って hub-login.sh を実行する。
# 手順と背景は docs/onprem-unity-runner.md
set -euo pipefail
KEYRINGS="$HOME/.local/share/keyrings"
UNIT_DIR="$HOME/.config/systemd/user"
SVC=unityhub-keepalive
SECRETS_DEST=org.freedesktop.secrets
CI_COLLECTION=/org/freedesktop/secrets/collection/ci

for cmd in xvfb-run unityhub busctl; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
done

# 1. パスワード無しのキーリング ci を既定にする
mkdir -p "$KEYRINGS"
chmod 700 "$KEYRINGS"
if [ ! -f "$KEYRINGS/ci.keyring" ]; then
  # gnome-keyring の平文形式(パスワード無し)のキーリング。項目はデーモンが追記する
  ( umask 077
    printf '[keyring]\ndisplay-name=ci\nctime=0\nmtime=0\nlock-on-idle=false\nlock-after=false\n' \
      > "$KEYRINGS/ci.keyring" )
  echo "created $KEYRINGS/ci.keyring"
fi
if [ "$(cat "$KEYRINGS/default" 2>/dev/null)" != "ci" ]; then
  printf ci > "$KEYRINGS/default"
  # 既定の切り替えはデーモンの再起動で反映される(ソケット起動で次の接続時に上がり直す)。
  # pkill -f はこのスクリプト自身の SSH セッションにも当たるので、プロセス名で完全一致させる
  pkill -u "$(id -u)" -x gnome-keyring-d || true
  sleep 2
  echo "default keyring -> ci"
fi
busctl --user call "$SECRETS_DEST" /org/freedesktop/secrets org.freedesktop.Secret.Service \
  Unlock ao 1 "$CI_COLLECTION" >/dev/null
alias_now="$(busctl --user call "$SECRETS_DEST" /org/freedesktop/secrets \
  org.freedesktop.Secret.Service ReadAlias s default)"
case "$alias_now" in
  *"$CI_COLLECTION"*) ;;
  *) echo "default alias is not ci: $alias_now" >&2; exit 1 ;;
esac

# 2. Hub の常駐サービス
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SVC.service" <<EOF
[Unit]
Description=Unity Hub on Xvfb (keeps the Personal license access token fresh)
After=network-online.target

[Service]
# A desktop (RDP) login imports its WAYLAND_DISPLAY / AT-SPI bus into the user manager; without
# this the Hub attaches to that session and dies with it. Stay on Xvfb only.
UnsetEnvironment=WAYLAND_DISPLAY DISPLAY AT_SPI_BUS_ADDRESS XDG_SESSION_TYPE
Environment=NO_AT_BRIDGE=1
# The passwordless "ci" keyring is locked after each daemon start; open it without a prompt
ExecStartPre=/usr/bin/busctl --user call $SECRETS_DEST /org/freedesktop/secrets org.freedesktop.Secret.Service Unlock ao 1 $CI_COLLECTION
ExecStart=/usr/bin/xvfb-run -a -s "-screen 0 1280x800x24 -nolisten tcp" $(command -v unityhub) --ozone-platform=x11 --disable-gpu
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable "$SVC.service" >/dev/null
systemctl --user restart "$SVC.service"
echo "$SVC: $(systemctl --user is-active "$SVC.service")"
echo "初回は RDP で入り、端末で hub-login.sh を実行して Unity アカウントにログインする"
