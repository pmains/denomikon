# Contract System v1.0

Standardized task brief and result schemas for auditable delegation across the
Poliscopic agent team.

## Files

| File | Purpose |
|---|---|
| `task-brief.schema.json` | Queen → specialist dispatch contract |
| `task-result.schema.json` | Specialist → Queen return envelope |
| `researcher-result.schema.json` | Researcher-specific extension |
| `analyst-result.schema.json` | Analyst-specific extension |
| `writer-result.schema.json` | Writer-specific extension |
| `engineer-result.schema.json` | Engineering roles extension (data-engineer, software-engineer, devops-engineer) |
| `worker-result.schema.json` | Worker-specific extension |
| `validations.md` | Validation rules and rejection criteria |
| `examples/research-only.md` | Test workflow 1: external research |
| `examples/analysis-only.md` | Test workflow 2: internal analysis |
| `examples/full-pipeline.md` | Test workflow 3: Researcher → Analyst → Writer → Review |

## Usage

### Dispatching a task

Every brief must include:
- `task_id` — unique identifier
- `why_this_agent` — justification for delegation
- `success_criteria` — measurable conditions for `succeeded` status
- `required_output` — format and location of deliverable
- `dependencies` — upstream tasks or artifacts

### Receiving a result

Validate the return before accepting:
1. `task_id` matches the dispatched brief
2. `agent` matches the assigned specialist
3. `status` is in the approved vocabulary
4. If `succeeded`, all success criteria are addressed
5. All claimed artifacts exist on disk
6. Role-specific required fields are present

### Status vocabulary

| Status | Meaning |
|---|---|
| `succeeded` | Every success criterion satisfied |
| `failed` | Task cannot complete |
| `blocked` | External dependency missing |
| `changes_required` | Work done but criteria not fully met |

### Artifact handoffs

Pass artifact file paths to downstream agents, not reconstructed summaries.
This preserves provenance and prevents context contamination.

## Principles

1. **Queen decides** what work is needed. Specialists perform judgment-intensive work.
2. **Validation is deterministic.** Every brief and result is checked against schemas.
3. **Success is binary.** Either all criteria are met (`succeeded`) or they aren't (any other status).
4. **Artifacts are the source of truth.** Summaries describe results; artifacts prove them.
