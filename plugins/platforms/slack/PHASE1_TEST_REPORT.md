# Slack Phase1 limited test report

The cancellation fix retains viewer ownership and one of four shared read slots until
its thread finishes. Each viewer admits one active and at most one pending request.
Cancelled reads do not publish. Thread URL buttons have an ACK-only handler.
Attachment counts now explicitly say their status as deliverables is unverified.

Limited stdlib smoke: exit 0. Memory probes covered active and repeated cancellation,
subsequent requests, pending cancellation before and immediately after owner release,
the shared read bound, and timeout cleanup. Fixture checks covered read-only WAL,
secret suppression, attachment wording, scoped thread URLs, grant revocation and cooldown.
A 10ms loop timer was used for thread callback delivery in this environment; this does
not establish live gateway responsiveness. No real profile, credentials or database were read.

The standalone smoke harness is retained locally and is NOT supplied in this PR;
its command is therefore not a checkout-only reproduction procedure. This is an
explicit reproducibility limitation, not a substitute for canonical regression.
The local command was `python -B phase1_final_smoke.py` (run with its actual local
path). A separate parent invocation on 2026-10-30 also returned exit 0; its output
and return code were captured from the same subprocess invocation.

The harness loads the committed memory contracts in
`tests/gateway/test_slack_home_worker_ownership.py`; corresponding WAL/redaction/
authorization fixture contracts are in `test_slack_coordination_home_revision.py`.
Syntax parsing and `git diff --check` passed.
The new adapter registration/fake dispatch/ACK unit contract was added but NOT RUN.
Pytest, the official runner and existing approval regressions were NOT RUN in this revision.
For an appropriately provisioned test environment, the intended unit command is:

```sh
scripts/run_tests.sh tests/gateway/test_slack_home_worker_ownership.py tests/gateway/test_slack_coordination_home.py tests/gateway/test_slack_coordination_home_revision.py
```

Earlier smoke output contains exceptions despite a separate zero-exit record. Those
original files are retained and are not success evidence. A separate parent-reported
successful run does not repair that mismatch. The new run saves output, return code
and source hashes together under a unique run directory.

Remaining limitations: unknown native assignee defaults render as unknown; WAL sidecar
races remain; revocation of already published Home views, approval binding, artifact
URLs and live observation of all three agents remain unconnected. This is not completion
of Phase1 or a formal acceptance decision.
