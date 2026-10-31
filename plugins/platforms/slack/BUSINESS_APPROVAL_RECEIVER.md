# Slack business approval: explicit result hand-off (draft)

This module is NOT deployed and never interprets approval as permission to
execute a tool. It is separate from exec approvals, Slack ingress, and the
existing producer's durable decision and receipt publication.

## Rationale

The existing producer stores the first decision in the private cards table,
then atomically writes a result markdown file to the approved outbox.
outbox_written_at means PUBLISHED, not RECEIVED, HANDLED or ACKNOWLEDGED.

business_approval_receiver.py adds a local, explicitly invoked state machine
using a business_approval_receipts table in the EXISTING approval SQLite DB.
No new Bot, queue, service, timer, automated executor or Slack message is
added. Existing cards schema and receipt bytes are unchanged.

## Workflow

The responsible operator is joushou. With an existing privately owned
database/outbox and an actually registered, decided card:

1. List (read-only):

    python -m plugins.platforms.slack.business_approval_receiver --db /ABS/business.sqlite --outbox /ABS/outbox --profile koumei list

2. Claim a validated delivered result:

    python -m plugins.platforms.slack.business_approval_receiver --db /ABS/business.sqlite --outbox /ABS/outbox --profile koumei claim --request-id business-<32-hex> --operator joushou

3. The operator processes/routes the business result OUTSIDE this CLI,
retaining request_id as the downstream idempotency key. The command does not
perform that work or grant authority to do it.

4. Only AFTER separate handling succeeds, acknowledge with a durable,
query-string-free evidence reference:

    python -m plugins.platforms.slack.business_approval_receiver --db /ABS/business.sqlite --outbox /ABS/outbox --profile koumei ack --request-id business-<32-hex> --operator joushou --claim-token <local-token> --evidence-ref HD-20261030-001/handled

claim creates the added table on first successful use; list does not.
No database is bootstrapped automatically. Only private, same-UID 0700
directories and 0600 files are accepted. The receipt filename is derived
from a fixed-format request_id. Receipt bytes must match the durable
SQLite decision exactly, using O_NOFOLLOW and link-count checks.
Missing, changed, badly permissioned or undelivered receipts fail closed.

Status values:
- receipt_pending: decision exists but not durably published.
- receipt_invalid: delivered flag set but receipt missing/invalid.
- ready: valid receipt has no active claim (including expired leases).
- claimed: an unexpired lease exists; not yet handled.
- acked: explicit acknowledgement with a processing evidence reference.

Claims use SQLite BEGIN IMMEDIATE. Concurrent duplicate claims are rejected.
Expired claims may be reissued under a different token; stale tokens fail.
Repeated identical ACKs succeed idempotently; changed evidence or actor fails.
State survives process restarts.

## Critical limits / release gate

- This is NOT an exactly-once external side-effect engine. If business
  handling succeeds and the process crashes before ACK, a retry could repeat
  that business handling. The downstream handler must deduplicate by
  request_id before executing any side effect.
- Operator strings are audit labels, not independent identity proofs.
  The trust boundary is the private process UID and local access.
- Human processing and failure recovery still require a signed-off runbook.
  This code supplies durable hand-off state, NOT automatic business execution.
- No fictional approval record may be created in the production database.
- Do not merge, modify live configuration, restart the gateway, or change
  test/prod DB on this draft. Phase 0 stability and notification-loop
  containment remain prerequisites. Muse handles approved activation.
- The receiver never modifies existing cards rows.
- Release requires reviewer verification of local ownership, migration and
  rollback, plus canonical and live tests.

## Tests

    HERMES_HOME=/home/hermes-svc/.hermes scripts/run_tests.sh tests/gateway/test_slack_business_approval_receiver.py tests/gateway/test_slack_business_approval.py

The new tests cover restart, claim/ACK, concurrency, lease takeover, stale
tokens, profile isolation, missing/tampered/symlink receipts, permissions,
missing DB, and minimal status output.

Do not call tests PASS until a real runner log is available.
