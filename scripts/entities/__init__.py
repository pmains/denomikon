"""
Poliscopic entity pipeline.

Active modules:
  detect_entities.py   — Pipeline orchestrator (runs all 6 phases)
  graph_builder.py     — Phase 1: Structured triples from DB tables
  sweep_docs.py        — Phase 2: Entity extraction from supporting_documents
  pattern_cascade.py   — Phase 3: Labeled header regex on agenda items
  role_classifier.py   — Phase 4: ML role classification (XGBoost ensemble)
  resolver.py          — Phase 5: Entity resolution/dedup
  event_extractor.py   — Phase 6 orchestrator: 3-stage event extraction
  event_extract.py     — Phase 6 Step 1: regex pattern extraction
  event_normalize.py   — Phase 6 Step 2: canonical normalization
  event_link.py        — Phase 6 Step 3: entity graph linking

Full pipeline documentation: docs/entities/PIPELINE.md

Shared utilities in extract.py:
  KNOWN_ORGANIZATIONS, normalize_name, get_or_create_entity, create_mention

Archived superseded scripts in archive/:
  backfill.py, people.py, cases.py, parcels.py, sweep_meetings.py,
  person_benchmark.py, resolve_entities.py, phase5_extractor.py,
  phase5_normalizer.py, phase5_linker.py
"""
