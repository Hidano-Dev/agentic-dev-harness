# Confirmation Channels — ユーザー確認・通知の単一チョークポイント

Orchestrator からユーザーへの**すべての**確認（エスカレーション）と通知（完了報告等）は、
このファイルの手順を経由する。目的は、確認チャネル（現在: Claude の質問窓 / 将来: Slack, Discord）を
**Adapter 節の追記だけで**差し替えられる状態を維持すること。

## 設計ルール（チャネル追加時も不変）

1. 確認リクエストは**チャネル非依存の形式**（下記）で組み立ててから、Adapter に渡す
2. リクエスト本文は**自己完結**させる。「上記の通り」「先ほどの設計」のような、
   会話履歴や画面を見ていないと理解できない参照を含めない（Slack / Discord 越しに読んでも
   単体で判断できる文面にする）
3. 回答を受け取ったら、リクエストと回答の両方を進行ログに記録してから処理を再開する
4. 回答が得られない限り先へ進まない（タイムアウト時のデフォルト動作は将来のチャネル用
   フィールドであり、現行の claude チャネルでは使わない）
5. **例外 — 進行ログ作成前の確認（pre-log confirmation）**: feature 名が未確定で
   `.kiro/orchestration/<feature>/log.md` がまだ存在しない時点の確認（Step 1 のマルチ Spec 判定）は、
   `feature: (未確定)` / `gate: none` で組み立ててよい。この場合はルール 3 を次のように読み替える:
   - 単一 Spec と確定して Phase 1 へ進む場合 → 回答を保持し、Step 2 で進行ログを作成した時点で
     `templates/orchestration-log.md` の **`## Pre-flight: マルチ Spec 判定` 節**（Phase エントリより前）に
     リクエストと回答を記録する
   - 分割経路へ抜けて進行ログを作成しない場合 → 完了報告にリクエストと回答をそのまま明記する

   記録先が存在しないことを理由に、確認内容と回答の記録を省略してはならない。

## 確認リクエスト形式（ConfirmationRequest）

```yaml
type: confirm | notify        # confirm = 回答が必要 / notify = 報告のみ
feature: <feature-name>       # 未確定の時点（設計ルール 5）では `(未確定)`
gate: S | A | B | C | D | E | none    # 対応するゲート（フェーズ外のエラー等は none）
title: <1行の件名>
context: |
  判断に必要な背景の要約（3〜10行）。何が起き、なぜユーザーの判断が必要かを自己完結で書く
artifacts:
  - <参照すべきファイルパス>   # 例: .kiro/specs/<f>/design.md の該当セクション
question: <確認したいことを1文で>   # type: confirm のみ
options:                       # type: confirm のみ。2〜4個。推奨があれば先頭に置き「(推奨)」を付す
  - label: <選択肢>
    description: <選ぶと何が起きるか>
timeout_default: <将来のチャネル用。無応答時のデフォルト選択肢 label。現行は未使用>
```

## チャネル設定

`.kiro/orchestration/config.json` を読む（無ければ `claude` チャネルとして動作。
テンプレート: `templates/orchestration-config.json`）:

```json
{
  "confirmation_channel": "claude",
  "channels": {
    "claude": {},
    "slack": { "webhook_env": "ORCH_SLACK_WEBHOOK_URL", "wait": "unsupported" },
    "discord": { "webhook_env": "ORCH_DISCORD_WEBHOOK_URL", "wait": "unsupported" }
  }
}
```

`confirmation_channel` に未実装のチャネルが指定されていた場合は、その旨を通知したうえで
`claude` チャネルにフォールバックする（黙って無視しない）。

## Adapters

### claude（現行の実装）

- **confirm**: AskUserQuestion ツールで送信する
  - `question` ← question、`header` ← gate 名（例: "Gate B"）、`options` ← options をそのまま対応させる
  - `context` は question の前に要約して含める（AskUserQuestion の質問文は自己完結が必須）
  - 選択肢は 4 個以内。「Other」は自動で付くので自前で追加しない
- **notify**: 通常のアシスタントメッセージとして出力する（ツール不要）

### slack（将来実装 — 設計のみ）

- **notify**: `channels.slack.webhook_env` の環境変数から Webhook URL を取得し、
  ConfirmationRequest を Block Kit に整形して POST する
- **confirm**: Webhook は一方向のため回答回収は未対応（`wait: "unsupported"`）。
  実装するときは Socket Mode / リアクション回収 / スレッド返信ポーリングのいずれかを
  `scripts/` に追加し、この節の `wait` 手順を書き換える。**SKILL.md 本体の変更は不要**
- 実装までの暫定動作: notify は slack へ送りつつ、confirm は claude チャネルへフォールバックする
  （通知と確認でチャネルが分かれてよい）

### discord（将来実装 — 設計のみ）

- slack と同一の設計。Webhook POST の整形が Discord Embed になる点のみ異なる

## 文面ガイド

- `title` と `question` は、それだけ読んでも判断対象がわかる具体性にする
  （悪い例: 「設計を承認しますか？」/ 良い例: 「csv-import: 新規依存 papaparse の導入を承認しますか？」）
- `options` の `description` には「選んだ場合に Orchestrator が次に何をするか」を書く
- 技術的判断を求めるときは、推奨案と根拠を必ず含める（丸投げしない）
