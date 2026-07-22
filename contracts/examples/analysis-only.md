# Test Workflow 2: Internal Data Analysis Only

**Objective:** Validate Analyst brief → result contract on a database-interrogation task.

## Brief

```json
{
  "contract_version": "1.0",
  "task_id": "test-analysis-001",
  "request_source": "queen",
  "objective": "Analyze the frequency and distribution of transportation-related agenda items across Maricopa County jurisdictions in the last 90 days. Identify which jurisdictions have the most transportation activity and which topics dominate.",
  "why_this_agent": "The primary evidence is inside Poliscopic's database — agenda items, meetings, and bodies. Analyst is the correct agent for interrogating internal project data.",
  "inputs": [
    {
      "type": "instruction",
      "location": "N/A",
      "description": "The database contains agenda items with body, meeting_date, meeting_type, and agenda_item_text. The topic_report configuration has the transportation keyword list."
    },
    {
      "type": "file",
      "location": "scripts/reports/config.yaml",
      "description": "Report configuration including transportation keywords for item filtering."
    }
  ],
  "required_work": [
    "Query the agenda_items table for transportation-related items in the last 90 days",
    "Count items by jurisdiction (body)",
    "Count items by meeting type (regular session, study session, etc.)",
    "Identify the most common topics/actions (using keyword clusters or item titles)",
    "Calculate trends: are transportation items increasing, decreasing, or stable?"
  ],
  "out_of_scope": [
    "Do not perform external research on transportation policy",
    "Do not write articles or editorial content",
    "Do not modify the database or schema"
  ],
  "success_criteria": [
    "Query covers at least 90 days of data",
    "Item counts are broken down by jurisdiction and meeting type",
    "Trend assessment included (direction and magnitude)",
    "Methodology documented (which queries, filters, keywords used)",
    "Confidence assessment per finding",
    "All calculations reproducible from documented queries"
  ],
  "required_output": {
    "format": "markdown",
    "location": "contracts/examples/test-analysis-001-result.md"
  },
  "dependencies": [],
  "risks": [
    "Keyword matching may miss items that use different terminology",
    "Some jurisdictions may have inconsistent agenda item text quality",
    "90-day window may include holidays or recess periods affecting volume"
  ],
  "deadline": null
}
```

## Expected Result

```json
{
  "contract_version": "1.0",
  "task_id": "test-analysis-001",
  "agent": "analyst",
  "status": "succeeded",
  "summary": "Analyzed 246 transportation-related agenda items across 12 jurisdictions in the last 90 days. Maricopa County Board of Supervisors accounts for 38% of all transportation items. Road abandonment/de-annexation is the most common action type (29% of items). Monthly volume is stable with no significant trend.",
  "artifacts": [
    {
      "path": "contracts/examples/test-analysis-001-result.md",
      "type": "markdown",
      "description": "Analysis memorandum with tables, counts, trend assessment, and methodology."
    }
  ],
  "evidence": [
    {
      "type": "database",
      "location": "poliscopic_dev",
      "description": "PostgreSQL database queried for transportation items. Query: SELECT body, meeting_date, agenda_item_title FROM agenda_items JOIN meetings ..."
    }
  ],
  "limitations": [
    "Keyword match may miss transportation items that use non-standard terminology",
    "Items without agenda_item_text could not be classified and are excluded",
    "90-day window does not capture seasonal patterns that require 12+ months"
  ],
  "errors": [],
  "recommended_next_step": "Pass to Writer if a public-facing analysis is needed, or to Researcher for external context on observed patterns.",

  "methodology": [
    "Queried agenda_items JOIN meetings for items where agenda_item_title ILIKE any keyword from the transportation config",
    "Grouped by body_code and meeting_type",
    "Applied procedural-item filter to exclude call-to-order, minutes, etc.",
    "Classified action types by keyword clusters (road: abandonment, de-annexation; safety: DUI, enforcement; etc.)"
  ],
  "data_scope": {
    "source": "poliscopic_dev database, agenda_items + meetings tables",
    "temporal_range": "2026-04-17 to 2026-07-17",
    "record_count": 246,
    "filters_applied": ["Keyword match against transportation terms", "Procedural item exclusion"],
    "limitations": ["Records without agenda_item_text excluded", "Some jurisdictions did not meet during period"]
  },
  "calculations": [
    { "name": "Total transportation items", "method": "COUNT(items)", "result": "246" },
    { "name": "Items by jurisdiction", "method": "COUNT GROUP BY body_name", "result": "BOS: 93, Phoenix: 41, Mesa: 23, ..." },
    { "name": "Trend direction", "method": "Month-over-month comparison", "result": "Stable (±5% month to month)" }
  ],
  "reproducibility": "Query: SELECT b.name, m.meeting_date, ai.agenda_item_title FROM agenda_items ai JOIN meetings m ON ... WHERE m.meeting_date >= '2026-04-17' AND (title ILIKE '%road%' OR title ILIKE '%transit%' ...). Full query in analysis output artifact.",
  "confidence": {
    "overall": "high",
    "per_finding": [
      { "finding": "Item counts by jurisdiction", "level": "high", "reason": "Direct database counts from reliable records" },
      { "finding": "Action type distribution", "level": "medium", "reason": "Keyword-based classification may mis-categorize some items" },
      { "finding": "Trend assessment", "level": "medium", "reason": "90-day window insufficient for robust trend detection" }
    ]
  }
}
```
