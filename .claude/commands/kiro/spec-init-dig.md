---
description: Run spec-init + spec-requirements + dig interview + validate-gap + spec-design + validate-design + spec-tasks in one shot (single spec only — work spanning multiple independent features must go through /kiro:spec-split → /kiro:spec-init-batch instead)
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, SlashCommand, TodoWrite, ToolSearch, AskUserQuestion
argument-hint: <project-description>
---

# Spec Init + Requirements + Dig + Validate + Design + Tasks (one-shot)

## Purpose

Chain the full spec workflow in a single session, from description to implementation-ready tasks, with validation gates after each artifact:

1. **spec-init** — create feature directory and initial spec files from the project description
2. **spec-requirements** — generate EARS-format requirements via the spec-requirements-agent
3. **dig (inline)** — interactively refine requirements.md using `AskUserQuestion` in the **main session context**
4. **validate-gap** — validate requirements against the existing codebase
5. **spec-design** — generate design.md from the dig-refined requirements
6. **validate-design** — validate design.md against requirements
7. **spec-tasks** — generate tasks.md from the design

Use this when starting a new spec where you already have a reasonably detailed project description and want to end up with a ready-to-implement task list in one sitting. The dig interview doubles as the user's review of requirements, so design and tasks proceed with auto-approval (`-y`). Each validate command runs exactly once; validation findings are applied in place, and validation failures are **soft** (warn and continue) — they never abort the workflow.

**Important**: The dig phase is intentionally inlined here (not a `/dig` sub-invocation). The standalone `/dig` command runs in a forked Agent subprocess and cannot reach `AskUserQuestion`, so it would silently degrade to a non-interactive fallback. Running dig inline in the main session keeps `AskUserQuestion` usable.

## Parse Arguments

- Project description: `$ARGUMENTS`

If `$ARGUMENTS` is empty, abort and instruct the user: `Usage: /kiro:spec-init-dig "<project description>"`.

### Multi-Spec Guard

Before anything else, judge whether the description contains **two or more units that could each run requirements → design → tasks → implementation independently**. If so, abort WITHOUT creating any files:

```
❌ この依頼は複数の Spec にまたがります。分割方針を議論したセッション内で /kiro:spec-split を実行し、
/clear 後に /kiro:spec-init-batch で各 Spec を初期化してください。
```

**Exception**: when invoked from `/kiro:spec-init-batch`, skip this guard — the plan document has already split the work into single-spec units (the description referencing `.kiro/multi-spec/` context is not a multi-spec signal).

## Progress Tracking

Initialize TodoWrite with 8 tasks:

```json
[
  {"content": "Initialize spec", "activeForm": "Initializing spec", "status": "pending"},
  {"content": "Generate requirements", "activeForm": "Generating requirements", "status": "pending"},
  {"content": "Load AskUserQuestion tool", "activeForm": "Loading AskUserQuestion tool", "status": "pending"},
  {"content": "Run dig interview", "activeForm": "Running dig interview", "status": "pending"},
  {"content": "Validate gap", "activeForm": "Validating gap", "status": "pending"},
  {"content": "Generate design", "activeForm": "Generating design", "status": "pending"},
  {"content": "Validate design", "activeForm": "Validating design", "status": "pending"},
  {"content": "Generate tasks", "activeForm": "Generating tasks", "status": "pending"}
]
```

Update after each phase (completed → next in_progress).

---

## Phase 1: Initialize Spec (inline implementation)

Mark task 1 as `in_progress`.

### Steps

1. **Generate Feature Name**
   - Convert `$ARGUMENTS` to lowercase kebab-case
   - Keep it concise (2-4 words ideally)
   - Strip special characters

2. **Check Uniqueness**
   - Glob `.kiro/specs/*/` — if the name already exists, append `-2`, `-3`, etc. until unique
   - Record the final feature name as `{feature-name}` for later phases

3. **Create Directory**
   - `mkdir -p .kiro/specs/{feature-name}`

4. **Initialize Files from Templates**

   Read:
   - `.kiro/settings/templates/specs/init.json`
   - `.kiro/settings/templates/specs/requirements-init.md`

   If either template is missing, abort with a clear error message pointing at the missing path.

   Replace placeholders:
   - `{{FEATURE_NAME}}` → generated feature name
   - `{{TIMESTAMP}}` → current ISO 8601 UTC timestamp (`date -u +"%Y-%m-%dT%H:%M:%SZ"`)
   - `{{PROJECT_DESCRIPTION}}` → `$ARGUMENTS`

   Write:
   - `.kiro/specs/{feature-name}/spec.json`
   - `.kiro/specs/{feature-name}/requirements.md`

5. **Update TodoWrite** — task 1 `completed`, task 2 `in_progress`.

6. **Output Progress**

   ```
   ✅ Spec initialized at .kiro/specs/{feature-name}/
   ```

**Continue IMMEDIATELY to Phase 2. Do not stop for user confirmation.**

---

## Phase 2: Generate Requirements

Task 2 is already `in_progress`.

### Execute SlashCommand

```
/kiro:spec-requirements {feature-name}
```

Wait for the subagent to finish. It will populate the `## Requirements` section of `requirements.md` with EARS-format requirements.

**IMPORTANT**: Ignore any "次のステップ" / "Next Step" guidance in the subagent output. It is for standalone usage.

Update TodoWrite — task 2 `completed`, task 3 `in_progress`.

Output:

```
✅ Requirements generated → Loading AskUserQuestion tool...
```

---

## Phase 3: Load AskUserQuestion Tool

`AskUserQuestion` is a deferred tool — its schema must be loaded before invocation. Run:

```
ToolSearch(query="select:AskUserQuestion", max_results=1)
```

Confirm the `<functions>` block for `AskUserQuestion` is returned. If the load fails, abort with an error and instruct the user to invoke `AskUserQuestion` manually after running dig.

Update TodoWrite — task 3 `completed`, task 4 `in_progress`.

Output:

```
✅ AskUserQuestion schema loaded → Starting dig interview...
```

---

## Phase 4: Dig Interview (inline, in this session)

Task 4 is already `in_progress`.

### Target File

`.kiro/specs/{feature-name}/requirements.md`

The `## Project Description (Input)` section is the original user input and MUST be preserved verbatim. Refinement targets the `## Requirements` section (and any sections you add like `## Open Questions and Decisions`, `## Dig Summary`).

### Why Inline, Not `/dig`

**Do NOT invoke `/dig` as a SlashCommand here.** The `/dig` plugin command declares `context: fork` + `agent: General-purpose`, which runs it in a forked Agent subprocess. Forked subagents cannot call `AskUserQuestion` (tool not available in that context), so `/dig` would silently fall back to non-interactive mode and defeat the purpose of this one-shot command. Execute the dig workflow inline in the current main session instead.

### Inline Dig Workflow

#### Step 4.1 — Context Gathering (silent)

Read, but do not ask questions yet:
- `.kiro/specs/{feature-name}/requirements.md` (just generated)
- `CLAUDE.md` (project + user global) for conventions/constraints
- Any `.kiro/steering/*.md` files present
- Related spec files referenced from requirements.md (if any)

Build a mental model of:
- **Stated goals** — what the feature must achieve
- **Stated constraints** — explicit bounds (scope/non-scope)
- **Implicit assumptions** — things taken for granted but not defended
- **Missing topics** — major areas not addressed at all

#### Step 4.2 — Assumption Mapping (silent)

Rank implicit assumptions by **risk** (how badly things go wrong if wrong). Categories:
- Feasibility — "this can be built with X"
- User/Caller — "consumers will behave this way"
- Scope — "does/doesn't include X"
- Dependency — "service/component X will be available"
- Architectural — "current architecture supports this"
- Performance — "this scales to N without issue"

Start the investigation from the **highest-risk** assumptions.

#### Step 4.3 — Deep Investigation (iterative rounds, using AskUserQuestion)

Each round:

1. Pick 2–3 questions targeting the current highest-risk unknowns (fewer = deeper). Do not exceed 3 per round.
2. Each question has **2–4 concrete options**, each with brief pros/cons in the `description`.
3. The first option should be the recommended one (append `(Recommended)` to its label).
4. Do NOT include an "Other" option — it is auto-added by the UI.
5. Align options with patterns from `CLAUDE.md` and `.kiro/steering/` when relevant.
6. Prefer single-select (`multiSelect: false`) unless the choices are genuinely independent.

Question categories to draw from (not a checklist — pick what actually matters):
- Assumption challenges ("The plan assumes X. Is this actually the case?")
- Trade-offs ("You chose X, but have you considered Y?")
- Scale/growth, failure modes, dependencies, security/privacy
- Maintenance/operational burden, migration/rollback, competing priorities

**Depth over breadth**: after each answer, analyze what **new** assumptions the answer reveals, and follow that thread at least 2 levels deep before switching to a fresh topic.

#### Step 4.4 — Apply Decisions (after each round)

Immediately write the decisions back to `.kiro/specs/{feature-name}/requirements.md`:

- Add or append a `## Open Questions and Decisions (Dig)` section (place it before `## Requirements` if not already present).
- Record each decision with an ID (`D-1`, `D-2`, …), the chosen option, rationale, and risk level.
- Cross-reference decisions from the relevant EARS Acceptance Criteria (`… (see D-3)`).
- If a decision changes an AC's wording, update the AC in place — do not leave stale text.
- Preserve existing AC numbering when possible; append new ACs rather than renumbering.

#### Step 4.5 — Completeness Evaluation

After applying each round, check honestly:
- [ ] All high-risk assumptions have been addressed
- [ ] Reached 2+ levels of depth on each major topic
- [ ] No new follow-up questions remain unasked from the last round
- [ ] Trade-offs explicitly acknowledged (not just decided)
- [ ] Failure modes for critical paths discussed
- [ ] requirements.md reflects every decision made

If any box is unchecked → return to Step 4.3 with the remaining items.
If all checked → proceed to the Final Summary.

Before finalizing, append a `## Dig Summary` section to requirements.md containing:
- Rounds completed, questions asked, decisions made (counts)
- Key discoveries (top 2–3 most impactful findings)
- All decisions table (ID, topic, decision, rationale, risk)
- Remaining risks (acknowledged but unresolved — flag for design phase)

### Rules (inline dig)

- **Always** use `AskUserQuestion` for questions — never conversational prose questions
- Each option carries brief pros/cons in its `description`
- `multiSelect: false` by default
- Align options with `CLAUDE.md` / `.kiro/steering/` patterns when present
- **Write decisions to the plan file** after every round — do not batch them to the end
- Know when to stop: complete the Step 4.5 checklist honestly, don't loop indefinitely

Update TodoWrite — task 4 `completed`, task 5 `in_progress` after the dig investigation is done and `## Dig Summary` is written.

Output:

```
✅ Dig interview complete → Validating gap...
```

**Continue IMMEDIATELY to Phase 5. Do not stop for user confirmation.**

---

## Phase 5: Validate Gap

Task 5 is already `in_progress`.

### Execute SlashCommand

```
/kiro:validate-gap {feature-name}
```

This validates the dig-refined requirements against the existing codebase (gap analysis).

### Handle Results

- Review the validation output. If it surfaces **critical** gaps or contradictions (requirements that conflict with existing code, missing prerequisites), apply the necessary corrections to `.kiro/specs/{feature-name}/requirements.md` directly — preserve dig decision IDs and AC numbering per the Phase 4 rules.
- Minor observations: do not edit requirements; carry them into the Final Summary's validation section.
- **This gate is soft**: if the command is missing in this project, errors out, or reports problems you cannot resolve, record the outcome (including `SKIPPED` if unavailable), note it for the Final Summary, and continue anyway.
- **Exception — audit violation**: if validate-gap reports an **audit violation** (unauthorized writes outside research.md, baseline tampering, or `APPEND_VIOLATION`), do NOT treat it as soft. The working tree may have been modified or corrupted during validation: **stop the workflow here**, report the violation details, and wait for the user's decision. Never record it as `SKIPPED` and continue to design.

**IMPORTANT**: Ignore any "次のステップ" / "Next Step" guidance in the subagent output. It is for standalone usage.

Update TodoWrite — task 5 `completed`, task 6 `in_progress`.

Output:

```
✅ Gap validation done → Generating design...
```

**Continue IMMEDIATELY to Phase 6. Do not stop for user confirmation.**

---

## Phase 6: Generate Design

Task 6 is already `in_progress`.

### Execute SlashCommand

```
/kiro:spec-design {feature-name} -y
```

The `-y` flag auto-approves requirements — the dig interview already served as the user's interactive review, so do not stop to ask for approval here.

Wait for the subagent to finish. It will generate `.kiro/specs/{feature-name}/design.md`.

**IMPORTANT**: Ignore any "次のステップ" / "Next Step" guidance in the subagent output. It is for standalone usage.

Update TodoWrite — task 6 `completed`, task 7 `in_progress`.

Output:

```
✅ Design generated → Validating design...
```

**Continue IMMEDIATELY to Phase 7. Do not stop for user confirmation.**

---

## Phase 7: Validate Design

Task 7 is already `in_progress`.

### Execute SlashCommand

```
/kiro:validate-design {feature-name}
```

This validates design.md for consistency with requirements.md and overall design quality.

### Handle Results

- If the validation surfaces **critical** issues (requirements not covered by the design, internal contradictions), fix `.kiro/specs/{feature-name}/design.md` in place before proceeding.
- Minor suggestions: leave design.md as-is; carry them into the Final Summary's validation section.
- **This gate is soft**: if the command is missing, errors out, or reports problems you cannot resolve, record the outcome (including `SKIPPED` if unavailable), note it for the Final Summary, and continue anyway.

**IMPORTANT**: Ignore any "次のステップ" / "Next Step" guidance in the subagent output. It is for standalone usage.

Update TodoWrite — task 7 `completed`, task 8 `in_progress`.

Output:

```
✅ Design validation done → Generating tasks...
```

**Continue IMMEDIATELY to Phase 8. Do not stop for user confirmation.**

---

## Phase 8: Generate Tasks

Task 8 is already `in_progress`.

### Execute SlashCommand

```
/kiro:spec-tasks {feature-name} -y
```

The `-y` flag auto-approves the design. Wait for the subagent to finish. It will generate `.kiro/specs/{feature-name}/tasks.md`.

**IMPORTANT**: Ignore any "次のステップ" / "Next Step" guidance in the subagent output. It is for standalone usage.

Update TodoWrite — task 8 `completed`.

---

## Final Summary

After all phases complete, output:

```
✅ spec-init-dig complete!

## Feature
- Name: {feature-name}
- Directory: .kiro/specs/{feature-name}/

## Generated Files
- .kiro/specs/{feature-name}/spec.json
- .kiro/specs/{feature-name}/requirements.md  (EARS requirements + dig decisions + gap fixes applied)
- .kiro/specs/{feature-name}/design.md        (validate-design fixes applied)
- .kiro/specs/{feature-name}/tasks.md

## Decisions from dig
<Table of decisions captured during dig, if available>

## Validation Results
- validate-gap:    <PASSED / FIXED (what was corrected) / warnings carried over / SKIPPED (reason)>
- validate-design: <PASSED / FIXED (what was corrected) / warnings carried over / SKIPPED (reason)>
<List any unresolved warnings the user should review before implementation>

## Next Steps
1. Review .kiro/specs/{feature-name}/design.md and tasks.md
2. Start implementation: /kiro:spec-run {feature-name}
```

---

## Safety & Fallback

### Empty Arguments
If `$ARGUMENTS` is empty or whitespace only, abort before Phase 1 with:

```
❌ Project description required.
Usage: /kiro:spec-init-dig "<project description>"
```

### Template Missing (Phase 1)
- Report which template file is missing (`init.json` or `requirements-init.md`)
- Suggest verifying `.kiro/settings/templates/specs/` exists
- Do not proceed to Phase 2

### Requirements Generation Failure (Phase 2)
- Stop workflow, leave spec directory intact
- Suggest: `Re-run manually with /kiro:spec-requirements {feature-name}`

### ToolSearch Failure (Phase 3)
- Skip Phase 4 (the inline dig workflow relies on `AskUserQuestion`) and continue from Phase 5 (validate-gap) — dig refinement is an enhancement, not a prerequisite for the rest of the workflow
- Tell the user: `AskUserQuestion could not be loaded — skipping the dig interview and proceeding to validation/design/tasks. To dig later, load the tool via ToolSearch(query="select:AskUserQuestion") and ask me to "run the dig interview on .kiro/specs/{feature-name}/requirements.md".`
- Mark task 3 as failed in TodoWrite, then set task 5 `in_progress`

### Dig Failure (Phase 4)
- Requirements.md is already generated and usable without dig refinement
- Do NOT suggest the standalone `/dig` command as a fallback — it runs forked and cannot use AskUserQuestion
- Suggest: `Re-run /kiro:spec-init-dig on the same description, or ask me in a fresh session to "dig on .kiro/specs/{feature-name}/requirements.md" after loading AskUserQuestion via ToolSearch`
- If the dig failure is non-fatal (requirements.md exists and is well-formed), still proceed to Phase 5 — validation, design, and tasks are more valuable than a perfect dig

### Validation Failure (Phase 5 / Phase 7)
- **Do not stop the workflow for an ordinary validation failure** — these gates are soft
- If `/kiro:validate-gap` or `/kiro:validate-design` is not available in this project, errors out, or times out: record `SKIPPED` with the reason, report it in the Final Summary's Validation Results, and continue to the next phase
- Unresolved validation warnings go into the Final Summary, not into an abort
- **Sole exception**: an **audit violation** reported by validate-gap (unauthorized writes outside research.md, baseline tampering, `APPEND_VIOLATION`) stops the workflow — the working tree may be corrupted, so report the violation and wait for the user instead of continuing

### Design Generation Failure (Phase 6)
- Stop workflow; requirements.md (with dig decisions) is intact and usable
- Suggest: `Re-run manually with /kiro:spec-design {feature-name} -y`

### Tasks Generation Failure (Phase 8)
- Stop workflow; requirements.md and design.md are intact and usable
- Suggest: `Re-run manually with /kiro:spec-tasks {feature-name} -y`

---

## Execution Rules

- Do **not** stop between phases in normal operation — this is a one-shot command
- Only stop early on hard errors (missing args, missing templates, generation failures in Phase 2/6/8) — validation failures (Phase 5/7) are soft, **except an audit violation from validate-gap** (unauthorized writes / baseline tampering / `APPEND_VIOLATION`), which stops the workflow for user judgment
- Each validate command (`validate-gap`, `validate-design`) MUST be attempted exactly once per run — never skip silently; a skip must be recorded and reported
- The only interactive pauses are the `AskUserQuestion` rounds inside the dig interview (Phase 4) — never pause for phase-transition approval
- Ignore "次のステップ" guidance from sub-commands — those are for standalone usage
- Keep intermediate output terse; the final summary is where detail goes
