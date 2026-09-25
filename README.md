# agentic-dev-harness

Claude Code / Codex で回す **Spec-Driven Development(SDD)ワークフロー**と、
Linear をタスクキューにして PR 作成〜マージまでを定期実行で進める
**自律ワーカー(linear-worker)** を、複数リポジトリへ配布・管理するための正本リポジトリ。

旧 `orchestration-development-template` の後継(履歴をそのまま引き継いでいる)。
SDD 一式に linear-worker・Linear 連携付き Git 運用ルール・実行リポジトリ台帳・導入手順を加えたもの。

## リポジトリの役割分担

| リポジトリ | 役割 | 本リポジトリとの関係 |
|---|---|---|
| **agentic-dev-harness**(本リポジトリ) | 配布物(`.claude/` `.codex/` `.kiro/settings/` `.agents/` `AGENTS.md`)の正本。実行リポジトリの台帳(`registry/`)と導入手順(`docs/`) | — |
| [unity-sdd-template](https://github.com/Hidano-Dev/unity-sdd-template) | 新規 Unity プロジェクトのリポジトリ生成テンプレート(設定済み Unity プロジェクト同梱・Unity バージョン管理) | 生成時(Template Init)と同期時(Orchestration Sync)に本リポジトリの main から配布物を取り込む。SDD / ワーカー関連の実体は持たない |
| 実行リポジトリ(unity-renderer など) | 実際に開発が進む場所。配布物を取り込み、`.kiro/orchestration/config.json` でリポジトリ固有の値を持つ | `registry/repos.yaml` に登録。Routine(定期実行)がここで linear-worker を起動する |

原則: **配布物の修正は本リポジトリで行い、実行リポジトリでは直接編集しない**。
実行リポジトリ側で緊急に直した場合は、同じ変更を本リポジトリへ戻す(戻し忘れると次回の同期で消える)。

## 何が入っているか

```
.claude/
├─ commands/kiro/      … /kiro:* コマンド(spec-init / requirements / design / tasks / impl / run / validate-* ほか)
├─ agents/kiro/        … 各コマンドが使う Claude サブエージェント
├─ rules/
│   ├─ sdd-workflow.md … SDD の進め方(CLAUDE.md から @import される入口)
│   └─ git-workflow.md … ブランチ命名・push・レビュー待ち・マージ判断(Linear 連携 / 自動マージは config で切替)
└─ skills/
    ├─ dev-orchestrator/ … spec-init → 実装 → PR を承認ゲート付きで自動オーケストレーション
    └─ linear-worker/    … Linear 駆動の自律ワーカー(選定・claim・レビューゲート・自動マージ・駐機)
        └─ templates/    … harness 設定の雛形(orchestration-config.json)と Routine プロンプト雛形
.codex/agents/          … Codex 用エージェント定義
.agents/skills/kiro-*/  … Codex 用 SDD スキル(canonical skill)
.kiro/settings/         … spec / steering のテンプレートと生成ルール
AGENTS.md               … Codex 用プロジェクトメモリ(配布される)
CLAUDE.md               … 本リポジトリ用。取り込み側は自分の CLAUDE.md から @.claude/rules/sdd-workflow.md を import する

registry/repos.yaml     … 実行リポジトリの台帳(配布されない)
templates/consumer/     … 取り込み側リポジトリに置く同期ワークフロー(配布されない)
docs/                   … 導入手順・運用メモ(配布されない)
```

## 配布の仕組み

取り込み側は本リポジトリの main を shallow clone し、`.claude .codex .kiro .agents AGENTS.md` を
ファイル単位で上書きコピーする(`templates/consumer/harness-sync.yml`)。

- ルート `CLAUDE.md` はコピーしない。取り込み側は自分の `CLAUDE.md` に
  `@.claude/rules/sdd-workflow.md` の 1 行を書けば SDD メモと Git 運用ルールが読み込まれる
- 上書きマージなので、取り込み側が独自に追加したファイル(自作 skill、`.claude/settings.json`、
  `.kiro/specs/`、`.kiro/steering/`、`.kiro/orchestration/`)は残る。本リポジトリ側で**削除**した
  ファイルは自動では消えないので、削除は `docs/` の変更履歴に明記して手動で追従する
- `AGENTS.md` に独自の追記があるリポジトリ(例: artgraph の節)は、同期対象から `AGENTS.md` を
  外す(`harness-sync.yml` の `SYNC_PATHS`)。本リポジトリは公開(public)なので、GitHub Actions は
  トークンなしで clone できる — **秘匿情報を本リポジトリに置かないこと**

## リポジトリ固有の設定(`.kiro/orchestration/config.json`)

配布物はリポジトリ固有の値(Linear のチーム名、ラベル名、テストコマンド、自動マージの可否など)を
持たない。実行リポジトリは `.kiro/orchestration/config.json` を 1 つ置き、
dev-orchestrator(確認チャネル)と linear-worker(キュー・ゲート・自動マージ)がそれを読む。
雛形は `.claude/skills/linear-worker/templates/orchestration-config.json`、キーの意味は
`linear-worker/SKILL.md` の「設定」節。

このファイルが無いリポジトリでは、linear-worker は起動せず、git-workflow は
「Linear 連携なし・自動マージなし(従来どおりユーザー承認)」として振る舞う。

## 新しいリポジトリでハーネスを動かす

`docs/onboarding.md` を参照(配布物の取り込み → config 作成 → Linear のラベル・プロジェクト →
Routine 作成 → 台帳登録)。Routine のプロンプトは
`.claude/skills/linear-worker/templates/routine-prompt.md` の雛形を使う(全リポジトリ同文)。

## メンテナンス

- SDD コマンド・スキル・ルールの改修はここで行い、PR でレビューしてから main にマージする
  (本リポジトリ自身は linear-worker の対象外。Codex のレビューを受ける)
- cc-sdd 本体の更新取り込み(`npx cc-sdd@latest` 等)もここで行う
- 実行リポジトリへの反映は各リポジトリの `Harness Sync`(または `Orchestration Sync`)ワークフローを
  手動実行する。自動追従したい場合はワークフローの `schedule` を有効化する
- 実行リポジトリを追加・停止したら `registry/repos.yaml` を更新する
