#!/usr/bin/env bash
# Production schema migration playbook.
#
# Safely migrates dev schema changes to production without downtime:
#   CREATE NEW TABLE → COPY DATA → SYNC → DEPLOY CODE → RELOAD → VERIFY → CLEANUP
#
# Usage:
#   ./migrate.sh                         # show steps (dry-run, no changes)
#   ./migrate.sh --step 1                # run a single step
#   ./migrate.sh --step 1 --confirm      # run step 1 with confirmation
#   ./migrate.sh --run-to 3              # run steps 1-3
#   ./migrate.sh --all                   # run full playbook with confirmations
#
# Each step is idempotent and can be re-run safely.

set -euo pipefail

cd "$(dirname "$0")"

set -a; source .env 2>/dev/null || true; set +a

PY=".venv/bin/python"
PROD="$PROD_DATABASE_URL"

# ═══════════════════════════════════════════════════════════════════════════
#  Step definitions
# ═══════════════════════════════════════════════════════════════════════════

step1_create() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Step 1: Create meeting_members + copy data                ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Creates meeting_members table, copies all rows from"
  echo "  meeting_supervisors (mapping supervisor_id → member_id)."
  echo "  Old table is NOT touched."
  echo ""
  PROD_DATABASE_URL="$PROD" $PY scripts/db/migrate_prod_db.py
  echo ""
  echo "  Verify:"
  PROD_DATABASE_URL="$PROD" $PY scripts/db/cleanup_prod_db.py --status
  echo ""
  echo "  ✅ Step 1 complete. Both tables coexist."
}

step2_sync() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Step 2: Sync remaining dev data to prod                   ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Syncs all pending rows from dev to prod, including any new"
  echo "  meeting_members data that arrived during Step 1."
  echo ""
  BATCH_SIZE=5000 BATCH_SLEEP_MS=100 $PY scripts/db/sync_prod.py
  echo ""
  echo "  ✅ Step 2 complete. Prod has latest data."
}

step3_deploy_code() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Step 3: Deploy code (rsync only, no restart)              ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  First showing what would be deployed (dry-run):"
  echo ""
  bash scripts/deploy_code.sh
  echo ""
  echo "  To deploy for real, run:"
  echo "    bash scripts/deploy_code.sh --execute"
  echo ""
  echo "  ✅ Step 3 complete. New code is on prod but NOT serving."
}

step4_reload() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Step 4: Verify imports + graceful gunicorn reload         ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Verifying new code imports on the production server:"
  ssh root@poliscopic.com "cd /opt/poliscopic && $PY scripts/verify_deploy.py --check-imports" || true
  echo ""
  echo "  Reloading gunicorn (SIGHUP — old workers finish gracefully):"
  bash scripts/reload_gunicorn.sh
  echo ""
  echo "  ✅ Step 4 complete. New code is now serving."
}

step5_verify() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Step 5: Full verification                                 ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Running full verify suite on production server:"
  echo ""
  ssh root@poliscopic.com "cd /opt/poliscopic && $PY scripts/verify_deploy.py" || true
  echo ""
  echo "  ✅ Step 5 complete. Site is healthy."
}

step6_cleanup() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Step 6: Drop old tables (only after verification)         ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  This drops meeting_supervisors. meeting_members must have data."
  echo "  Guard: --cleanup refuses if meeting_members is empty."
  echo ""
  PROD_DATABASE_URL="$PROD" $PY scripts/db/cleanup_prod_db.py
  echo ""
  echo "  ✅ Step 6 complete. Old tables removed."
}

rollback() {
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  ROLLBACK: Deploy old code, preserve old table             ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Meeting_supervisors still has all its data."
  echo "  Run rollback.sh to deploy old code and reload gunicorn."
  echo ""
  bash rollback.sh
  echo ""
  echo "  ✅ Rollback complete."
}


# ═══════════════════════════════════════════════════════════════════════════
#  Dispatch
# ═══════════════════════════════════════════════════════════════════════════

steps() {
  echo ""
  echo "  ┌─────────────────────────────────────────────────────────────────┐"
  echo "  │  Production Migration Playbook                                   │"
  echo "  ├─────┬───────────────────────────────────────────────────────────┤"
  echo "  │  1  │ Create meeting_members table + copy data from old table   │"
  echo "  │  2  │ Sync dev → prod (BATCH_SIZE=5000, throttled)              │"
  echo "  │  3  │ Deploy new code via rsync (no restart)                    │"
  echo "  │  4  │ Verify imports + graceful gunicorn reload (SIGHUP)        │"
  echo "  │  5  │ Full verification: imports, HTTP, DB queries              │"
  echo "  │  6  │ Drop old tables (--cleanup, with guardrails)              │"
  echo "  │  R  │ Rollback — deploy old code, old table still has data      │"
  echo "  └─────┴───────────────────────────────────────────────────────────┘"
  echo ""
}

confirm_step() {
  local label="$1"
  echo ""
  echo "  ──────────────────────────────────────────────────"
  echo "  About to run: $label"
  echo "  ──────────────────────────────────────────────────"
  echo -n "  Proceed? [y/N] "
  read -r reply
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) echo "  Skipped." ; return 1 ;;
  esac
}

if [ $# -eq 0 ]; then
  steps
  echo "  Run with --all to execute, or --step N to run one step."
  echo ""
  exit 0
fi

case "${1:-}" in
  --step)
    step="${2:-}"
    confirm="${3:-}"
    case "$step" in
      1) [ "$confirm" = "--confirm" ] && confirm_step "Step 1" && step1_create || step1_create ;;
      2) [ "$confirm" = "--confirm" ] && confirm_step "Step 2" && step2_sync || step2_sync ;;
      3) [ "$confirm" = "--confirm" ] && confirm_step "Step 3" && step3_deploy_code || step3_deploy_code ;;
      4) [ "$confirm" = "--confirm" ] && confirm_step "Step 4" && step4_reload || step4_reload ;;
      5) [ "$confirm" = "--confirm" ] && confirm_step "Step 5" && step5_verify || step5_verify ;;
      6) [ "$confirm" = "--confirm" ] && confirm_step "Step 6" && step6_cleanup || step6_cleanup ;;
      r|R|rollback) rollback ;;
      *) echo "Unknown step: $step"; steps; exit 1 ;;
    esac
    ;;
  --run-to)
    target="${2:-}"
    case "$target" in
      1) step1_create ;;
      2) step1_create; step2_sync ;;
      3) step1_create; step2_sync; step3_deploy_code ;;
      4) step1_create; step2_sync; step3_deploy_code; step4_reload ;;
      5) step1_create; step2_sync; step3_deploy_code; step4_reload; step5_verify ;;
      *) echo "Unknown target: $target"; exit 1 ;;
    esac
    ;;
  --all)
    confirm_step "Step 1 (create table + copy data)" && step1_create
    confirm_step "Step 2 (sync dev → prod)" && step2_sync
    confirm_step "Step 3 (deploy code — dry-run)" && step3_deploy_code
    confirm_step "Step 4 (verify imports + reload gunicorn)" && step4_reload
    sleep 3
    confirm_step "Step 5 (full verification)" && step5_verify
    confirm_step "Step 6 (drop old tables)" && step6_cleanup
    echo ""
    echo "  🎉 Migration complete!"
    ;;
  --rollback|rollback)
    rollback
    ;;
  *)
    echo "Usage:"
    echo "  ./migrate.sh                          # show steps"
    echo "  ./migrate.sh --step N                 # run one step"
    echo "  ./migrate.sh --step N --confirm       # run with confirmation prompt"
    echo "  ./migrate.sh --run-to N               # run steps 1-N"
    echo "  ./migrate.sh --all                    # full playbook with prompts"
    echo "  ./migrate.sh --rollback               # deploy old code, keep old table"
    exit 1
    ;;
esac
