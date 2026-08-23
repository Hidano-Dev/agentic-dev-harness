# Orchestration Log: {{FEATURE_NAME}}

- Started: {{TIMESTAMP}}
- Mode: {{MODE}} <!-- 許容値: new | resume -->
- Options: {{OPTIONS}} <!-- --stop-after 等。指定がなければ none -->
- Environment: {{ENVIRONMENT}} <!-- 許容値: local | degraded-claude-only | cloud-impl-skipped -->

<!-- 以下、各フェーズ完了ごとに追記する。既存エントリは書き換えない -->

<!--
  Pre-flight: 新規モードの Step 1 でマルチ Spec 判定をユーザーに確認した場合のみ、
  Phase エントリより前に 1 回だけ記録する（confirmation-channels.md 設計ルール 5）。
  確認しなかった場合はこの節ごと省略する。
-->

## Pre-flight: マルチ Spec 判定 — {{TIMESTAMP}}

- Question: {{QUESTION}}
- Answer: {{ANSWER}}
- Decision: {{DECISION}} <!-- 許容値: single-spec | split -->

## Phase {{PHASE_NUMBER}}: {{PHASE_NAME}} — {{TIMESTAMP}}

- Command: `{{COMMAND}}`
- Result: {{RESULT_SUMMARY}} <!-- SubAgent サマリ 1〜3行 -->
- Reviewer: {{REVIEWER_RESULT}} <!-- validate コマンドと結果。なければ none -->
- Gate {{GATE}}: {{GATE_DECISION}}
  <!-- GATE 許容値: S | A | B | C | D | E -->
  <!-- GATE_DECISION 許容値: AUTO-APPROVED | CONFIRMED | ESCALATED | REJECTED -->
  - Rationale: {{RATIONALE}} <!-- どの基準に照らして何を確認したか（1〜3行） -->
  - Escalation: {{ESCALATION_RECORD}} <!-- ESCALATED / CONFIRMED (Gate S) の場合: 質問 / 選択肢 / ユーザーの回答。なければ none -->
  - Retry: {{RETRY_RECORD}} <!-- 差し戻し再実行した場合: 回数と理由。なければ none -->
- Branch/PR: {{BRANCH_PR}} <!-- Phase 5〜6 のみ: 作業ブランチ名 / PR URL。それ以外は n/a -->
