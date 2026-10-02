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
- 外部レビューボットを使う場合は、対象リポジトリに GitHub App をインストール済み
  (例: Codex。`@codex review` で再トリガーできること)
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
   `.github/workflows/harness-sync.yml` を配置(旧版があれば配布版に差し替える)、ルート `CLAUDE.md` に
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

- CI(fast checks 相当)が PR で走ること。CI が無い場合、二次ゲート手順 4 は「CI なし」として記録される
- ブランチ保護で「未解決のレビュースレッドがあるとマージ不可」を有効にしておくと、
  ワーカーの手順 6(スレッドの明示 resolve)と整合する(private リポジトリでは有料プランが必要)
- 外部レビューボットを使う場合は PR に対して動くことを 1 回確認する

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
- Issue の書き方: タイトルは英語、本文は日本語でよい。1 Issue = 1 PR の粒度にする。
  複数 Issue にまたがる設計(1 つの実装単位が複数要件を跨ぐ)は、ワーカーが `needs-human` を
  付けて質問する。SDD(unity-sdd-kit)導入リポジトリでは、spec(`.kiro/specs/`)由来の作業は
  tasks.md のタスク粒度で Issue を切るとワーカーが迷わない

## 運用中の注意

- 配布物(`.claude/skills/linear-worker/` `.claude/rules/git-workflow.md`)を実行リポジトリ側で
  直接編集しない。直した場合は本リポジトリへ戻す。最新化は実行リポジトリの Actions で `Harness Sync` を実行する
- Routine が同時に 2 つ以上動く構成にしない(並行度 1 が前提。複数リポジトリはそれぞれ
  別 Routine でよいが、同一リポジトリに 2 本立てない)
- ワーカーが `needs-human` を付けて駐機した Issue は、判断を Issue コメントで返す。
  ラベルは再開したワーカーが外すので手で外さなくてよい(外すと通常の選定対象に戻る)
- `.claude/` `.github/` `.kiro/settings/` `.kiro/orchestration/` `.agents/` `.codex/` とルートの `CLAUDE.md` `AGENTS.md` を変える PR は自動マージされない。人間がマージする

## 手動で行う場合(ワークフローが使えないとき)

PAT を用意できない等の理由で Onboard Repository を使えない場合は、同じことを手で行う:

1. `templates/consumer/harness-sync.yml` を対象の `.github/workflows/harness-sync.yml` に置き、
   Actions から 1 回実行する(配布物のコピーと、ルート `CLAUDE.md` への
   `@.claude/rules/git-workflow.md` の追記まで行われる)
2. ルート `CLAUDE.md` に `@.claude/rules/git-workflow.md` の行が入ったことを確認する
3. `.claude/skills/linear-worker/templates/orchestration-config.json` を
   `.kiro/orchestration/config.json` にコピーして値を埋める(ローカルで
   `python3 scripts/onboard/render_config.py --repo owner/name --team <チーム> --out <path>` でも生成できる)
4. Linear にリポジトリ名のプロジェクトとラベル 2 つを作る
5. `registry/repos.yaml` にエントリを追記して PR を出す
