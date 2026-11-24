#!/usr/bin/env bash
# 常駐中の Unity Hub を一時的に止め、画面に出した Hub で Unity アカウントにログインし直す
# 使い方: RDP で入ったデスクトップの端末から hub-login.sh
#
# 常駐の Hub(install-hub-keepalive.sh)は Xvfb 上で動いていて画面が見えない。Hub は 1 プロセスしか
# 起動できない(2 つ目は既存の Hub に処理を渡して終わる)ので、常駐を止めてから画面に出す。
# ログインしたら Hub を「終了」する(ウィンドウを閉じるだけだとトレイに残って戻らない)。
# 終了すると常駐に戻る。ログイン情報はパスワード無しの既定キーリング ci に保存される。
set -euo pipefail
SVC=unityhub-keepalive

if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  echo "画面がありません。RDP で入ったデスクトップの端末から実行してください" >&2
  exit 1
fi

systemctl --user stop "$SVC.service"
trap 'systemctl --user start "$SVC.service"; echo "$SVC: $(systemctl --user is-active "$SVC.service")"' EXIT
echo "Hub を起動します。Sign in でログインし、ライセンス(Preferences > Licenses)が有効か確認したら Hub を終了してください"
unityhub
