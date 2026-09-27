# 配布物の変更履歴

取り込み側は上書きマージで追従するため、**削除・改名**を伴う変更はここに明記し、
取り込み側で手動追従が必要なものを分かるようにする。

## 2026-09-27 — 導入(オンボーディング)を GitHub Actions 化

配布物の変更はない(本リポジトリ側の運用ツールのみ)。

- `.github/workflows/onboard-repo.yml`(**Onboard Repository**)— フォーム入力 1 回で、対象リポジトリへの
  配布物コピー・`harness-sync.yml` 配置・`CLAUDE.md` の import 行・`config.json` 生成を PR にし、
  Linear のチーム / プロジェクト確認とラベル作成(`LINEAR_API_KEY` がある場合)、`registry/repos.yaml` への
  登録、Routine 設定シートの出力までを行う。`linear_team` 空欄で SDD ワークフローのみの導入
- `.github/workflows/registry-update.yml`(**Registry Update**)— Routine ID・config・status を台帳へ直接反映
- `scripts/onboard/` — 上記が使うスクリプト(標準ライブラリのみの Python。ローカルでも実行可)
- `templates/consumer/harness-sync.yml` — 同期先にシンボリックリンクがあればコピー前に失敗するステップ
  (リンク先の無関係なファイルを上書きしない)と、同期後に `config.json` の JSON 妥当性・プレースホルダ残り・
  `CLAUDE.md` の import 行を検査して警告するステップを追加
- `docs/onboarding.md` — 手順をワークフロー前提に書き換え(手動手順は末尾に残置)

**取り込み側で必要な手動追従**

- 既存の実行リポジトリは `harness-sync.yml` を新版に差し替えると検査ステップが有効になる(任意)

## 2026-09-25 — Routine プロンプト雛形からプレースホルダを廃止

- `.claude/skills/linear-worker/templates/routine-prompt.md` — `{{GITHUB_REPO}}` / `{{LINEAR_TEAM}}` を
  無くし、`---` 以下をそのまま貼れる同文にした。対象リポジトリは Routine のソース設定、Linear の
  チーム / プロジェクトは `.kiro/orchestration/config.json` から決まるため、プロンプトに書く必要がない。
  推奨許可ツールに Skill / SlashCommand / Task を追加(§4 一次ゲートの `/code-review` 等を起動するため)
- `docs/onboarding.md` — 手順 5 のプロンプト・許可ツールの説明を上記に合わせた

**取り込み側で必要な手動追従**

- 既存の Routine のプロンプトを新しい本文に貼り替える(旧プロンプトのままでも動作は同じ。
  `linear.team` の有無を前提ガードに含めた点だけが差分)

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
