# 新しいリポジトリでハーネスを動かす(導入手順)

対象: 本リポジトリの配布物を取り込み、Linear 駆動の自律ワーカー(linear-worker)を
定期実行で動かしたいリポジトリ。SDD ワークフローだけ使いたい場合は手順 1 と 2 だけでよい
(config.json が無ければ linear-worker は起動せず、Git 運用も従来どおりユーザー承認制になる)。

## 前提(組織・アカウント側で 1 回だけ)

- Linear と GitHub 組織の連携が有効(ブランチ名の Issue ID から PR を自動で紐付け、
  ブランチ作成で In Progress、マージで Done へ遷移させる機能)
- claude.ai の MCP コネクタに Linear が接続済み(Routine から使う)
- 外部レビューボットを使う場合は、対象リポジトリに GitHub App をインストール済み
  (例: Codex。`@codex review` で再トリガーできること)

## 1. 配布物を取り込む

**unity-sdd-template から生成したリポジトリ**: 生成時に取り込まれている。最新化は
Actions タブの `Orchestration Sync` を実行する。

**それ以外のリポジトリ**: `templates/consumer/harness-sync.yml` を
`.github/workflows/harness-sync.yml` として置き、Actions タブから 1 回実行する
(ローカルで `.claude .codex .kiro .agents AGENTS.md` を手動コピーしてもよい)。
`AGENTS.md` に独自の追記があるなら `SYNC_PATHS` から `AGENTS.md` を外す。

ルート `CLAUDE.md` に次の行があることを確認する(無ければ追記):

```
@.claude/rules/sdd-workflow.md
```

## 2. `.kiro/orchestration/config.json` を作る

`.claude/skills/linear-worker/templates/orchestration-config.json` をコピーして値を埋める。

| キー | 決めること |
|---|---|
| `linear.team` / `linear.project` | ワーカーが拾うキュー。project は null 可 |
| `linear.labels.needs_human` / `needs_local` | 手順 3 で作るラベル名。`needs_local` は「ローカル環境・実機が無いと検証できない」の意味で、リポジトリに合わせて命名してよい(unity-renderer は `needs-unity`) |
| `github.repo` / `default_branch` | `owner/name` とデフォルトブランチ |
| `checks.fast` | 一次ゲートでローカル実行するコマンド(lint / typecheck / test)。空のまま自動マージを有効にしない |
| `review.bot` | 外部レビューボット。無ければ `null` |
| `auto_merge.enabled` | **最初は false** で数回まわし、ゲートの動きを確認してから true にする |
| `auto_merge.protected_paths` | ワーカーが自分で変えてはいけないパス(既定のままでよい) |

このファイルはリポジトリにコミットする(同期で上書きされない)。

## 3. Linear 側を整える

1. チーム(または既存チーム内のプロジェクト)を用意し、`config.json` の値と一致させる
2. ラベルを 2 つ作る(説明文も付ける):
   - `needs-human` — 人間の判断が必要。自律ワーカーは着手しない
   - `needs-local`(名前は任意)— ローカル環境・実機が必要。自律ワーカーはスキップしてユーザーへ通知する
3. Issue の書き方: タイトルは英語、本文は日本語でよい。1 Issue = 1 PR の粒度にする。
   複数 Issue にまたがる設計(1 つの実装単位が複数要件を跨ぐ)は、ワーカーが `needs-human` を
   付けて質問する。spec(`.kiro/specs/`)由来の作業は tasks.md のタスク粒度で Issue を切ると
   ワーカーが迷わない

## 4. GitHub 側を整える

- CI(fast checks 相当)を PR で走らせる。CI が無い場合、二次ゲート手順 4 は「CI なし」として記録される
- ブランチ保護で「未解決のレビュースレッドがあるとマージ不可」を有効にしておくと、
  ワーカーの手順 6(スレッドの明示 resolve)と整合する
- 外部レビューボットを使う場合はインストールし、PR に対して動くことを 1 回確認する

## 5. Routine(定期実行)を作る

claude.ai/code/routines(または Claude Code の `/schedule`)で作成する。

| 項目 | 値 |
|---|---|
| 名前 | 例: `自律駆動ハーネス (<repo>)` |
| cron | 1 時間間隔(Routine の最小間隔)。例: `52 * * * *`(UTC) |
| ソース | `https://github.com/<owner>/<repo>` |
| プロンプト | `.claude/skills/linear-worker/templates/routine-prompt.md` の `---` 以下をそのまま貼る(置換箇所なし。全リポジトリ同文なのでメモアプリ等に保存して使い回せる。対象リポジトリは Routine のソース、チームは config.json から決まる) |
| MCP コネクタ | Linear(必須)。Notion 等は任意 |
| 許可ツール | Bash / Read / Write / Edit / Glob / Grep / WebFetch / WebSearch に加え、`/code-review` `/kiro:validate-impl` `/kiro:spec-impl` の起動に必要な Skill / SlashCommand / Task(Agent) |
| 通知 | push 通知を有効にしておくと空振り・駐機の報告が届く |

作成後に 1 回手動実行し、ログで「config を読んだ → キューを確認した → 空振りまたは着手」の
流れを確認する。`auto_merge.enabled` を false にしたまま最初の PR がゲートを通過して
「マージ承認待ち」で駐機するところまで見てから、true に切り替える。

## 6. 台帳に登録する

`registry/repos.yaml` にリポジトリ・Linear・Routine(名前・cron・ID)・status を追記して
本リポジトリに PR を出す。停止・一時停止したときも status を更新する。

## 運用中の注意

- 配布物(`.claude/` など)を実行リポジトリ側で直接編集しない。直した場合は本リポジトリへ戻す
- Routine が同時に 2 つ以上動く構成にしない(並行度 1 が前提。複数リポジトリはそれぞれ
  別 Routine でよいが、同一リポジトリに 2 本立てない)
- ワーカーが `needs-human` を付けて駐機した Issue は、判断を Issue コメントで返す。
  ラベルは再開したワーカーが外すので手で外さなくてよい(外すと通常の選定対象に戻る)
- `.claude/` `.github/` `.kiro/settings/` を変える PR は自動マージされない。人間がマージする
