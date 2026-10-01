# 配布物の変更履歴

取り込み側は上書きマージで追従するため、**削除・改名**を伴う変更はここに明記し、
取り込み側で手動追従が必要なものを分かるようにする。

## 2026-10-01 — Harness Sync が config の auto_merge 移行を適用する

- `.claude/skills/linear-worker/scripts/migrate_config.py`(配布物・新設)— 既存の
  `.kiro/orchestration/config.json` に配布元の設定変更を移行として 1 度ずつ適用する。対象は `auto_merge` のみ。
  適用済みの ID は config の `applied_migrations` に記録し、その後に人間が値を戻しても再適用しない
  - `2026-10-01-protect-orchestration-config`: `auto_merge.protected_paths` に `.kiro/orchestration/` を追加
    (キーが無ければ既定の 3 パスごと書く)
  - `2026-10-01-auto-merge-enabled`: `auto_merge.enabled` を true にする。`protected_paths` に既定の保護パス
    (`.claude/` `.github/` `.kiro/settings/` `.kiro/orchestration/`)がすべて入っているときだけ適用する
    (保護が欠けたまま自動マージが始まらないように。独自に保護を外した config は有効化を見送り、警告する)
  - config の形が想定外で適用できない移行は記録せずに警告し、修正後の同期で再試行する
- `templates/orchestration-config.json` — `applied_migrations` に全移行の ID を入れる(新規生成した config は
  移行済み扱いになり、導入 PR で人間が決めた値を同期が上書きしない)
- `templates/consumer/harness-sync.yml` — コピー後に上記スクリプトを実行するステップを追加。
  取り込み側の `harness-sync.yml` が配布版と異なる場合に警告を出す(ワークフロー自体は `GITHUB_TOKEN` では
  更新できないため同期されない)
- `.claude/skills/linear-worker/SKILL.md`(配布物)— 設定表に `applied_migrations` を追加

**取り込み側で必要な手動追従**

- `.github/workflows/harness-sync.yml` を新しい `templates/consumer/harness-sync.yml` に差し替えてから
  Harness Sync を実行する。実行時に移行が適用され、変更は `chore: sync harness assets` として
  デフォルトブランチへ直接コミットされる(適用内容は Actions の notice に出る)。
  **`auto_merge.enabled` を意図して false にしていたリポジトリも 1 度 true になる**ので、
  false のままにしたい場合は同期後に false へ戻す(以降の同期では上書きされない)
- unity-sdd-template 生成先(`orchestration-sync.yml` で追従)はこのステップを持たないため、
  `config.json` を手で更新する(上記 2 項目)

## 2026-10-01 — `protected_paths` の既定に `.kiro/orchestration/` を追加

- `templates/orchestration-config.json` — `auto_merge.protected_paths` の既定に `.kiro/orchestration/` を追加。
  `auto_merge` 等を書き換える PR(config 自体の変更)がパス判定で自動マージ・巡回マージされないようにする
- `.claude/skills/linear-worker/SKILL.md` / `.claude/rules/git-workflow.md`(配布物)、`docs/onboarding.md` — 既定値の記述を更新

**取り込み側で必要な手動追従**

- 上記「Harness Sync が config の auto_merge 移行を適用する」を参照(既存 config へは移行として届く)

## 2026-10-01 — 雛形の `auto_merge.enabled` を true に変更

- `templates/orchestration-config.json` — `auto_merge.enabled` の既定を false → true に変更
  (キーが無い config は従来どおり false として扱う)
- `.claude/skills/linear-worker/SKILL.md`(配布物)— 設定表の既定値を更新
- `docs/onboarding.md` / `.github/workflows/onboard-repo.yml` / `scripts/onboard/routine_sheet.py` —
  「最初は false のまま」の案内を、true で生成されることと、先にゲートを観察したい場合は false にする手順へ変更

**取り込み側で必要な手動追従**

- 上記「Harness Sync が config の auto_merge 移行を適用する」を参照(既存 config へは移行として届く)

## 2026-10-01 — 駐機 PR の巡回で CI 失敗・マージコンフリクトも修正し、条件を満たせばマージする

- `.claude/skills/linear-worker/SKILL.md`(配布物)
  - §1-A: 巡回の判定に「マージ可否(`mergeable` / `mergeStateStatus`)」と「現在ヘッドの CI」を追加。
    P1 以上の指摘・PR 起因の CI 失敗・マージコンフリクトのいずれかがあれば再 claim し、
    コンフリクト解消 → CI 修正 → 指摘対応の順に 1 回の作業でまとめて直して push・再駐機する
  - コンフリクトはデフォルトブランチを `git merge` して解消する(rebase・force-push はしない)。
    方針判断が要る衝突、原因不明・PR の範囲で直せない CI 失敗は直さず `駐機理由: escalation` で人間へ返す
  - CI の一過性失敗は失敗ジョブを 1 ヘッドあたり 1 回だけ再実行(成果物に数えない)。
    デフォルトブランチでも落ちている既存の失敗はスコープ外として報告のみ
  - 安全弁の回数を `P1 修正連続` から `巡回修正連続` に改名し、CI・コンフリクトの修正 push も通算
    (旧形式の記録は同じ値として読む)
  - §5 スナップショットに駐機 PR の CI 状態・コンフリクト有無を併記。§6 に push 済みブランチの rebase 禁止を明記
  - §1-A 巡回マージ(新設): 要修正が無く、P0/P1 未対応なし・未解決スレッドなし・現在ヘッドの CI green・
    コンフリクトなし・外部レビュー Completed(または待機期限超過)・人間の保留なしを満たす `merge-approval`
    駐機 PR は、人間の承認を待たずにマージしてブランチを削除する。`protected_paths` 該当 PR と、
    同じ起動で修正 push した PR は対象外
  - §4: 自動マージ後にヘッドブランチを削除する(`--delete-branch`)
- `templates/orchestration-config.json` — `auto_merge.merge_parked`(既定 true。キーが無い場合も true)を追加
- `templates/routine-prompt.md` — 上記をプロンプトに明記。**Routine のプロンプトを貼り直すこと**
- `.claude/rules/git-workflow.md`(配布物)— §6 の駐機 PR 巡回の記述に CI 失敗・コンフリクト・巡回マージを追記
- `docs/onboarding.md` — 初回観察中も駐機 PR が巡回マージされ得ることを追記

**取り込み側で必要な手動追従**

- 各 Routine のプロンプトを新しい `routine-prompt.md` の `---` 以下に差し替える
- Routine の許可ツールの Bash で `gh run view` / `gh run rerun` / `gh pr checks` / `gh pr merge` が使えること
- **既存の駐機 PR も巡回マージの対象になる**(config にキーが無ければ true 扱い)。駐機 PR を必ず人間が
  マージしたいリポジトリは `.kiro/orchestration/config.json` の `auto_merge.merge_parked` を false にする

## 2026-09-30 — 駐機 PR のレビュー巡回と状態スナップショット

- `.claude/skills/linear-worker/SKILL.md`(配布物)
  - §1-A(新設): 新規選定の前に、マージ承認待ちで駐機している PR を巡回する。駐機後に届いた
    P1 以上の指摘(人間の具体的な修正指示を含む)は再 claim して修正・push・返信し、再び駐機して
    マージ判断を人間へ返す。P2 以下は一括棄却。修正 push は 1 起動 1 Issue の実作業に数える
  - §3 駐機手順: claim 解放の追記に `駐機理由: <merge-approval|requirements|no-go|escalation|scope>`
    を併記する(巡回対象は `merge-approval` のみ)
  - §5: 終了時に最新状態のスナップショット(人間の対応待ち・進行中・実機待ち・次の候補・この起動の
    結果)を Linear のプロジェクトのステータス更新として投稿する。Linear プロジェクトの Overview に
    リンクされた Notion ページは、この起動で古くなった箇所だけを元と同程度以下の文字数で書き換える
    (記述を足さない。詳細は Linear 側に書く)
- `templates/orchestration-config.json` — `reporting.linear_status` / `reporting.notion`(既定とも true)を追加
- `templates/routine-prompt.md` — 上記 2 点をプロンプトに明記。**Routine のプロンプトを貼り直すこと**
- `.claude/rules/git-workflow.md`(配布物)— §6 に駐機 PR の巡回を追記

**取り込み側で必要な手動追従**

- 各 Routine のプロンプトを新しい `routine-prompt.md` の `---` 以下に差し替える
- Notion を更新させる場合は、Linear プロジェクトの Overview に Notion ページのリンクを 1 つ置き、
  Routine の MCP コネクタに Notion を入れる(config の変更は不要。`reporting` キーが無くても既定値で動く)
- この変更以前に駐機した PR は `駐機理由` を持たない。判断依頼コメントがマージ承認のみなら
  `merge-approval` とみなして巡回する

## 2026-09-27 — Onboard Repository の入力を規約化して削減

- `.github/workflows/onboard-repo.yml` — フォーム入力を `target_repo` / `linear_team` / `sdd_only` /
  `review_bot` / `direct_push` の 5 つに削減。`linear_team` 空欄は variable `LINEAR_DEFAULT_TEAM` に
  フォールバック(SDD のみ導入は `sdd_only` で明示)。`linear_project` は廃止してリポジトリ名に固定、
  `needs_local_label` は `needs-local` 固定、`checks_fast` は空固定、`sync_agents_md` はマーカー判定に置換
- `scripts/onboard/linear_setup.py` — リポジトリ名のプロジェクトが無ければ作成する(同名が別チームに
  だけある場合は失敗)
- `AGENTS.md`(配布物)— 先頭にマーカー行 `managed-by: agentic-dev-harness` を追加。取り込み側の
  `AGENTS.md` にマーカーが無ければ独自ファイルとみなし上書きしない
- `templates/consumer/harness-sync.yml` — 上記マーカー判定を追加(`SYNC_PATHS` から `AGENTS.md` を
  外す運用は不要になった)
- `.claude/skills/linear-worker/SKILL.md`(配布物)— §4 手順 1: `checks.fast` が空なら CI 定義・
  `package.json` 等から lint / typecheck / test 相当を推定して実行し、実行したコマンドを記録する
  (従来は「該当なし」で素通り)

**取り込み側で必要な手動追従**

- 独自の `AGENTS.md` を持つリポジトリ(unity-renderer)は、`harness-sync.yml` を新版に差し替えれば
  `SYNC_PATHS` から `AGENTS.md` を外す運用をやめてよい(旧版のままでも除外設定が効くので安全。
  旧版で `AGENTS.md` を含めたまま独自化していた場合は、Onboard Repository の再実行が除外へ書き換える)。
  配布版をそのまま使っているリポジトリは、旧版の `harness-sync.yml` を残すと次回の同期で
  マーカー付きの新版 `AGENTS.md` に置き換わる(内容は同じ)
- unity-sdd-template 生成先の `orchestration-sync.yml` は同じマーカー判定を持たない。独自の
  `AGENTS.md` を持つ生成先はループから外す運用を続ける(テンプレート側の追従は別途)

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
