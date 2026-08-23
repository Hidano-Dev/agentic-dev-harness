---
name: dev-orchestrator
description: >-
  cc-sdd (Kiro-style Spec-Driven Development) の全工程（spec-init → requirements →
  design → tasks → implementation → validation）をエンドツーエンドで自動オーケストレーションする。
  各フェーズの承認をポリシーに基づき代行し、人間の判断が必要なものだけをユーザーに確認する。
  ユーザーが「最後まで開発して」「全自動で開発して」「開発をオーケストレーションして」
  「SDDフローを全部回して」「要件から実装まで一気にやって」のように依頼したときに使用する。
  単一フェーズだけの依頼（要件だけ作りたい、設計だけ見直したい等）にはこの Skill を使わず、
  対応する /kiro:* コマンドを直接使うこと。
argument-hint: "<feature-description | feature-name> [--stop-after <requirements|design|tasks|implementation>] [--brownfield|--greenfield]"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Task, SlashCommand, AskUserQuestion
---

# Dev Orchestrator

cc-sdd の各フェーズコマンド（`/kiro:*`）を正しい順序で呼び出し、フェーズ間の**承認ゲートを代行**する
Orchestrator 型 Skill。

## Critical Rules（最初に読むこと）

1. **実作業を自分でやらない**。要件・設計・タスク・実装の生成はすべて `/kiro:*` コマンド
   （内部で専用 SubAgent に委譲される）に任せる。Orchestrator が持つのは
   「順序制御・承認判断・エスカレーション・進行ログ」だけ
2. **`-y`（自動承認フラグ）はゲート通過の結果としてのみ付与する**。ゲート判定前に付けてはならない
3. **ユーザーへの確認・通知は必ず `references/confirmation-channels.md` の手順を経由する**。
   AskUserQuestion をこの手順の外で直接呼ばない（確認チャネルを将来 Slack / Discord に
   差し替えるための単一チョークポイント）
4. **ゲート判定基準は `references/approval-policy.md` に従う**。判定に迷ったときのデフォルトは
   エスカレーション（ユーザー確認側に倒す）
5. **すべてのゲート判定とエスカレーションを進行ログに記録する**（監査証跡・再開用の状態源）

## Instructions

### Step 1: 引数解釈と再開判定

1. `$ARGUMENTS` が `.kiro/specs/` に存在する feature 名 → **再開モード**。
   `spec.json` の `phase` / `approvals` と `.kiro/orchestration/<feature>/log.md` から現在地を判定し、
   未完了の最初のフェーズから続行する
2. それ以外の文字列 → **新規モード**。ただし Phase 1 に進む前に**マルチ Spec 判定**を行う:
   説明文が「requirements → design → tasks → 実装を他と独立に回せる単位」を 2 つ以上含む場合は
   Phase 1 に進まず、`/kiro:spec-split`（分割方針を議論したセッション内で実行）→ `/clear` →
   `/kiro:spec-init-batch` の経路を案内して停止する。判定に迷う場合は Step 4 の確認手順で
   分割の要否をユーザーに確認する。ただしこの時点では feature 名も進行ログも未作成のため、
   `references/confirmation-channels.md` の**設計ルール 5（pre-log confirmation）**に従い
   `feature: (未確定)` / `gate: none` で組み立て、回答は Step 2 のログ作成時に最初のエントリとして
   記録する（分割経路へ抜けた場合は完了報告に明記する）。単一 Spec と判定したら Phase 1 から開始する
3. オプション:
   - `--stop-after <phase>`: 指定フェーズのゲート通過後に停止して報告
     （実装前に止めたい場合は `--stop-after tasks`、PR を作らない場合は `--stop-after implementation`）
   - `--brownfield` / `--greenfield`: validate-gap の強制実行 / スキップ。
     未指定時はリポジトリに既存実装コードがあるかで自動判定する

### Step 2: 進行ログの初期化

`.kiro/orchestration/<feature>/log.md` を `templates/orchestration-log.md` から作成する
（新規モードでは feature 名が確定する spec-init 直後に作成し、Phase 1 と Gate S の結果を
最初のエントリとして記録する。再開モードでは既存ログを読み、追記を続ける）。
以降、各フェーズ完了ごとに追記する:

- フェーズ名 / 実行コマンド / 結果サマリ（1〜3行）
- ゲート判定: `AUTO-APPROVED`（根拠） / `ESCALATED`（質問と回答） / `REJECTED`（差し戻し理由）

ログは追記のみで書き換えない。

### Step 3: フェーズ・パイプラインの実行

各コマンドは SlashCommand ツールで起動する。

| # | フェーズ | 実行 | Reviewer | ゲート |
|---|---------|------|----------|--------|
| 0 | Steering 確認 | `.kiro/steering/` の存在確認のみ | — | 空なら注記して続行（新規作成はしない） |
| 1 | 初期化 | `/kiro:spec-init "<description>"` | — | **Gate S**（着手前スコープ確認・必須） |
| 2 | 要件定義 | `/kiro:spec-requirements <f>` | brownfield 時 `/kiro:validate-gap <f>` | **Gate A** |
| 3 | 設計 | `/kiro:spec-design <f> -y` | `/kiro:validate-design <f>` | **Gate B** |
| 4 | タスク分解 | `/kiro:spec-tasks <f> -y` | セルフチェック（ポリシー参照） | **Gate C** |
| 5 | 実装 + 検証 | `/kiro:spec-run <f>`（validate-impl 内蔵） | spec-run 内蔵 | **Gate D**（完了判定） |
| 6 | PR 作成 | ブランチ push + PR 作成（下記） | — | **Gate E** |

**Gate S（着手前スコープ確認）**: 新規モードでは spec-init 完了直後に、**必ず 1 回**ユーザーに
スコープ確認を行う（承認代行の対象外。Step 4 と同じ確認手順を使う）。確認内容: 生成された
feature 名 / 解釈したスコープの要約（含む・含まない）/ 単一 Spec としての妥当性
（複数 Spec に分割すべき規模でないか）/ brownfield 判定 / 停止予定フェーズ。
選択肢は `references/approval-policy.md` の Gate S 節に従う。「続行」「中止」以外の回答は
次の 2 系統に分けて扱う:

- **スコープ修正**（単一 Spec のまま範囲を直す）→ 説明を修正して spec-init からやり直し、再度 Gate S を行う
- **「複数 Spec に分割すべき」**（Step 1 の事前判定が誤りだった場合）→ **spec-init からやり直してはならない**。
  オーケストレーションをここで停止し、`/kiro:spec-split` → `/clear` → `/kiro:spec-init-batch` の経路へ誘導する。
  このとき spec-init が作成済みのスタブ（`.kiro/specs/<feature>/` の spec.json と requirements.md のみ。
  ユーザー入力を含まないテンプレート展開物）を**削除する**（残すと spec-init-batch の一意性チェックで
  `-2` サフィックスのゴミディレクトリが生えるため）。進行ログを作成済みなら停止理由を追記し、
  未作成なら完了報告に確認内容・回答・削除したパスを明記する

**この確認を通過したら、以降のゲートはポリシーに基づく代行モードで進む**
（記事の「最終確認後は全自動」に相当）。再開モードでは進行ログに Gate S 通過記録があればスキップする。

**Phase 5 の前処理**: 現在のブランチがデフォルトブランチ（main 等）の場合、実装コミットを
直接積まないよう `feature/<feature-name>` ブランチを作成して切り替えてから spec-run を実行する。
すでに作業ブランチ上ならそのまま使う。使用ブランチ名を進行ログに記録する。

**Phase 6（PR 作成）の手順**:

1. Gate E（`references/approval-policy.md`）を判定する
2. 通過したら: `git push -u origin <branch>` → `gh pr create` で PR を作成する
   - タイトル: feature 名 + 1行要約
   - 本文: スコープ要約（spec.json / requirements.md の冒頭から）、spec-run のタスク結果テーブル、
     validate-impl の要約、`.kiro/specs/<feature>/` への参照
   - リポジトリに PR テンプレートがあればその構成に従う
3. `gh` CLI が使えない環境では push まで行い、PR 作成用の URL とタイトル・本文案を
   完了報告に含めて縮退する（失敗ではなく正常な縮退）
4. PR の URL を進行ログに記録する

**各フェーズの共通手順**:

1. フェーズコマンドを実行し、SubAgent のサマリを受け取る
2. Reviewer（対になる validate コマンド）を実行する
3. `references/approval-policy.md` の該当ゲート基準で判定する:
   - **PASS** → 進行ログに `AUTO-APPROVED` と根拠を記録し、次フェーズへ
     （Gate A 通過 → `spec-design -y`、Gate B 通過 → `spec-tasks -y`。
     Gate C 通過時は spec.json を直接更新: `approvals.tasks.approved: true`、
     `ready_for_implementation: true`）
   - **修正可能な指摘あり** → 指摘をフィードバックとして同フェーズの生成コマンドを再実行
     （merge モード）。**再実行は各フェーズ最大 2 回**。解消しなければエスカレーション
   - **エスカレーション条件に該当** → Step 4 の手順でユーザーに確認し、回答に従う

### Step 4: ユーザー確認（エスカレーション）

`references/confirmation-channels.md` の手順に従い、チャネル非依存の確認リクエストを組み立てて
送信する。回答を進行ログに記録してから再開する。確認をスキップして先に進んではならない。

### Step 5: Gate D（完了判定）→ Phase 6 → 報告

- spec-run のサマリ（タスク別 OK/FAIL/TIMEOUT、validate-impl の DECISION と指摘）を受け取る
- 全タスク OK かつ validate-impl が **GO** → Gate D 通過。Phase 6（PR 作成）へ進む
- validate-impl が **MANUAL_VERIFY_REQUIRED** → Gate D **不通過**（重大な指摘の有無にかかわらず、
  必須検証が実行不能で完了を主張できない状態）。エスカレーションし、不足している検証手順・
  環境前提を提示して、手動検証するか「現状で受け入れて PR 化する」かをユーザーが決める
- FAIL/TIMEOUT あり、または validate-impl が **NO-GO**・重大な指摘 → **自動修正ループには入らず**
  エスカレーション（spec-run の無人実行方針を踏襲。修正して再実行するかはユーザーが決める。
  「現状で受け入れて PR 化する」をユーザーが選んだ場合のみ Phase 6 へ進む）

**完了報告の内容**（1つのメッセージで。進行ログにも同内容を追記）:

1. feature 名と最終フェーズ
2. 各ゲートの判定結果（AUTO-APPROVED / ESCALATED の別と件数）
3. spec-run のタスク結果テーブルと validate-impl の要約（実施した場合）
4. PR の URL（作成した場合）またはブランチ名と PR 作成手順（縮退時）
5. 残課題・ユーザーに委ねた判断の一覧
6. 次のアクション（あれば。PR 作成済みなら「PR レビューは人間が行う」ことを明記）

## コンテキスト管理ルール

- 成果物（requirements.md / design.md / tasks.md）は**全文を読み込まない**。承認判断は
  SubAgent のサマリと Reviewer の検証結果を一次情報とし、必要な場合のみ該当セクションだけを Read する
- フェーズ間の情報受け渡しはすべて `.kiro/specs/<feature>/` 配下のファイル経由で行う
  （SubAgent が書き、次の SubAgent が読む）。Orchestrator は「完了した事実と判定結果」だけを保持する

## 実行環境の前提と縮退

- **ローカル実行が前提**。Phase 5 は `codex exec` と `claude -p` を使う（分担・フォールバック手順は
  `/kiro:spec-run` 側に定義済み。本 Skill はそれを呼ぶだけで再定義しない）
- Reviewer（`/kiro:validate-gap` / `/kiro:validate-design` / `/kiro:validate-impl`）も codex-first
  （各コマンド側に定義済み。`codex` 不在・失敗時は Claude サブエージェントに自動フォールバック、確認不要）
- `codex` 不在時は spec-run 自身が `claude -p` のみのモードに自動降格する（確認不要）
- `claude` CLI もネスト起動できない環境（クラウド等）では、Gate C までを完走して停止し、
  「ローカルで `/kiro:spec-run <feature>` を実行してください」と報告する（失敗ではなく正常な縮退）

## Examples

**Example 1: 新規機能をフル自動で開発**

ユーザー: 「CSVインポート機能を最後まで開発して」

1. 新規モードと判定 → `/kiro:spec-init "CSVインポート機能..."` → feature 名取得
2. **Gate S**: 解釈したスコープ（含む: アップロード/検証/取込、含まない: エクスポート）を
   ユーザーに 1 回確認 → OK → 以降は承認代行モード
3. 進行ログ作成 → Phase 2〜4 をゲート判定しながら進行（既存コードありなので validate-gap 実施）
4. Gate B で validate-design が「新規依存ライブラリ追加」を検出 → エスカレーション →
   ユーザーが承認 → 続行
5. feature ブランチ作成 → `/kiro:spec-run <feature>` → 全タスク OK → Gate D 通過
6. **Phase 6**: push + PR 作成 → PR URL を含めて完了報告

**Example 2: 実装前まで進めて止める**

ユーザー: 「通知設定画面、タスク分解まで自動で進めて」

→ `--stop-after tasks` 相当。Gate C 通過後に停止し、
「実装は `/kiro:spec-run <feature>` で開始できます」と報告。

**Example 3: 中断からの再開**

ユーザー: 「csv-import の開発を続きから」

→ `.kiro/specs/csv-import/` が存在 → 再開モード。spec.json と進行ログから
「design 承認済み・tasks 未生成」と判定 → Phase 4 から続行。

## Troubleshooting

**症状**: spec-run が連続失敗ガードで打ち切られた
**対応**: Gate D の失敗系としてエスカレーション。打ち切り理由（直近の失敗ログの要点）を
確認リクエストに含める。自動でリトライしない。

**症状**: Reviewer が 2 回連続 NO-GO
**対応**: 自分で設計判断を上書きせずエスカレーション。NO-GO 理由の要約と選択肢
（差し戻し / 要件フェーズへ戻る / 指摘を許容して続行）を提示する。

**症状**: フェーズコマンドがハードエラー（spec ディレクトリ不整合等）
**対応**: 原因を特定して修正を 1 回試みる。解消しなければエスカレーション。

**症状**: `.kiro/steering/` が空
**対応**: 停止しない。進行ログに注記し、完了報告で `/kiro:steering` の実行を推奨する。
