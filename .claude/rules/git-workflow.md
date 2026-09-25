# Git Workflow

作業開始からマージ判断までの Git 運用ルール。

Linear 連携・自動マージなど「リポジトリごとに有無が変わる」項目は
`.kiro/orchestration/config.json`(雛形: `.claude/skills/linear-worker/templates/orchestration-config.json`)
の設定で切り替える。ファイルが無いリポジトリでは Linear 連携なし・自動マージなしとして扱う。

## 1. 作業開始時: ブランチ確認

- 作業に着手する前に `git branch --show-current` で現在のブランチを確認する。
- 出力が空の場合は detached HEAD（CI の checkout やコミット指定の作業環境等）。この場合は
  `git symbolic-ref refs/remotes/origin/HEAD`（取得できなければ `gh repo view --json defaultBranchRef`）で
  デフォルトブランチを特定し、リモートと同期したうえでそこから作業ブランチを作成する
  （detached HEAD のまま実作業コミットを積まない）。
- `main` / `master` などのデフォルトブランチ上と判断した場合は、実作業コミットを積む前に
  作業内容に応じた名前でブランチを切る。
- 他人・他作業のブランチ上と判断した場合は、**その場で分岐しない**（先行作業のコミットが
  新しい PR に混入するため）。クリーンなデフォルトブランチへ切り替え、
  `git fetch origin && git pull --ff-only`（または `git switch -c <branch> origin/<default>`）で
  リモートと同期してから作業ブランチを作成する。
  ただし、その先行作業の続き・修正を依頼されている場合はそのブランチをそのまま使ってよい。
- 命名規約（dev-orchestrator の規約に準拠）:
  - `<type>`: 機能追加 `feature` / バグ修正 `fix` / ドキュメント・設定等の雑務 `chore` または `docs`
  - `<topic>`: 英小文字ケバブケース。ブランチ名全体を英語で書き、日本語を含めない
  - **Linear 連携あり**（config に `linear.team` がある）: `<type>/<issue-id>-<topic>`。
    `<issue-id>` は対応する Linear Issue の識別子を小文字にしたもの（例: `hid-12`）。
    Linear はブランチ名に含まれる Issue ID から PR を自動で紐付け、ブランチ作成で In Progress、
    マージで Done へ状態遷移させるため、必ず含める。
    例: `feature/hid-13-mov-prores4444`、`fix/hid-6-remove-manual-ffmpeg`、
    `chore/hid-19-linear-branch-naming`。
    対応する Linear Issue がない作業は、ブランチを切る前に Linear へ Issue を起票する
    （PR にしない使い捨ての検証作業は除く）。Issue 作成は Linear MCP から行える。
  - **Linear 連携なし**: `<type>/<topic>`（例: `feature/spec-run-retry`）
- Linear 連携ありの場合、Issue タイトルと PR タイトルは英語で書く（本文・説明は日本語でよい）。
- Linear 連携ありの場合、ブランチを切ったら Linear MCP で対応 Issue のステータスを
  **In Progress** に更新する。ローカルでのブランチ作成は Linear / GitHub から検知されないため、
  push するまで Issue の状態は自動では変わらない（push 後は PR のマージで Done へ自動遷移する）。
- すでに適切な作業ブランチ上にいる場合はそのまま使う。

## 2. コミット

- 変更内容を簡潔にまとめたコミットメッセージを付ける（1 行目で「何をしたか」が分かること）。
- 無関係な変更は 1 コミットに混ぜず、意味のある単位で分割する。

## 3. Push

- 作業が一段落したら（タスク完了・レビュー依頼可能な状態になったら）リモートへ push する。
  - 初回: `git push -u origin <branch>`
- PR が未作成なら `gh pr create` で作成する。
- **例外**: push / PR 作成は、実行中のワークフローまたはユーザーがそれを許可している場合に限る。
  dev-orchestrator の `--stop-after implementation` のように「PR を作らない」指定がある実行や、
  ユーザーがローカル作業のみを求めている場合は、push / PR 作成を行わず完了報告に留める。

## 4. リモートレビュー待ち

- **dev-orchestrator の除外**: dev-orchestrator（Phase 6）が作成した PR はこの待機ループの
  対象外。同スキルの承認ポリシーどおり PR URL を即時報告して完了とし、PR レビューは人間に
  委ねる（ユーザーまたはワークフロー側で明示的にレビュー待機を指示された場合のみ待機する）。
- Push 後、数分〜数十分でリモート上のコーディングエージェントによるレビューが PR コメントとして投稿される。
- レビュー到着は次のいずれかで検知する:
  - 定期チェック（既定）: `gh pr view <PR> --comments` や
    `gh api repos/{owner}/{repo}/pulls/<PR>/comments` を 5〜10 分間隔でポーリングする。
    エージェントセッション中は Monitor / ScheduleWakeup 等の待機機構があればそれを使い、
    短間隔のビジーポーリングはしない。
  - フック等の通知機構が設定されている場合はそちらを優先する。
- **待機期限とフォールバック**: レビューエージェントが未設定・停止中・無応答の可能性があるため、
  待機は無期限にしない。既定の上限は 60 分（config の `review.wait_minutes`。ユーザーが別の
  期限を指定した場合はそちらを優先）。期限までにレビューが到着しなければ、PR の URL と
  「レビュー未着のため人間のレビューに委ねる」旨を報告してこのループを終了する
  （`PushNotification` 等でユーザーへの通知手段があれば併用する）。
  この期限はスケジュール実行（routine）が単一 PR の待機に無期限に占有され、後続の実行が
  進まなくなる事態を防ぐためのものでもあり、必ず適用する。
  - **手動再トリガー**: コメントで再レビューを起動できるボット（config の
    `review.bot.retrigger_comment`。例: Codex の `@codex review`）を使っている場合、待機期限に
    達する前に**1 回だけ**そのトリガーコメントを投稿してよい（スタール検知: push から 30 分
    経っても最新コミットに対するレビューが開始・完了していない場合が目安）。再トリガー後も
    上記の待機期限は変わらず適用する。
- レビューが来るまでの間、ユーザーから別途指示があればそちらを先に進めてよい。
- **linear-worker の例外（フェイルオープン、2026-09-24 決定）**: `.claude/skills/
  linear-worker/SKILL.md` §4 の外部レビューゲートに限り、上記の待機期限に達しても「人間の
  レビューに委ねてループを終了する」のではなく、**他ゲート（一次ゲート全項目・CI）のみに
  基づきマージ判定へ進む**（spec 由来の実装は `/kiro:validate-impl` 対応済みも含む。同スキルの
  手順を参照。この例外は linear-worker 経由の PR にのみ適用され、他の待機ループは本節の
  既定どおり人間へ委ねる）。

## 5. レビュー対応ループ

- レビューコメントが来たら内容を確認し、必要な修正を行って commit → push する。
- **spec 由来の実装 PR の例外**: dev-orchestrator / spec-run が作成した PR（`.kiro/specs/` の
  タスクに基づく実装）へのレビュー修正は、このループで直接 commit しない。該当タスクの
  実装フロー（`/kiro:spec-impl` 等）に戻して修正し、push 前に `/kiro:validate-impl` を
  再実行して検証証跡を更新してから push する（Gate D の証跡が古いまま PR を更新しない）。
  ドキュメントや設定のみの軽微な修正で spec タスクに影響しない場合は直接 commit してよい。
- 対応不要と判断したコメントは、理由を添えて返信または記録する（黙殺しない）。
- 「修正 → push → 再レビュー待ち」を、レビューの懸念箇所がなくなるまで繰り返す。

## 6. マージ判断

- **既定（config の `auto_merge.enabled` が false または未設定）**: 懸念箇所がなくなったら、
  PR の URL・対応内容の要約を添えてユーザーにマージ判断を仰ぐ。
  **マージ自体はユーザーの承認なしに実行しない**（`gh pr merge` を自律的に実行するのは禁止）。
- **自動マージ条件（`auto_merge.enabled` が true のリポジトリのみ。2026-09-23 決定。詳細な
  手順は `.claude/skills/linear-worker/SKILL.md` §4）**: 次の両ゲートを満たす PR は
  ユーザー承認なしで自動マージしてよい（`auto_merge.method`、`expectedHeadSha` 指定）。
  - 一次ゲート（能動レビュー・セッション内で完結）: config の `checks.fast` が
    ローカル green、`/code-review`（high）の全 finding を修正または理由付き棄却、
    spec 由来の実装は `/kiro:validate-impl` も対応済み
  - 二次ゲート（外部レビュー・機械判定）: 現在のヘッド SHA で CI green、
    外部レビューボット（`review.bot`）の summary コメントが現在ヘッドに対して
    ✅ Completed（または push 起点の待機期限に達し、linear-worker/SKILL.md §4 の
    フェイルオープン規定によりその旨を記録済み。`review.bot` が null なら「外部レビューなし」を
    記録）、未解決のレビュースレッドがゼロ（各スレッドは修正返信または理由付き
    返信で閉じる。P2 以下の指摘は linear-worker/SKILL.md §4 の重大度ベース処理に
    よる一括棄却コメント + resolve で閉じてよい）
- **例外（従来どおりユーザー承認必須）**: config の `auto_merge.protected_paths`（既定
  `.claude/` / `.github/` / `.kiro/settings/`）等のポリシー・権限・CI 定義を変更する PR、
  spec の NO-GO ゲートに関わる判断、Issue のスコープを逸脱する変更。
  linear-worker はこれらの承認待ちに入った時点で claim を解放して駐機する
  （`.claude/skills/linear-worker/SKILL.md` §3 の駐機手順。判断待ち PR が
  後続の定期実行を塞がないようにするため）。
- 自動マージ条件を満たせない・判断に迷う場合は、従来どおり PR の URL・
  対応内容の要約を添えてユーザーにマージ判断を仰ぐ。
