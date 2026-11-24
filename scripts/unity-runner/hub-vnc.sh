#!/usr/bin/env bash
# 常駐中の Unity Hub(unityhub-keepalive の Xvfb)の画面を VNC で一時的に公開する
# 使い方(Windows 側から。終わったら VNC を切れば x11vnc も終わる):
#   ssh -L 5901:localhost:5900 unity-ci '~/ci-setup/hub-vnc.sh'
#   → VNC ビューアで localhost:5901 に接続し、Hub で Sign in する
#
# RDP のデスクトップ(GNOME Shell)は Electron/Chrome で落ちることがあるので(2026-11-24)、
# GNOME を使わずに常駐の Hub へ直接ログインする。x11vnc は 127.0.0.1 だけで待ち受け、
# SSH トンネル経由でしか届かない(パスワードは付けない)。前提: sudo apt install x11vnc
set -euo pipefail
SVC=unityhub-keepalive
command -v x11vnc >/dev/null || { echo "x11vnc がありません: sudo apt install x11vnc" >&2; exit 1; }

main="$(systemctl --user show -p MainPID --value "$SVC.service")"
[ "${main:-0}" != 0 ] || { echo "$SVC が動いていません: systemctl --user start $SVC" >&2; exit 1; }
xvfb="$(pgrep -P "$main" -x Xvfb | head -1)"
[ -n "$xvfb" ] || { echo "$SVC の Xvfb が見つかりません" >&2; exit 1; }

# xvfb-run は Xvfb を ":<n> ... -auth <file>" で起動する。その表示番号と認証ファイルを使う
mapfile -d '' args < "/proc/$xvfb/cmdline"
display="" auth=""
for i in "${!args[@]}"; do
  case "${args[$i]}" in
    :[0-9]*) display="${args[$i]}" ;;
    -auth) auth="${args[$((i+1))]}" ;;
  esac
done
[ -n "$display" ] && [ -n "$auth" ] || { echo "Xvfb の表示番号か認証ファイルが分かりません: ${args[*]}" >&2; exit 1; }

echo "VNC: $display を localhost:5900 で公開します(接続を切ると終了)"
exec x11vnc -display "$display" -auth "$auth" -localhost -rfbport 5900 -nopw -once -noxdamage -quiet
