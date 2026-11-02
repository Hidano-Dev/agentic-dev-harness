# 新しいリポジトリでハーネスを動かす(導入手順)

対象: 本リポジトリの配布物を取り込み、Linear 駆動の自律ワーカー(linear-worker)を
定期実行で動かしたいリポジトリ。SDD ワークフロー(kiro commands / dev-orchestrator)は
本リポジトリに含まれないので、使う場合は [unity-sdd-kit](https://github.com/Hidano-Dev/unity-sdd-kit) の
手順で別途導入する(SDD だけ使いたい場合は本手順は不要)。

ファイルのコピー・項目埋め・台帳登録は本リポジトリの Actions(**Onboard Repository**)が
行う。人が手でやるのは「入力フォームを埋める」「PR をマージする」「Routine を作る」
「GitHub App / CI を確認する」だけ。

## 前提(組織・アカウント側で 1 回だけ)

- Linear と GitHub 組織の連携が有効(ブランチ名の Issue ID から PR を自動で紐付け、
  ブランチ作成で In Progress、マージで Done へ遷移させる機能)
- claude.ai の MCP コネクタに Linear が接続済み(Routine から使う)
- **Codex のレビューは Actions で動かす**(`codex-review` ワークフロー。config `review.provider: action`)。
  Codex GitHub App のレビューは使わない — App 側の自動レビューは**無効にする**(有効のままだと 1 ヘッドに
  2 本のレビューが付く)。App を使い続けたいリポジトリだけ config を `review.provider: app` にする
- **ハーネス用の GitHub App**(組織に 1 つ。同期 PR の作成・ワークフロー更新・コンフリクト解消の push・
  ブランチ保護の設定に使う。GITHUB_TOKEN では push で CI が起動せず、`.github/workflows/` も更新できないため):

  1. Organization settings → Developer settings → GitHub Apps → New GitHub App。名前は任意
     (例: `hidano-harness-bot`)、Homepage URL は本リポジトリ、Webhook は Active を外す
  2. Repository permissions:

     | 権限 | レベル | 用途 |
     |---|---|---|
     | Contents | Read and write | 同期 PR・コンフリクト解消・back-merge の push、develop の作成 |
     | Pull requests | Read and write | 同期 PR・Release PR の作成と更新、auto-merge の予約 |
     | Workflows | Read and write | `.github/workflows/` の配布物の更新 |
     | Issues | Read and write | ラベルの作成・付け外し、コメント |
     | Administration | Read and write | 既定ブランチの変更・auto-merge の有効化・branch protection(`configure_repo.py`) |
     | Actions | Read and write | 必須チェックにする CI の推定、回数上限後に人間が Approve した PR の `codex-review` の再実行 |
     | Checks | Read | 必須チェックにする CI の推定 |
     | Metadata | Read | (必須) |

  3. 作成後に Private key を生成し、App を **All repositories**(または対象リポジトリすべて)にインストールする
  4. **組織の Actions secret**(Organization settings → Secrets and variables → Actions。Repository access は
     All repositories)に `HARNESS_APP_ID`(App ID)と `HARNESS_APP_PRIVATE_KEY`(秘密鍵の PEM 全文)を置く
- **組織の Actions secret `OPENAI_API_KEY`**(`codex-review` 用。従量課金)。無いリポジトリではレビューが
  「レビュー不可(fail-open)」になり、他のゲートだけでマージへ進む
- **本リポジトリの Actions secret**(Settings → Secrets and variables → Actions):

  | secret | 用途 | 作り方 |
  |---|---|---|
  | `HARNESS_ONBOARD_TOKEN` | 対象リポジトリへ配布物を push し PR を作る | GitHub の fine-grained PAT。Resource owner は組織(Hidano-Dev)、Repository access は対象になり得るリポジトリ(All repositories でよい)、Permissions は **Contents: Read/Write、Pull requests: Read/Write、Workflows: Read/Write、Metadata: Read**。Workflows 権限が無いと `.github/workflows/harness-sync.yml` を push できない |
  | `LINEAR_API_KEY`(任意) | Linear のチーム存在確認、プロジェクト・ラベルの作成 | Linear の Settings → API → Personal API keys。無ければその部分だけスキップされ、プロジェクトとラベルは手で作る |

- **本リポジトリの Actions variable**(同じ画面の Variables タブ。任意):

  | variable | 用途 |
  |---|---|
  | `LINEAR_DEFAULT_TEAM` | `linear_team` を空欄にしたときに使うチーム名。本リポジトリを clone して使う人は自分のチーム名を置く(ワークフロー本文に固定値は無い) |

## 1. Actions → **Onboard Repository** を実行(自動)

本リポジトリの Actions タブで **Onboard Repository** を選び、フォームを埋めて Run する。

| 入力 | 決めること |
|---|---|
| `target_repo` | `owner/name` |
| `linear_team` | ワーカーが拾うキューのチーム名。空欄なら variable `LINEAR_DEFAULT_TEAM`(どちらも無ければ失敗) |
| `review_bot` | `codex` または `none`。config の `review.bot` を決めるだけで、GitHub 側のレビュー設定(App のインストール・自動レビュー)は変えない |
| `configure_settings` | 既定 on。`develop` を作って既定ブランチにし(元の既定ブランチ `main` はリリースブランチになる)、auto-merge を有効化し、develop / main の branch protection を設定する(`scripts/sync/configure_repo.py`。ハーネス用 GitHub App が必要)。単一ブランチ運用のままにしたい場合だけ off |
| `direct_push` | 既定は PR 作成。新規リポジトリで即反映したいときだけ on。実行ごとの指定で保存されない(次回は既定に戻る) |

フォームに無い項目は規約で決まる。変えたい場合は生成後の `config.json` を編集する:

| 項目 | 規約 |
|---|---|
| `linear.project` | リポジトリ名(`owner/name` の `name`)と同名。無ければワークフローが作る |
| `linear.labels` | `needs-human` / `needs-local` / `needs-unity`。ラベルを付ける判断はワーカーが行う(着手後に実機が必要と分かった Issue に自分で付けて手放す) |
| `checks.fast` | 空。空ならワーカーが CI 定義・`package.json` 等から lint / typecheck / test 相当を推定して実行する。固定したいコマンドがあれば入れる |
| `checks.unity_ci` | null。Unity テストをセルフホストランナーの CI で回すリポジトリでは `{"workflow": "ci.yml", "platform": "linux"}` のように入れる(`docs/onprem-unity-runner.md`)。入れると `needs-unity` の Issue がワーカーの対象になる |

ワークフローが行うこと:

1. **Linear**(`LINEAR_API_KEY` がある場合): チーム名の存在を確認し(無ければ候補一覧を出して失敗する。config に誤った名前が入るのを防ぐ)、リポジトリ名と同名のプロジェクトを確認して無ければ作り(同名が別チームにだけある場合は重複を作らず失敗する)、`needs-human` / `needs-local` のラベルを説明文付きで作る(既にあれば何もしない)
2. **対象リポジトリ**: 配布物(`.claude/skills/linear-worker` `.claude/rules/git-workflow.md`)をコピー、
   `.github/workflows/harness-sync.yml` と `.github/workflows/docs-policy.yml`(集約型ドキュメントを
   リリース用 PR 以外で変更していたら失敗させる)を配置(旧版があれば配布版に差し替える)、ルート `CLAUDE.md` に
   `@.claude/rules/git-workflow.md` を保証、`.kiro/orchestration/config.json` を雛形から
   生成(`auto_merge.enabled` は true)→ `chore: onboard agentic-dev-harness` として PR を作成。
   SDD 一式(`.claude/commands/kiro` 等)・`AGENTS.md`・unity-sdd-template 生成先の
   `orchestration-sync.yml` には触れない
3. **台帳**: `registry/repos.yaml` にエントリを追加して本リポジトリにコミット
   (Routine の ID は後で手順 4 で埋める)
4. **ジョブサマリ**に「残りの手作業」と **Routine 設定シート**(名前・cron・ソース・
   コネクタ・許可ツール・プロンプト本文)を出力

冪等なので再実行してよい。既存の `config.json` / `CLAUDE.md` の import / 台帳エントリは
上書きしない(配布物の同期だけ毎回行う)。

## 2. 導入 PR をマージする

PR の `config.json` を確認してマージする。規約から変えたい項目(プロジェクト名・ラベル名・
`checks.fast` の固定など)があればここで編集する。
`auto_merge.enabled` は雛形どおり **true** で生成される。ゲートを通過した PR は自動でマージされるため、
先にゲートの動きを見たい場合はここで false にして数回まわし、確認してから true に戻す。

## 3. GitHub 側を確認する(手動)

- 導入 PR をマージすると、`harness-sync` が配布物一式(`codex-review` / `policy-gate` / `conflict-sweeper` /
  `promote` 等のワークフローを含む)の**同期 PR を自動で作る**。初回は `policy-gate` がまだ base に無いので
  人間がマージする(以後の同期 PR は配布元 main との完全一致が検証できれば自動でマージされる)
- CI(fast checks 相当)が PR で走ること。`configure_settings` を on にした場合、`configure_repo.py` は
  develop の必須チェックを、`pull_request` で走る既存 CI から推定して設定する。`codex-review` / `policy-gate` /
  `aggregate-docs` は、そのワークフローが develop に入った後(初回の同期 PR のマージ後)に `harness-sync` が
  `configure_repo.py --protection-only` で必須チェックへ足す(`pull_request_target` のワークフローは base に
  入るまで動かないため、先に必須にすると PR が永遠に待ち状態になる)。導入より後に CI を足したら、Rollout(下記)を `configure_settings` 付きで再実行する
- 1 本目の PR で `codex-review` が「指摘なし」または指摘付きの review を 1 件投稿することを確認する
- **Linear の GitHub 連携**(2 段構成のとき): Linear の Settings → Integrations → GitHub の
  ブランチごとのワークフロー設定で、`develop` へのマージを Done(または独自の状態)に、`main` へのマージを
  リリース済みにする。設定しない場合、Issue は develop へのマージ(`Fixes`)で Done になる

## 配布物の更新(自動)

本リポジトリの `main` が更新されると、各取り込み側リポジトリの `harness-sync` が次のいずれかで検知して
同期 PR(ラベル `harness-sync`)を作り、自動マージを予約する。リポジトリを 1 つずつ回る作業は不要。

- 取り込み側の既定ブランチへの push(作業が進んでいるリポジトリはすぐ追従する)
- 毎日 1 回の定期実行(止まっているリポジトリも 1 日以内に追従する)
- Actions 画面からの手動実行

同期 PR は配布物とコアに触れるが、`policy-gate` が「配布元 main の指定コミットを適用した結果と完全一致」を
機械的に確かめられれば人間の承認なしにマージされる(配布元の Release PR で人間がレビュー済みのため)。
config の移行(`migrate_config.py`)による変更もこの検証に含まれる。

**v2 への初回移行**(旧 `harness-sync.yml` は手動実行専用で、GITHUB_TOKEN ではワークフローを更新できない):
本リポジトリの Actions → **Harness Rollout** を `main` から実行する。台帳(`registry/repos.yaml`)の
`sync: workflow` かつ status が active / pilot のリポジトリに同期 PR を作る。`configure_settings` を on にすると
develop の作成・既定ブランチの変更・branch protection も行い、config のブランチ設定(`develop` / `main`)を
書き換える(config はコアなので、その同期 PR は人間の Approve が要る。1 リポジトリにつき 1 回だけ)。

## 4. Routine(定期実行)を作り、台帳に ID を記入する

1. claude.ai/code/routines(または Claude Code の `/schedule`)で、手順 1 のジョブサマリにある
   **Routine 設定シート**の値をそのまま使って作成する。プロンプトは全リポジトリ同文
   (`.claude/skills/linear-worker/templates/routine-prompt.md` の `---` 以下)で置換箇所はない
2. 1 回手動実行し、ログで「config を読んだ → キューを確認した → 空振りまたは着手」の流れを確認する。
   `auto_merge.enabled` が true なら、最初の PR がゲートを通過して自動マージされるところまで見る
   (手順 2 で false にした場合は「マージ承認待ち」で駐機するところまで見てから true に切り替える)。なお `auto_merge.merge_parked`(既定 true)により、
   駐機 PR も次回以降の巡回で条件(P0/P1 未対応なし・CI green・コンフリクトなし・人間の保留なし)を
   満たせばマージされる。駐機 PR を必ず人間がマージしたい場合は `merge_parked` を false にする
3. Actions → **Registry Update** を `name=<リポジトリ名>` `routine_id=<Routine の ID>`
   `config=true` で実行する(台帳に直接コミットされる。停止・一時停止したときも
   `status` をここで更新する)

## Linear 側の運用メモ

- ラベルの意味: `needs-human` — 人間の判断が必要。自律ワーカーは着手しない /
  `needs-local`(config で別名にしてもよい)— ローカル環境・実機・人の目が必要。自律ワーカーはスキップしてユーザーへ通知する /
  `needs-unity` — Unity Editor でのテスト実行が必要。`checks.unity_ci` を設定したリポジトリではワーカーが実装して CI で検証し、
  未設定のリポジトリでは `needs-local` と同じ扱い(`docs/onprem-unity-runner.md`)。
  `LINEAR_API_KEY` を置いていない場合はこれらのラベルとリポジトリ名のプロジェクトを手で作る(`needs-unity` は Onboard Repository では作らない)
- ドキュメントの置き場所(`.claude/rules/git-workflow.md` §7): 開発途中の記録(調査メモ・設計検討・
  後で直す指摘)は Linear の Issue / プロジェクト Document に置き、`README.md` などの集約型ドキュメント
  (config の `docs.aggregate_paths`)はリリース用 PR だけが更新する。反映すべき内容は Issue の
  「ドキュメント反映メモ」と `docs-pending` ラベルで溜まるので、リリース時に `release-docs` ラベルを付けた
  Issue(例: `Release docs for v1.2`)を起票すると、ワーカーがまとめて 1 本の PR にする。
  `docs-pending` / `release-docs` のラベルが無ければワーカーが作る
- Issue の書き方: タイトルは英語、本文は日本語でよい。1 Issue = 1 PR の粒度にする。
  複数 Issue にまたがる設計(1 つの実装単位が複数要件を跨ぐ)は、ワーカーが `needs-human` を
  付けて質問する。SDD(unity-sdd-kit)導入リポジトリでは、spec(`.kiro/specs/`)由来の作業は
  tasks.md のタスク粒度で Issue を切るとワーカーが迷わない

## 運用中の注意

- 配布物(`sync-manifest.json` の `owned`。`.claude/skills/linear-worker/` `.claude/rules/git-workflow.md`
  `.harness/invariants.json`)を実行リポジトリ側で直接編集しない(`policy-gate` が失敗させる)。直す場合は
  本リポジトリを直す。最新化は自動(上記「配布物の更新」)
- 並列度は config の `worker.max_parallel`(雛形 2)。同一リポジトリに Routine を複数立てても、ワーカーは
  claim の数と `領域:` の重なりで調整する。手動のクラウドセッション・worktree での並列作業と併用してよい
- **Release PR**(develop → main)は `promote` が常に 1 本開いておく。本文の先頭にコア・配布物・指示文の
  変更が並ぶので、そこを中心に見て人間がマージする(merge commit。squash しない)
- 既知の制約: `codex-review` と `policy-gate` は `pull_request_target` で base の定義を使い、App の秘密鍵を使う
  再評価は `policy-gate-relabel.yml`(`workflow_run`。既定ブランチの定義)で行うので、PR がそれらを書き換えて secret を読んだりゲートを
  外したりはできない。ただし PR が**新しく足した**ワークフローは PR の定義で動くため、エージェントが**意図的に**
  同名のチェック(`policy-gate` 等)を出して必須チェックを偽装することまでは仕組みで防げない。`.github/` の変更は `policy-gate` が人間の Approve を求め、Release PR の
  先頭にも強調されるので、そこで確認する。より強くしたい場合は CODEOWNERS で `.github/` にコードオーナーの
  レビューを必須にする(配布物のワークフローを更新する同期 PR も人間の承認待ちになる)
- ワーカーが `needs-human` を付けて駐機した Issue は、判断を Issue コメントで返す。
  ラベルは再開したワーカーが外すので手で外さなくてよい(外すと通常の選定対象に戻る)
- コア(config の `auto_merge.protected_paths`。既定 `.github/` `.harness/` `.kiro/orchestration/` `.kiro/settings/`
  `.claude/settings.json` `.codex/`)を変える PR は、人間が Approve するまで `policy-gate` が通さない。
  `CLAUDE.md` `AGENTS.md` などの指示文は、`codex-review` と不変条件(`.harness/invariants.json`)を通れば
  develop へ自動マージされる(2026-11-01 の段階化。既存リポジトリの config も同期時の移行で切り替わる)。
  人間は Release PR でまとめて確認する

## 手動で行う場合(ワークフローが使えないとき)

PAT を用意できない等の理由で Onboard Repository を使えない場合は、同じことを手で行う:

1. `templates/consumer/harness-sync.yml` を対象の `.github/workflows/harness-sync.yml` に、
   `templates/consumer/docs-policy.yml` を `.github/workflows/docs-policy.yml` に置き、harness-sync を
   Actions から 1 回実行する(配布物のコピーと、ルート `CLAUDE.md` への
   `@.claude/rules/git-workflow.md` の追記まで行われる)
2. ルート `CLAUDE.md` に `@.claude/rules/git-workflow.md` の行が入ったことを確認する
3. `.claude/skills/linear-worker/templates/orchestration-config.json` を
   `.kiro/orchestration/config.json` にコピーして値を埋める(ローカルで
   `python3 scripts/onboard/render_config.py --repo owner/name --team <チーム> --out <path>` でも生成できる)
4. Linear にリポジトリ名のプロジェクトとラベル 2 つを作る
5. `registry/repos.yaml` にエントリを追記して PR を出す
