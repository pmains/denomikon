# Test Workflow 1: External Research Only

**Objective:** Validate Researcher brief → result contract on an evidence-gathering task.

## Brief

```json
{
  "contract_version": "1.0",
  "task_id": "test-research-001",
  "request_source": "queen",
  "objective": "Research how other local governments structure transportation impact fees and find 3-5 example jurisdictions with published fee schedules.",
  "why_this_agent": "The primary evidence is outside Poliscopic's database — municipal fee schedules, ordinances, and published reports. Researcher is the correct agent for external evidence gathering.",
  "inputs": [
    {
      "type": "instruction",
      "location": "N/A",
      "description": "Focus on Arizona municipalities first, then comparable Sun Belt cities. Look for fee schedules, enabling ordinances, and methodology documents."
    }
  ],
  "required_work": [
    "Search for transportation impact fee schedules in Arizona cities (Phoenix, Tucson, Mesa, Chandler, Gilbert)",
    "Expand to 2-3 comparable Sun Belt cities if Arizona examples are insufficient",
    "Assess source credibility for each finding",
    "Document the fee methodology where available",
    "Identify open questions and gaps"
  ],
  "out_of_scope": [
    "Do not query the Poliscopic database",
    "Do not write analysis or conclusions about the data",
    "Do not write articles or draft policy recommendations"
  ],
  "success_criteria": [
    "At least 3 jurisdictions with published fee schedules or ordinances documented",
    "Each source includes URL, accessed date, and credibility assessment",
    "Methodology of fee calculation documented where available",
    "Freshness assessment included (how current is each source)",
    "Unresolved questions explicitly stated"
  ],
  "required_output": {
    "format": "markdown",
    "location": "contracts/examples/test-research-001-result.md"
  },
  "dependencies": [],
  "risks": [
    "Fee schedules may be in PDF ordinances rather than web pages",
    "Some municipalities may not publish fee methodology online",
    "Arizona examples may be limited — may need to expand geographic scope"
  ],
  "deadline": null
}
```

## Expected Result

```json
{
  "contract_version": "1.0",
  "task_id": "test-research-001",
  "agent": "researcher",
  "status": "succeeded",
  "summary": "Found transportation impact fee schedules for 4 Arizona municipalities (Phoenix, Chandler, Gilbert, Tucson) and 1 Sun Belt comparator (Austin, TX). Three jurisdictions publish explicit fee schedules; two reference formulas tied to trip-generation rates.",
  "artifacts": [
    {
      "path": "contracts/examples/test-research-001-result.md",
      "type": "markdown",
      "description": "Research memorandum with source list, fee structures, and methodology documentation."
    }
  ],
  "evidence": [
    {
      "type": "url",
      "location": "https://www.phoenix.gov/streets/transportation-impact-fees",
      "description": "Phoenix Transportation Impact Fee schedule"
    },
    {
      "type": "url",
      "location": "https://www.chandleraz.gov/...",
      "description": "Chandler development impact fees ordinance"
    }
  ],
  "limitations": [
    "Fee schedules change annually — findings are current as of access dates",
    "Two jurisdictions have incomplete methodology documentation online",
    "No statewide Arizona database exists — each city sourced individually"
  ],
  "errors": [],
  "recommended_next_step": "Pass to Analyst for comparison against Poliscopic data, or to Writer for article if sufficient context exists.",

  "sources": [
    { "title": "Phoenix Transportation Impact Fee Schedule", "url": "https://www.phoenix.gov/streets/transportation-impact-fees", "type": "government_data", "accessed_at": "2026-07-17T10:00:00Z" },
    { "title": "Chandler Development Impact Fees", "url": "https://www.chandleraz.gov/...", "type": "government_data", "accessed_at": "2026-07-17T10:05:00Z" },
    { "title": "Austin Transportation Impact Fees", "url": "https://www.austintexas.gov/...", "type": "government_data", "accessed_at": "2026-07-17T10:12:00Z" }
  ],
  "source_quality": [
    { "source": "Phoenix schedule", "credibility": "high", "currency": "2026", "note": "Official city government page, updated annually" },
    { "source": "Chandler ordinance", "credibility": "high", "currency": "2025", "note": "Official municipal code, may lag by one budget cycle" },
    { "source": "Austin schedule", "credibility": "high", "currency": "2026", "note": "Published by city finance department" }
  ],
  "freshness": "All sources accessed within the last hour. Schedules are generally updated annually; three of five sources are current-year.",
  "unresolved_questions": [
    "Whether fee amounts are adjusted for inflation annually or tied to a construction cost index",
    "Whether any municipality exempts affordable housing from transportation impact fees"
  ]
}
```
