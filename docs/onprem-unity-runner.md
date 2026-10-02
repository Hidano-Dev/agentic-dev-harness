# オンプレ Unity CI ランナー(Ubuntu)

Unity Editor でのテスト実行が必要な Issue(`needs-unity`)を自律ワーカーの対象にするための、
自宅 LAN 上の Ubuntu 機を GitHub Actions のセルフホストランナーにする手順と運用メモ。
2026-10-03 に構築した(決定の経緯は末尾)。

ワーカー(Routine)は claude.ai のクラウドで動き、LAN 上の機械には届かない。そこで Unity の
検証は **CI 経由**にする: ワーカーは Unity を起動せずに実装・push し、PR の CI がランナー上で
Unity Test Runner を回し、その結果(CI green)を linear-worker の二次ゲート(SKILL.md §4 手順 4)で
検証証跡として使う。

```
Routine (cloud) ─ 実装・push ─→ GitHub ─ CI job (runs-on: [self-hosted, linux-unity]) ─→ Ubuntu
       ↑                                                    │ Unity batchmode + Xvfb
       └──── gh run view / 結果 XML・ログ(artifact)を読んで判定 ←┘
```

## 構成(2026-10-03 時点)

| 項目 | 値 |
|---|---|
| ホスト | `hidano-Precision-Tower-7910`(192.168.0.101)、Ubuntu 24.04、Xeon E5-2687W v4 ×2(48 スレッド)、188 GB、5.5 TB |
| GPU | Quadro M4000。NVIDIA のカーネルモジュールは未ビルド(kernel 7.0 系 HWE に `dkms` 未導入)。テストは Xvfb(ソフトウェア描画)で回しており GPU は未使用 |
| Unity | `~/Unity/Hub/Editor/6000.3.19f1`(FacialControl)、`~/Unity/Hub/Editor/6000.0.36f1`(oscdesk)。Hub でログインした **Personal ライセンス**(`~/.config/unity3d/Unity/licenses/`)。batchmode で追加の認証は不要 |
| ツール(`~/.local`、sudo 不要) | gh / git-lfs / pwsh 7.4 / node 22 + corepack(pnpm) / .NET 8 SDK / actions-runner |
| ランナー | リポジトリ単位。`~/runners/<repo>/`、名前 `unity-ci-ubuntu-<repo>`、ラベル `unity`, `linux-unity`。user systemd(`actions-runner-<repo>.service`、linger 有効)で常駐 |
| 登録済み | `Hidano-Dev/FacialControl`、`Hidano-Dev/oscdesk` |
| SSH | Windows 機の `~/.ssh/config` に `Host unity-ci`(専用鍵 `unity_ci_ubuntu`) |

Organization レベルのランナーにしなかった理由: Hidano-Dev は Free プランで、ランナーグループの
「public リポジトリに許可」設定が使えず、public の FacialControl / oscdesk から使えない。
また登録に `admin:org` スコープが要り、OAuth のスコープはアカウント単位で他の Organization にも
及ぶため採らなかった。リポジトリ単位なら `repo` スコープで登録トークンを発行できる。

## 構築手順

### 1. 前提(人の作業)

- Ubuntu 機に SSH 鍵で入れること(Claude Code からは非対話で入るため、パスフレーズ無しの専用鍵)
- Unity Hub を入れ、GUI でログインして Personal ライセンスを有効化しておく(1 回だけ。RDP が有効)
- `Xvfb` / `xvfb-run` が入っていること(`sudo apt install xvfb`)

### 2. Unity のインストール(sudo 不要)

Hub のヘッドレス CLI で、対象リポジトリの `ProjectSettings/ProjectVersion.txt` と同じバージョンを入れる
(changeset も同ファイルにある)。別バージョンで開くとプロジェクトが書き換わるので、バージョンは厳密に合わせる。

```bash
xvfb-run -a unityhub --headless install --version 6000.0.36f1 --changeset 9fe3b5f71dbb
xvfb-run -a unityhub --headless editors --installed
```

### 3. ツール類(sudo 不要)

```bash
scp scripts/unity-runner/server-setup-userlocal.sh unity-ci:~/ci-setup/
ssh unity-ci 'bash ~/ci-setup/server-setup-userlocal.sh'
```

`~/.local/opt` に展開し `~/.local/bin` にリンクする。`~/.bashrc` の先頭(非対話シェルでも読まれる位置)に
PATH / `DOTNET_ROOT` / `UNITY_EDITORS_ROOT` を書く。冪等。

### 4. ランナーの登録(リポジトリごと)

登録トークンは 1 時間で失効する。発行とサーバーでの登録を 1 行で:

```bash
gh api -X POST repos/Hidano-Dev/<repo>/actions/runners/registration-token --jq .token \
  | ssh unity-ci 'bash ~/ci-setup/register-runner.sh <repo> "$(cat)"'
```

`register-runner.sh` は `~/actions-runner`(展開済みの本体)を `~/runners/<repo>/` にコピーして
`config.sh --unattended --replace` で登録し、ジョブ用の環境(`.env` / `.path`: `UNITY_EDITORS_ROOT`、
`DOTNET_ROOT`、`~/.local/bin`)を書き、user systemd のサービスを enable --now する。
`loginctl enable-linger` 済みなので再起動後も自動で上がる。

セルフホストランナーの登録は「GitHub からこの機械でコードを実行できる口を開ける」操作なので、
Claude Code の自動モードでは実行が止められる。**この 1 行は人が実行する**。

```bash
gh api repos/Hidano-Dev/<repo>/actions/runners --jq '.runners[] | .name + " " + .status'   # online を確認
ssh unity-ci 'systemctl --user status actions-runner-<repo>'
```

### 5. Unity パッケージの private git 依存

`Packages/manifest.json` に `git+ssh://git@github.com/...` の依存があると、ランナーの SSH 鍵で
取りに行く。FacialControl は `NHidano/Mikunote_Models`(モデル資産)を参照しているので、
ランナーの公開鍵(`~/.ssh/id_ed25519.pub`)をそのリポジトリの **read-only deploy key** に登録する。
GitHub のホスト鍵は `gh api meta --jq '.ssh_keys[]'` から `~/.ssh/known_hosts` に入れてある
(`ssh-keyscan` ではなく公式 API の値を使う)。

```bash
ssh unity-ci 'ssh -T git@github.com'   # "Hi <owner>/<repo>!" なら deploy key が効いている
```

### 6. CI ワークフロー側

FacialControl の `.github/actions/run-unity-tests`(composite action)を参照。要点:

- `runs-on: [self-hosted, linux-unity]`、`shell: bash`、`xvfb-run -a "$UNITY_EDITORS_ROOT/<ver>/Editor/Unity" -batchmode -nographics -projectPath ... -runTests ...`
- `-quit` は付けない(付けるとテストが走らない)。終了コード 0 = 成功、2 = 失敗したテストあり
- 起動前に `ProjectVersion.txt` とエディタのバージョン一致を確認する(漂流の検出)
- ランナーは 1 台で全ジョブが同じ作業ディレクトリを使うため、`actions/checkout` は `clean: false` にし、
  `git clean -ffdx -e <project>/Library` で **Library(import キャッシュ)だけ残す**。初回だけ全 import が走る
- public リポジトリでは fork からの PR でセルフホストのジョブを起動しない
  (`github.event.pull_request.head.repo.full_name == github.repository`)。リポジトリ設定の
  「外部コントリビューターの workflow は承認必須」と二重にする
- 結果 XML と Unity ログ(`-logFile`)を artifact に上げる。ワーカーはこれを読んで直す

### 7. ハーネス側の設定

対象リポジトリの `.kiro/orchestration/config.json`:

```json
"linear": { "labels": { "needs_unity": "needs-unity" } },
"checks": { "unity_ci": { "workflow": "ci.yml", "platform": "linux" } }
```

`checks.unity_ci` を入れると、linear-worker は `needs-unity` の Issue を候補に含め、CI の Unity ジョブを
検証証跡にする(SKILL.md §1 / §3 / §4)。null のままなら `needs-local` と同じ扱い。
Linear 側では「Unity で検証できれば済む」Issue を `needs-unity`、「Windows 固有・実機・人の目が要る」Issue を
`needs-local` に分ける。

## 運用メモ・落とし穴

- **`pkill -f "runTests"` は自分の SSH セッションも殺す**(コマンド文字列に同じ語が入る)。`pkill -f "[r]unTests"` にする
- `curl ... | grep -m1` は grep が先に閉じて curl が 23 で落ちる。`set -o pipefail` のスクリプトでは JSON を python3 で全量読む
- Unity は `-nographics` でもディスプレイを要求するので `xvfb-run -a` で包む。`ssh-askpass` が無いので、
  git 依存の解決でホスト鍵の確認が出ると止まる(事前に known_hosts へ)
- Windows 固有の挙動(パス区切り、ProRes 等の Windows 専用 API)は Linux の CI では検出できない。
  そういう Issue は `needs-local` のままにする。必要になったら Windows 機にもランナーを立てる(2026-10-03 時点では見送り)
- GPU を使う(実描画のテスト)なら `sudo apt install dkms nvidia-driver-580` でモジュールをビルドする。
  モニタ非接続は原因ではない(ヘッドレスで動く)
- 2 台目のランナーを同じリポジトリに足すと Unity のジョブが並列になり、同じ `_work` を共有しないので
  Library も別になる(ディスクは十分)。CI 待ちが長くなったら検討する
- `unattended-upgrades` が有効。カーネル更新で NVIDIA モジュールが外れることはあるが Xvfb 運用には影響しない

## 決定の経緯(2026-10-03)

- 止まっていた Issue: HID-5(unity-renderer、Windows 専用の ProRes 書き出し)、HID-61/62/64(oscdesk、EditMode 検証と
  実装)、HID-68/69/70/71(FacialControl、失敗テストの修正)。このうち Windows 固有の HID-5 / HID-70 は当面手動、
  それ以外を Linux CI で自動化する
- Unity は Personal ライセンス前提(シリアルによる batchmode 有効化は不可。Hub ログインで作った
  ライセンスファイルが batchmode でもそのまま使えることを確認した)
- 将来: Ubuntu 上で `claude -p` による linear-worker を cron 実行し、push → CI 待ちの往復なしに
  `needs-unity` のキューをローカルで回す案(Phase 4)。CI 経由の運用が安定してから判断する
