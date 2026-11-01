# Git Workflow

作業開始からマージまでの Git 運用ルール。

Linear 連携・自動マージ・ブランチ構成など「リポジトリごとに有無が変わる」項目は
`.kiro/orchestration/config.json`(雛形: `.claude/skills/linear-worker/templates/orchestration-config.json`)
の設定で切り替える。ファイルが無いリポジトリでは Linear 連携なし・自動マージなし・単一ブランチ運用として扱う。

- Linear をタスクキューとする自律運用(定期実行)は `.claude/skills/linear-worker/SKILL.md` に従う。
  対象チーム・ラベル・自動マージ可否などリポジトリ固有の値は `.kiro/orchestration/config.json` に置き、
  スキル本文には書かない。
- 本文中の dev-orchestrator / `/kiro:*` / `.kiro/specs/` に関する規定は、SDD ワークフロー
  ([unity-sdd-kit](https://github.com/Hidano-Dev/unity-sdd-kit))が導入されているリポジトリでのみ適用する。
  SDD が無いリポジトリでは「spec 由来の実装」は発生しないため、該当する例外・条件は読み飛ばしてよい。

## 0. 全体像(2026-11-01 決定)

待機・判定・マージはエージェントのセッションではなく **GitHub 側の仕組み**が受け持つ。
エージェントの仕事は「実装 → push → 自動マージの予約 → 届いた指摘への対応」で、レビューの到着を
長時間待ったり、コメントやリアクションの意味を解釈したりしない。

| 役割 | 担当 |
|---|---|
| レビュー | `codex-review` ワークフロー(Actions で Codex を 1 ヘッド 1 回実行し、結果を 1 件の PR review として投稿) |
| 保護パス・不変条件の判定 | `policy-gate` ワークフロー |
| コンフリクト解消 | `conflict-sweeper` ワークフロー(文書の追記同士の衝突を自動で解消。残りはエージェントへ) |
| マージ | GitHub のネイティブ auto-merge(必須チェック green + 未解決スレッドなしで GitHub がマージ) |
| 終端の人間レビュー | `promote` ワークフローが維持する Release PR(作業ブランチ → リリースブランチ) |
| 配布物の更新 | 各リポジトリの `harness-sync` ワークフローが配布元の更新を検知して同期 PR を作る |

**ブランチ構成**: config の `github.release_branch` が設定されているリポジトリ(既定の新規導入)は 2 段構成。

- **作業ブランチ** = `github.default_branch`(既定 `develop`。GitHub の既定ブランチ)。エージェントの PR は
  すべてここへ出し、ゲートを通ればエージェントの裁量で(人間の承認なしに)マージされる。
- **リリースブランチ** = `github.release_branch`(既定 `main`)。作業ブランチからの Release PR だけが入り、
  **人間がレビューしてマージする**。エージェントはリリースブランチへ直接 PR を出さない・マージしない。

`release_branch` が null のリポジトリは単一ブランチ運用(`default_branch` に PR を出し、§6 の条件でマージ)。

## 1. 作業開始時: ブランチ確認

- 作業に着手する前に `git branch --show-current` で現在のブランチを確認する。
- 出力が空の場合は detached HEAD(CI の checkout やコミット指定の作業環境等)。この場合は
  `git symbolic-ref refs/remotes/origin/HEAD`(取得できなければ `gh repo view --json defaultBranchRef`)で
  既定ブランチを特定し、リモートと同期したうえでそこから作業ブランチを作成する
  (detached HEAD のまま実作業コミットを積まない)。
- **起点は作業ブランチ**(config の `github.default_branch`。無ければ GitHub の既定ブランチ)。
  リリースブランチ(`github.release_branch`)から作業ブランチを切らない(hotfix も作業ブランチ経由で
  Release PR に乗せる)。
- 作業ブランチ / リリースブランチの上にいる場合は、実作業コミットを積む前に作業内容に応じた名前でブランチを切る。
- 他人・他作業のブランチ上と判断した場合は、**その場で分岐しない**(先行作業のコミットが
  新しい PR に混入するため)。`git fetch origin` のうえ `git switch -c <branch> origin/<作業ブランチ>` で
  作業ブランチの最新から切る。ただし、その先行作業の続き・修正を依頼されている場合はそのブランチをそのまま使ってよい。
- 命名規約(SDD 導入時は dev-orchestrator もこの規約に従う):
  - `<type>`: 機能追加 `feature` / バグ修正 `fix` / ドキュメント・設定等の雑務 `chore` または `docs`
  - `<topic>`: 英小文字ケバブケース。ブランチ名全体を英語で書き、日本語を含めない
  - **Linear 連携あり**(config に `linear.team` がある): `<type>/<issue-id>-<topic>`。
    `<issue-id>` は対応する Linear Issue の識別子を小文字にしたもの(例: `hid-12`)。
    Linear はブランチ名に含まれる Issue ID から PR を自動で紐付けるため、必ず含める。
    例: `feature/hid-13-mov-prores4444`、`fix/hid-6-remove-manual-ffmpeg`。
    対応する Linear Issue がない作業は、ブランチを切る前に Linear へ Issue を起票する
    (PR にしない使い捨ての検証作業は除く)。Issue 作成は Linear MCP から行える。
    **例外**: Routine 等のクラウドセッションで実行環境が作業ブランチ(`claude/...` 等)を
    割り当てている場合は、割り当てブランチをそのまま使い、PR タイトルの Issue ID・本文の
    `Fixes <ISSUE-ID>`・Linear MCP での PR 添付で紐付けを補う
    (`.claude/skills/linear-worker/SKILL.md` §2 手順 4〜5)。
  - **Linear 連携なし**: `<type>/<topic>`(例: `feature/spec-run-retry`)
- Linear 連携ありの場合、Issue タイトルと PR タイトルは英語で書く(本文・説明は日本語でよい)。
- Linear 連携ありの場合、ブランチを切ったら Linear MCP で対応 Issue のステータスを
  **In Progress** に更新する(push するまで Issue の状態は自動では変わらないため)。
- すでに適切な作業ブランチ上にいる場合はそのまま使う。

## 2. コミット

- 変更内容を簡潔にまとめたコミットメッセージを付ける(1 行目で「何をしたか」が分かること)。
- 無関係な変更は 1 コミットに混ぜず、意味のある単位で分割する。

## 3. Push と自動マージの予約

- 作業が一段落したら(タスク完了・レビュー依頼可能な状態になったら)リモートへ push する。
  - 初回: `git push -u origin <branch>`
- PR が未作成なら `gh pr create --base <作業ブランチ>` で作成する(draft にしない。draft の間は
  `codex-review` が走らない)。
- **自動マージの予約**: §6 で自動マージが許されるリポジトリ・PR では、PR 作成直後に
  `gh pr merge <PR> --auto --<auto_merge.method> --match-head-commit <SHA>` を実行する。以後のマージは
  GitHub が必須チェック(CI・`codex-review`・`policy-gate` 等)と未解決スレッドなしを確認して行う。
  エージェントはマージの瞬間に立ち会わない。予約できない(リポジトリで auto-merge が無効・ブランチ保護が
  無い)場合は §6 の手動判定に戻る。
- **例外**: push / PR 作成は、実行中のワークフローまたはユーザーがそれを許可している場合に限る。
  (SDD 導入時の)dev-orchestrator の `--stop-after implementation` のように「PR を作らない」指定がある実行や、
  ユーザーがローカル作業のみを求めている場合は、push / PR 作成を行わず完了報告に留める。

## 4. レビュー

- **dev-orchestrator の除外(SDD 導入時)**: dev-orchestrator(Phase 6)が作成した PR はこの節の待機の
  対象外。PR URL を即時報告して完了とする(ユーザーまたはワークフロー側で明示的に指示された場合のみ待機する)。

### 4-1. レビューは Actions が 1 ヘッド 1 回行う(config `review.provider` = `action`)

- PR の作成・push のたびに `codex-review` ワークフローが自動で走る。エージェントはレビューを**依頼しない**
  (`@codex review` 等のコメントを投稿しない)。同じ PR で前の実行が走っていれば新しいヘッドの実行が
  取り消して置き換えるので、1 ヘッドに対するレビューは常に 1 回。
- ワークフローは差分を見てレビューの範囲を決める(`review_plan.py`):
  - **省略**: 軽微な変更(テキストのみ・コメントのみ。`classify_change.py` が fast track と判定)、または
    前回レビュー済みのヘッドから「PR 自身の差分」が変わっていない(作業ブランチの取り込み・コンフリクト解消だけ)
  - **全体**: 初回
  - **差分のみ**: 2 回目以降。前回の指摘が直ったかと、変更した行の P0/P1 だけを見る(毎回新しい P2 が
    出続けて収束しない事態を防ぐ)
  - **上限**: config の `review.action.max_rounds`(既定 3)回レビューした PR は、それ以上レビューしない
- 結果は**指摘の有無にかかわらず 1 件の PR review** として投稿される。P0/P1 は該当行のスレッド、P2 以下は
  review 本文の一覧(スレッドを作らない)。`codex-review` のチェックは「このヘッドのレビューが完了した」ことを
  示し、指摘があっても成功になる(マージを止めるのは未解決の P0/P1 スレッド)。
- Codex が応答しない・API キーが無い等でレビューできなかった場合、config の `review.fail_open`(既定 true)なら
  その旨を PR に記録してチェックを成功させる(他のゲートでマージへ進む)。false ならチェックを失敗させ、
  人間が対応する。
- **状態の確認**: `python3 .claude/skills/linear-worker/scripts/review_status.py --pr <N>` が
  `pending / running / clean / findings / skipped / unavailable / timeout` を JSON で返す。エージェントは
  コメントやリアクションを自分で読み解かず、この出力で判断する。
- **待ち方**: 指摘への対応をこのセッションで済ませたい場合だけ、Monitor / ScheduleWakeup 等で 5〜10 分間隔に
  `review_status.py` を見る(短間隔のビジーポーリング・bash の `while sleep` ループはしない)。上限は config の
  `review.wait_minutes`(既定 60 分。現在ヘッドの push から数える)。上限に達したら待つのをやめて終了してよい —
  自動マージは予約済みなので、指摘が無ければ GitHub がマージし、指摘があれば後続の定期実行
  (linear-worker の巡回)が拾う。**レビューの到着を待つためだけにセッションを占有しない**。

### 4-2. Codex GitHub App を使うリポジトリ(config `review.provider` = `app`。後方互換)

- レビュー要の push のたびに、push したエージェント自身が config の `review.bot.request_comment`
  (例: `@codex review`)を 1 回だけ投稿する。依頼の直前に、そのヘッドへのレビューが開始・完了していれば投稿しない。
- 完了の判定は `review_status.py --provider app` に任せる。ボットの review・summary コメントに加え、
  **指摘ゼロのときにボットが付ける 👍 リアクション**(依頼コメントまたは PR 本文)も「完了・指摘なし」として扱う。
  👀 は「実行中」。
- 依頼から 30 分経ってもそのヘッドへのレビューが開始していなければ、1 回だけ再依頼してよい。待ち方と上限は 4-1 と同じ。
- config の `review.provider` が null、またはボットが分からないリポジトリでは外部レビューなしとして扱い、
  PR を作った時点でユーザーへ報告する(自動マージしない設定なら人間のレビューに委ねる)。

## 5. レビュー対応

- **P0 / P1**: 必ず対応する。内容を検証し、正しければ修正して push し、スレッドに修正コミットの SHA を
  添えて返信して resolve する。誤検知・非該当なら理由を返信して resolve する(黙殺しない)。
  resolve は `resolveReviewThread` で明示的に行い、スレッド一覧を取り直して `isResolved` を確認する
  (返信だけでは解決済みにならない)。
- **P2 以下**: スレッドにならないので個別対応は不要。検証して実害があると分かったものだけ P1 として扱い直して直す。
- 人間のレビューコメントのうち具体的な修正指示は P1 として扱う。質問・方針相談・スコープ判断には
  エージェントが答えを決めず、人間の判断待ちとして報告する。
- 修正の push で再び `codex-review` が走る(差分のみ)。新たな P0/P1 が返れば同じように対応する。
  P0/P1 の修正 push に対して新たな P0/P1 が **3 回連続**で返る場合は人間へエスカレーションする
  (linear-worker の駐機手順)。
- **spec 由来の実装 PR の例外(SDD 導入時)**: dev-orchestrator / spec-run が作成した PR(`.kiro/specs/` の
  タスクに基づく実装)へのレビュー修正は、このループで直接 commit しない。該当タスクの
  実装フロー(`/kiro:spec-impl` 等)に戻して修正し、push 前に `/kiro:validate-impl` を
  再実行して検証証跡を更新してから push する。ドキュメントや設定のみの軽微な修正で spec タスクに
  影響しない場合は直接 commit してよい。

## 6. マージ

### 6-1. 作業ブランチへのマージ(エージェントの裁量)

config の `auto_merge.enabled` が true のリポジトリでは、次をすべて満たした PR を GitHub の auto-merge が
マージする(§3 で予約する)。エージェントは条件を偽装・回避しない(`--admin` によるブランチ保護の回避、
必須チェックの削除、スレッドを読まずに resolve する等は禁止)。

- 一次ゲート(push 前にエージェントがセッション内で行う):
  - config の `checks.fast` がローカル green(Linear 連携ありなら `check_docs_policy.py` も green)
  - 軽微な変更(fast track)でなければ `/code-review`(high)の全 finding を修正または理由付きで棄却
  - SDD 導入時、spec 由来の実装は `/kiro:validate-impl` も対応済み
- 必須チェック(GitHub が判定): CI green、`codex-review` 完了、`policy-gate` 成功、`docs-policy` 成功、
  未解決のレビュースレッドなし

**`policy-gate` が人間の承認を求める変更**(保護の段階化、2026-11-01 決定):

- **コア**(config の `auto_merge.protected_paths`。既定 `.github/` `.harness/` `.kiro/orchestration/`
  `.kiro/settings/` `.claude/settings.json` `.codex/`)= ゲート・権限・CI・この config を変える変更。
  現在ヘッドに対する人間の Approve が無ければ `policy-gate` が失敗し、マージされない。
  エージェントが自分の制約を自分で緩めないための仕組みで、エージェントはこれを迂回しない。
- **配布物**(`sync-manifest.json` の `owned`。`.claude/skills/linear-worker/` `.claude/rules/git-workflow.md` 等)を
  取り込み側で直接編集する変更。直したい場合は配布元(agentic-dev-harness)を直す。
- **指示文**(ルートや各階層の `CLAUDE.md` `AGENTS.md`、リポジトリ固有の `.claude/` の rules・skills 等)は
  コアではない。`codex-review` と `policy-gate` の不変条件(`.harness/invariants.json`。force-push 禁止・
  テスト無効化による green 化の禁止・コアの自動マージ除外などの記述が残っていること)を通れば、
  人間の承認なしに作業ブランチへマージしてよい。人間は Release PR でまとめて確認する。
- **配布元からの同期 PR**(`harness-sync` が作る、ラベル `harness-sync`)は配布物とコアに触れるが、
  内容が配布元の指定コミットを適用した結果と完全一致することを `policy-gate` が機械的に確かめられれば
  人間の承認なしにマージされる(配布元の Release PR で人間がレビュー済みのため)。

**軽微な変更(fast track)**: `classify_change.py --base origin/<作業ブランチ>` が 0(テキストのみ・
コメントのみ)の PR は `codex-review` がレビューを省略し、`/code-review` も不要。CI の扱いは
config の `fast_track.require_ci`(既定 true)。オンプレの Unity CI(`checks.unity_ci.workflow`)は
ブランチ保護の必須チェックにしない(テキスト・コメントだけの変更では結果が変わらず、オンプレ機が止まると
Queued のまま終わらないため)。

**`auto_merge.enabled` が false・config が無いリポジトリ**: 自動マージを予約しない。ゲートを満たしたら
PR の URL・対応内容の要約を添えてユーザーにマージ判断を仰ぐ。**マージ自体はユーザーの承認なしに
実行しない**(`gh pr merge` を自律的に実行するのは禁止)。linear-worker が駐機させたマージ承認待ち PR に
後から届いた P1 以上の指摘・CI 失敗・マージコンフリクトは、後続の定期実行が拾って修正・push する
(`.claude/skills/linear-worker/SKILL.md` §1-A)。

### 6-2. リリースブランチへのマージ(人間)

- `promote` ワークフローが「作業ブランチ → リリースブランチ」の Release PR を常に 1 本開いておき、
  作業ブランチが進むたびに本文(取り込まれた PR・Linear Issue の一覧、**コア・配布物・指示文の変更を先頭に強調**、
  diffstat)を更新する。
- Release PR は**人間がレビューしてマージする**(merge commit。squash しない — 作業ブランチと履歴が
  分かれて次の Release PR が衝突するため)。エージェントは Release PR をマージしない・auto-merge を予約しない。
- Release PR のレビューで問題が見つかったら、作業ブランチ側に修正 PR を出す(Release PR のブランチへ直接 push しない)。
- リリースブランチへ hotfix 等が直接入った場合は、`promote` がリリースブランチ → 作業ブランチの back-merge PR を
  作って自動マージを予約する。

### 6-3. 例外(どの構成でもユーザーの判断が必須)

- (SDD 導入時の)spec の NO-GO ゲートに関わる判断、Issue のスコープを逸脱する変更。
  linear-worker はこれらの判断待ちに入った時点で claim を解放して駐機する
  (`.claude/skills/linear-worker/SKILL.md` §3 の駐機手順)。
- 判断に迷う場合は、PR の URL・対応内容の要約を添えてユーザーに判断を仰ぐ。

## 7. ドキュメントとコンフリクト

並列に動く複数の PR が同じファイルを編集すると、コンフリクトが連鎖して自動進行が止まる。
対策は「衝突しにくい置き方」と「起きた衝突の自動解消」の 2 段で行う。

### 7-1. 起きたコンフリクトは自動で解消する(全リポジトリ共通、2026-11-01 決定)

- 作業ブランチが進むたびに `conflict-sweeper` ワークフローが、作業ブランチを base とする open PR のうち
  コンフリクトしたものに作業ブランチをマージする。文書(config の `conflicts.auto_resolve_extensions`。
  既定 `.md` `.rst` `.adoc` 等。`.txt` や依存・ビルド定義・エージェントへの指示は対象外)の衝突のうち、**両側が同じ位置に追記しただけ**のもの(一覧・ログ・表への
  行追加、同じ見出しの末尾への追記など。並列開発で起きる文書の衝突の大半)は両方を残して自動で解消し、
  push する。どのファイルにも適用される汎用の規則で、特定のファイルや置き場所に依存しない。
- 同じ既存行を両側が書き換えた衝突・コードの衝突は自動では解消せず、PR にラベル
  `needs-conflict-resolution`(config の `conflicts.label`)を付けてコメントする。linear-worker の巡回、
  または PR の作者エージェントが、作業ブランチをマージして両側の意図を読んで解消する(**rebase しない**。
  push 済みブランチの rebase は force-push を要するため)。どちらを残すかが仕様・方針の判断になる場合は
  人間へエスカレーションする。
- 自動解消の push で PR 自身の差分が変わらなければ、`codex-review` は再レビューしない。

### 7-2. 衝突しにくい置き方

- 1 つの変更につき 1 ファイルで済む置き方を優先する(全員が同じ一覧ファイルの同じ行を書き換える
  形を避け、項目ごとのファイルに分ける・状態は Linear に持たせる、など)。
- **Linear 連携ありのリポジトリ**(config に `linear.team` がある)では、開発途中の記録はリポジトリに書かず
  Linear に置く:
  - タスク単位の調査メモ・判断ログ・作業の経過 → その Issue の本文とコメント
  - 複数 Issue にまたがる設計検討 → Linear プロジェクトの Document
  - 後で直す軽微な指摘(旧 `docs/backlog.md`) → 優先度 Low の Issue として Backlog に起票する
- **集約型ドキュメント**(config の `docs.aggregate_paths`。既定 `README.md` / `CHANGELOG.md` /
  `docs/backlog.md`。Linear 連携ありのみ): 多くの PR が同じ箇所へ追記するファイル。**リリース用 PR 以外では
  変更しない**。リリース用 PR は、ヘッドブランチが `docs.release_branch_prefix`(既定 `release/`)で
  始まるか、GitHub ラベル `docs.release_label`(既定 `release-docs`)が付いた PR を指す。
  取り込み側の CI(`docs-policy`)と linear-worker の一次ゲートが
  `.claude/skills/linear-worker/scripts/check_docs_policy.py` で検査する。
  集約型ドキュメントに反映すべき変更(利用者から見える機能・設定・手順の変化)は、Issue に見出し
  `## ドキュメント反映メモ` で始まるコメントを投稿し(どのファイルに何を書くか。文面の案でよい)、Linear ラベル
  `linear.labels.docs_pending`(既定 `docs-pending`)を付ける。PR 本文にはその要約と Issue へのリンクを書く。
- **Notion** は人間が管理する要約(`.claude/skills/linear-worker/SKILL.md` §5)であり、
  エージェントが開発途中の記録を書く場所にはしない。
- **リポジトリに残すもの**(この節の制限を受けない): `CLAUDE.md` や `.claude/` などの
  エージェントへの指示、SDD 導入時の `.kiro/specs/` と `.kiro/steering/`、コードと一緒に変わる
  リファレンス(ソース内のコメント・機能ごとのファイルに分かれた API や設定の説明など)。
- **リリース時の整理**: リリースのタイミングで、人間が Linear ラベル `docs.release_label` 付きの
  Issue(例: `Release docs for v1.2`)を起票する。担当者(linear-worker を含む)は、状態が Done で
  `docs-pending` が付いた Issue のドキュメント反映メモを集めて集約型ドキュメントを 1 本の
  リリース用 PR で更新し、マージ後に取り込んだ Issue から `docs-pending` を外す
  (手順の詳細は linear-worker/SKILL.md §3「リリース用ドキュメント整理」)。

## 8. 並列開発(2026-11-01 決定)

- 同じリポジトリで複数のエージェント(クラウドセッション・worktree・定期実行)が同時に動いてよい。
  linear-worker の同時実行数は config の `worker.max_parallel`(キーが無ければ 1)。
- 着手時の claim に**触る予定の領域**(ディレクトリ・ファイルの接頭辞)を書き、他の有効な claim と
  領域が重なる Issue は後回しにする(linear-worker/SKILL.md §1・§2)。重なりの判定は助言的なもので、
  すり抜けた衝突は §7-1 の仕組みで解消する。
- 長く開いた PR ほど衝突する。PR は小さく保ち、§3 で自動マージを予約して早く作業ブランチへ入れる。
