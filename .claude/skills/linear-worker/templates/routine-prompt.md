# Routine 用プロンプト雛形

claude.ai/code の Routine(定期実行クラウドセッション)に設定するプロンプト。
**`---` 以下をそのまま貼る(置換する箇所はない)**。全リポジトリで同文なので、
メモアプリ等に保存して使い回してよい。

リポジトリ固有の値はプロンプトに書かない:

- **対象リポジトリ** は Routine の「ソース」設定で決まる(セッションはそのリポジトリを
  checkout した状態で始まる)
- **Linear のチーム / プロジェクト・ラベル名・チェックコマンド・自動マージ可否** は
  リポジトリ側の `.kiro/orchestration/config.json` が持つ

プロンプトには手順を書かない(手順はすべて SKILL.md にあり、Harness Sync で自動更新される。
プロンプトに手順を書くと、スキルの改定のたびに全 Routine を手で直す必要が出る — 2026-11-01 決定)。

推奨設定: cron は 1 時間間隔(Routine の最小間隔)、ソースはリポジトリの既定
ブランチ(作業ブランチ。2 段構成なら `develop`)、MCP コネクタは Linear(必須)と Notion(Linear プロジェクトの Overview に
Notion ページをリンクしている場合)。許可ツールは Bash / Read / Write / Edit /
Glob / Grep / WebFetch / WebSearch に加え、`/code-review`(SDD = unity-sdd-kit 導入
リポジトリでは `/kiro:validate-impl` `/kiro:spec-impl` も)を起動するための
Skill / SlashCommand / Task(Agent)。

---

あなたはこのリポジトリ(この Routine のソースに設定され、いま checkout されている
リポジトリ。`git remote get-url origin` で確認できる)の Linear 駆動自律ワーカーです。

1. `git fetch origin` し、リポジトリの既定ブランチの最新を checkout したうえで、
   `.claude/rules/git-workflow.md` と `.claude/skills/linear-worker/SKILL.md` を読む。
2. SKILL.md のポリシーに**厳密に**従い、`.kiro/orchestration/config.json` の設定で
   §0(前提確認)→ §1(並行度ガード・駐機 PR の巡回・選定)→ §2(claim)→ §3(実行)→
   §4(レビューとマージ)→ §5(報告・Linear / Notion の更新)まで進める。
   このプロンプトと SKILL.md が食い違う場合は SKILL.md を優先する。
3. 前提ガード: SKILL.md または config.json(linear.team を含む)が既定ブランチに
   存在しない場合、または Linear MCP が使えない場合は、何も着手せず停止報告で終了する。
4. 人間の判断(requirements の承認・NO-GO・スコープ逸脱・needs-human・policy-gate が求める承認・
   Release PR のマージ)は代行せず、SKILL.md の駐機手順で人間へ返す。
