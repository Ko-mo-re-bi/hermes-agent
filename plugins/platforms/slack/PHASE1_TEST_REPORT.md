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

The standalone stdlib harness is now supplied as `phase1_smoke.py` in this directory.
From the checkout root, with Python >= 3.12, run:

```sh
python -B plugins/platforms/slack/phase1_smoke.py --fixture-root /path/to/your/scratch-dir
```

The existing scratch root argument is mandatory; modules resolve relative to this
checkout. Fixtures and logs use only unique TemporaryDirectory paths below that root.
Fixture directories are cleaned; the evidence directory is retained with stdout/stderr,
exit code and SHA-256 hashes of the entrypoint, both implementation modules and the
loaded ownership contracts, all under one run ID. No default/system temp is used.
The scratch original and earlier logs remain untouched. This does not alter or
impersonate pytest/conftest and is separate from canonical regression.
The bundled entrypoint returned exit 0 on 2026-10-30, run ID
`phase1-smoke-c6heduod`; all existing smoke assertions were retained, including
ACK-only, timeout, cancellation ownership and the four-worker bound.

The harness loads the committed memory contracts in
`tests/gateway/test_slack_home_worker_ownership.py`; corresponding WAL/redaction/
authorization fixture contracts are in `test_slack_coordination_home_revision.py`.
Syntax parsing and `git diff --check` passed.
The local official runner and existing approval regressions were not run in this revision.
See the separate remote report below for the Mac result.
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

## Remote canonical runner report (raw log not independently retrieved)

Mac peer reported the official runner completed at commit
`3a17801924b36dabd55ac4893bc483a3835be84e`: exit 0, five files,
81 tests (ownership 1, revision 1, home 12, approval_buttons 20, business 47).
Status: **remote reported, raw log not independently retrieved**. The parent has
not obtained the original log or duration artifact. Implementation modules and
existing pytest files remain unchanged to preserve the behavior tested on that SHA.
This report is not live Slack verification or formal acceptance. Phase1 remains
incomplete; Phase2/3 have not started; no production changes were applied.

## Local lint scope

`git diff --check` and `ruff check --no-cache plugins/platforms/slack/phase1_smoke.py`
passed. Ten existing `scripts/check` guards were invoked (footguns, shebangs,
no-tmp, config-writers, os-marker-fakes, agents-md, case-collisions, lazy-deps,
profile-archives, blob-size). Eight passed; footguns and no-tmp failed. New harness
encoding findings (including the BOM-tolerant read policy on the follow-up guard run)
were fixed and the final harness was rerun successfully. The complete guard was
not rerun after the final encoding-only fix.
Existing footgun findings in `business_approval.py` and literal temporary paths in
pre-existing untracked evidence files were left untouched, as required by scope.
The full lint suite and local canonical pytest were not run; no bootstrap occurred.
