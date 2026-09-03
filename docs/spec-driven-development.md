# Spec-Driven Development with GitHub spec-kit — Reference Implementation

> **Purpose of this document**: A self-contained explanation of how specification-based
> development is implemented in the `tailoredai` (Dalberg MCP Pipeline) repo using
> GitHub's **spec-kit**, with real artifacts from a shipped feature as the worked
> example. Written so another agent (or engineer) can understand and replicate the
> setup without access to this repo.

---

## 1. What this is

Spec-driven development (SDD) here means: **no non-trivial feature goes straight to
code.** Every feature flows through a pipeline of AI-authored, human-gated artifacts —
specification → plan → task list → implementation — all stored in-repo next to the
code they produced. The framework is [github/spec-kit](https://github.com/github/spec-kit)
(version `0.12.8.dev0`), initialized with the Claude Code integration
(`ai_skills: true`, sequential feature numbering).

The system has three pillars:

| Pillar | Location | Role |
|---|---|---|
| Framework layer | `.specify/` | Constitution, templates, deterministic bash scripts, state |
| Command layer | `.claude/skills/speckit-*/` | Ten slash commands, one per pipeline phase |
| Artifact layer | `specs/NNN-feature-name/` | Per-feature spec, plan, tasks, design docs |

---

## 2. Directory layout

```text
.specify/
├── memory/
│   └── constitution.md          # Project principles — enforced as a gate, not advice
├── templates/
│   ├── spec-template.md         # Copied into each new feature dir as the starting spec
│   ├── plan-template.md
│   ├── tasks-template.md
│   ├── checklist-template.md
│   └── constitution-template.md
├── scripts/bash/
│   ├── create-new-feature.sh    # Branch/dir naming, numbering, template copy
│   ├── common.sh                # Feature-state resolution (which feature is active)
│   ├── setup-plan.sh            # Scaffolds plan phase
│   ├── setup-tasks.sh           # Scaffolds tasks phase
│   └── check-prerequisites.sh   # Verifies upstream artifacts exist before a phase runs
├── workflows/speckit/workflow.yml   # Declarative full-cycle definition with review gates
├── feature.json                 # {"feature_directory": "specs/007-..."} — active feature
├── init-options.json            # How the project was initialized
└── integration.json             # Installed integration (claude)

.claude/skills/
├── speckit-specify/SKILL.md     # Each skill = the full prompt-procedure for one phase
├── speckit-clarify/SKILL.md
├── speckit-plan/SKILL.md
├── speckit-tasks/SKILL.md
├── speckit-analyze/SKILL.md
├── speckit-checklist/SKILL.md
├── speckit-implement/SKILL.md
├── speckit-converge/SKILL.md
├── speckit-taskstoissues/SKILL.md
└── speckit-constitution/SKILL.md

specs/
├── 001-mcp-entra-oauth/         # Fully executed features...
├── 002-fix-opensearch-sigv4-auth/
├── 003-confidential-redaction/
├── 004-mcp-retrieval-latency/
├── 005-sqs-event-ingestion/     # ← the worked example below (complete artifact set)
├── 006-dquals-faiss-quantization/
└── 007-soft-facet-fallback/     # ← mid-pipeline: spec.md + checklist only, no plan yet
```

---

## 3. The pipeline

### Commands (slash-command skills)

| Command | Phase | Produces |
|---|---|---|
| `/speckit-specify <description>` | Specify | `spec.md` — requirements only, zero implementation detail |
| `/speckit-clarify` | Clarify | Up to 5 targeted questions; answers are encoded back into `spec.md` |
| `/speckit-plan` | Plan | `plan.md` + `research.md`, `data-model.md`, `contracts/`, `quickstart.md` |
| `/speckit-tasks` | Tasks | `tasks.md` — dependency-ordered, story-grouped task list |
| `/speckit-checklist` | Gate | Quality checklist for the current artifact |
| `/speckit-analyze` | Gate | Non-destructive cross-artifact consistency analysis (spec ↔ plan ↔ tasks) |
| `/speckit-implement` | Implement | Executes `tasks.md`, checking off tasks as they land |
| `/speckit-converge` | Converge | Diffs shipped code against spec/plan/tasks; appends unbuilt work as new tasks |
| `/speckit-taskstoissues` | Optional | Converts tasks to GitHub issues |
| `/speckit-constitution` | Governance | Amends the constitution + syncs dependent templates |

### The full cycle with human gates

`.specify/workflows/speckit/workflow.yml` declares the canonical sequence — note the
**human review gates** between phases, with rejection aborting the run:

```yaml
steps:
  - id: specify
    command: speckit.specify
  - id: review-spec
    type: gate
    message: "Review the generated spec before planning."
    options: [approve, reject]
    on_reject: abort
  - id: plan
    command: speckit.plan
  - id: review-plan
    type: gate
    message: "Review the plan before generating tasks."
    options: [approve, reject]
    on_reject: abort
  - id: tasks
    command: speckit.tasks
  - id: implement
    command: speckit.implement
```

### Deterministic scaffolding (the bash layer)

The AI commands shell out to bash scripts for everything that should be
deterministic, so naming/numbering/state never depend on model behavior:

- **`create-new-feature.sh`** — takes the natural-language description, strips stop
  words, builds a 3–4-word kebab-case short name, computes the next sequential
  number by scanning `specs/` for the highest `NNN-` prefix, creates
  `specs/NNN-short-name/`, copies in `spec-template.md`, and persists the active
  feature to `.specify/feature.json`. Supports `--json` (machine-readable output for
  the calling agent), `--dry-run`, `--short-name`, `--number`.
- **`common.sh`** — resolves "which feature is active" with a precedence chain:
  `SPECIFY_FEATURE_DIRECTORY` env var → `.specify/feature.json` → error. This lets
  downstream commands (`plan`, `tasks`, `implement`) find their inputs without the
  user restating the feature.
- **`check-prerequisites.sh`** — refuses to run a phase whose upstream artifact is
  missing (no plan without a spec, no tasks without a plan).

Feature directory names double as **branch names** (`005-sqs-event-ingestion` is
both), keeping the spec history and git history aligned.

---

## 4. The constitution: principles as a merge gate

`.specify/memory/constitution.md` is the piece that makes this more than scaffolding.
It is versioned (semver — currently v1.0.0), amended only via `/speckit-constitution`
(which must propagate changes to the templates in the same commit), and its
principles are written **from real incidents in this codebase**, not generic advice:

1. **Source-Agnostic Retrieval** — new data sources go through the source registry
   (`config/retrieval_sources.yaml`), never hardcoded into the MCP tool layer.
2. **Reuse Before Building** — search for an existing helper/client/pattern before
   writing a new one; a new abstraction is only justified when nothing fits.
3. **No Endpoint Ships Without an Explicit Auth Decision** *(non-negotiable)* —
   exists because an auth middleware was once removed with a "reinstate before
   production" comment that was never acted on, leaving a production server open to
   the internet. Comments like that may no longer be merged at all.
4. **Structured, Loud Observability** — `structlog` everywhere; fragile fallback
   modes must announce themselves at boot, not fail silently later.
5. **Environment-Driven Configuration** — every knob flows through
   `pydantic-settings` from env vars, and must be added to `.env.example` with a
   comment **in the same change**.

Two enforcement mechanisms make these binding:

- The constitution's *Development Workflow* section **mandates** the spec-kit
  pipeline for all non-trivial features (new endpoints, auth flows, external
  integrations). Only small isolated fixes are exempt.
- Every `plan.md` must contain a **Constitution Check** section, evaluated as a
  gate before implementation begins. Any violation must be explicitly justified in
  the plan's *Complexity Tracking* section.

---

## 5. Worked example: feature `005-sqs-event-ingestion`

This feature (event-driven Airtable ingestion via an SQS poller + worker) ran the
entire pipeline end-to-end. Its artifact set:

```text
specs/005-sqs-event-ingestion/
├── spec.md              # WHAT: user stories, functional requirements, edge cases
├── plan.md              # HOW: tech context, constitution check, file-level change map
├── research.md          # Phase 0: design decisions (port strategy from a donor commit)
├── data-model.md        # Phase 1: entities — cursor, queue message, job ledger, config
├── contracts/
│   └── internal-interfaces.md   # message schema, state-store layout, CLI contracts
├── quickstart.md        # Phase 1: local + EC2 validation guide
├── checklists/
│   └── requirements.md  # spec quality gate, passed before planning was allowed
└── tasks.md             # Phase 2: the executable task list (all checked off)
```

### 5.1 The spec — implementation-free, prioritized, testable

Specs are organized around **user stories with priorities**, each carrying an
*Independent Test* and Given/When/Then acceptance scenarios, followed by numbered
functional requirements and an explicit edge-case inventory. Excerpt:

```markdown
### User Story 1 - New/updated Airtable documents are ingested automatically (Priority: P1)

A consultant adds or replaces a document attachment on a D.Quals record in Airtable.
Without anyone running a manual pipeline command, the document is extracted, embedded,
and becomes searchable through the MCP retrieval tools within minutes.

**Why this priority**: This is the core value of the feature — today ingestion only
happens when someone manually runs a batch sync, so search results go stale between runs.

**Independent Test**: Modify one attachment column on one Airtable record; verify the
record's document appears in search results within one polling cycle plus processing
time, with no manual command issued.

**Acceptance Scenarios**:
1. **Given** the scheduled poller and background worker are running, **When** a
   record's watched attachment column is modified in Airtable, **Then** within one
   polling cycle the record is queued and the worker extracts, embeds, and indexes
   its documents.
...

### Functional Requirements
- **FR-001**: The system MUST detect Airtable records whose watched attachment
  columns changed since the last poll and queue exactly one message per changed record.
- **FR-002**: Change detection MUST be scoped per configured target table via a
  per-target opt-in flag, so additional tables can be enabled through configuration alone.
```

The spec also carries a dated amendment trail (e.g. a password-protected-attachment
edge case added as an *amendment 2026-07-22*), so requirement changes are visible in
place rather than buried in chat history.

### 5.2 The quality checklist — gate between spec and plan

`checklists/requirements.md` verifies the spec before planning may begin. Key items:

```markdown
## Content Quality
- [x] No implementation details (languages, frameworks, APIs)
- [x] Written for non-technical stakeholders

## Requirement Completeness
- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Success criteria are measurable and technology-agnostic
- [x] Edge cases are identified

## Notes
- All prior ambiguities were resolved directly with the user before specification: ...
- Named technologies that appear (Airtable, SQS, S3, Docker, cron, Claude) are
  pre-existing environmental constraints of this system, not implementation choices
  introduced by this spec.
- Ready for `/speckit-plan`.
```

Note the "Notes" convention: it records that clarifications were resolved *with the
user* and explains why named technologies don't violate the no-implementation-detail
rule — decisions with rationale, in-repo.

### 5.3 The plan — file-precise, constitution-checked

`plan.md` contains a Technical Context block (language, dependencies, storage,
testing, target platform, performance goals, constraints, scale), then the gate:

```markdown
## Constitution Check
*GATE: evaluated against `.specify/memory/constitution.md` v1.0.0 — PASS
(pre-Phase-0 and re-checked post-design).*

- **I. Source-Agnostic Retrieval**: PASS — no retrieval/MCP-layer changes; ingestion-side only.
- **II. Reuse Before Building**: PASS — reuses `S3Uploader`, `pipeline/common/aws.py`
  client factories (extends with `sqs_client` in the same module), ...
- **III. Explicit Auth Decision (NON-NEGOTIABLE)**: PASS — no new HTTP/MCP endpoints. ...
- **IV. Structured, Loud Observability**: PASS — ... reset script prints explicit
  PASS/FAIL health checks and an unmissable temporary-cadence banner ...
- **V. Environment-Driven Configuration**: PASS — all knobs via env
  (`WORKER_MAX_ATTEMPTS` added with comment) and `config/airtable_ingestion.yaml`.

No violations → Complexity Tracking not required.
```

The plan then maps the change **file by file**, marking each NEW or EDIT, down to
individual function additions:

```text
src/pipeline/
├── common/
│   ├── aws.py                   # EDIT: + sqs_client()
│   └── state_store.py           # NEW (donor: PipelineStateStore)
└── airtable_ingestion/
    ├── models.py                # EDIT: + poll_enabled, allowed_extensions, ...
    └── pipeline.py              # EDIT (hand-port): keys_written plumbing, format gate
```

### 5.4 The tasks — phased, dependency-ordered, story-tagged

`tasks.md` uses the format **`[ID] [P?] [Story] Description`** where `[P]` marks
tasks safe to run in parallel and `[US1]` ties a task to the user story it serves.
Phases run: Setup → **Foundational (blocking)** → one phase per user story *in
priority order*, each ending in a **Checkpoint** describing verifiable state:

```markdown
## Phase 2: Foundational (Blocking Prerequisites)
**⚠️ CRITICAL**: complete before any user story phase

- [x] T003 [P] Port additive hunk: add `sqs_client(...)` to `src/pipeline/common/aws.py`
- [x] T004 Port new file verbatim: `src/pipeline/common/state_store.py` (depends on T002)
- [x] T009 Unit test: `tests/unit/test_state_store.py` — cursor round-trip + ...

**Checkpoint**: state store, queue producer, SQS client, changed-record queries,
and config fields all exist and are tested

## Phase 3: User Story 1 — Automatic ingestion (Priority: P1) 🎯 MVP
**Independent Test**: quickstart.md "EC2 validation" steps 2–4

- [x] T010 [US1] Hand-port `src/pipeline/airtable_ingestion/pipeline.py` per plan Phase B: ...
- [x] T011 [P] [US1] Port `scripts/run_poller.py` verbatim from donor; only edit: ...
```

Key properties: the P1 phase is a **deliverable MVP on its own**; tasks name exact
files and exact edits (traceable back to the plan); tests are tasks too; checkboxes
record execution state. All of 005's tasks are checked — the feature shipped.

### 5.5 Lifecycle state across features

The `specs/` directory shows the pipeline in different stages simultaneously:
001–006 have full artifact sets (executed), while `007-soft-facet-fallback` has only
`spec.md` + a checklist — spec written, awaiting `/speckit-plan`. The active-feature
pointer `.specify/feature.json` currently targets 007.

---

## 6. How to replicate this setup

1. **Initialize**: `specify init` (spec-kit CLI) in the repo with your AI integration
   (here: `--ai claude` with skills). This creates `.specify/` and the command skills.
2. **Write a real constitution** via `/speckit-constitution` — derive principles from
   your project's actual failure modes, not boilerplate. Mark true non-negotiables.
   Mandate the workflow itself in the constitution so it is self-enforcing.
3. **Per feature**: `/speckit-specify <natural-language description>` →
   review/`/speckit-clarify` → `/speckit-checklist` → `/speckit-plan` (must pass the
   Constitution Check) → `/speckit-tasks` → `/speckit-analyze` → `/speckit-implement`
   → `/speckit-converge` if code and spec drift.
4. **Conventions that make it work**:
   - Specs describe *what/why* only; checklists enforce that no *how* leaks in.
   - User stories carry priorities and independent tests; P1 alone must be shippable.
   - Plans are file-precise and constitution-gated; violations need written justification.
   - Tasks are the single execution ledger — checked off in place, story-tagged,
     with `[P]` parallelism markers and per-phase checkpoints.
   - Clarifications and amendments are written back into the artifacts with dates
     and rationale, so the "why" survives the chat session that produced it.

---

*Source repo: Dalberg MCP Pipeline (`tailoredai`). Framework: github/spec-kit
0.12.8.dev0, Claude Code integration, sequential feature numbering. Constitution
v1.0.0, ratified 2026-07-08.*
