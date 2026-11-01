# Unified Slack Home integration (release gate)
rules_version: v9

This branch is intentionally based on the existing Hermes production lineage, **not** modern upstream main. It changes only approval-related files and the existing observation Home module. Never reset the VPS checkout to upstream main.

- `coordination_home.enabled: true`: the observation Home is the **only** publisher; the approval inbox contributes a bounded section through `home_blocks`. An approval event delegates back to the observation publisher instead of overwriting the view.
- `coordination_home.enabled: false`: approval inbox may publish its standalone Home if explicitly opted in.
- The observation access grant applies to both sections in unified mode. Users other than the fixed approver get no business approval content.
- DM remains separately feature-flagged, and approval never authorizes command execution.
- Verify the 100-block Home budget, profile-bound issuer trust and SQLite state, multiple workspaces, restart and rate limits in the canonical runner. This branch has no live deployment evidence.
- Before production: isolate the exact files, back up existing adapter diff and original file, compare SHA/config/permissions, confirm Phase 0 and Trivy exposure, and obtain deployment owner Muse's external-shell validation.
- Only after these gates: controlled Gateway restart, readback and a real (not fabricated) issuer request, identity-negative checks, double-click and expiry/rollback. Manual approver click cannot be impersonated.

Suggested targeted runner: `scripts/run_tests.sh tests/gateway/test_slack_business_approval_inbox.py tests/gateway/test_slack_coordination_home.py tests/gateway/test_slack_coordination_home_revision.py tests/gateway/test_slack_business_approval.py`.

Release status: **NOT DEPLOYED**.
