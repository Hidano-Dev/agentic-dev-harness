# 配布物の変更履歴

取り込み側は上書きマージで追従するため、**削除・改名**を伴う変更はここに明記し、
取り込み側で手動追従が必要なものを分かるようにする。

## 2026-09-25 — agentic-dev-harness として再編

orchestration-development-template を履歴ごと引き継ぎ、以下を追加・変更した。

**追加**

- `.claude/skills/linear-worker/` — Linear 駆動の自律ワーカー(unity-renderer で試験運用した版を汎用化)。
  チーム・ラベル・チェックコマンド・自動マージ可否を `.kiro/orchestration/config.json` から読む
- `.claude/skills/linear-worker/templates/orchestration-config.json` — 上記 config の雛形
- `.claude/skills/linear-worker/templates/routine-prompt.md` — Routine プロンプトの雛形
- `registry/` `templates/consumer/` `docs/` — 台帳・同期ワークフロー・導入手順(配布対象外)

**変更**

- `.claude/rules/git-workflow.md` — ブランチ命名に Linear Issue ID を組み込む規約(Linear 連携ありの場合)、
  レビュー待機期限と再トリガー、linear-worker のフェイルオープン例外、自動マージ条件(config で有効化した
  場合のみ)を追加。config が無いリポジトリでは従来どおり(`<type>/<topic>`、マージはユーザー承認)
- `.claude/rules/sdd-workflow.md` — Development Rules に codex-first validate と linear-worker の参照を追記
- `.claude/skills/dev-orchestrator/SKILL.md` — Phase 5 のブランチ作成を git-workflow の規約に委ね、
  Linear 連携ありなら Issue ID 付きブランチ + In Progress 更新を行う

**取り込み側で必要な手動追従**

- なし(削除・改名したファイルは無い)。unity-renderer の独自改修版 kiro commands との差分は
  `registry/repos.yaml` の notes を参照
