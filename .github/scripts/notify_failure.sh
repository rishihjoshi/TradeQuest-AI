#!/usr/bin/env bash
# Open (or reuse) a tracking issue when a TradeQuest workflow fails.
#
# WHY: between 2026-08-10 and 2026-08-17 every Agent and Market Open run failed with an
# exhausted Anthropic credit balance. Nothing surfaced it, so the book sat unmanaged for a
# week with an open naked short. Scheduled workflows have no UI — they need to shout.
#
# Reuses an existing open issue with the same title instead of filing one per run, so a
# multi-day outage produces one issue with a comment trail rather than 20 duplicates.
#
# Usage: notify_failure.sh "<workflow name>"

set -euo pipefail

WORKFLOW="${1:-TradeQuest}"
TITLE="🔴 ${WORKFLOW} workflow is failing"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
STAMP="$(date -u +'%Y-%m-%d %H:%M UTC')"

BODY="Scheduled run failed at ${STAMP}.

- Workflow: \`${WORKFLOW}\`
- Run: ${RUN_URL}

**Trading impact:** order execution may be stalled. Check for an open short with
\`python bot/rebalance_trueup.py --dry-run\` before assuming the book is flat.

This issue stays open until the workflow goes green again; repeat failures are added as comments."

EXISTING="$(gh issue list --state open --search "\"${TITLE}\" in:title" \
              --json number,title --jq ".[] | select(.title == \"${TITLE}\") | .number" \
            | head -n1)"

if [[ -n "${EXISTING}" ]]; then
  echo "Reusing issue #${EXISTING}"
  gh issue comment "${EXISTING}" --body "Still failing — ${STAMP}. Run: ${RUN_URL}"
else
  gh issue create --title "${TITLE}" --body "${BODY}"
fi
