#!/usr/bin/env python3
"""
Graph builder — SQL → typed triple materialization DAG.

Phase 1 of the information extraction pipeline: extract relationship edges
from existing structured DB tables before writing any NLP code.

Strategy: Load ALL existing entities into memory at start (one query).
For each source, batch-check against in-memory dict, only INSERT missing
entities with bulk operations.  Edges are also batched.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Generator, Optional

from sqlalchemy import Connection, text

sys.path.insert(0, "scripts")
from db.core import get_engine
from entities.entity_utils import clean_normalized_name, normalize_entity_name, is_firm_name

log = logging.getLogger("graph_builder")
WATERMARK_TABLE = "_graph_builder_watermark"


@dataclass
class EntitySpec:
    name: str
    normalized_name: str
    entity_type: str
    jurisdiction_id: int | None = None
    is_government: bool = False


@dataclass
class EdgeSpec:
    from_entity_norm: str
    from_type: str
    to_entity_norm: str
    to_type: str
    relationship: str
    source_type: str
    source_id: int


@dataclass
class MentionSpec:
    """An entity mention to create alongside a relationship edge.

    Provides text provenance for structured relationships so entity pages
    can display "Member of Board of Supervisors (from body_membership #3)".

    entity_type is needed for cache lookup since entity_norm alone isn't
    unique across types (e.g., "john smith" could be person or organization).
    """
    entity_norm: str
    entity_type: str
    source_type: str
    source_id: int
    mention_text: str
    role: str | None = None
    confidence: float = 1.0


class Source:
    name: str = ""
    query: str = ""
    description: str = ""

    def produce(self, rows: list[dict]) -> Generator[tuple[Optional[EntitySpec], Optional[EdgeSpec], Optional[MentionSpec]], None, None]:
        raise NotImplementedError


def load_all_entity_ids(connection: Connection) -> dict[tuple[str, str], int]:
    """Load all existing (normalized_name, entity_type) -> id mappings."""
    result = {}
    rows = connection.execute(text("SELECT normalized_name, entity_type, id FROM entities")).fetchall()
    for r in rows:
        result[(str(r[0]), str(r[1]))] = int(r[2])
    log.info("  Loaded %d existing entity mappings", len(result))
    return result


def run_source(
    source: Source,
    connection: Connection,
    entity_cache: dict[tuple[str, str], int],
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[int, int, dict[tuple[str, str], int]]:
    """Execute a single graph source: entities → edges → entity_mentions.

    For each row returned by the source's SQL query, calls source.produce()
    to yield EntitySpec, EdgeSpec, and MentionSpec tuples. Performs four
    bulk phases:

    Phase 1-2: Deduplicate and bulk-insert any new entities not in cache.
    Phase 3:   Bulk-insert MEMBER_OF / HAS_APPLICANT / etc. edges.
    Phase 4:   For body_membership edges, create entity_mentions so
               relationships have text provenance ("Member of BOS, from
               body_membership #3") and entity pages display mention details.

    Returns (entities_created, edges_created).
    """
    rows = connection.execute(text(source.query)).fetchall()

    entity_specs: list[EntitySpec] = []
    edge_specs: list[EdgeSpec] = []
    mention_specs: list[MentionSpec] = []

    for rd in rows:
        row_dict = dict(rd._mapping)
        for es, ed, ms in source.produce([row_dict]):
            if es:
                entity_specs.append(es)
            if ed:
                edge_specs.append(ed)
            if ms:
                mention_specs.append(ms)

    if verbose:
        log.info("  Query returned %d rows → %d entity specs, %d edge specs, %d mention specs",
                 len(rows), len(entity_specs), len(edge_specs), len(mention_specs))

    if dry_run:
        unique_e = len(set((s.normalized_name, s.entity_type) for s in entity_specs))
        return unique_e, len(edge_specs), {}

    # Deduplicate entity specs
    seen = {}
    for s in entity_specs:
        key = (s.normalized_name, s.entity_type)
        if key not in seen:
            seen[key] = s
    unique_specs = list(seen.values())

    # Phase 1: Find what's new vs existing
    existing_count = 0
    to_insert: list[EntitySpec] = []
    for s in unique_specs:
        key = (s.normalized_name, s.entity_type)
        if key in entity_cache:
            existing_count += 1
            # Update last_seen_at so we know when the entity was last referenced
            # in structured data. Do NOT increment mention_count — that field
            # tracks textual mentions (from sweep_docs, pattern_cascade), not
            # structured data sightings (see fix #1).
            connection.execute(
                text("UPDATE entities SET last_seen_at = now() WHERE id = :id"),
                {"id": entity_cache[key]},
            )
        else:
            to_insert.append(s)

    # Phase 2: Bulk insert new entities
    new_ids: dict[tuple[str, str], int] = {}
    if to_insert:
        # Build VALUES list for bulk INSERT
        val_parts = []
        params = {}
        for i, s in enumerate(to_insert):
            val_parts.append(f"(:et{i}, :name{i}, :nn{i}, :jurisdiction_id{i}, :is_government{i})")
            params[f"et{i}"] = s.entity_type
            params[f"name{i}"] = s.name
            params[f"nn{i}"] = s.normalized_name
            params[f"jurisdiction_id{i}"] = s.jurisdiction_id
            params[f"is_government{i}"] = s.is_government

        val_clause = ", ".join(val_parts)
        rows = connection.execute(
            text(f"""
                INSERT INTO entities
                    (entity_type, name, normalized_name, is_government,
                     jurisdiction_id, resolution_status,
                     first_seen_at, last_seen_at, mention_count,
                     created_at, updated_at)
                SELECT v.et, v.name, v.nn, v.is_government,
                       CAST(v.jurisdiction_id AS INTEGER), 'unresolved',
                       now(), now(), 1, now(), now()
                FROM (VALUES {val_clause}) AS v(et, name, nn, jurisdiction_id, is_government)
                RETURNING normalized_name, entity_type, id
            """),
            params,
        ).fetchall()
        for r in rows:
            key = (str(r[0]), str(r[1]))
            new_ids[key] = int(r[2])
            # entity_cache[key] = int(r[2])  -- moved after transaction (S1)

    if verbose:
        log.info("  → %d existing, %d new entities", existing_count, len(to_insert))

    # Save entity count before to_insert is reassigned in Phase 3 (C1 fix)
    new_entity_count = len(to_insert)

    # Phase 3: Create edges (bulk — first resolve IDs, then bulk-insert)
    #
    # Use merged lookup: existing cache + freshly inserted entities from
    # Phase 2. This avoids relying on entity_cache being populated before
    # the transaction commits (S1 cache-buffering fix).
    merged_lookup = {**entity_cache, **new_ids}
    resolved_edges = []
    edges_skipped = 0
    for s in edge_specs:
        from_key = (s.from_entity_norm, s.from_type)
        to_key = (s.to_entity_norm, s.to_type)
        from_id = merged_lookup.get(from_key)
        to_id = merged_lookup.get(to_key)
        if not from_id or not to_id:
            edges_skipped += 1
            continue
        resolved_edges.append((from_id, to_id, s))

    # Bulk: load existing edges for this source in one query
    existing_set = set()
    if edge_specs:
        rows = connection.execute(
            text("""
                SELECT from_entity_id, relationship, to_entity_id,
                       provenance_type, provenance_id
                FROM entity_relationships
                WHERE provenance_type = :pt
            """),
            {"pt": edge_specs[0].source_type},
        ).fetchall()
        for r in rows:
            existing_set.add((int(r[0]), str(r[1]), int(r[2]), str(r[3]), int(r[4])))

    # Build bulk INSERT
    to_insert = []
    for from_id, to_id, s in resolved_edges:
        key = (from_id, s.relationship, to_id, s.source_type, s.source_id)
        if key in existing_set:
            edges_skipped += 1
            continue
        to_insert.append((from_id, to_id, s))

    if to_insert:
        val_parts = []
        params = {}
        for i, (from_id, to_id, s) in enumerate(to_insert):
            val_parts.append(f"(:feid{i}, :teid{i}, :rel{i}, :pt{i}, :pid{i}, :sl{i}, :ek{i})")
            params[f"feid{i}"] = from_id
            params[f"teid{i}"] = to_id
            params[f"rel{i}"] = s.relationship
            params[f"pt{i}"] = s.source_type
            params[f"pid{i}"] = s.source_id
            params[f"sl{i}"] = f"Structured: {s.relationship}"
            params[f"ek{i}"] = "relational"

        val_clause = ", ".join(val_parts)
        connection.execute(
            text(f"""
                INSERT INTO entity_relationships
                    (from_entity_id, to_entity_id, relationship,
                     provenance_type, provenance_id, source_label,
                     edge_kind, confidence, created_at)
                SELECT v.feid, v.teid, v.rel, v.pt, v.pid, v.sl,
                       v.ek, 1.0, now()
                FROM (VALUES {val_clause}) AS v(feid, teid, rel, pt, pid, sl, ek)
            """),
            params,
        )

    edges_created = len(to_insert)
    if verbose:
        log.info("  → %d edges created, %d skipped", edges_created, edges_skipped)

    # Phase 4: Create entity_mentions for provenance
    #
    # Each source can yield MentionSpec objects alongside entities and edges.
    # These provide text provenance so entity detail pages can display
    # "MEMBER_OF Phoenix Planning Commission (from body_membership #3)".
    # This phase resolves entity_id from the merged lookup and bulk-inserts.
    mentions_created = 0
    seen_mention_keys: set[tuple[int, str, int, str | None]] = set()
    if mention_specs and merged_lookup:
        value_rows: list[str] = []
        bind_params: dict[str, object] = {}
        row_number = 0
        for ms in mention_specs:
            key = (ms.entity_norm, ms.entity_type)
            entity_id = merged_lookup.get(key)
            if not entity_id:
                continue
            # Check for duplicate mention (same entity + source + role)
            # to keep Phase 4 idempotent within a single run.
            dup_key = (entity_id, ms.source_type, ms.source_id, ms.role)
            if dup_key in seen_mention_keys:
                continue
            seen_mention_keys.add(dup_key)

            value_rows.append(
                f"(:entity_id_{row_number}, :source_type_{row_number},"
                f" :source_id_{row_number}, :mention_text_{row_number},"
                f" :role_{row_number}, :conf_{row_number})"
            )
            bind_params[f"entity_id_{row_number}"] = entity_id
            bind_params[f"source_type_{row_number}"] = ms.source_type
            bind_params[f"source_id_{row_number}"] = ms.source_id
            bind_params[f"mention_text_{row_number}"] = ms.mention_text
            bind_params[f"role_{row_number}"] = ms.role or ""
            bind_params[f"conf_{row_number}"] = ms.confidence
            row_number += 1

        if value_rows:
            values_clause = ", ".join(value_rows)
            connection.execute(
                text(f"""
                    INSERT INTO entity_mentions
                        (entity_id, source_type, source_id, mention_text,
                         role_in_context, confidence, extracted_by, created_at)
                    SELECT v.entity_id, v.source_type, v.source_id,
                           v.mention_text, v.role_in_context,
                           v.confidence, 'graph_builder', now()
                    FROM (VALUES {values_clause})
                    AS v(entity_id, source_type, source_id,
                         mention_text, role_in_context, confidence)
                """),
                bind_params,
            )
            mentions_created = row_number

    if verbose:
        log.info("  → %d entity_mentions created", mentions_created)

    return new_entity_count, edges_created, new_ids


# ── Source Implementations ──────────────────────────────────────────────

class BodyMembershipSource(Source):
    name = "body_memberships"
    description = "Board member → body membership edges"
    query = """
        SELECT DISTINCT ON (bm.person_id, bm.public_body_id)
            bm.id AS membership_id,
            p.name AS person_name,
            p.normalized_name AS person_norm,
            bm.role,
            bm.term_start::text AS term_start,
            pb.name AS body_name,
            pb.body_code
        FROM body_memberships bm
        JOIN persons p ON p.id = bm.person_id
        JOIN public_bodies pb ON pb.id = bm.public_body_id
        ORDER BY bm.person_id, bm.public_body_id, bm.term_start DESC
    """

    def produce(self, rows):
        for r in rows:
            pn = (r.get("person_name") or "").strip()
            pn_norm = (r.get("person_norm") or "").strip()
            bn = (r.get("body_name") or "").strip()
            bc = (r.get("body_code") or "").strip()
            mid = r.get("membership_id") or 0
            if not pn or not pn_norm or not bn or not bc:
                continue
            yield EntitySpec(pn, pn_norm, "person", is_government=False), None, None
            yield EntitySpec(bn, bc, "organization", is_government=True), None, None
            yield None, EdgeSpec(pn_norm, "person", bc, "organization",
                                 "MEMBER_OF", "body_membership", mid), \
                MentionSpec(entity_norm=pn_norm, entity_type="person",
                            source_type="body_membership", source_id=mid,
                            mention_text=pn, role="MEMBER_OF")


class MeetingAttendanceSource(Source):
    name = "meeting_attendance"
    description = "Meeting attendance edges"
    query = """
        SELECT DISTINCT ON (mm.body, mm.meeting_id, mm.member_id)
            mm.id AS att_id,
            p.name AS person_name,
            p.normalized_name AS person_norm,
            mm.body,
            mm.meeting_id,
            mm.meeting_db_id,
            mm.present,
            m.jurisdiction_id
        FROM meeting_members mm
        JOIN persons p ON p.id = mm.member_id
        LEFT JOIN meetings m ON m.id = mm.meeting_db_id
    """

    def produce(self, rows):
        for r in rows:
            pn = (r.get("person_name") or "").strip()
            pn_norm = (r.get("person_norm") or "").strip()
            body_code = (r.get("body") or "").strip()
            meeting_id = (r.get("meeting_id") or "").strip()
            att_id = r.get("att_id") or 0
            present = r.get("present")
            jur_id = r.get("jurisdiction_id")
            if not pn or not pn_norm or not meeting_id:
                continue

            meeting_key = f"{body_code}/{meeting_id}"

            # Create meeting entity so PRESENT_AT edges have a target (C2 fix)
            yield EntitySpec(
                name=f"{body_code} Meeting {meeting_id}",
                normalized_name=meeting_key,
                entity_type="meeting",
                jurisdiction_id=jur_id,
            ), None, None

            yield EntitySpec(pn, pn_norm, "person"), None, None
            if present is True:
                yield None, EdgeSpec(pn_norm, "person", meeting_key, "meeting",
                                     "PRESENT_AT", "meeting_member", att_id), \
                    MentionSpec(entity_norm=pn_norm, entity_type="person",
                                source_type="meeting_member", source_id=att_id,
                                mention_text=pn, role="PRESENT_AT")


class PZItemDetailsSource(Source):
    name = "pz_item_details"
    description = "Applicant, case, recommendation edges"
    query = """
        SELECT DISTINCT ON (pz.id)
            pz.id AS pz_id,
            pz.case_number,
            pz.applicant,
            pz.recommendation,
            pz.presented_by,
            pz.body,
            pz.meeting_db_id,
            pz.agenda_item_number,
            m.jurisdiction_id
        FROM pz_item_details pz
        LEFT JOIN meetings m ON m.id = pz.meeting_db_id
        WHERE pz.case_number IS NOT NULL AND pz.case_number != ''
        ORDER BY pz.id
    """

    def produce(self, rows):
        for r in rows:
            cn = (r.get("case_number") or "").strip()
            applicant = (r.get("applicant") or "").strip()
            rec = (r.get("recommendation") or "").strip()
            presenter = (r.get("presented_by") or "").strip()
            pz_id = r.get("pz_id") or 0
            jur_id = r.get("jurisdiction_id")
            if not cn:
                continue
            case_norm = normalize_entity_name(cn)
            yield EntitySpec(cn, case_norm, "case", jurisdiction_id=jur_id), None, None

            if applicant:
                # D5: Detect "Person, Firm" compound in applicant field.
                # E.g. "Chris Webb, Rose Law Group PC" → person REPRESENTS firm → case.
                an = normalize_entity_name(applicant)
                person_name = None
                org_name = None
                an_person = None
                an_org = None

                if "," in applicant:
                    # Split on the FIRST comma — that separates the person name
                    # from the firm name. Multi-comma names like
                    # "Carolyn Oberholtzer, Bergin, Frakes, Smalley & Oberholtzer"
                    # are: person="Carolyn Oberholtzer", firm="Bergin, Frakes, Smalley & Oberholtzer"
                    potential_person, potential_org = [
                        p.strip() for p in applicant.split(",", 1)
                    ]

                    # Verify: second part should look like a firm, first should not
                    if is_firm_name(potential_org) and not is_firm_name(potential_person):
                        person_name = potential_person
                        org_name = potential_org
                        an_person = clean_normalized_name(person_name)
                        an_org = normalize_entity_name(org_name)

                if person_name and org_name:
                    # Split model: person REPRESENTS org, org APPLIED_FOR case
                    yield EntitySpec(person_name, an_person, "person"), None, None
                    yield EntitySpec(org_name, an_org, "organization"), None, None
                    yield None, EdgeSpec(an_person, "person", an_org, "organization",
                                         "REPRESENTS", "pz_item_detail", pz_id), \
                        MentionSpec(entity_norm=an_person, entity_type="person",
                                    source_type="pz_item_detail", source_id=pz_id,
                                    mention_text=person_name, role="REPRESENTS")
                    yield None, EdgeSpec(an_org, "organization", case_norm, "case",
                                         "HAS_APPLICANT", "pz_item_detail", pz_id), \
                        MentionSpec(entity_norm=an_org, entity_type="organization",
                                    source_type="pz_item_detail", source_id=pz_id,
                                    mention_text=org_name, role="HAS_APPLICANT")
                else:
                    # Flat model: single org APPLIED_FOR case
                    yield EntitySpec(applicant, an, "organization"), None, None
                    yield None, EdgeSpec(an, "organization", case_norm, "case",
                                         "HAS_APPLICANT", "pz_item_detail", pz_id), \
                        MentionSpec(entity_norm=an, entity_type="organization",
                                    source_type="pz_item_detail", source_id=pz_id,
                                    mention_text=applicant, role="HAS_APPLICANT")
            if rec:
                rn = normalize_entity_name(rec)
                # Create recommendation entity so HAS_RECOMMENDATION edges resolve (C3 fix)
                yield EntitySpec(rec, rn, "recommendation", jurisdiction_id=jur_id), None, None
                yield None, EdgeSpec(case_norm, "case", rn, "recommendation",
                                     "HAS_RECOMMENDATION", "pz_item_detail", pz_id), \
                    MentionSpec(entity_norm=rn, entity_type="recommendation",
                                source_type="pz_item_detail", source_id=pz_id,
                                mention_text=rec, role="HAS_RECOMMENDATION")
            if presenter:
                # Use clean_normalized_name for people — strips titles while
                # preserving the raw name in entity.name (D1 fix)
                sfn = clean_normalized_name(presenter)
                yield EntitySpec(presenter, sfn, "person"), None, None
                yield None, EdgeSpec(sfn, "person", case_norm, "case",
                                     "HAS_STAFF", "pz_item_detail", pz_id), \
                    MentionSpec(entity_norm=sfn, entity_type="person",
                                source_type="pz_item_detail", source_id=pz_id,
                                mention_text=presenter, role="HAS_STAFF")


SOURCES: list[Source] = [
    BodyMembershipSource(),
    MeetingAttendanceSource(),
    PZItemDetailsSource(),
]


def run_phase(
    engine,
    source_filter: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """Run graph builder phase. Returns structured result dict.

    Iterates over all SOURCES, processes each against the DB, and
    accumulates totals. Respects watermarks (unless force=True).
    """
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
                source_name VARCHAR(64) PRIMARY KEY,
                last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                entities_created INTEGER NOT NULL DEFAULT 0,
                edges_created INTEGER NOT NULL DEFAULT 0
            );
        """))

    watermarks = {}
    if not force:
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT source_name, last_run_at FROM {WATERMARK_TABLE}")).fetchall()
            watermarks = {r[0]: r[1] for r in rows}

    # Load ALL existing entity IDs once at start
    with engine.connect() as conn:
        entity_cache = load_all_entity_ids(conn)

    total_entities = 0
    total_edges = 0
    skipped = 0

    for source in SOURCES:
        if source_filter and source_filter.lower() not in source.name.lower():
            continue

        wm = watermarks.get(source.name)
        if wm:
            log.info("[SKIP] %s — last run at %s", source.name, wm)
            skipped += 1
            continue

        log.info("%s — %s", source.name, source.description)

        try:
            with engine.begin() as conn:
                entities, edges, new_ids = run_source(
                    source, conn, entity_cache,
                    dry_run=dry_run, verbose=verbose,
                )
                if not dry_run:
                    conn.execute(
                        text(f"""
                            INSERT INTO {WATERMARK_TABLE} (source_name, last_run_at, entities_created, edges_created)
                            VALUES (:sn, now(), :ec, :edc)
                            ON CONFLICT (source_name) DO UPDATE SET
                                last_run_at = now(), entities_created = :ec, edges_created = :edc
                        """),
                        {"sn": source.name, "ec": entities, "edc": edges},
                    )
            # Transaction committed — safely update shared cache (S1 fix)
            entity_cache.update(new_ids)
            total_entities += entities
            total_edges += edges
            log.info("  ✓ %d entities, %d edges", entities, edges)
        except Exception as e:
            log.error("  ✗ Failed: %s", e, exc_info=verbose)
            if not force:
                raise

    return {
        "success": True,
        "entities_created": total_entities,
        "edges_created": total_edges,
        "sources_total": len(SOURCES),
        "sources_skipped": skipped,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    engine = get_engine()
    result = run_phase(
        engine,
        source_filter=args.source,
        dry_run=args.dry_run,
        force=args.force,
        verbose=args.verbose,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d entities, %d edges (%d sources, %d skipped)",
             mode, result["entities_created"], result["edges_created"],
             result["sources_total"], result["sources_skipped"])

    print(json.dumps({"phase": "graph_builder", **result}))


if __name__ == "__main__":
    main()
