"""Persistence layer — re-exports from submodules.

Core engine:     ``db.core``
ORM models:      ``db.models``
Meeting utils:   ``db.meeting_utils`` (normalization, display names)
Migrations:      ``db.migrations`` (init_db, schema migrations, seeds)
Queries:         ``db.queries`` (all get_* read functions)
Vote persistence: ``db.persist`` (persist_meeting, persist_votes, upsert)
Vote analysis:   ``db.votes`` (tallying, majority, controversy, swing votes)

Usage: ``from db import get_session, Meeting, Permit, init_db, ...``
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Re-export from submodules
from db.core import DATABASE_URL, get_engine, set_database_url, get_session
from db.core import _engine, _SessionLocal
from sqlalchemy import text
from db.models import (
    Base,
    Person, Supervisor, MeetingSupervisor, AgendaItemVote, SupervisorVote,
    Meeting, Case, PZItemDetail, CaseEvent, AgendaItem,
    PublicBodyMember, MeetingAttendance, MemberVote,
    ExecutiveSessionParticipant, SupportingDocument,
    PermitReport, Jurisdiction, PublicBody, BodySeat, BodyMembership, Permit,
)
from db.helper import _parse_date
from db.meeting_utils import (
    normalize_meeting_type, extract_meeting_context, extract_meeting_body,
    build_meeting_display_name, backfill_meeting_normalization,
    is_canceled_meeting, mark_meeting_canceled,
)
from db.migrations import (
    init_db, init_poliscopic_models, _migrate_existing_tables,
    backfill_multi_jurisdiction_columns, backfill_body_column,
    _migrate_table, _migrate_col, _ensure_index,
    _migrate_supervisors_to_public_body_members, seed_default_jurisdictions,
    _migrate_permit_normalized_fields, _drop_deprecated_person_columns,
    _migrate_membership_model,
)
from db.persist import (
    create_or_get_meeting, update_sync_status, upsert_meeting,
    persist_meeting, _upsert_case_and_event, replace_meeting_data_safe,
    persist_votes, persist_pz_votes, _detect_vote_attributes, _ensure_membership,
    _find_or_create_person, infer_absence_for_meeting,
)
from db.queries import (
    _resolve_jurisdiction_id,
    get_meetings_by_date_range, get_meetings_by_status,
    get_sync_status_summary, get_failed_meetings,
    get_meeting_attendance, get_executive_session_participants,
    get_split_votes, get_dissenting_votes,
    get_supervisor_by_slug_or_name, get_bos_supervisors,
    get_supervisor_vote_stats, get_supervisor_split_votes,
    get_supervisor_dissents, get_supervisor_abstentions,
    get_supervisor_absences, get_supervisor_full_voting_record,
    get_supervisor_slug, get_supervisor_majority_alignment_stats,
    get_supervisor_voting_alignment, get_supervisor_swing_votes,
    get_public_bodies_by_jurisdiction, get_body_members,
    _enhance_member_for_template,
)
from db.votes import (
    _normalize_vote_value, _make_supervisor_slug,
    infer_majority_position, compute_vote_tally,
)
