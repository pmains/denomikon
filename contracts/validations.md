# Contract Validation Rules

Every task brief and result passes through a validation gate before and after delegation.
These rules determine whether a contract is accepted, rejected, or flagged.

---

## Rule 1: `task_id` must match

The result's `task_id` must exactly match the brief's `task_id`.
A mismatch means the wrong work was returned or the result is from a different dispatch.

**Severity:** fatal. The result is discarded. Queen must re-dispatch or investigate.

---

## Rule 2: `agent` must match the assigned specialist

The result's `agent` field must be the same specialist that received the brief.
If the result claims to be from a different agent, the contract is invalid.

**Severity:** fatal. Discard the result.

---

## Rule 3: `status` must use the approved vocabulary

Approved statuses:

| Status | Meaning |
|---|---|
| `succeeded` | Every success criterion from the brief was satisfied. |
| `failed` | The task cannot complete. Something is wrong. |
| `blocked` | An external dependency is missing. Task could complete once unblocked. |
| `changes_required` | Work was done but success criteria were not fully met. The agent identified specific gaps. |

`succeeded` does **not** mean the agent attempted the work. It means every stated
success criterion was demonstrably satisfied. If the agent cannot verify a criterion,
it must return `blocked`, `failed`, or `changes_required`.

**Severity:** fatal if status is not in the approved set. Warning if `succeeded` is
used but the result's own `limitations` or `errors` suggest criteria were not fully met.

---

## Rule 4: `succeeded` requires all success criteria to be addressed

The result's summary or artifacts must explicitly address every success criterion
from the brief. If any criterion is unaddressed, the status cannot be `succeeded`.
Queen should verify this before accepting the result.

**Responsibility:** Queen. The agent should address all criteria, but Queen must
validate before routing downstream.

---

## Rule 5: Every claimed artifact must exist

Every entry in `artifacts[].path` must be a readable file at the specified location.
Artifacts that don't exist are grounds for `changes_required` or `failed`.

**Severity:** error. A missing artifact invalidates the claim of completion.

---

## Rule 6: Blocking limitations prevent `succeeded`

If the limitations array contains entries that contradict any success criterion,
the status cannot be `succeeded`. Examples:

- Criterion: "Identify all transportation-related items." Limitation: "Keyword match may miss items that use different terminology."
- Criterion: "Query must cover the last 14 days." Limitation: "Data only available from 7 days ago."

**Severity:** warning. Queen should flag this and decide whether to accept the
result or request revision.

---

## Rule 7: Role-specific required fields

| Agent | Required fields beyond common envelope |
|---|---|
| researcher | `sources`, `source_quality`, `freshness` |
| analyst | `methodology`, `data_scope`, `calculations` |
| writer | `claims` |
| data-engineer | `changed_files`, `tests` (if applicable) |
| software-engineer | `changed_files`, `tests` (if applicable) |
| devops-engineer | `changed_files` |
| worker | `command`, `working_directory`, `pid`, `log_path`, `exit_status` |

**Severity:** fatal if required fields are missing.

---

## Rule 8: Downstream agents receive artifact paths, not summaries

When passing a result to a downstream agent, Queen must provide artifact file paths
rather than pasting the full summary into the next brief. This preserves provenance
and prevents context contamination.

Exception: very short outputs where a file on disk adds unnecessary overhead.
Queen should note the exception in the brief.

---

## Rule 9: `contract_version` must match between brief and result

The result's `contract_version` must exactly match the version in the brief.
A mismatch means the schemas are incompatible.

**Severity:** warning. Accept only if the difference is known and compatible.

---

## Validation Quick Reference

```python
def validate_result(brief: dict, result: dict) -> list[dict]:
    issues = []

    # Rule 1
    if result.get("task_id") != brief.get("task_id"):
        issues.append({"rule": 1, "severity": "fatal", "message": "task_id mismatch"})

    # Rule 2
    if result.get("agent") != brief.get("assigned_agent", result.get("agent")):
        issues.append({"rule": 2, "severity": "fatal", "message": "agent mismatch"})

    # Rule 3
    if result.get("status") not in ("succeeded", "failed", "blocked", "changes_required"):
        issues.append({"rule": 3, "severity": "fatal", "message": f"invalid status: {result.get('status')}"})

    # Rule 5
    for art in result.get("artifacts", []):
        if not os.path.exists(art["path"]):
            issues.append({"rule": 5, "severity": "error", "message": f"artifact not found: {art['path']}"})

    # Rule 7
    role_fields = {
        "researcher": ["sources", "source_quality", "freshness"],
        "analyst": ["methodology", "data_scope", "calculations"],
        "writer": ["claims"],
        "worker": ["command", "working_directory", "pid", "log_path", "exit_status"],
    }
    agent = result.get("agent")
    if agent in role_fields:
        for field in role_fields[agent]:
            if field not in result:
                issues.append({"rule": 7, "severity": "fatal", "message": f"missing required field '{field}' for {agent}"})

    # Rule 9
    if result.get("contract_version") != brief.get("contract_version"):
        issues.append({"rule": 9, "severity": "warning", "message": "contract_version mismatch"})

    return issues
```
