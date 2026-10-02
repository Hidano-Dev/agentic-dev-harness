# agentic-dev-harness

Linear をタスクキューにして PR 作成〜マージまでを定期実行で進める
**自律ワーカー(linear-worker)** と、それが従う **Git 運用ルール**を、複数リポジトリへ配布・管理するための
正本リポジトリ。実行リポジトリの台帳と導入(オンボーディング)の自動化も持つ。

旧 `orchestration-development-template` の後継(履歴をそのまま引き継いでいる)。
かつては Spec-Driven Development(SDD)ワークフロー一式(kiro commands / dev-orchestrator /
Codex skills / `.kiro/settings` / `AGENTS.md`)も配布していたが、2026-10 に
[unity-sdd-kit](https://github.com/Hidano-Dev/unity-sdd-kit) へ移管した(`docs/changelog.md`)。
本リポジトリは SDD に依存せずに動く。SDD を使うリポジトリは unity-sdd-kit を併せて導入する。

## リポジトリの役割分担

| リポジトリ | 役割 | 本リポジトリとの関係 |
|---|---|---|
| **agentic-dev-harness**(本リポジトリ) | 配布物(`.claude/skills/linear-worker/` `.claude/rules/git-workflow.md`)の正本。実行リポジトリの台帳(`registry/`)と導入手順(`docs/`) | — |
| [unity-sdd-kit](https://github.com/Hidano-Dev/unity-sdd-kit) | SDD ワークフロー一式(kiro commands / dev-orchestrator / Codex skills / `.kiro/settings` / `AGENTS.md`)の正本。Unity 向けの拡張を含む | 独立。取り込み側では両方の同期ワークフローが共存する(所有パスが重ならない) |
| [unity-project-template](https://github.com/Hidano-Dev/unity-project-template) | 新規 Unity プロジェクトのリポジトリ生成テンプレート(設定済み Unity プロジェクト同梱・Unity バージョン管理・Unity CLI) | 独立(SDD / ワーカーを持たない)。生成後に必要なものを導入する |
| 実行リポジトリ(unity-renderer など) | 実際に開発が進む場所。配布物を取り込み、`.kiro/orchestration/config.json` でリポジトリ固有の値を持つ | `registry/repos.yaml` に登録。Routine(定期実行)がここで linear-worker を起動する |

原則: **配布物の修正は本リポジトリで行い、実行リポジトリでは直接編集しない**。
実行リポジトリ側で緊急に直した場合は、同じ変更を本リポジトリへ戻す(戻し忘れると次回の同期で消える)。

## 何が入っているか

```
.claude/
├─ rules/
│   └─ git-workflow.md … ブランチ命名・push・レビュー待ち・マージ判断(Linear 連携 / 自動マージは config で切替)。
│                        linear-worker への入口。CLAUDE.md から @import される
└─ skills/
    └─ linear-worker/    … Linear 駆動の自律ワーカー(選定・claim・レビューゲート・自動マージ・駐機)
        ├─ scripts/      … config.json の移行スクリプト(migrate_config.py)
        └─ templates/    … harness 設定の雛形(orchestration-config.json)と Routine プロンプト雛形
CLAUDE.md               … 本リポジトリ用。取り込み側は自分の CLAUDE.md から @.claude/rules/git-workflow.md を import する

registry/repos.yaml     … 実行リポジトリの台帳(配布されない)
templates/consumer/     … 取り込み側リポジトリに置く同期ワークフロー(配布されない)
scripts/onboard/        … 導入ワークフローが使うスクリプト(配布されない)
scripts/unity-runner/   … オンプレ Unity CI ランナー(Ubuntu)の構築・登録スクリプト(配布されない。docs/onprem-unity-runner.md)
docs/                   … 導入手順・運用メモ(配布されない)
.github/workflows/
├─ onboard-repo.yml     … Onboard Repository: 新しいリポジトリへの導入をフォーム入力 1 回で行う
└─ registry-update.yml  … Registry Update: 台帳の Routine ID / config / status を更新
```

## 配布の仕組み

取り込み側は本リポジトリの main を shallow clone し、本リポジトリが所有するパス
(`.claude/skills/linear-worker` `.claude/rules/git-workflow.md`)だけをコピーする
(`templates/consumer/harness-sync.yml`)。

- 所有ディレクトリは丸ごと本リポジトリの管理下なので、本リポジトリで削除したファイルは
  取り込み側からも削除される。それ以外(自作 skill、`.claude/settings.json`、SDD 一式、
  `.kiro/specs/` `.kiro/steering/` `.kiro/orchestration/`)には触れない
- ルート `CLAUDE.md` はコピーしない。`@.claude/rules/git-workflow.md` の import 行が無ければ
  同期時に末尾へ追記する(それ以外の内容には触れない)
- `.kiro/orchestration/config.json` の `auto_merge` には、配布元が定義した移行
  (`.claude/skills/linear-worker/scripts/migrate_config.py`)を同期のたびに未適用分だけ適用する
- 本リポジトリは公開(public)なので、GitHub Actions はトークンなしで clone できる —
  **秘匿情報を本リポジトリに置かないこと**

## リポジトリ固有の設定(`.kiro/orchestration/config.json`)

配布物はリポジトリ固有の値(Linear のチーム名、ラベル名、テストコマンド、自動マージの可否など)を
持たない。実行リポジトリは `.kiro/orchestration/config.json` を 1 つ置き、
linear-worker(キュー・ゲート・自動マージ)がそれを読む(SDD 導入時は dev-orchestrator も
確認チャネルを読む)。雛形は `.claude/skills/linear-worker/templates/orchestration-config.json`、
キーの意味は `linear-worker/SKILL.md` の「設定」節。

このファイルが無いリポジトリでは、linear-worker は起動せず、git-workflow は
「Linear 連携なし・自動マージなし(従来どおりユーザー承認)」として振る舞う。

## 新しいリポジトリでハーネスを動かす

本リポジトリの Actions で **Onboard Repository** を実行する(対象リポジトリ・Linear チーム・
外部レビューボットをフォームで入力)。配布物のコピー・同期ワークフローの配置・
`CLAUDE.md` の import・`config.json` 生成を PR にし、Linear のラベル作成と台帳登録まで行う。
残る手作業は PR のマージ、CI / レビューボットの確認、Routine の作成(ジョブサマリの設定シートを
貼る)、**Registry Update** での Routine ID 記入。詳細と前提の secret は `docs/onboarding.md`。

SDD ワークフローも使う場合は、unity-sdd-kit の手順で別途導入する。

## メンテナンス

- linear-worker・Git 運用ルールの改修はここで行い、PR でレビューしてから main にマージする
  (本リポジトリ自身は linear-worker の対象外。Codex のレビューを受ける)
- SDD コマンド・スキル・ルール(cc-sdd 本体の更新取り込みを含む)は unity-sdd-kit で行う
- 実行リポジトリへの反映は各リポジトリの `Harness Sync` ワークフローを手動実行する。
  自動追従したい場合はワークフローの `schedule` を有効化する
- 実行リポジトリを追加・停止したら `registry/repos.yaml` を更新する(追加は Onboard Repository が、
  Routine ID・status の変更は Registry Update が行う。手で編集して PR を出してもよい)
