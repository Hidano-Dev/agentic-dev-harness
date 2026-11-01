---
name: linear-worker
description: >-
  Linear をタスクキューとして次の 1 Issue を自律的に選定・実装・マージまで進める
  ワーカーのポリシー。Routine から起動された新規リモートセッション、または
  「Linear の次のタスクを進めて」と依頼されたセッションで使用する。
  1 回の起動で処理する Issue は 1 件のみ。対象チーム・ラベル・チェックコマンド等は
  `.kiro/orchestration/config.json` から読む(リポジトリ固有の値をこのスキルに書かない)。
---

# Linear 駆動 自律ワーカー

Linear(設定ファイルで指定したチーム / プロジェクト)を唯一のタスクキューとし、
人間の個別指示なしで Issue を 1 件選定して完了(マージ・Done)まで進める。
Git 運用は `.claude/rules/git-workflow.md`、spec 由来の実装は `.kiro/specs/` の
SDD フローに従う。本書はその上に載る「選定・承認・マージ・停止」のポリシーである。

SDD ワークフロー(`/kiro:*` コマンド・dev-orchestrator)は本スキルとは別に
[unity-sdd-kit](https://github.com/Hidano-Dev/unity-sdd-kit) が配布する。本書の「spec 由来」
「`/kiro:*`」「SDD フェーズ承認」に関する規定は SDD が導入されているリポジトリでのみ適用し、
導入されていない(`.claude/commands/kiro/` が無い)リポジトリでは spec 由来の作業は発生しないので
読み飛ばす。

## 設定(`.kiro/orchestration/config.json`)

このスキルはリポジトリ固有の値を一切持たない。起動時に必ず
`.kiro/orchestration/config.json` を読み、次のキーを使う(雛形:
`templates/orchestration-config.json`。SDD 導入時は dev-orchestrator の `confirmation_channel` と
同じファイルに同居する)。**ファイルが無い・`linear.team` が無い場合は何も着手せず、
「harness 設定なし」と報告して終了する**(対象キューが特定できないまま動かない)。

| キー | 意味 | 既定 |
|---|---|---|
| `linear.team` | 選定対象のチーム名 | 必須 |
| `linear.project` | 選定対象を絞るプロジェクト名(null なら絞らない) | null |
| `linear.labels.needs_human` | 人間判断待ち。付いた Issue には着手しない | `needs-human` |
| `linear.labels.needs_local` | ローカル環境・実機・人の目が必要。スキップして実機待ちキューへ | `needs-local` |
| `linear.labels.needs_unity` | Unity Editor でのテスト実行が必要。`checks.unity_ci` があるリポジトリでは候補に含め、CI の Unity ジョブを検証証跡にする(§3 / §4 手順 1)。無いリポジトリでは `needs_local` と同じ扱い | `needs-unity` |
| `linear.labels.docs_pending` | 集約型ドキュメントへの反映待ち。ドキュメント反映メモを書いた Issue に付け、リリース用ドキュメント整理で外す(§3) | `docs-pending` |
| `github.repo` | `owner/name` | 必須 |
| `github.default_branch` | 作業ブランチ(作業の起点・PR の base。GitHub の既定ブランチ)。キーが無ければ `gh repo view --json defaultBranchRef` で取る | `develop`(雛形) |
| `github.release_branch` | リリースブランチ。Release PR(`promote` ワークフロー)だけが入り、人間がマージする。null なら単一ブランチ運用(`git-workflow.md` §0) | `main`(雛形) / null(キーが無い場合) |
| `checks.fast` | 一次ゲートでローカル実行するコマンド配列。空ならワーカーがリポジトリから推定する(§4 手順 1) | `[]` |
| `checks.unity_ci` | Unity テストをセルフホストランナーで回す CI の情報(`workflow` = ワークフローファイル名、`platform` = ランナーの OS)。null なら「Unity を CI で検証できないリポジトリ」(`docs/onprem-unity-runner.md`) | null |
| `review.provider` | 外部レビューの実行方式。`action` = `codex-review` ワークフロー(Actions で 1 ヘッド 1 回実行。依頼コメント不要)/ `app` = Codex GitHub App に `review.bot.request_comment` で依頼(後方互換)/ null = 外部レビューなし | `action` |
| `review.action` | `provider: action` の設定(`workflow` / `check_name` = 必須チェック名 `codex-review` / `max_rounds` = 1 PR あたりのレビュー回数上限 / `model` / `effort`) | `max_rounds`: 3 |
| `review.fail_open` | Codex がレビューできなかった(API キーなし・無応答)ときに `codex-review` を成功させて他のゲートで進むか | true |
| `review.bot` | `provider: app` のボット(`name` / `login` / `summary_marker` / `request_comment`)。`request_comment` は依頼コメント(例: `@codex review`。旧キー `retrigger_comment` も同じ意味で読む) | null |
| `review.wait_minutes` | 外部レビューを**セッション内で**待つ上限(分)。過ぎたら待たずに終了してよい(自動マージは予約済み) | 60 |
| `conflicts.auto_resolve` | `conflict-sweeper` が文書の追記同士の衝突を自動解消するか | true |
| `conflicts.auto_resolve_extensions` | 自動解消の対象にする拡張子 | `[".md", ".markdown", ".rst", ".adoc", ".asciidoc"]`(`requirements*.txt` `CMakeLists.txt` `CLAUDE.md` `AGENTS.md` `SKILL.md` 等は拡張子にかかわらず対象外) |
| `conflicts.label` | 自動解消できなかった PR に付くラベル。§1-A の巡回対象 | `needs-conflict-resolution` |
| `fast_track.enabled` | 軽微な変更(テキストのみ・コメントのみ)の PR をレビュー依頼・待機なしでマージ判定へ進めるか(§4「軽微な変更」)。キーが無ければ true | true |
| `fast_track.text_extensions` | 「テキストのみ」とみなす文書の拡張子。`requirements*.txt` `CMakeLists.txt` などの依存・ビルド定義と、任意の階層の `CLAUDE.md` `AGENTS.md` `SKILL.md`・`.claude/` `.cursor/` 等のエージェントへの指示は、ここに含めてもテキスト扱いしない | `[".md", ".markdown", ".rst", ".adoc", ".asciidoc"]` |
| `fast_track.text_exclude_paths` | テキストでも fast track にしないパス接頭辞(spec 文書・実行時に読まれ得る Unity の TextAsset) | `[".kiro/", "Assets/", "Packages/"]` |
| `fast_track.require_ci` | fast track の PR も現在ヘッドの CI green を待ってマージするか。false なら CI の完了を待たない(失敗が出ていればマージしない)。どちらの場合も `checks.unity_ci.workflow`(オンプレの Unity CI)は待たない | true |
| `worker.max_candidates` | 1 起動あたりの候補試行上限 | 5 |
| `worker.claim_stale_hours` | 放棄 claim とみなす経過時間 | 24 |
| `worker.max_parallel` | チーム内で同時に実作業してよいワーカー数(§1 並行度ガード)。キーが無ければ 1 | 2(雛形) |
| `docs.aggregate_paths` | 集約型ドキュメント(`git-workflow.md` §7)。リリース用 PR 以外では変更しない。`/` で終わる値はディレクトリ接頭辞。空配列なら制限なし | `["README.md", "CHANGELOG.md", "docs/backlog.md"]` |
| `docs.release_branch_prefix` | このブランチ接頭辞の PR はリリース用 PR として集約型ドキュメントを変更してよい | `release/` |
| `docs.release_label` | リリース用ドキュメント整理の Issue に付ける Linear ラベルと、そのリリース用 PR に付ける GitHub ラベル(同名) | `release-docs` |
| `auto_merge.enabled` | 作業ブランチへの自動マージ(GitHub auto-merge の予約)を許可するか | true(雛形の値。キーが無い場合は false として扱う) |
| `auto_merge.method` | `merge` / `squash` / `rebase` | `merge` |
| `auto_merge.protected_paths` | **コア**(ゲート・権限・CI・config)のパス接頭辞。変更する PR は人間の Approve が無いと `policy-gate` が失敗する。指示文(`CLAUDE.md` `AGENTS.md` 等)はここに入れない(`git-workflow.md` §6-1) | `[".github/", ".harness/", ".kiro/orchestration/", ".kiro/settings/", ".claude/settings.json", ".codex/"]` |
| `auto_merge.merge_parked` | マージ承認待ちで駐機した PR を、巡回で条件を満たせばマージ + ブランチ削除するか(§1-A 巡回マージ。`enabled` とは独立) | true |
| `reporting.linear_status` | 終了時に Linear プロジェクトへステータス更新を投稿するか(§5) | true |
| `reporting.notion` | 終了時に Linear プロジェクトの Overview にリンクされた Notion ページの古くなった記述を直すか(§5) | true |
| `applied_migrations` | Harness Sync が適用済みの config 移行の ID(`scripts/migrate_config.py`)。ワーカーは読まない。手で編集しない | 全移行の ID |

以下の本文では、`needs-human` / `needs-local` / `needs-unity` はそれぞれ `linear.labels.*` に
設定した実際のラベル名を指す(例: unity-renderer では `needs_local` = `needs-unity`。この場合
`needs_unity` と同名になるが、`checks.unity_ci` が null なので両方とも実機待ちとして扱われ、
挙動は変わらない)。

## 0. 起動時の前提確認

1. `.kiro/orchestration/config.json` を読む(上記)。
2. `git fetch origin <default_branch>` し、作業ブランチの最新から作業すること。
3. Linear MCP が使えることを確認する。使えない場合は何もせず、その旨を報告して終了する。
4. **起動時に作業ツリーが clean であることを確認する**(`git status --short` が空)。
   起動時点で未コミット差分がある場合は、それはユーザーや他プロセスの作業の可能性が
   あるため**何も破棄せず**、その旨を報告して終了する。この保証により、以後の
   クリーンアップ(§1)で破棄してよい差分は「本起動中にワーカー自身が生成したもの」
   に限定される。

## 1. タスク選定(1 件のみ)

**並行度ガード(`worker.max_parallel`、2026-09-24 決定・2026-11-01 拡張)**: 選定を始める前に、
チーム内の In Progress Issue に付いた**他セッションの有効な claim**(撤退・解放されて
おらず、放棄回収の条件も満たさないもの)を数える。その数が `worker.max_parallel`(キーが無ければ 1)
以上なら、**新規着手せず「並行上限に到達(稼働中: <ID>…)」と空振り報告して終了する**。
上限未満なら、他の有効な claim の **`領域:`**(§2 手順 2)を控えておき、選定で使う(下記)。
**ユーザー判断待ちで駐機中の Issue(§3 の駐機手順で claim 解放済み)は「有効な claim」に数えない** —
判断待ち PR が 1 件あるだけでキュー全体が止まる事態を防ぐため。この事前確認は
**助言的**であり(同時起動した相手がまだ claim を投稿していない瞬間はすり抜け得る)、
確定判定は §2 手順 3 の claim 投稿後のチーム全体再確認が行う。

**領域の重なり**: 選定した候補について、着手前に触る予定の領域(ディレクトリ・ファイルの接頭辞。
Issue の本文とリポジトリの構成から見積もる。迷ったら広めに取る)を決める。他の有効な claim の領域と
接頭辞で重なる(一方が他方の接頭辞である)候補は**この起動では後回し**にし、次の候補へ進む
(空振りに数えない。`worker.max_candidates` には数える)。重なりの見積もりは助言的で、すり抜けた
衝突は `conflict-sweeper` と §1-A の巡回が解消する(`git-workflow.md` §7-1)。

### 1-A. 駐機 PR の巡回(新規選定の前に行う、2026-09-30 決定・2026-10-01 / 2026-11-01 拡張)

自動マージを予約して手放した PR(`駐機理由: auto-merge`)とマージ承認待ちで駐機した PR
(`駐機理由: merge-approval`)は、レビューの P0/P1 指摘が後から届いても、CI が
落ちても、作業ブランチの進行でマージコンフリクトが出ても(`conflict-sweeper` が自動で
解消できず `conflicts.label` を付けたもの)、誰かが拾わないと止まってしまう。これを防ぐため、
並行度ガードを通過したら新規選定の**前に**駐機 PR を巡回し、**P1 以上の指摘・CI 失敗・
マージコンフリクト**(以下まとめて「要修正」)があれば対応して push まで進める。
要修正が無い場合: `auto-merge` の PR は GitHub がマージするので触らない(予約が外れていれば
再予約する。下記「処理」)。`merge-approval` の PR は、`auto_merge.merge_parked` が有効なら
条件を満たした時点でマージしてブランチを削除する(下記「巡回マージ」)。

**対象**: チーム(`linear.project` 設定時はそのプロジェクト)の Issue のうち、次を
すべて満たすもの。作成日時の古い順に見る。

- 状態が In Progress で、**有効な claim が無い**(すべての
  claim が解放・撤退済み。巡回の再 claim が競合で敗れて撤退した後も、この条件は
  満たし続ける)
- **最新の駐機記録**(「claim 解放(…で駐機)」の追記のうち最も
  新しいもの。その後の競合撤退の追記には左右されない)の駐機理由(§3 駐機手順 2 の
  `駐機理由:`)が **`auto-merge`**(`needs-human` は付かない)または **`merge-approval`**
  (`needs-human` 付き)。理由の記載が無い旧形式の駐機は、判断依頼
  コメントがマージ承認のみを求めている場合に限り `merge-approval` とみなす
  (判別できなければ対象外)
- 紐付く PR が存在する。PR の状態で分岐する: オープンなら下記の判定へ進む。
  マージ済みなら Done 遷移だけ確認し(失敗していれば Done にする)、クローズ済み
  なら報告に載せる(この 2 つは判定・処理の対象外)

`requirements` / `no-go` / `escalation` / `scope` の駐機は人間の判断そのものを待って
いるため巡回しない。

**判定**(PR ごと。コード変更を伴わないので claim 不要):

1. **マージ可否**: `gh pr view <N> --json headRefOid,mergeable,mergeStateStatus` で
   取得する。`mergeable` が `CONFLICTING`(`mergeStateStatus` が `DIRTY`)なら
   「コンフリクトあり」。`UNKNOWN` は GitHub が計算中なので少し置いて 1 回だけ取り直し、
   なお `UNKNOWN` ならこの巡回では判定しない(次回に回す)。`BEHIND`(コンフリクトは
   無いが作業ブランチより古いだけ)は要修正に数えない。
2. **CI**: 現在のヘッド SHA のチェック(`gh pr checks <N>`、または
   `statusCheckRollup`)を取得する。実行中・待機中ならこの巡回では判定しない。
   失敗(`FAILURE` / `ERROR` / `TIMED_OUT` / `CANCELLED` 等)があれば、失敗ジョブの
   ログを読み、原因を次に振り分ける。ログの取得と再実行は Check の提供元で分ける:
   GitHub Actions の Check(詳細 URL が `/actions/runs/<run-id>/` を含む)は
   `gh run view <run-id> --log-failed` / `gh run rerun <run-id> --failed`。それ以外
   (CircleCI 等の外部 CI)は Check の詳細 URL・出力から読めるものを読み、提供元の
   再実行手段がワーカーから使えなければ再実行せず、一過性と見立てた旨を添えて
   報告の「人間の対応待ち」に載せる(`gh run` を外部 CI に使わない)。
   - **PR 起因**: PR の変更が原因(テスト・lint・型・ビルドの失敗)→ 要修正
   - **一過性**: ランナー障害・ネットワーク・外部サービスの一時エラー・タイムアウト等、
     PR の変更と無関係で再実行で通る見込みのもの → 上記の手段で失敗ジョブを
     再実行する(**1 ヘッド SHA あたり 1 回まで**。claim 不要・成果物に数えない)。
     同じヘッドで再実行済みなのに再び失敗した場合は PR 起因として扱い直して検証する
   - **既存の失敗**: 作業ブランチの最新でも同じジョブが同じ理由で落ちている
     (PR 外の問題)→ この PR では直さない(スコープ外)。報告の「人間の対応待ち」に
     載せる
   - CI が設定されていない・チェックが 1 件も無い場合は「CI なし」として扱う
3. **レビュー**: `review_status.py --pr <N>` で現在ヘッドの外部レビューの状態を取り、
   未解決レビュースレッド、PR のレビュー・コメント(人間のものを含む)を取得する。
   `pending` / `running` ならこの巡回では外部レビューの結果を待たない(次回に回す)。
4. `review.provider` が `app` の場合だけ: `classify_change.py --base origin/<default_branch>` で
   PR 全体を判定し、レビュー要で、ボットが現在ヘッドをまだレビューしておらず、そのコミットで
   まだ依頼していなければ `review.bot.request_comment` を 1 回だけ投稿する(§4「外部レビュー」の `app` の上限と共通。
   依頼した PR は、この巡回では下記「巡回マージ」の対象にしない)。`action` ではワークフローが
   push ごとに自動でレビューするので依頼しない。null なら人間のレビューだけを手順5 で扱う。
5. 未対応の指摘を §4「重大度ベースの指摘処理」で分類する。人間のレビューコメントの
   うち**具体的な修正指示**は P1 相当として扱う。質問・方針相談・スコープ判断は
   ワーカーが答えず、人間の判断待ちとして報告に載せる。

**処理**:

- **要修正なし(P2 以下のみ・CI の一過性失敗の再実行だけ等)**: P2 以下のスレッドがあれば §4 の
  一括棄却(一括コメント 1 件 + resolve)を行う。そのうえで:
  - `駐機理由: auto-merge` の PR: `gh pr view <N> --json autoMergeRequest` で予約を確かめる。
    予約が外れていれば(GitHub は権限の無い push 等で予約を解除する)§4「マージ」の条件で再予約する。
    `policy-gate` が人間の承認待ちで失敗している PR は再予約しても進まないので、`merge-approval` に
    切り替えて駐機し直す(`needs-human` を付け、判断依頼コメントを書く)。
  - `駐機理由: merge-approval` の PR: 下記「巡回マージ」の条件を見る。
  マージしない場合は次の駐機 PR へ進む。いずれも成果物に数えない
  (§1 の「1 起動 1 Issue」を消費しない)。
- **要修正あり(P1 以上・PR 起因の CI 失敗・コンフリクトのいずれか)**: §3 駐機手順 4 に
  従い**編集前に**再 claim する(§2 手順 3 の競合検出を経る。敗者なら何もせず終了)。
  `merge-approval` の PR では `needs-human` を**外さない**(マージ承認という判断はまだ済んでいない)。そのうえで、
  PR のブランチを checkout し、該当するものを**この順に** 1 回の作業でまとめて直す
  (コンフリクト解消で CI の失敗理由や指摘箇所が変わり得るため):
  1. **コンフリクト解消**(`conflict-sweeper` が自動解消できず `conflicts.label` を付けたもの):
     `git fetch origin <default_branch>` し、
     `git merge origin/<default_branch>` で取り込んで解消する(**rebase しない** —
     push 済みブランチの rebase は force-push を要し §6 で禁止)。両側の意図を読んで
     機械的に決まるもの(片側だけの変更、独立した箇所の追加、import や一覧の並び等)は
     解消してよい。**両側が同じ振る舞いを別の意図で変えていて、どちらを残すかが仕様・
     方針の判断になる**場合は推測で解消せず、`git merge --abort` して下記の
     エスカレーションへ回す。解消後はマージコミットのまま残し、push 後に `conflicts.label` を外す。
  2. **CI 修正**: PR 起因の失敗をログから特定して直す。テストのスキップ・無効化・
     期待値をただ実装に合わせる書き換えによる green 化は禁止(§6)。原因がテストの
     誤りだと判断した場合はその根拠を修正コミットと PR コメントに書く。
  3. **P1 以上の指摘**: 指摘を検証して修正する。誤検知なら理由付き返信 + resolve で
     棄却する。
  4. spec 由来の実装なら、上記の修正は `git-workflow.md` §5 の例外どおり
     `/kiro:spec-impl` のフローで行い、push 前に `/kiro:validate-impl` を再実行する
     (コンフリクト解消だけでも、解消で実装が変わった場合は再実行する)。
  5. §4 一次ゲート手順 1〜2(`checks.fast`、修正差分 — コンフリクト解消を含む — への
     `/code-review` high)を通す。
  6. push し、レビュースレッドには修正コミット SHA を添えて返信・resolve、
     `review.provider` が `app` でレビュー要なら(§4「軽微な変更」の判定)レビュー依頼を 1 回投稿する
     (`action` ではワークフローが自動で差分レビューする)。CI 修正・コンフリクト解消は PR に 1 件コメントで記録する
     (原因・対応・コミット SHA)。以降は §4 と同じ待機上限・重大度処理で再レビューを
     扱ってよい。待機上限までに結果が出なければ次回の巡回に任せる(待機を延長しない)。
  7. **再び駐機**する。`auto-merge` の PR は自動マージを再予約して(§4「マージ」)`駐機理由: auto-merge` で
     claim を解放する。`merge-approval` の PR は `駐機理由: merge-approval` のまま駐機し、判断依頼
     コメントに「駐機後に発生した <P1 以上の指摘 / CI 失敗 / マージコンフリクト> へ
     対応済み(対応内容・コミット SHA)。マージ判断をお願いします」と追記する
     (巡回マージの対象なら「次回以降の巡回で条件を満たせば自動でマージします」と
     添える)。修正した PR を同じ起動で巡回マージ
     しない(修正後のレビュー・CI を次回の巡回で確かめ、人間が差分を見る間を置くため)。
  8. この対応は **1 起動 1 Issue の実作業に数える**。新規選定には進まず §5 の報告で
     終了する。
- **エスカレーション**: コンフリクトの解消に方針判断が要る、CI 失敗の原因が特定
  できない・PR の範囲で直せない、のいずれかなら、修正を push せず(作業ツリーは
  §1 の空振り時と同じ基準でクリーンに戻す)判断依頼コメントに状況(衝突ファイル・
  失敗ジョブと原因の見立て)を書き、`駐機理由: escalation` で再駐機して人間へ返す。
- **安全弁**: 巡回での修正 push(P1 以上・CI・コンフリクトのいずれによるものも)は
  §4 の「3 回連続」に通算する。回数は claim 解放の追記に `巡回修正連続: <n>` として
  残し、次回の巡回はそれを読んで加算する(旧形式の `P1 修正連続: <n>` も同じ値として
  読む)。次回の巡回で要修正が無ければ 0 に戻す。3 回に達したら修正せず
  `駐機理由: escalation` に変えて人間へ返す。

**巡回マージ**(2026-10-01 決定): 要修正が無い駐機 PR は、次を**すべて**満たせば
人間の承認を待たずにマージし、ブランチを削除する。1 つでも欠ければマージせず
駐機を続ける(欠けた条件は §5 の報告に書く)。

- `auto_merge.merge_parked` が false でない(キーが無ければ true として扱う)
- PR が `auto_merge.protected_paths` の変更を**含まない**(ポリシー・権限・CI 定義を
  変える PR は、ワーカーが自分の制約を自分で緩めないよう常に人間がマージする。§4 末尾)
- PR が draft でない
- **CI**: 現在ヘッド SHA のチェックがすべて成功(CI なしのリポジトリは「CI なし」と
  記録)。実行中・失敗・既存の失敗(スコープ外として報告したもの)があればマージしない。
  ただし PR 全体が fast track なら、`checks.unity_ci.workflow`(オンプレの Unity CI)は
  実行中・待機中でも待たない(失敗していればマージしない。§4「軽微な変更」)
- **コンフリクトなし**: `mergeable` が `MERGEABLE`。`mergeStateStatus` が `BLOCKED` /
  `BEHIND` 等でブランチ保護によりマージできない場合は、ブランチの更新や保護の回避を
  せず報告に載せる
- **レビュー**: 現在ヘッドに対して P0・P1 相当の未対応指摘が無く、未解決のレビュー
  スレッドがゼロ(§4「二次ゲート」と同じく `isResolved` で確認)。外部レビューが
  設定されていれば、`review_status.py` が現在ヘッドに対して `clean` / `findings`(P0/P1 は対応済み)/
  `skipped` / `unavailable`(fail-open。その旨が PR に記録済み)/ `timeout`(`provider: app` の待機期限超過。
  §4「外部レビュー」のフェイルオープン。その旨をコメントで記録する)のいずれかを返すこと。
  `pending` / `running` ならマージしない
- **人間の保留が無い**: PR の `reviewDecision` が `CHANGES_REQUESTED` でない。かつ
  **PR と Issue の全履歴**(最新の駐機記録以降に限らない — ワーカーの再駐機で保留
  コメントが古い駐機記録の前に埋もれても保留は続くため)で、人間による保留依頼
  (「待って」「マージしないで」等)や人間からの質問が**1 件も残っていない**こと。
  つまり、そうした依頼・質問が無いか、あるならその**すべてが後続の人間のコメントで
  明示的に解除・回答済み**であること(1 件でも未解除・未回答があればマージしない)。
  ワーカーの修正 push・再駐機・返信は解除・回答に数えない。判別に迷うコメントがあれば
  マージしない
- この起動で当該 PR に修正 push をしていない(上記手順 7)
- この起動で当該 PR に外部レビューボットへのレビュー依頼(上記手順 4)を投稿していない。
  依頼した PR は、返ってくる P0/P1 指摘を次回の巡回で確認するまでマージしない

マージの手順:

1. **マージコマンドの直前に、上記の巡回マージ条件をすべて取り直す**(ヘッド SHA・
   `mergeable`・現在ヘッドの CI・未解決スレッドと P0/P1・外部レビューの summary・
   最新の駐機記録以降の人間のレビュー/コメント)。判定時から 1 つでも変わっていれば
   マージせず次回の巡回へ回す(巡回中に CI が失敗へ変わる、人間が保留を書き込む、
   別の駐機 PR のマージでコンフリクトが生じる等があり得るため。`--match-head-commit`
   が防ぐのは新しい push だけで、Check やコメントの変化は防げない)。
2. `gh pr merge <N> --<auto_merge.method> --match-head-commit <手順1で確認したヘッド SHA>
   --delete-branch` を実行する。
3. **マージの完了を確認する**: `gh pr view <N> --json state,mergedAt,mergeCommit` で
   `state` が `MERGED` であることを確かめる。merge queue が必須のブランチでは
   コマンドの成功はキュー投入にすぎず、キュー内で失敗・取り消しになり得るため、
   `MERGED` でなければ完了扱いにしない — PR に「巡回マージ: merge queue に投入」と
   コメントして駐機を続け、次回の巡回で `MERGED` なら手順 4 へ、キューから外れて
   オープンに戻っていれば通常の判定からやり直す。リモートのヘッドブランチは
   `MERGED` を確認してから削除状態を確かめ、残っていれば削除する(ブランチ削除だけが
   失敗した場合 — 保護されている等 — はマージを成功扱いにして報告に残す)。
4. `MERGED` を確認してから、PR と Issue に「巡回マージ: 駐機後の要修正なし・
   CI green・コンフリクトなし・レビュー <Completed / 待機期限超過> を確認してマージ
   (マージコミット SHA)」とコメントし、Linear の Done 自動遷移を確認する(失敗して
   いれば手動で Done にする)。`needs-human` は Done の Issue に残っても無害なので
   外さなくてよい。リリース用 PR なら、本文に列挙した Issue から `docs-pending` を外す。
5. マージはコード変更を伴わないので claim 不要で、成果物にも数えない。続けて次の
   駐機 PR の巡回へ進む。

巡回で実作業が発生しなければ、そのまま下記の新規選定へ進む。

次の条件をすべて満たす Issue を、上から順の優先で 1 件選ぶ:

- チーム `linear.team`(`linear.project` が設定されていればそのプロジェクト)、
  状態が Todo または Backlog
- **ブロックされていない**(blocked-by リレーションの相手が未完了の Issue は除外)
- ラベル `needs-local` が**付いていない**(実機・ローカル環境必要 → スキップ対象)
- ラベル `needs-unity` が付いている場合は、config の `checks.unity_ci` が **null でない**
  (Unity テストを CI で回せるリポジトリ)。null なら `needs-local` と同じくスキップ対象。
  `needs-unity` の Issue は、Unity を自分で起動せずに実装し、push 後の CI で検証する
  (§3)。本文に「Windows でのみ再現」「Play 中に人が操作して確認」等、CI の
  プラットフォーム(`checks.unity_ci.platform`)や自動実行で検証できない記述があれば、
  着手せず `needs-local` に付け替えて理由をコメントし、次の候補へ進む
- ラベル `needs-human` が**付いていない**(人間判断待ち → 着手禁止)
- 他のワーカーが処理中でない(In Progress かつ直近のコメントで別セッションが claim している Issue は除外)
- **放棄 claim の回収**: In Progress の Issue でも、「claim コメントの投稿から
  `worker.claim_stale_hours` 時間以上経過」かつ「オープンな PR が紐付いていない(または
  紐付く PR がクローズ済み)」ものは放棄されたとみなし、選定候補に含めてよい。回収する
  場合は claim 前に「放棄 claim を回収する」旨と根拠(元 claim の日時・PR 不在)をコメントする
- 親 Issue が「サブ Issue へ分割済みの親」である場合は親ではなくサブ Issue を選ぶ

優先順: Priority(Urgent > High > Medium > Low > None)→ 同順位なら作成日時の古い順。

**空振り時の再試行**(2026-09-24 決定): 選定した候補を**成果物ゼロのまま
手放した**場合(claim 競合で敗退、着手前に needs-local と判明、質問の needs-human を
付けただけ等)は、そこで終了せず §1 に戻って**次の候補を試す**。手放す際の claim
解放(§3)を済ませ、さらに**作業ツリーをクリーンに戻してから**戻ること
(`git status` で未コミット差分が無いことを確認。破棄してよいのは
**本起動中にワーカー自身が変更したと特定できるパス**の差分のみ。§0-4 の起動時
clean 確認は出発点にすぎず、起動後にユーザー・フック・他プロセスが worktree を
変更した可能性は排除できないため、自分が触れた覚えのないパスに差分が現れた場合は
**破棄せず停止・報告**する。
破棄してよいか迷う規模の差分が残っている場合は空振りではなく「作業済み」とみなし、
差分をコミット・push して状況を報告し終了する — 差分の次候補への持ち越しは禁止)。
無限ループ防止のため、1 起動あたりの候補試行は **`worker.max_candidates` 件**まで
とし、上限到達時は試行結果の一覧を報告して終了する。
なお「1 起動 1 Issue」は「実際に作業(コミット・spec 成果物の生成・PR)まで進める
Issue が 1 件」の意味であり、空振りはカウントしない。requirements を生成して人間
承認待ちで停止するケースは成果物があるため 1 件分として終了する(空振りではない)。
**成果物の定義**: 実作業コミット・spec 成果物の生成・PR を指す。§2 手順 4 の
初回の空ブランチ push(作業ブランチと同一内容)は成果物に**数えない**。空振りで
手放す際は、その空リモートブランチを削除してよい。

**候補ゼロ(または全候補が空振り)の場合**: `needs-local`(および `checks.unity_ci` が
null のリポジトリの `needs-unity`)でスキップした Issue があればその一覧を
「実機待ちキュー」としてユーザーへ報告して終了する。それも無ければ
「キューは空」と報告して終了する。空振りでもエラーではない。

## 2. claim(着手宣言)

選定した Issue に対して:

1. 状態を **In Progress** に更新し、自分を assignee にする。
2. Issue に claim コメントを 1 件投稿する: 「自律ワーカーが着手(セッション: <このセッションの一意な URL/ID>)。ブランチ: <ブランチ名>」に続けて、
   1 行で `領域: <接頭辞>, <接頭辞>…`(§1 で見積もった触る予定の領域)を書く。作業中に領域が広がったら
   claim コメントに追記する。
3. **競合検出(claim の勝者確定・チーム全体)**: コメント投稿後に、選定した Issue
   だけでなく**チーム内の全 In Progress Issue** の claim コメントを再取得する。
   別セッションの有効な claim(撤退済み・解放済み・放棄回収条件該当を除く)を投稿時刻順に並べ、
   次のどちらかに当たれば敗者として撤退する:
   (a) **同じ Issue** に自分より先の claim がある、
   (b) 自分より先に投稿された有効な claim が `worker.max_parallel` 件以上ある、または
   自分より先の claim と `領域:` が重なる(`領域:` の無い旧形式の claim は全域と重なるものとして扱う)。
   撤退する場合は自分の claim コメントに「競合検出のため撤退」と追記して
   解放し、ブランチも作らない。投稿時刻が同一の場合はセッション ID の辞書順で
   小さい方を勝者とする(決定的規則)。撤退時の Issue 状態の扱いは競合相手の
   claim 先で分ける: **同じ Issue** への競合なら状態・assignee には触れない
   (勝者のもの)。**別の Issue** への競合なら、自分が選定した Issue は誰の管理下
   にも残らないため、**状態を元(Todo / Backlog)へ戻してから**終了する
   (戻し漏れは放棄回収まで最長 `worker.claim_stale_hours` 時間キューから消えるため禁止)。
   **敗者は次の候補にも進まず**「先行ワーカー稼働中」として空振り報告で終了する
   (並行上限を守るため。§1 の空振り再試行はこのケースには適用しない)。
   §1 の事前確認をすり抜けた同時起動も、この投稿後再確認で必ず収束する(全員が同じ
   claim 一覧を同じ規則で評価するため、先着順に上限まで・領域が重ならないものだけが残る)。
4. 勝者確定後、`git-workflow.md` の命名規約でブランチを作成し、**すぐに push** する
   (`<type>/<issue-id>-<topic>`。Linear の PR 連動はブランチ名の Issue ID で効く)。
   **実行環境が作業ブランチを割り当てている場合は例外**(2026-10-01 決定): Routine 等の
   クラウドセッションが `claude/...` などの作業ブランチを割り当て、そのブランチへの
   push を前提・制約としている場合は、命名規約より**割り当てブランチを優先**してそのまま
   使う(別名ブランチを切り直さない。push 拒否での空振りやブランチの二重化を避けるため)。
   この場合ブランチ名による Linear 連動が効かないため、手順 5 で PR 側から必ず紐付ける。
5. **PR の Linear 紐付け**(PR 作成時。ブランチ名に Issue ID が入っていても行う):
   - PR タイトルの先頭に Issue ID を入れる(例: `[HID-12] fix: ...`)。
   - PR 本文に `Fixes <ISSUE-ID>`(例: `Fixes HID-12`)を 1 行入れる。マージ時の
     Done 自動遷移はこのマジックワードで効く。
   - PR 作成後、Linear MCP で Issue を取得し PR が紐付いていること(attachments 等)を
     確認する。紐付いていなければ Linear MCP で PR の URL を Issue の attachment として
     追加する。§1-A の駐機巡回と §1 の放棄 claim 回収は「Issue に紐付く PR」で判定する
     ため、紐付け漏れは禁止。

## 3. 実行

- **spec 由来のタスク**(`.kiro/specs/<feature>/tasks.md` にある作業): 該当タスクを
  `/kiro:spec-impl` のフローで実装する。タスクの完了チェックと検証証跡の更新を忘れない。
- **単発の chore / fix / docs**: 通常どおり実装する。
- **ドキュメントの置き場所**(`git-workflow.md` §7): リリース用ドキュメント整理(下記)以外の
  Issue では `docs.aggregate_paths` のファイルを変更しない。利用者から見える変化があれば、
  Issue に `## ドキュメント反映メモ` のコメント(どのファイルに何を書くかの案)を投稿して
  `docs-pending` ラベルを付け(ラベルがチームに無ければ作る)、PR 本文に要約と Issue への
  リンクを書く。調査メモ・設計検討・作業ログもリポジトリにファイルとして足さず、Issue の
  コメントか Linear プロジェクトの Document に書く。Issue 自体が集約型ドキュメントの
  編集だけを求めている場合(「README に X を追記」等)は、PR を作らずにその内容を
  ドキュメント反映メモとして記録し、`docs-pending` を付けて Done にする(1 件の実作業に数える)。
- **`needs-unity` の Issue**(`checks.unity_ci` があるリポジトリ): 実行環境に Unity は無いので、
  コードを直して push し、PR の CI(`checks.unity_ci.workflow`)の Unity ジョブで検証する。
  赤になったら `gh run view <run> --log-failed` と artifact の結果 XML / Unity ログ
  (`test-results/*.xml` `*.log`)を読んで直し、再 push する(push → CI 待ち → 修正の
  往復は 1 回が数十分かかるので、1 回の push にまとめて直す)。「失敗するテストを直す」
  Issue では、まず Issue 記載の失敗が CI でも再現することを確認してから直す
  (CI で再現しないなら Windows 固有の可能性が高い → `needs-local` に付け替えて手放す)。
  ランナーは 1 台でジョブは直列に走るため、CI の完了は `review.wait_minutes` とは別に
  最大 90 分待ってよい(それでも queued のままなら、ランナーの停止として `needs-human` を
  付けて報告する)。
- 途中で `needs-local` 相当(ローカル環境・実機がないと検証できない)と判明した場合:
  Issue に `needs-local` ラベルを付け、状態を Todo に戻し、理由をコメントして手放す。
  まだ成果物(§1 の定義。初回の空ブランチ push は含まない)が無ければ §1 の
  空振り再試行として次の候補へ進む。すでに作業を積んでいた場合はその旨を報告して
  終了する。
- スコープが Issue の記載から大きく逸脱する判断が必要になった場合: `needs-human`
  ラベルを付けて質問をコメントする(勝手にスコープを広げない)。成果物が無ければ
  claim 解放のうえ §1 の空振り再試行として次の候補へ進み、作業済みなら下記の
  駐機手順で終了する。
- **claim の解放**: 上記を含め、Issue の所有を保持しないまま終了する場合
  (needs-local / needs-human で手放す、実装を断念する等)は、必ず**自分の claim
  コメントに「claim 解放(理由)」を追記して無効化**してから終了する。解放されて
  いない claim は §2 の競合検出で「有効な claim」と見なされ、後続セッションが
  永久に撤退し続けるため、解放漏れは禁止(In Progress のまま異常終了した場合の
  救済は §1 の放棄 claim 回収が担う)。

### リリース用ドキュメント整理(2026-10-14 決定)

Linear ラベル `docs.release_label`(既定 `release-docs`)が付いた Issue は、集約型
ドキュメントをまとめて更新するリリース用の作業として次の手順で進める。

1. 前回までのリリース用 PR(GitHub ラベル `docs.release_label` が付いたマージ済み PR)の本文に
   「取り込んだ Issue」として列挙された Issue に `docs-pending` が残っていれば外す
   (前回のマージ後に外し損ねた分。重複して取り込まないため)。
2. チーム(`linear.project` 設定時はそのプロジェクト)の、状態が Done で `docs-pending` が
   付いた Issue を集め、それぞれの最新の `## ドキュメント反映メモ` を読む。
3. メモを整理して `docs.aggregate_paths` のファイルを更新する。メモの羅列ではなく、
   リリース時点のコードと突き合わせて利用者向けの記述に書き直す(後の Issue で
   覆された変更は最終形だけを書く)。
4. PR に GitHub ラベル `docs.release_label` を付ける(リポジトリに無ければ
   `gh label create <name> --force` で作る)。ラベルが付くまで CI の Docs Policy と
   §4 手順 1 の検査は失敗するので、PR 作成と同時に付ける。PR 本文に「取り込んだ Issue」として
   Issue ID を列挙する。
5. マージを確認したら(§4 末尾、または §1-A 巡回マージの手順 4)、列挙した Issue から
   `docs-pending` を外す。

### 自動マージ待ちの駐機(2026-11-01 決定)

§4 で自動マージを予約した PR は、マージを GitHub に任せて claim を手放す(レビュー到着や CI の完了を
待ってセッションや並行枠を占有しない):

1. 自分の claim コメントに「claim 解放(自動マージ待ちで駐機)」と追記し、`PR: #N` と
   `駐機理由: auto-merge` を併記する。`needs-human` は付けない。
2. Issue は In Progress のまま残す(マージで `Fixes` により Done へ遷移する)。
3. 後から届いた P0/P1・CI 失敗・自動解消できなかったコンフリクトは §1-A の巡回が拾う。

### ユーザー判断待ちの駐機(パーキング、2026-09-24 決定)

ユーザーの判断・承認を待つ状態で終了する場合 — `policy-gate` が人間の承認を求める PR(§4 末尾。
ポリシー変更等)の承認待ち、重大度ベース処理(§4)の needs-human エスカレーション、
requirements の人間承認待ち、spec の NO-GO ゲート — は、Issue を「駐機」させて
キューを塞がない:

1. Issue に `needs-human` ラベルを付け、判断依頼コメント(判断してほしい事項。
   PR があればそのリンクも)を投稿する。
2. **自分の claim コメントに「claim 解放(ユーザー判断待ちで駐機)」と追記して
   解放する**(PR があれば「PR: #N」を併記する。requirements 承認待ち等、PR の
   ない駐機では省略してよい)。あわせて `駐機理由: <種別>` を 1 行で併記する。
   種別は `merge-approval`(マージ承認待ち。`auto_merge.enabled` が false、または
   `policy-gate` が人間の承認を求めている)/ `requirements` / `no-go` / `escalation`(安全弁・
   重大度処理からのエスカレーション)/ `scope`(スコープ逸脱の質問)。
   人間の判断待ちの駐機では `merge-approval` だけが §1-A の巡回対象になる(`auto_merge.merge_parked` が
   有効なら、条件を満たした時点で巡回がマージする。`policy-gate` が承認を求める PR は除く)。
   解放済み claim は §1 の並行度ガードにも §2 の
   競合検出にも数えられないため、後続の定期実行は次の Issue へ着手できる。
3. Issue の状態は **In Progress のまま残す**(選定対象は Todo/Backlog のみなので
   他ワーカーに取られず、オープン PR が紐付いていれば放棄 claim 回収の対象にも
   ならない)。PR が無い駐機(requirements 承認待ち等)は `worker.claim_stale_hours`
   時間で放棄回収の対象になり得るが、`needs-human` ラベルが着手を禁止するため
   回収されても実作業は始まらない。
4. 駐機した PR の subscribe / send_later チェックイン(§5)は維持してよい
   (これらの機構が使える実行環境の場合)。
   ただしユーザーの回答を受けて修正・レビュー対応などの実作業を再開する場合は、
   **編集・テスト・コミットを含む一切の実作業を始める前に** §1 の並行度ガードを
   再確認し、claim を取り直す(新しい claim コメントを投稿し、§2 手順 3 の
   競合検出を経る)。push 直前ではなく再開前に行うこと — 再 claim 前に作業を
   進めると、別ワーカーと隣接ファイルを並行編集してから初めて競合に気づくことに
   なり、並行度ガード・領域の重なり判定の目的が破れる。別セッションの有効な claim があれば作業を
   再開せず、send_later で再試行を予約する。**再 claim が §2 手順 3 の競合検出で
   敗者になった場合は例外**: 新しい claim のみ解放し、Issue は In Progress の
   駐機状態のまま維持する(§2 の「状態を Todo / Backlog へ戻す」は新規選定の
   撤退用であり、駐機 Issue の元状態は In Progress なので適用しない)。
5. ユーザーの判断が済んだら(PR マージによる Done 自動遷移、または回答
   コメント)、作業を再開する側が **再 claim の勝者確定後に** `needs-human` を
   外す(§1-A の巡回によるレビュー対応・CI 修正・コンフリクト解消は判断が済んだことにならないので外さない)(勝者確定前に外すと、競合で敗れた際に Issue が通常の選定対象へ露出する
   ため先に外さない)。マージで Done になった Issue に残ったラベルは無害なので
   放置してよい。

### SDD フェーズ承認ポリシー(2026-09-23 決定。SDD 導入リポジトリのみ)

- **requirements**: 生成まで。承認は必ず人間。生成したら Issue にリンクを
  コメントし、上記の駐機手順(`needs-human` + claim 解放)で終了する。
- **design / tasks**: `/kiro:validate-design` 等のレビューで GO(条件付き GO は
  指摘反映後)なら自動承認して先へ進んでよい。
- **NO-GO ゲート**(spike 等、spec が人間判断と定める箇所): 必ず停止して
  ユーザーへ提示し、上記の駐機手順で終了する。

## 4. レビューとマージ(2026-11-01 改定。GitHub auto-merge + Actions レビュー)

ゲートの判定とマージは GitHub 側が行う(`git-workflow.md` §0・§6)。ワーカーは一次ゲートを
セッション内で通してから push し、`auto_merge.enabled` が true なら**自動マージを予約して claim を
手放す**(§3「自動マージ待ちの駐機」)。レビューの到着を待つためだけにセッションを占有しない。
`auto_merge.enabled` が false(または未設定)のリポジトリでは予約せず、一次ゲートと下記の
二次ゲートを満たしたら §3 の駐機手順(`駐機理由: merge-approval`)で駐機し、§5 の報告に
「マージ承認待ち」として PR を載せる。

PR の base は作業ブランチ(`github.default_branch`)。`github.release_branch` へ PR を出さない・
Release PR(`promote` ワークフローが作る)をマージしない・auto-merge を予約しない(人間がマージする)。

### 軽微な変更(fast track、2026-10-15 決定)

push 前に差分を判定する:

```
python3 .claude/skills/linear-worker/scripts/classify_change.py --base origin/<default_branch>
```

終了コード 0(fast track)になるのは、差分がテキストのみ(`fast_track.text_extensions` の
文書。`fast_track.text_exclude_paths` 配下と、依存・ビルド定義やエージェントへの指示を除く)と
コメントのみ(対応言語のソースで、変更前後ともコードを含まない行 — コメントだけの行・空行 — しか
変わっておらず、コメントを除いたコードも等しい)の組み合わせだけの場合。`auto_merge.protected_paths` の
ファイル・未対応の言語・ファイルの追加/削除(テキスト以外)・字句解析できないファイルを
1 つでも含めば 1(レビュー要)、判定できなければ 2(レビュー要として扱う)。
`fast_track.enabled` が false なら常に 1。

fast track の PR は一次ゲートの手順2(`/code-review`)を省く(spec 由来の実装なら手順3 は省かない)。
`codex-review` ワークフローも同じ判定でレビューを省略する(`provider: app` のリポジトリでは依頼しない)。
CI は `fast_track.require_ci`(既定 true)に従う。オンプレの Unity CI(`checks.unity_ci.workflow`)は
ブランチ保護の必須チェックにしない(テキスト・コメントだけの変更では結果が変わらず、オンプレ機が
止まっていると Queued のまま最長 24 時間終わらないため。2026-10-15 決定)。Unity CI が必須チェックに
なっていてマージできない(`mergeStateStatus` が `BLOCKED`)場合は、保護を回避せず(`--admin` を使わない)
「Unity CI が必須チェックのため完了待ち」と報告する。

fast track の PR でも、外部レビューや人間が自発的に投稿した指摘があれば重大度ベースの指摘処理(下記)で
扱う。判定がレビュー要へ変わった(コードを変える修正を足した等)場合は通常のゲートで扱う。

### 一次ゲート(push 前、セッション内で完結)

1. `checks.fast` の全コマンドがローカルで green。配列が空なら、リポジトリの CI 定義
   (`.github/workflows/`)・`package.json` の scripts・`Makefile` / `pyproject.toml` 等から
   lint / typecheck / test に相当するコマンドを推定して実行し、**実行したコマンドを
   PR コメントまたは §5 の報告に記録する**(config には書き戻さない。固定したい場合は
   人間が `checks.fast` に入れる)。推定できるものが無ければ「該当なし」と記録する。
   Unity のテスト(`-runTests`)は実行環境に Unity が無いので推定対象から外し、
   `checks.unity_ci` があるリポジトリでは CI の Unity ジョブの結果と run の URL を
   「Unity テストの検証証跡」として記録する。`needs-unity` の Issue はこの記録が必須。
   あわせて `python3 .claude/skills/linear-worker/scripts/check_docs_policy.py --base
   origin/<default_branch> --head-ref <ブランチ名> --labels-json '<PR のラベル名の JSON 配列>'`
   を実行し、green であること(集約型ドキュメントを変更していない、またはリリース用 PR)。
   失敗したら集約型ドキュメントの変更を取り消し、内容をドキュメント反映メモへ移す(§3)。
2. `/code-review`(effort: high)を自分で実行し、CONFIRMED / PLAUSIBLE の全 finding を
   修正または理由付きで棄却として記録する。
3. spec 由来の実装なら `/kiro:validate-impl` を実行し、指摘ゼロまたは対応済みであること。
4. 変更がコア(`auto_merge.protected_paths`)・配布物(`sync-manifest.json` の `owned`)に触れるか確かめる。
   触れる場合は `policy-gate` が人間の Approve を求めるので、予約しても自動ではマージされない
   (下記「人間の承認が要る PR」)。配布物はそもそも取り込み側で直接編集しない(配布元を直す)。

### push と自動マージの予約

1. push し、PR を作成する(`gh pr create --base <default_branch>`。draft にしない)。
2. `auto_merge.enabled` が true で、下記「人間の承認が要る PR」に当たらなければ、
   `gh pr merge <N> --auto --<auto_merge.method> --match-head-commit <push したヘッド SHA>` で
   自動マージを予約する。予約がエラーになる(リポジトリで auto-merge が無効・作業ブランチに保護が無い)
   場合は予約せず、下記「二次ゲート」を自分で確かめて `駐機理由: merge-approval` で駐機し、
   報告に「auto-merge が使えない(`scripts/sync/configure_repo.py` の未適用)」と書く。
3. 以後のマージは GitHub が行う: 必須チェック(CI・`codex-review`・`policy-gate`・`docs-policy`)が
   green、未解決のレビュースレッドがゼロになった時点でマージされ、`Fixes <ISSUE-ID>` で Issue が Done になる。
4. 指摘への対応をこのセッションで済ませたい場合だけ、`review_status.py --pr <N>` を 5〜10 分間隔で見る
   (Monitor / ScheduleWakeup 等。bash の `while sleep` ループは使わない)。上限は `review.wait_minutes`
   (現在ヘッドの push から)。`findings` なら下記の重大度ベースの指摘処理で直して push し(予約は
   維持される。`--match-head-commit` は予約時のヘッドにだけ効く)、`clean` / `skipped` / `unavailable` なら
   何もしない。上限に達したら待つのをやめる。
5. §3「自動マージ待ちの駐機」で claim を解放して §5 の報告で終了する。後から届いた P0/P1・CI 失敗・
   自動解消できなかったコンフリクトは §1-A の巡回が拾う。

### 外部レビュー(`review.provider`)

- **`action`**(既定): `codex-review` ワークフローが PR の作成・push ごとに 1 回だけ自動で走り、
  結果を指摘の有無にかかわらず **1 件の PR review** として投稿する(P0/P1 は該当行のスレッド、
  P2 以下は本文の一覧)。2 回目以降は前回からの差分だけを見て、前回の指摘の解消と変更行の P0/P1 だけを
  指摘する。`review.action.max_rounds` 回で打ち止め。作業ブランチの取り込み・コンフリクトの自動解消だけの
  push は再レビューしない。ワーカーはレビューを**依頼しない**。レビューできなかった場合
  (`unavailable`)は `review.fail_open` に従う(true なら他のゲートで進む。その旨はワークフローが PR に記録する)。
- **`app`**(後方互換): レビュー要の push のたびに `review.bot.request_comment` を **1 ヘッド 1 回だけ**
  投稿する(依頼の直前に `review_status.py --provider app` が `running` / `clean` / `findings` を返すなら
  投稿しない)。完了の判定は `review_status.py --provider app` に任せる(ボットの review・summary
  コメントに加え、指摘ゼロのときの 👍 リアクションも `clean` として扱う)。依頼から 30 分経っても `pending` なら
  1 回だけ再依頼してよい。`review.wait_minutes` を過ぎても完了しなければ `timeout` となり、
  「外部レビューが待機期限内に応答しなかったため、一次ゲートと CI のみに基づき進行する」旨を PR に記録して
  進める(フェイルオープン。2026-09-24 決定)。**これは「レビューを通過したとみなす」ものではない** —
  人間の承認が要る PR には適用しない。`provider: app` ではボットのレビュー完了が必須チェックにならないので、
  自動マージの予約は `review_status.py` が `clean` / `findings`(対応済み)/ `timeout` を返してから行う。
- **null**: 外部レビューなし。PR を作った時点で報告に「外部レビューなし」と書く。

期限後に遅れて届いた指摘: マージ前なら重大度ベースの指摘処理で扱う。マージ後なら新規の指摘として扱い
(該当箇所を直す別 PR、または軽微なら優先度 Low の Issue として Backlog に起票し、本文に元の PR と指摘への
リンクを書く。`docs/backlog.md` 等のファイルには追記しない — `git-workflow.md` §7)、マージ済み PR を巻き戻さない。

### 二次ゲート(GitHub が判定する。`auto_merge.enabled` が false のときはワーカーが同じ条件を確かめる)

- CI が**現在のヘッド SHA** で green(CI が設定されていないリポジトリでは「CI なし」と記録する)
- 外部レビュー: `review_status.py` が現在ヘッドに対して `clean` / `findings`(P0/P1 は対応済み)/
  `skipped` / `unavailable`(fail-open)/ `timeout`(`provider: app`)
- `policy-gate` が成功(不変条件・コアと配布物の変更に対する人間の承認)
- **未解決のレビュースレッドがゼロ**: 各 finding スレッドに
  (a) 修正 push + コミット SHA を明記した返信、または
  (b) 対応不要の理由を明記した返信、を投稿したうえで
  **`resolveReviewThread` で明示的に resolve し、スレッド一覧を再取得して
  `isResolved` を確認**する(GitHub のスレッドは返信だけでは解決済みに
  ならない。「最後のコメントが自分」は解決の代替にならない)

**重大度ベースの指摘処理**(2026-09-24 決定): 外部レビューの finding は重大度で処理を分け、周回を数えない。

- **P1 以上(P0・P1・blocker/critical/security 等の最上位相当を含む。実害あり)**: 必ず対応する。
  内容を検証し、正しければ修正 push(`provider: app` なら依頼を 1 回)、誤検知・非該当なら
  理由付き返信 + resolve で棄却する(P1 以上の黙殺は禁止)。
- **P2 以下(nit・提案・仮定的・スコープ外)**: `provider: action` ではスレッドにならない(review 本文の一覧)
  ので対応不要。`provider: app` 等でスレッドになったものは個別対応せず、まとめて resolve し、PR に
  **一括棄却コメントを 1 件だけ**投稿して記録する(例: 「P2 以下の指摘は運用ポリシーにより自動棄却」)。
  検証して実害があると判明した指摘に限り「P1 以上」として扱い直してよい。重大度ラベルが無い・判別できない
  finding は P2 として扱わず、内容を検証してどちらかに振り分ける。
- **暴走防止の安全弁**: P1 以上の修正 push に対して新たな P1 以上が
  **3 回連続**で返る場合は、自動マージの予約を取り消し(`gh pr merge <N> --disable-auto`)、
  `needs-human` として §3 の駐機手順(`駐機理由: escalation`)でユーザーへエスカレーションする
  (P2 以下の件数はこの回数に数えない)。

### 人間の承認が要る PR(`policy-gate` が止める)

PR が次のいずれかに当たる場合、`policy-gate` は現在ヘッドへの人間の Approve が無い限り失敗する
(ワーカーが自分の制約を自分で緩めないための仕組み。`git-workflow.md` §6-1)。ワーカーはこれを
迂回しない(ブランチ保護の変更・`--admin`・チェックの再実行で通そうとする等は禁止)。

- コア(`auto_merge.protected_paths`。既定 `.github/` `.harness/` `.kiro/orchestration/` `.kiro/settings/`
  `.claude/settings.json` `.codex/`)を変更する
- 配布物(`sync-manifest.json` の `owned`)を変更する(配布元からの同期 PR で、内容が配布元の指定コミットと
  完全一致すると検証できたものを除く)

こうした PR は自動マージを予約せず、承認待ちに入った時点で §3 の駐機手順(`駐機理由: merge-approval`)に
従い claim を解放して終了する(判断待ち PR 1 件で後続の定期実行を塞がない)。
指示文(`CLAUDE.md` `AGENTS.md`、リポジトリ固有の `.claude/` の rules・skills 等)はここに当たらない —
`codex-review` と `policy-gate` の不変条件を通れば自動マージしてよい(人間は Release PR でまとめて見る)。

### 手動でマージする場合(`auto_merge.enabled` が false の巡回マージ、§1-A)

マージは `auto_merge.method`(既定 merge commit)方式、`expectedHeadSha` を必ず指定し、
マージ後にリモートのヘッドブランチを削除する(`gh pr merge --match-head-commit <SHA>
--delete-branch`。削除だけ失敗した場合はマージ成功扱いで報告に残す)。merge queue が
必須のブランチではコマンドの成功はキュー投入にすぎないため、PR の `state` が
`MERGED` になったことを確かめてから完了扱い(Done 確認・報告)にする(§1-A 巡回マージの
手順 3 と同じ)。

いずれの経路でも、マージ後に Linear の自動遷移(Done)を確認し(次回の巡回で確認してよい)、失敗して
いれば手動で Done にする。リリース用 PR なら、本文に列挙した Issue から `docs-pending` を外す
(§3「リリース用ドキュメント整理」手順 5)。

## 5. 報告と終了

終了時に必ず:

1. Issue へ結果コメント(やったこと・PR リンク・残件)を投稿する。
2. ユーザー向けの完了報告(セッションの最終メッセージ)に、処理した Issue、
   PR、マージ有無、次に人間の判断が必要な事項を 1 か所にまとめる。
3. 未マージの PR を残して終了する場合は、実行環境で使えるなら subscribe + send_later の
   チェックインを設定してから終了する(放置しない)。使えない環境では次回の定期実行が
   駐機 PR を拾えるよう、Issue コメントに PR と待ち事項を明記する。
4. **最新状態を Linear に書き、古くなった Notion の記述を直す**(下記)。空振り・
   先行ワーカー稼働中による終了でも行う。§0 の前提ガードで終了した場合(config や
   Linear MCP が無い)は書けないので省略し、その旨を最終メッセージに含める。

### 最新状態の記録(2026-09-30 決定)

人間が PR や Issue を個別に開かなくても「いまキューがどうなっているか」を把握できる
ようにする。詳細な記録は **Linear**、人間が管理する要約は **Notion** と役割を分ける。

#### Linear: スナップショット(詳細はすべてこちら)

各起動の終了時に次の内容をテキストでまとめる。Linear から毎回取り直した
**現在の状態**を書く(前回のスナップショットの差分更新はしない)。

- 実行日時(UTC)とこのセッションの URL / ID
- この起動の結果: 巡回・処理した Issue と PR、やったこと、マージ有無
  (§1-A の巡回マージを含む。駐機を続けた PR は巡回マージの欠けた条件も)
  (空振りならその理由と試行した候補)
- **人間の対応待ち**(最重要。先頭に置く): `needs-human` の Issue ごとに
  駐機理由・PR・判断してほしい事項。§1-A でレビュー対応・CI 修正・コンフリクト解消を
  行った PR はその旨も。駐機 PR の CI 状態(green / 失敗中 / 既存の失敗でスコープ外)と
  コンフリクト有無も併記する。開いている Release PR(`github.release_branch` がある場合)の URL と
  取り込み待ちの PR 数も載せる
- 進行中: 有効な claim がある In Progress Issue(`領域:` も)と、自動マージ待ち(`駐機理由: auto-merge`)の PR
- 実機待ちキュー: `needs-local` の Issue(`checks.unity_ci` が null のリポジトリでは
  `needs-unity` の Issue も)
- 次の候補: §1 の選定条件で上位 3 件
- ヘルス: 人間待ちが無ければ `onTrack`、人間待ちがあれば `atRisk`、エラーで作業を
  中断したなら `offTrack`

`reporting.linear_status` が false でなければ、`linear.project` のプロジェクトに
ステータス更新(project update)として投稿する。ヘルスは上記。直近のステータス更新と
「人間の対応待ち」「進行中」「実機待ちキュー」「次の候補」が同一で、この起動で何も
処理していない(空振り)場合は投稿しない(毎時の同文投稿で履歴を埋めないため)。
`linear.project` が null なら投稿せず、その旨を報告に書く。

#### Notion: 古くなった箇所だけを書き換える

Notion のページは人間が管理・把握する要約であり、膨らませると人間が追えなくなる。
ワーカーは**この起動の結果で記述が事実と食い違った箇所だけ**を直し、ページを育てない。

- **対象ページ**: `linear.project` のプロジェクトの Overview(説明本文)に書かれた
  Notion ページのリンク。リンクが無い・複数あってどれか決められない場合は Notion を
  スキップし、その旨を最終メッセージに書く(ページを推測で探したり新規作成したりしない)。
  `reporting.notion` が false、または `linear.project` が null ならスキップする。
- **手順**: ページを取得し、この起動で変わった事実(Issue の状態・PR のマージ・
  駐機・判断待ち事項・次の作業など)と矛盾する記述を探す。矛盾が無ければ**何も書かない**
  (空振りの起動では通常ここで終わる)。
- **書き換えの制約**:
  - 古くなった箇所(文・箇条・表のセル単位)を、**元の文字数と同程度かそれ以下**の
    新しい記述で置き換える。元の箇所より長くしない。
  - 節・見出し・表の行・実行ログなど**新しい記述を足さない**。古くなった記述の
    削除は、置き換える内容が無い場合に限り行ってよい。
  - 古くなっていない箇所、人間が書いた方針・メモ・判断理由には触れない。
  - 字数に収まらない詳細は Notion に書かず Linear(上記スナップショット)に書く。
    必要なら置き換え後の文を「詳細は Linear のステータス更新参照」程度に縮める。
- Notion MCP が使えない・ページにアクセスできない場合はスキップして最終メッセージに
  書く。

どちらも書き込みに失敗しても作業自体は完了扱いとし、失敗した旨を最終メッセージ
(この起動で処理した Issue があればその結果コメントにも)に残す(エラーで作業結果を
巻き戻さない)。

## 6. 禁止事項

- 1 回の起動で 2 件以上の Issue の実作業(コミット・spec 成果物生成・PR)を進めること
  (空振り再試行のための claim → 解放はこれに当たらない。並列化は `worker.max_parallel` で
  ワーカーの数を増やして行い、1 ワーカーが複数件を抱えない)
- `needs-human` の付いた Issue への着手、および人間承認ゲートの代行
  (例外: §1-A の巡回で、`駐機理由: merge-approval` の PR に生じた P1 以上の指摘・
  CI 失敗・マージコンフリクトへ再 claim のうえ対応すること。その場合も `needs-human` は
  外さない。また `auto_merge.merge_parked` が有効なとき、§1-A の巡回マージ条件を
  すべて満たした `merge-approval` 駐機 PR をマージすること。`policy-gate` が人間の承認を
  求める PR、requirements・NO-GO・スコープ等の人間判断は代行しない)
- push 済みブランチの rebase(force-push を要するため。コンフリクトは作業
  ブランチのマージで解消する)
- レビュー指摘(自レビュー・外部ボットとも)を黙殺したままのマージ
  (§4 の重大度ベース処理による P2 以下の一括棄却は、一括コメントと resolve の
  記録が残るため黙殺に当たらない)
- force-push、テストのスキップ/無効化による green 化
- コア(`auto_merge.protected_paths`)・配布物を変更する PR を人間の承認なしにマージさせること
  (`policy-gate` の迂回 — ブランチ保護・必須チェックの変更、`--admin`、`.harness/invariants.json` の
  緩和を含む)
- リリースブランチ(`github.release_branch`)への PR 作成・Release PR のマージ・その auto-merge の予約
- `.kiro/orchestration/config.json` の値をワーカー自身が書き換えること
