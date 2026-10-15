# Git Workflow

作業開始からマージ判断までの Git 運用ルール。

Linear 連携・自動マージなど「リポジトリごとに有無が変わる」項目は
`.kiro/orchestration/config.json`(雛形: `.claude/skills/linear-worker/templates/orchestration-config.json`)
の設定で切り替える。ファイルが無いリポジトリでは Linear 連携なし・自動マージなしとして扱う。

- Linear をタスクキューとする自律運用(定期実行)は `.claude/skills/linear-worker/SKILL.md` に従う。
  対象チーム・ラベル・自動マージ可否などリポジトリ固有の値は `.kiro/orchestration/config.json` に置き、
  スキル本文には書かない。
- 本文中の dev-orchestrator / `/kiro:*` / `.kiro/specs/` に関する規定は、SDD ワークフロー
  ([unity-sdd-kit](https://github.com/Hidano-Dev/unity-sdd-kit))が導入されているリポジトリでのみ適用する。
  SDD が無いリポジトリでは「spec 由来の実装」は発生しないため、該当する例外・条件は読み飛ばしてよい。

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
- 命名規約（SDD 導入時は dev-orchestrator もこの規約に従う）:
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
    **例外**: Routine 等のクラウドセッションで実行環境が作業ブランチ（`claude/...` 等）を
    割り当てている場合は、割り当てブランチをそのまま使い、PR タイトルの Issue ID・本文の
    `Fixes <ISSUE-ID>`・Linear MCP での PR 添付で紐付けを補う
    （`.claude/skills/linear-worker/SKILL.md` §2 手順 4〜5）。
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
  （SDD 導入時の）dev-orchestrator の `--stop-after implementation` のように「PR を作らない」指定がある実行や、
  ユーザーがローカル作業のみを求めている場合は、push / PR 作成を行わず完了報告に留める。

## 4. リモートレビュー（依頼と待機）

- **dev-orchestrator の除外（SDD 導入時）**: dev-orchestrator（Phase 6）が作成した PR はこの待機ループの
  対象外。同スキルの承認ポリシーどおり PR URL を即時報告して完了とし、PR レビューは人間に
  委ねる（ユーザーまたはワークフロー側で明示的にレビュー待機を指示された場合のみ待機する）。

### 4-1. 軽微な変更はレビューを依頼しない（fast track、2026-10-15 決定）

- push したら（PR 作成時を含む）まず差分を判定する:
  `python3 .claude/skills/linear-worker/scripts/classify_change.py --base origin/<default_branch>`
  （終了コード 0 = fast track、1 = レビュー要、2 = 判定できない＝レビュー要として扱う）。
- **fast track** になるのは、差分が次のどちらか（または両方）だけの場合:
  - **テキストのみ**: ソースコードではない文書（config の `fast_track.text_extensions`。既定 `.md` `.txt` 等）の
    変更。ただし `fast_track.text_exclude_paths`（既定 `.kiro/` `Assets/` `Packages/`）配下は除く
  - **コメントのみ**: 対応言語（C# / C 系・シェーダ / Java / JS・TS / Go / Rust / Swift / Kotlin / Scala /
    Dart / CSS / Python）のソースで、コメントと空白以外が変わっていない（`//go:build` `// @ts-ignore`
    `# type:` 等のディレクティブ的なコメントはコード扱い。Python の docstring もコード扱い）
- `auto_merge.protected_paths` のファイル（`CLAUDE.md` や `.claude/` などエージェントへの指示・CI 定義・設定）は、
  文書であっても挙動を変えるので fast track にならない。判定に迷う変更・未対応の言語も「レビュー要」に倒れる。
- fast track の PR では、外部レビューの**依頼も待機もしない**。コンフリクトが無いことを確認でき次第 §6 の
  マージ判断へ進む（CI の扱いは config の `fast_track.require_ci`。既定 true。オンプレの Unity CI
  ＝ `checks.unity_ci.workflow` は待たない）。
- config の `fast_track.enabled` が false のリポジトリでは、すべての PR を「レビュー要」として扱う。

### 4-2. レビューが必要な PR は開発エージェントが自分で依頼する

- 外部レビューボットの自動レビュー（push・PR 作成で自動的に始まるもの）には頼らない（ボット側の自動
  レビューは無効にしてよい）。**レビュー要の push のたびに、push したエージェント自身が PR に
  レビュー依頼コメント**（config の `review.bot.request_comment`。旧キー `review.bot.retrigger_comment` も
  同じ意味で読む。例: Codex の `@codex review`）を投稿する。
  - 依頼の直前に、そのヘッド SHA に対するレビューがすでに開始・完了していれば投稿しない
    （ボットの自動レビューが残っている間の二重起動を避ける）。
  - 依頼は 1 ヘッド SHA につき 1 回。下記のスタール時の再依頼 1 回を除き、同じコミットへ重ねて投稿しない。
  - 前回レビュー済みのコミットからの追加分だけを見て fast track なら（`classify_change.py --base
    <前回レビューされた SHA>`。文言・コメントの手直しだけの push 等）、再依頼せず前回のレビュー結果を引き継ぐ。
    デフォルトブランチを取り込んだ push は取り込んだ変更も差分に入るので、通常はレビュー要になる。
  - config が無いリポジトリでも、外部レビューボットが入っていると分かれば（過去の PR にそのボットの
    レビューがある等）そのボットの依頼コメント（Codex なら `@codex review`）で同様に依頼・監視する。
    `review.bot` が null、またはボットが分からなければ依頼先が無いので、PR を作った時点でユーザーへ報告し、
    レビューは人間に委ねる。
- **依頼したら、レビュー到着まで自動で監視する**（依頼して終わりにしない）。到着の判定は、現在の
  ヘッド SHA に対するボットのレビュー（`review.bot.summary_marker` を含む summary コメント、または
  ボットのレビュー・指摘スレッド）が完了したこと。
  - 監視の手段: PR のイベント購読（`subscribe_pr_activity` 等）やフック等の通知機構があればそれを優先する。
    無ければ Monitor / ScheduleWakeup / send_later 等の待機機構で `gh pr view <PR> --comments` や
    `gh api repos/{owner}/{repo}/pulls/<PR>/comments` を 5〜10 分間隔で確認する（短間隔のビジーポーリングはしない）。
  - **スタール時の再依頼**: 依頼から 30 分経ってもそのヘッドへのレビューが開始・完了していなければ、
    **1 回だけ**依頼コメントを再投稿してよい。
- **待機期限とフォールバック**: レビューエージェントが未設定・停止中・無応答の可能性があるため、
  待機は無期限にしない。既定の上限は 60 分（config の `review.wait_minutes`。現在ヘッドの push と、そのヘッドへの
  最初の依頼の遅い方から数える。ユーザーが別の期限を指定した場合はそちらを優先）。期限までにレビューが到着しなければ、PR の URL と
  「レビュー未着のため人間のレビューに委ねる」旨を報告してこのループを終了する
  （`PushNotification` 等でユーザーへの通知手段があれば併用する）。
  この期限はスケジュール実行（routine）が単一 PR の待機に無期限に占有され、後続の実行が
  進まなくなる事態を防ぐためのものでもあり、必ず適用する（スタール時の再依頼では延長しない）。
- レビューが来るまでの間、ユーザーから別途指示があればそちらを先に進めてよい。
- **linear-worker の例外（フェイルオープン、2026-09-24 決定）**: `.claude/skills/
  linear-worker/SKILL.md` §4 の外部レビューゲートに限り、上記の待機期限に達しても「人間の
  レビューに委ねてループを終了する」のではなく、**他ゲート（一次ゲート全項目・CI）のみに
  基づきマージ判定へ進む**（SDD 導入時、spec 由来の実装は `/kiro:validate-impl` 対応済みも含む。同スキルの
  手順を参照。この例外は linear-worker 経由の PR にのみ適用され、他の待機ループは本節の
  既定どおり人間へ委ねる）。

## 5. レビュー対応ループ

- レビューコメントが来たら内容を確認し、必要な修正を行って commit → push する。
- **spec 由来の実装 PR の例外（SDD 導入時）**: dev-orchestrator / spec-run が作成した PR（`.kiro/specs/` の
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
  linear-worker が駐機させたマージ承認待ち PR に後から届いたレビューの P1 以上の指摘・
  CI 失敗・マージコンフリクトは、後続の定期実行が拾って修正・push し、再び承認待ちに戻す
  （コンフリクトはデフォルトブランチのマージで解消し rebase しない。`.claude/skills/
  linear-worker/SKILL.md` §1-A）。config の `auto_merge.merge_parked` が false でなければ、
  要修正が無く P0/P1 未対応なし・CI green・コンフリクトなし・人間の保留なしを満たした
  駐機 PR は巡回がマージしてブランチを削除する（2026-10-01 決定。`protected_paths` に
  該当する PR は対象外で、引き続きユーザーがマージ判断する）。
- **自動マージ条件（`auto_merge.enabled` が true のリポジトリのみ。2026-09-23 決定。詳細な
  手順は `.claude/skills/linear-worker/SKILL.md` §4）**: 次の両ゲートを満たす PR は
  ユーザー承認なしで自動マージしてよい（`auto_merge.method`、`expectedHeadSha` 指定）。
  - 一次ゲート（能動レビュー・セッション内で完結）: config の `checks.fast` が
    ローカル green、`/code-review`（high）の全 finding を修正または理由付き棄却、
    SDD 導入時、spec 由来の実装は `/kiro:validate-impl` も対応済み
  - 二次ゲート（外部レビュー・機械判定）: 現在のヘッド SHA で CI green、
    外部レビューボット（`review.bot`）の summary コメントが現在ヘッドに対して
    ✅ Completed（または push 起点の待機期限に達し、linear-worker/SKILL.md §4 の
    フェイルオープン規定によりその旨を記録済み。`review.bot` が null なら「外部レビューなし」を
    記録）、未解決のレビュースレッドがゼロ（各スレッドは修正返信または理由付き
    返信で閉じる。P2 以下の指摘は linear-worker/SKILL.md §4 の重大度ベース処理に
    よる一括棄却コメント + resolve で閉じてよい）
- **fast track の即時マージ（2026-10-15 決定）**: §4-1 で fast track と判定された PR は、
  外部レビューと `/code-review` を待たない。
  - `auto_merge.enabled` が true のリポジトリでは、次を確認でき次第ユーザー承認なしでマージする
    （`auto_merge.method`、`--match-head-commit <SHA>`、マージ後にブランチ削除。詳細な手順は
    linear-worker/SKILL.md §4「軽微な変更の高速マージ」）:
    - マージ直前に取り直した現在ヘッドで `classify_change.py` が fast track
    - `checks.fast` がローカル green（Linear 連携ありなら `check_docs_policy.py` も green）
    - **コンフリクトなし**: `gh pr view <PR> --json mergeable` が `MERGEABLE`
    - CI: config の `fast_track.require_ci` が false でなければ、現在ヘッドで green（CI なしは記録）。
      false なら CI の完了を待たない（失敗が出ている PR はマージしない。ブランチ保護で必須の
      チェックがあればその完了は待つ）
    - **オンプレの Unity CI は待たない**: config の `checks.unity_ci.workflow`（セルフホストランナーで
      走る Unity テスト）は、`require_ci` にかかわらず実行中・待機中でも待たない（テキスト・コメントだけの
      変更では結果が変わらず、オンプレ機が止まっているとジョブが Queued のまま終わらないため）。
      すでに失敗していればマージしない。ブランチ保護の必須チェックでマージできなければ、保護を回避せず
      完了待ちとして報告する
    - 未解決のレビュースレッドがゼロ、人間の保留（`CHANGES_REQUESTED`・「待って」等）が無い、draft でない
  - `auto_merge.enabled` が false・config が無いリポジトリでは、レビュー待ちを省いて、
    コンフリクトが無いことを確認したらすぐにユーザーへマージ判断を仰ぐ（下記の例外も従来どおり）。
- **例外（従来どおりユーザー承認必須）**: config の `auto_merge.protected_paths`（既定
  `.claude/` / `.github/` / `.kiro/settings/` / `.kiro/orchestration/` / `.agents/` / `.codex/` / ルートの `CLAUDE.md` / `AGENTS.md`）等のポリシー・権限・CI 定義・
  orchestration 設定を変更する PR、
  （SDD 導入時の）spec の NO-GO ゲートに関わる判断、Issue のスコープを逸脱する変更。
  linear-worker はこれらの承認待ちに入った時点で claim を解放して駐機する
  （`.claude/skills/linear-worker/SKILL.md` §3 の駐機手順。判断待ち PR が
  後続の定期実行を塞がないようにするため）。
- 自動マージ条件を満たせない・判断に迷う場合は、従来どおり PR の URL・
  対応内容の要約を添えてユーザーにマージ判断を仰ぐ。

## 7. ドキュメントの置き場所(2026-10-14 決定)

並列に動く複数の PR が同じドキュメントへ追記すると、マージコンフリクトが連鎖して
自動進行が止まる。これを防ぐため、**開発途中の記録はリポジトリに書かず Linear に置き、
集約型ドキュメントはリリース時にまとめて更新する**。

Linear 連携あり(config に `linear.team` がある)のリポジトリにのみ適用する。
Linear 連携なしのリポジトリでは記録先が無いため、従来どおりリポジトリ内のドキュメントを直接更新してよい。

- **集約型ドキュメント**(config の `docs.aggregate_paths`。既定 `README.md` / `CHANGELOG.md` /
  `docs/backlog.md`): 多くの PR が同じ箇所へ追記するファイル。**リリース用 PR 以外では
  変更しない**。リリース用 PR は、ヘッドブランチが `docs.release_branch_prefix`(既定 `release/`)で
  始まるか、GitHub ラベル `docs.release_label`(既定 `release-docs`)が付いた PR を指す。
  取り込み側の CI(`templates/consumer/docs-policy.yml`)と linear-worker の一次ゲートが
  `.claude/skills/linear-worker/scripts/check_docs_policy.py` で検査する。
- **開発途中の記録は Linear に置く**(リポジトリに新しいファイルとして足さない):
  - タスク単位の調査メモ・判断ログ・作業の経過 → その Issue の本文とコメント
  - 複数 Issue にまたがる設計検討 → Linear プロジェクトの Document
  - 後で直す軽微な指摘(旧 `docs/backlog.md`) → 優先度 Low の Issue として Backlog に起票する
  - **集約型ドキュメントに反映すべき変更**(利用者から見える機能・設定・手順の変化)→
    Issue に見出し `## ドキュメント反映メモ` で始まるコメントを投稿し(どのファイルに何を
    書くか。文面の案でよい)、Linear ラベル `linear.labels.docs_pending`(既定 `docs-pending`)を
    付ける。PR 本文にはその要約と Issue へのリンクを書く(レビュアーが差分で文面を見られないため)
- **Notion** は人間が管理する要約(`.claude/skills/linear-worker/SKILL.md` §5)であり、
  エージェントが開発途中の記録を書く場所にはしない。
- **リポジトリに残すもの**(この節の制限を受けない): `CLAUDE.md` や `.claude/` などの
  エージェントへの指示(起動時に自動で読まれる必要がある)、SDD 導入時の `.kiro/specs/` と
  `.kiro/steering/`(feature 単位に分かれていて検証証跡でもある)、コードと一緒に変わる
  リファレンス(ソース内のコメント・機能ごとのファイルに分かれた API や設定の説明など)。
- **リリース時の整理**: リリースのタイミングで、人間が Linear ラベル `docs.release_label` 付きの
  Issue(例: `Release docs for v1.2`)を起票する。担当者(linear-worker を含む)は、状態が Done で
  `docs-pending` が付いた Issue のドキュメント反映メモを集めて集約型ドキュメントを 1 本の
  リリース用 PR で更新し、マージ後に取り込んだ Issue から `docs-pending` を外す
  (手順の詳細は linear-worker/SKILL.md §3「リリース用ドキュメント整理」)。
