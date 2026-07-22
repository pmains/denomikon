# Test Workflow 3: Full Pipeline — Researcher → Analyst → Writer → Editorial Review

**Objective:** Validate artifact-based handoffs across a multi-agent workflow using the contract schemas.

---

## Step 1: Researcher Brief

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-research",
  "request_source": "queen",
  "objective": "Research how Arizona municipalities are regulating data center development — find current ordinances, moratoria, incentive programs, and policy trends across at least 5 cities.",
  "why_this_agent": "The primary evidence is outside Poliscopic's database — municipal ordinances, state legislation, and industry reports. Researcher has the tools and brief for external evidence gathering.",
  "inputs": [
    {
      "type": "instruction",
      "location": "N/A",
      "description": "Focus on Arizona: Phoenix, Mesa, Chandler, Goodyear, Buckeye, Tucson, Scottsdale. Look for zoning ordinances, development agreements, impact fee policies, and any moratoria or incentive programs related to data centers."
    }
  ],
  "required_work": [
    "Search for data center ordinances in Phoenix, Mesa, Chandler, Goodyear, Tucson, Buckeye, Scottsdale",
    "Document any moratoria, incentive programs, or special zoning districts",
    "Note water/power requirements and infrastructure impact discussions",
    "Assess source credibility and currency for each finding"
  ],
  "out_of_scope": [
    "Do not query Poliscopic's database — that's Analyst's role",
    "Do not write conclusions or policy recommendations"
  ],
  "success_criteria": [
    "At least 5 municipalities with documented data center policies",
    "Each source includes URL, accessed date, and credibility assessment",
    "Moratoria or incentives clearly identified where present",
    "Freshness assessment included"
  ],
  "required_output": {
    "format": "markdown",
    "location": "contracts/examples/test-pipeline-001-research.md"
  },
  "dependencies": [],
  "risks": [
    "Some cities may not have publicly available data center-specific policies",
    "Ordinances may be in PDF format requiring extraction"
  ],
  "deadline": null
}
```

### Researcher Result

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-research",
  "agent": "researcher",
  "status": "succeeded",
  "summary": "Found data center policies in 6 of 7 Arizona municipalities surveyed. Mesa and Chandler have formal data center zoning districts with incentives. Phoenix is evaluating impact. Goodyear and Buckeye have development agreements for specific projects. No active moratoria found.",
  "artifacts": [
    {
      "path": "contracts/examples/test-pipeline-001-research.md",
      "type": "markdown",
      "description": "Research memorandum with source list, ordinances, and policy documentation."
    }
  ],
  "evidence": [
    { "type": "url", "location": "https://www.mesaaz.gov/...", "description": "Mesa data center overlay zoning district" },
    { "type": "url", "location": "https://www.chandleraz.gov/...", "description": "Chandler data center incentive program" }
  ],
  "limitations": [
    "Phoenix policy is under discussion — no adopted ordinance yet",
    "Scottsdale has no data center-specific policy — general commercial zoning applies"
  ],
  "errors": [],
  "recommended_next_step": "Pass to Analyst for comparison against Poliscopic meeting data to identify discussed data center projects.",
  "sources": [
    { "title": "Mesa Data Center Overlay", "url": "https://www.mesaaz.gov/...", "type": "government_data", "accessed_at": "2026-07-17T10:00:00Z" },
    { "title": "Chandler Data Center Incentives", "url": "https://www.chandleraz.gov/...", "type": "government_data", "accessed_at": "2026-07-17T10:05:00Z" }
  ],
  "source_quality": [
    { "source": "Mesa overlay district", "credibility": "high", "currency": "2026", "note": "Adopted ordinance, official city code" },
    { "source": "Chandler incentives", "credibility": "high", "currency": "2025", "note": "Economic development program page" }
  ],
  "freshness": "All sources accessed within last 2 hours. Ordinances are adopted and current. Incentive programs reviewed annually."
}
```

---

## Step 2: Analyst Brief

Queen passes the research artifact path, not a summary.

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-analysis",
  "request_source": "queen",
  "objective": "Analyze data center-related agenda items and meeting discussions across Maricopa County jurisdictions in the last 12 months. Identify which jurisdictions have discussed data centers, what actions were taken, and whether the volume is increasing.",
  "why_this_agent": "The primary evidence is inside Poliscopic's database. Analyst is the correct agent for querying meeting data and identifying trends.",
  "inputs": [
    {
      "type": "instruction",
      "location": "N/A",
      "description": "Query for items mentioning data centers, server farms, or related terms. Use the same set of jurisdictions the Researcher covered."
    },
    {
      "type": "artifact",
      "location": "contracts/examples/test-pipeline-001-research.md",
      "description": "Research findings on data center policies. Use this to contextualize which jurisdictions to prioritize and what policy types to look for in meeting records."
    }
  ],
  "required_work": [
    "Query agenda_items for 'data center', 'server farm', 'hyperscale', and related terms over 12 months",
    "Count items by jurisdiction and meeting type",
    "Classify each mention as: discussion, application, approval, ordinance, or other",
    "Cross-reference with researcher findings: do meeting discussions match adopted policies?",
    "Assess trend: are data center items increasing?"
  ],
  "out_of_scope": [
    "Do not conduct additional external research",
    "Do not write articles or policy recommendations"
  ],
  "success_criteria": [
    "All data center mentions in last 12 months identified across target jurisdictions",
    "Items classified by action type (discussion, application, approval, ordinance)",
    "Cross-reference with researcher findings completed",
    "Trend assessment with confidence level"
  ],
  "required_output": {
    "format": "markdown",
    "location": "contracts/examples/test-pipeline-001-analysis.md"
  },
  "dependencies": ["test-pipeline-001-research"],
  "risks": [
    "Keyword search may miss items that use non-standard terminology",
    "Items may be discussed in meetings without appearing in agenda titles"
  ],
  "deadline": null
}
```

### Analyst Result

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-analysis",
  "agent": "analyst",
  "status": "succeeded",
  "summary": "Found 17 data center-related agenda items across 4 jurisdictions in the last 12 months. Mesa dominates with 8 items (zoning code amendments for data center overlay). Chandler has 4 items (development agreements). Goodyear 3. Buckeye 2. Trend is increasing: 5 items in Q1 2026 vs 2 in Q1 2025. Meeting discussions align with adopted policies identified by Researcher.",
  "artifacts": [
    { "path": "contracts/examples/test-pipeline-001-analysis.md", "type": "markdown", "description": "Analysis memorandum with counts, classifications, and cross-reference." }
  ],
  "evidence": [
    { "type": "database", "location": "poliscopic_dev", "description": "Query: SELECT ... WHERE agenda_item_title ILIKE '%data center%'" }
  ],
  "limitations": [
    "Only agenda titles were searched — items discussed but not titled may be missed",
    "12-month window may not capture multi-year development timelines",
    "Some jurisdictions do not have searchable text for older meetings"
  ],
  "errors": [],
  "recommended_next_step": "Pass to Writer with both research and analysis artifacts for article production.",

  "methodology": [
    "Queried agenda_items for data center terms in title and text",
    "Classified each item into action type category",
    "Cross-referenced jurisdiction-level counts against Researcher's documented policies"
  ],
  "data_scope": {
    "source": "poliscopic_dev database",
    "temporal_range": "2025-07-17 to 2026-07-17",
    "record_count": 17,
    "filters_applied": ["Keyword match: data center, server farm, hyperscale, colocation"],
    "limitations": ["Title-only search may miss items discussed but not keyword-tagged"]
  },
  "calculations": [
    { "name": "Items by jurisdiction", "method": "COUNT GROUP BY body_code", "result": "Mesa: 8, Chandler: 4, Goodyear: 3, Buckeye: 2" },
    { "name": "Items by action type", "method": "Classification by title patterns", "result": "Zoning amendment: 6, Development agreement: 5, Discussion: 4, Other: 2" },
    { "name": "Quarterly trend", "method": "Q-over-Q comparison", "result": "Q1 2025: 2, Q2 2025: 3, Q3 2025: 4, Q4 2025: 3, Q1 2026: 5 — increasing" }
  ],
  "reproducibility": "Full query and classification methodology documented in analysis artifact.",
  "confidence": {
    "overall": "medium",
    "per_finding": [
      { "finding": "Item counts by jurisdiction", "level": "high", "reason": "Direct database match on titles" },
      { "finding": "Action type classification", "level": "medium", "reason": "Title-based classification may mis-categorize" },
      { "finding": "Trend direction", "level": "medium", "reason": "12 months is minimal for trend detection" }
    ]
  }
}
```

---

## Step 3: Writer Brief

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-writer",
  "request_source": "queen",
  "objective": "Write a 600-800 word article on the data center policy landscape in Maricopa County, based on the research and analysis artifacts. The article should be suitable for publication on poliscopic.com.",
  "why_this_agent": "Writer is the content producer — this task requires transforming structured data and research into publishable editorial content with source tracing.",
  "inputs": [
    {
      "type": "artifact",
      "location": "contracts/examples/test-pipeline-001-research.md",
      "description": "Researcher findings on data center policies across Arizona municipalities."
    },
    {
      "type": "artifact",
      "location": "contracts/examples/test-pipeline-001-analysis.md",
      "description": "Analyst findings on data center-related meeting activity."
    }
  ],
  "required_work": [
    "Synthesize research and analysis artifacts into a coherent article",
    "Every factual claim must cite its source artifact and location",
    "Distinguish between adopted policies, discussions, and proposed actions",
    "Follow STYLE.md editorial standards",
    "Include source traceability for every claim"
  ],
  "out_of_scope": [
    "Do not add new research or analysis",
    "Do not make unsourced claims about policy impacts"
  ],
  "success_criteria": [
    "600-800 word article suitable for publication",
    "Every factual claim traced to source artifact",
    "Distinction clear between adopted policy and discussion",
    "Follows STYLE.md standards",
    "Includes headline and summary"
  ],
  "required_output": {
    "format": "file",
    "location": "drafts/data-center-landscape-2026-07-17.md"
  },
  "dependencies": ["test-pipeline-001-research", "test-pipeline-001-analysis"],
  "risks": [
    "Researcher identified gaps (e.g., Phoenix policy not yet adopted) must be clearly characterized",
    "Limitations from analysis (title-only search) should inform confidence in article claims"
  ],
  "deadline": null
}
```

### Writer Result

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-writer",
  "agent": "writer",
  "status": "changes_required",
  "summary": "Completed 720-word draft. Headline: 'Arizona Cities Compete for Data Center Investment as Mesa Leads with Zoning Incentives'. Includes source traceability for every claim. However, I could not verify one claim about Goodyear's water impact analysis — the research artifact notes the source was a staff presentation, not an adopted document. Marked as unverified.",
  "artifacts": [
    { "path": "drafts/data-center-landscape-2026-07-17.md", "type": "file", "description": "Article draft with inline source citations." }
  ],
  "evidence": [
    { "type": "artifact", "location": "contracts/examples/test-pipeline-001-research.md", "description": "External policy research" },
    { "type": "artifact", "location": "contracts/examples/test-pipeline-001-analysis.md", "description": "Internal meeting data analysis" }
  ],
  "limitations": [
    "Goodyear water impact claim cited to staff presentation — not independently verifiable from source documents",
    "Chandler incentive amounts not publicly disclosed — article notes confidential agreement",
    "Phoenix section is speculative (policy under discussion, not adopted)"
  ],
  "errors": [],
  "recommended_next_step": "Editorial review recommended before publication. One claim needs verification. See claims[2].",

  "claims": [
    {
      "claim": "Mesa adopted a data center overlay zoning district in 2025",
      "source": "contracts/examples/test-pipeline-001-research.md",
      "location": "paragraph 3",
      "qualifier": "stated_in_source"
    },
    {
      "claim": "Data center agenda items increased from 2 in Q1 2025 to 5 in Q1 2026",
      "source": "contracts/examples/test-pipeline-001-analysis.md",
      "location": "paragraph 5",
      "qualifier": "stated_in_source"
    },
    {
      "claim": "Goodyear expects data center water usage of 1.2 million gallons per day",
      "source": "contracts/examples/test-pipeline-001-research.md",
      "location": "paragraph 7",
      "qualifier": "attributed_to_third_party"
    }
  ],
  "source_mappings": {
    "contracts/examples/test-pipeline-001-research.md": "Used for policy descriptions, ordinance details, and jurisdiction context",
    "contracts/examples/test-pipeline-001-analysis.md": "Used for meeting counts, trend data, and action type classifications"
  },
  "editorial_constraints": [
    "Confidential incentive amounts noted but not disclosed",
    "Phoenix policy described as 'under discussion' rather than adopted"
  ],
  "publication_readiness": "needs_review"
}
```

---

## Step 4: Editorial Review

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-review",
  "request_source": "queen",
  "objective": "Review the data center article draft for factual accuracy, source traceability, and editorial standards compliance before publication.",
  "why_this_agent": "Editorial review requires independent assessment of claims against source artifacts. This is a high-risk boundary — content will be published.",
  "inputs": [
    {
      "type": "artifact",
      "location": "drafts/data-center-landscape-2026-07-17.md",
      "description": "Article draft with inline source citations."
    },
    {
      "type": "artifact",
      "location": "contracts/examples/test-pipeline-001-research.md",
      "description": "Research memorandum for source verification."
    },
    {
      "type": "artifact",
      "location": "contracts/examples/test-pipeline-001-analysis.md",
      "description": "Analysis memorandum for data verification."
    },
    {
      "type": "instruction",
      "location": "N/A",
      "description": "Apply the review rubric from contracts/validations.md: check every claim against its cited source, flag unsupported statements, verify source credibility, ensure policy status (adopted vs. discussed) is correctly characterized."
    }
  ],
  "required_work": [
    "Verify every claim in the claims list against its cited source",
    "Check that adopted policies and discussions are clearly distinguished",
    "Assess whether the Goodyear water impact claim should be included given its source quality",
    "Flag any missing citations or editorial standard violations"
  ],
  "out_of_scope": [
    "Do not rewrite the article — identify defects only",
    "Do not add new research or analysis"
  ],
  "success_criteria": [
    "Every claim in the article assessed against its source",
    "Policy status (adopted vs. discussed) verified for each jurisdiction",
    "One unverified claim flagged",
    "Publication readiness confirmed or blocked"
  ],
  "required_output": {
    "format": "json",
    "location": "drafts/data-center-landscape-2026-07-17-review.json"
  },
  "dependencies": ["test-pipeline-001-writer"],
  "risks": [
    "The Goodyear claim depends on a staff presentation — not independently verifiable",
    "Article may need revision before publication"
  ],
  "deadline": null
}
```

### Review Result

```json
{
  "contract_version": "1.0",
  "task_id": "test-pipeline-001-review",
  "agent": "critic",
  "status": "changes_required",
  "summary": "Review complete. 6 of 7 claims verified against source artifacts. One blocking issue: the Goodyear water impact claim cites a staff presentation, not an adopted document or ordinance. Recommend either: (a) include with clear attribution to the presentation, or (b) remove and note as unconfirmed. All other claims are correctly sourced. Editorial standards are followed. Tone is appropriate for poliscopic.com.",
  "artifacts": [
    { "path": "drafts/data-center-landscape-2026-07-17-review.json", "type": "json", "description": "Review report with per-claim assessment and blocking issues." }
  ],
  "evidence": [
    { "type": "artifact", "location": "drafts/data-center-landscape-2026-07-17.md", "description": "Reviewed article" },
    { "type": "artifact", "location": "contracts/examples/test-pipeline-001-research.md", "description": "Source verification artifact" }
  ],
  "limitations": [
    "Could not independently verify Goodyear presentation source — it was not accessible online"
  ],
  "errors": [],
  "recommended_next_step": "Writer revises to clearly attribute the Goodyear claim to a staff presentation. Queen decides whether to publish with the attribution or drop the claim.",
  "blocking_issues": [
    {
      "requirement": "Every factual claim must be supported by a verifiable source",
      "location": "paragraph 7",
      "finding": "Goodyear water use estimate (1.2 million gallons/day) cited to staff presentation — not independently verifiable"
    }
  ],
  "nonblocking_issues": [],
  "approved": false
}
```
