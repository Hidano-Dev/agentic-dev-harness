# Agentic SDLC and Spec-Driven Development

Kiro-style Spec-Driven Development on an agentic SDLC

## Project Context

### Paths
- Steering: `.kiro/steering/`
- Specs: `.kiro/specs/`

### Steering vs Specification

**Steering** (`.kiro/steering/`) - Guide AI with project-wide rules and context
**Specs** (`.kiro/specs/`) - Formalize development process for individual features

### Active Specifications
- Check `.kiro/specs/` for active specifications
- Use `/kiro:spec-status [feature-name]` to check progress

## Development Guidelines
- Think in English, generate responses in Japanese. All Markdown content written to project files (e.g., requirements.md, design.md, tasks.md, research.md, validation reports) MUST be written in the target language configured for this specification (see spec.json.language).

## Scale Routing（spec 作業の着手前に必ず判定）
依頼内容が「requirements → design → tasks → 実装を他と独立に回せる単位（= 1 Spec）」を何個含むかを最初に判定する:
- **単一 Spec** → 下記 Minimal Workflow へ（`/dev-orchestrator`、`/kiro:spec-init-dig`、または Phase 1 を個別実行）
- **複数 Spec にまたがる** → `/kiro:spec-init` / `/kiro:spec-init-dig` に**直行しない**。分割方針を議論したセッション内で `/kiro:spec-split` を実行し、`/clear` 後に `/kiro:spec-init-batch` で各 Spec を初期化する
- 判定に迷う場合は分割の要否をユーザーに確認する

## Minimal Workflow
- Full auto: `/dev-orchestrator "description"` — spec-init から実装・PR 作成までを自動オーケストレーション（着手前にスコープ確認を 1 回行い、以降の承認はポリシーに基づき代行。人間の判断が必要なものだけ確認。詳細: `.claude/skills/dev-orchestrator/SKILL.md`）
- One-shot spec: `/kiro:spec-init-dig "description"` — init から tasks まで一括（dig インタビュー・検証ゲート付き）
- Multi-spec: `/kiro:spec-split`（ブレストしたセッション内）→ `/clear` → `/kiro:spec-init-batch` — 複数 Spec への分割とバッチ初期化
- Phase 0 (optional): `/kiro:steering`, `/kiro:steering-custom`
- Phase 1 (Specification):
  - `/kiro:spec-init "description"`
  - `/kiro:spec-requirements {feature}`
  - `/kiro:validate-gap {feature}` (optional: for existing codebase)
  - `/kiro:spec-design {feature} [-y]`
  - `/kiro:validate-design {feature}` (optional: design review)
  - `/kiro:spec-tasks {feature} [-y]`
- Phase 2 (Implementation): `/kiro:spec-impl {feature} [tasks]`
  - `/kiro:validate-impl {feature}` (optional: after implementation)
- Progress check: `/kiro:spec-status {feature}` (use anytime)

## Development Rules
- 3-phase approval workflow: Requirements → Design → Tasks → Implementation
- Human review required each phase; use `-y` only for intentional fast-track
- Keep steering current and verify alignment with `/kiro:spec-status`
- Follow the user's instructions precisely, and within that scope act autonomously: gather the necessary context and complete the requested work end-to-end in this run, asking questions only when essential information is missing or the instructions are critically ambiguous.

## Steering Configuration
- Load entire `.kiro/steering/` as project memory
- Default files: `product.md`, `tech.md`, `structure.md`
- Custom files are supported (managed via `/kiro:steering-custom`)

## Git Workflow
<!--
  取り込み側テンプレート (unity-sdd-template 等) は自身の CLAUDE.md から
  本ファイルだけを import する契約のため、Git 運用ルールはここから相対 import で
  読み込ませる（@ の相対パスは import を書いたファイルのディレクトリ基準で解決される）。
-->
@git-workflow.md
