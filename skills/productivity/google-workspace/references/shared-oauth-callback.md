# Shared Google OAuth: current isolated verification (2026-11-03)

This section supersedes the historical receipts below. All earlier uncommitted
work has been retained on `hermes/google-shared-oauth-callback-20261102`.
Public callback `login()` remains unconditionally fail-closed before client-file
access, sockets or token exchange. No activation, real credential access, Google
request, deployment, push or PR is part of this work. Do not bypass this gate
through the legacy manual authorization commands.

## Current implementation and writer inventory

All repository token writes use `scripts/credential_store.py`:

| Actual entry | Shared persistence |
| --- | --- |
| `google_api.get_credentials` | `refresh_session`: locked read → SDK refresh → guarded save |
| `setup.check_auth` (`--check`) | Same `refresh_session` protocol |
| `gws_bridge.get_valid_token` → `refresh_token` | `refresh_session`: locked read → fakeable HTTP refresh → guarded save |
| `oauth_callback.Session._complete` | `credential_transaction(..., read_existing=False, backup=True)` |
| `setup.exchange_auth_code` | `atomic_save` with pre-exchange destination version |
| `setup.revoke` | Shared exclusion throughout revoke and deletion |
| `credential_store.restore_backup` | Explicit restore under shared exclusion |

The common lock is the permanent sibling `.google-token.lock` (0600), opened
without following symlinks, with exclusive POSIX `flock`. Refresh sessions wait for this same lock; callback
and other writes retain nonblocking fail-closed contention. Never unlink/replace the lock file or rename its private directory.
Refresh sessions read JSON **after acquiring the lock and retain it through refresh
and commit**. A waiting participant rereads the winner and skips redundant refresh.
A second reread before saving discards an in-flight result if a nonparticipating
writer changed the payload; callers receive the winner without replacing its inode. Callback checks its pre-session file metadata
under the same lock before exchange. Manual exchange checks pre-exchange metadata.

Staging uses same-directory `mkstemp` (0600), serialization, flush, file fsync,
`os.replace`, then directory fsync. Only callback creates the sibling
`google_token.json.backup` before replacing existing credentials; that backup is
also staged, fsynced, replaced and directory-fsynced with mode 0600. Backup failure
prevents token replacement. Restore validates JSON and uses the same byte-save
protocol and lock without refreshing or making another backup.

A kill before replacement retains the old complete file and can leave a private
orphan staging file. Recovery acquires the same lock before removing verified
uncommitted staging files. A kill after replacement leaves complete new JSON.
A directory fsync failure after replace reports failure but may leave the new
file installed; no automatic rollback overwrites a later writer. These tests
prove process interruption behavior and fsync ordering, **not actual power-loss
survival**. Hardware/filesystem fsync guarantees require separate validation.
Private stable local directories and trusted same-user processes are assumptions;
unknown/external writers and deployment copies are not proven to participate.

## Reproducible fictional verification

Run only in this isolated checkout with the existing test interpreter:

```sh
HERMES_PYTHON=/tmp/google-oauth-test-python scripts/run_tests.sh -j 1 tests/skills/test_google_workspace_shared_store.py tests/skills/test_google_workspace_callback.py tests/skills/test_google_workspace_setup.py tests/skills/test_google_workspace_api.py tests/skills/test_google_workspace_credential_files.py tests/skills/test_google_workspace_setup_deps.py -q
```

The wrapper redirects runner scratch from read-only `/var/tmp` to `/tmp`; it
changes no guard, hooks, production settings or dependency environment. The
new shared-store tests run the actual three refresh entry points in concurrent
processes, replacing SDK/HTTP transports with fictional responses. Each route takes its turn as the first refresher. A separate flock probe must
fail during its transport; both contenders reach the same lock before release.
Only the leader refreshes; all readers reuse its result without changing its inode.
Three additional cases deliberately replace the token without cooperation during
refresh and verify that every route discards its stale result. Six further subprocess cases
SIGKILL each actual route immediately before/after replacement, verify old/new
complete JSON, private staging permissions, lock release and recovery. Additional
checks cover file/replace/directory sync order and actual callback backup → shared
restore → existing bridge reader. Existing callback contention/failure tests remain.
IPv4 fake-server integration skips in this sandbox because socket creation is
denied; no successful real HTTP, SSH or Google check is claimed.

The implementation receipt and source hashes are in
`shared-oauth-validation.txt`. The previous 70-test independent PASS was rejected:
it covered save-time races without proving refresh exclusion. That receipt is
superseded by the current review and test results; do not use its hashes or verdict.
Current implementation and independent review: **75 PASS, 0 FAIL, 1 socket SKIP**
each, on matching reviewed code/test hashes. Gate status remains **CLOSED**. No live Drive/Calendar read or deployed-writer
inventory is supplied by fictional tests. No always-on listener is installed.

## PR and later reapplication handoff

Do not create a PR or write GitHub from this task. Give the final local diff,
receipt and independent verdict to the separately authorized PR operator. Merge
requires source/test proof, fictional race/interruption/permissions/restore PASS,
and independent PASS. The named branch is the sole source of this work.

After eventual authorized merge, the operator must record the merged commit and
compare SHA256 of **each changed script**, including `credential_store.py`, with
`git show MERGED:skills/productivity/google-workspace/scripts/FILE`. Compare only
source scripts in the active skills tree; never inspect credential files. Actual
active-tree identity has not been checked by this isolated task.

For an update that overwrites those scripts, prepare a scripts-only patch from the
reviewed base to the approved merged commit, using `git diff --binary
--relative=skills/productivity/google-workspace BASE MERGED --
skills/productivity/google-workspace/scripts`. Save it outside the active tree.
The authorized operator works in a disposable copy of the active skill first:

1. Compare source hashes against the approved merged commit. If all match,
   `--check` succeeds and reapplication is a no-op.
2. Otherwise run `git apply --check PATCH` in that disposable skill copy. A failure
   is a conflict: stop; never force replacement. A successful check is required
   before `git apply PATCH` there.
3. Rerun the fictional canonical suite against the reapplied scripts and compare
   every script hash again. Only the separately authorized deployment operator
   transfers validated source to the active tree; perform the same check there.
4. Rerun the hash comparison/`--check`: it must report the already-current no-op.
   Leave activation closed if any gate or source identity is missing.

This is a future idempotent reapplication procedure, not a deployment performed
here. No commit, main change, installed-skill change or external write is implied.

---

# Historical receipts (superseded by the current section above)

# Shared Google connection: isolated callback implementation

This change adds an isolated callback prototype. `setup.py --callback` is now
**disabled unconditionally** before client-file reads, socket creation or Google
requests because shared-writer exclusion cannot be established. Do not use
`--auth-url`, `--auth-code`, `--check-live` or `--revoke` for this procedure. No live
login or deployment was performed during implementation. Approval belongs to the
request owner; the instructions below are for a separately authorized deployment.

## Compatibility and deployment gates

The checked-in `google_api.py` uses `Credentials.from_authorized_user_file` on
`get_hermes_home()/google_token.json`; `gws_bridge.py` reads `token`, `expiry`,
`refresh_token`, `client_id`, `client_secret`, `token_uri` from the same file.
The new flow writes that authorized-user JSON shape, including `type`, `scopes`,
and UTC `expiry`. It requires a new refresh token and explicit granted scopes;
there is no fallback that reads or merges previous credentials.

There is no `gapi-shared` definition in this checkout. Its actual executable,
profile, service account owner, credential path, and scope needs MUST be confirmed
from nonsecret deployment metadata before rollout. Compatibility with the repository
reader does not establish compatibility with that external wrapper. Do not open
credential files to investigate. Do not create another connector.

The callback receives the complete `setup.py.SCOPES` list directly from the CLI:
Gmail readonly/send/modify, Calendar, Drive, Contacts readonly, Sheets and Docs
(eight scopes). It requests and requires every existing permission; it does not
silently replace the shared connection with a Drive/Calendar readonly grant.
The token response must explicitly include all required scopes. A missing scope
field or any omitted required permission fails before saving, even if callback
query parameters claim the full grant. Previous credential bytes remain intact.
No old credential file is read to infer scopes. Confirm that the external shared
reader needs no additional permissions before adoption.

Use an existing Google **Desktop app** client (`installed` JSON). Web/mobile client
files are rejected. The approved operator must establish client type, enabled APIs for all existing services (including Drive
and Calendar), consent-screen/test-user eligibility and organization policy
without exposing client values. Endpoints are fixed Google HTTPS URLs; arbitrary
endpoints in downloaded metadata are ignored. No new dependencies are required.
Google documents desktop loopback support here:
https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration
and PKCE here:
https://developers.google.com/identity/protocols/oauth2/native-app
These are requirements to verify against the actual approved client, not evidence
of a successful live authentication.

## Reachability and future authorized execution

The repository's `website/docs/guides/oauth-over-ssh.md` describes local SSH
forwarding. For this helper, from the browser machine, use the existing authorized
SSH host/identity (no SSH config edits):

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 approved-host
```

With an explicitly selected owning Hermes profile, its service user, and private
profile directory, the future authorized operator runs:

```sh
python skills/productivity/google-workspace/scripts/setup.py --callback --callback-port 8765
```

The only printed URL is `http://127.0.0.1:8765/start/<random-one-time-value>`. Open it in the user's own
browser privately; do not paste or record this one-time start capability in chat.
The fixed `/start` endpoint is disabled. Each session generates an independent
256-bit random start path, which is accepted once, only before expiry. Wrong paths
do not consume it. This prevents guessing the start URL; a local process that can
observe the printed URL can still consume it first. The start endpoint redirects to Google without printing the authorization
URL. The user logs in and consents there. No copying of callback URLs or codes is
needed or permitted. The callback contains the code as required by OAuth; the
response immediately replaces the browser history entry with `/complete` or
`/failed`. This cannot guarantee removal from browser extensions, network capture,
or browser internals: use a trusted browser without diagnostic recording. HTTP
access logs are disabled. No code, state or verifier is saved to disk. Do not enable
HTTP debug logging, shell tracing, terminal recording or a reverse proxy.

Both ends must bind IPv4 loopback, on the same port. No public listener, ingress,
TLS proxy, firewall or gateway change is required. The listener lives for five
minutes, with bounded per-connection and token-exchange timeouts. A callback with
valid state consumes the attempt even on refusal or exchange failure; invalid
state cannot exchange a code. PKCE S256 binds the exchange to the in-memory verifier.
Google, rather than this client, verifies PKCE. Retry requires a fresh process.

A fake HTTP roundtrip test is supplied, but socket creation is prohibited in the
current sandbox, so it has not run successfully. Only the in-process state machine,
fake token exchange and file persistence are verified here. SSH from the real user's machine,
port availability there, Google acceptance and real consent are NOT verified.
Before authentication, confirm the approved forwarding route with a harmless
loopback-only probe, and establish the intended service owner/profile/path using
nonsecret metadata. Do not call this reachability complete until that succeeds.

## Backup, atomic save and restoration

Before any separately approved live run, pause competing refresh writers and back
up the exact owning credential file using the existing approved private backup
mechanism. This implementation deliberately does not read/copy an old token. Do
not use chat, stdout, command substitutions, repository files or a public temporary
directory for backups. Keep ownership and mode 0600 in a mode-0700 directory; the
operator must record only backup location and timestamp, never values. A filesystem
snapshot is suitable if the established operational procedure supports it.

The isolated `atomic_save` helper acquires a nonblocking POSIX `flock` on the
private destination **directory inode**, held across temporary-file creation,
serialization, file fsync, metadata check and atomic replacement. Contention fails
without modifying the destination. Closing the directory descriptor releases the
lock, including on exceptions, KeyboardInterrupt and process death. No removable
lockfile, PID inference, timeout lock stealing or shell lock is used. This protocol
requires a stable private directory on a local POSIX filesystem: do not rename or
replace the directory while any writer runs. Every participating writer must use
the same directory lock for its entire read/refresh/write transaction, including
deletions and restoration. Locking only the final write permits stale refreshes.
The current helper tests a save transaction only, not a migration of those writers.

The complete new JSON is staged in a same-directory 0600 file and fsynced before
`os.replace`. Serialization, file fsync, metadata conflict, replacement failure and
catchable interruption before commit preserve old bytes and remove staging files.
The old file is never truncated, read, copied or automatically restored. Restoring
old bytes after releasing exclusion could destroy another writer's valid update.
A process killed before replacement leaves the previous file and possibly a private
orphan staging file; an approved recovery operator must hold the same lock while
discarding uncommitted staging files, without displaying contents. Death after
replacement leaves a complete new file. This is process-crash atomicity, **not
power-loss durability**: the directory entry is not fsynced, and storage/filesystem
failure, directory replacement and nonparticipating writers are outside the claim.

**Production fails closed.** `setup.py` (refresh, legacy OAuth, revoke),
`google_api.py` (refresh) and `gws_bridge.py` (refresh) do not participate in this
lock. External `gapi-shared` writers are unknown. Metadata checks remain useful
conflict detection but cannot close their check/replace race. No operator flag or
"writers stopped" assertion bypasses the CLI refusal. Merely acquiring this lock
never proves exclusion of those writers. A separately reviewed migration of every
actual writer, verified external-wrapper ownership/path/scopes and a proven
operational exclusion/recovery contract are required before enabling a live entry.
The earlier execution example is a future reachability shape, **not an executable
live procedure in this revision**; it currently returns failure without reading
credentials or initiating authentication.

No old grant is revoked. A separately approved restoration must exclude all actual
writers and restore the private backup with a same-directory atomic replacement,
preserving ownership/mode, before restarting consumers. A failed precommit save
needs no restore because old bytes were retained. Replacement does not reverse
Google consent changes. No backup or restoration of real credentials was performed.

## Secret-free live recovery check (not executed here)

After separate approval, check through the **actual existing shared reader**, not a
new connection. It must load the owning credential file privately, refresh in
memory if needed, and make only these reads:

* Drive v3 `files.list(pageSize=1, fields="nextPageToken")`.
* Calendar v3 `calendarList.list(maxResults=1, fields="nextPageToken")`.

Discard bodies and identifiers, including pagination tokens; report only
`Drive read: PASS/FAIL` and `Calendar read: PASS/FAIL`. Catch errors without printing
exception text, URLs, HTTP bodies or credentials. Do not use generic `google_api.py`
commands that print file/event data for this check. An authorized verifier should
use the existing reader's in-process API and suppress SDK debug logs, with no token
or secret in argv. Confirm it uses the exact shared connection before declaring
recovery. Merely parsing JSON or passing the fake test is insufficient. If the
external reader cannot perform such a check, that is an unresolved rollout gate.

## Isolated verification receipt (2026-11-02)

Branch: `hermes/google-shared-oauth-callback-20261102`. No commit, push, PR,
production change, real credential read or live Google login was performed.

All tests used the canonical `scripts/run_tests.sh`, one worker, fictional values.
An existing test-library-only bundle was copied into
`/tmp/google-callback-test-runtime/testlibs` and supplied pytest through `PYTHONPATH`;
no package installation or package-manager change was made. An initial runner
attempt could not import pytest, tried PM bootstrap and stopped at a read-only
cache lock. Another run hit the home-I/O guard while using the original library
location; copying only test libraries to `/tmp` resolved that without guard edits.
The first new omission tests counted the runner-created `hermes_test` directory
as an unexpected file; corrected assertions compare against the pre-attempt directory
contents. The final run above passed without retries or guard changes. The runner initially
failed before tests because `/var/tmp` is read-only. A temporary interpreter wrapper
outside the repository redirected only `_runner_scratch_root()` to
`/tmp/google-callback-test-runtime/scratch`. No guard/hook/config files changed.

* Final three-file run: callback **23 passed, 1 skipped**, setup **3 passed**,
  API **7 passed**; total **33 passed, 0 failed, 1 skipped**, one worker, **3.6 s**.
* `git diff --check`: passed.
* HTTP integration skips explicitly on `PermissionError` creating an IPv4 socket;
  no successful browser-machine forwarding or history scrub is claimed.
* Coverage includes all eight setup scopes in authorization and saved payload,
  each missing permission, malicious callback scope claims, omitted token scope,
  valid grant to existing bridge reader, private save, replay, state, PKCE,
  consent refusal, expiry and serialization/fsync/replacement failures preserving
  previous bytes and cleaning temporary files.
* Historical `git log -p -S REDIRECT_URI` could not complete: this partial clone
  tried to fetch missing objects but GitHub DNS was unavailable. Current source
  shows the manual localhost-code-copy flow; its historical intent is unconfirmed.

The acceptance conditions are **not fully verified**. The missing real HTTP fake-AS
run, external shared-reader path/scope contract and browser-machine SSH reachability
remain required before adoption. Live authentication and recovery checks are
separate authorized operational work, never implied by this receipt.

## Independent review corrections (2026-11-02)

The isolated revision adds metadata conflict detection and a random single-use
start path. Fake competing writers exercise direct rewrite, atomic replacement,
deletion and creation both during exchange and after temporary-file fsync (eight
cases). All eight overwrote competing state before the fix and fail safely after
it. Start tests cover fixed/wrong paths, independent session paths, replay,
expiry and absence of state/verifier/client secret/code in captured output.

Current limited run: callback **32 passed, 1 skipped**, setup **3 passed**, API
**7 passed**: **42 passed, 0 failed, 1 skipped**, one worker, 4.4 seconds (final rerun).
The fake HTTP-AS roundtrip still skips because AF_INET socket creation is denied.
`/tmp/google-oauth-test-python` supplied the existing test interpreter to the
canonical runner and redirected runner scratch from read-only `/var/tmp` to `/tmp`;
no repository runner, guard, hooks or config changed. The initial default-runner
attempt failed at a read-only PM cache lock; no dependencies were installed.
The older receipt above describes the preceding session, not the current runtime.

Existing-writer exclusion, external `gapi-shared` compatibility, SSH reachability,
Google client acceptance and real recovery remain unverified. No real credential
contents or production files were accessed, and no live authorization, commit,
push or PR was performed. Adoption remains the request owner's decision.

## Cooperative exclusion revision receipt (2026-11-02)

The CLI now fails closed unconditionally, before client credential access or
network activity. Existing tracked/untracked work was retained and extended;
legacy writers and external services were not modified. The directory lock is
verified only with fictional participating writers, never claimed to protect
nonparticipants. CLI refusal is tested with credential reads and Session creation
replaced by forbidden operations.

Canonical limited run:
`HERMES_PYTHON=/tmp/google-oauth-test-python scripts/run_tests.sh -j 1 tests/skills/test_google_workspace_callback.py tests/skills/test_google_workspace_setup.py tests/skills/test_google_workspace_api.py -q`

Result: **47 passed, 0 failed, 1 skipped**, three files, one worker, **4.6 s**.
Callback: **37 passed, 1 skipped**; setup: **3 passed**; API: **7 passed**.
The pre-existing temporary interpreter wrapper described above was reused; no
package installation, runner/guard/hooks/config edit or full-suite run occurred.
New tests cover actual subprocess lock contention, lock release after forced
process termination, precommit KeyboardInterrupt cleanup/retry, and abrupt
process exit immediately before/after atomic replacement. The precommit crash
test explicitly verifies the private orphan and its removal under exclusion;
postcommit death verifies complete new JSON. Existing fake exchange/save failure
and competing nonparticipant metadata-conflict tests also pass.

The HTTP fake-AS test still skips on denied IPv4 socket creation. Real browser
SSH reachability, Google client acceptance, external gapi-shared compatibility,
all-writer participation, power-loss durability and real Drive/Calendar recovery
are **unverified**. Production acceptance is not achieved. No real credentials,
live authentication, production writes, commit, push or PR were performed.

## HD-20261102-008 isolated handoff (2026-11-02)

This revision continues existing work `20261102-212430-appdev-ad3f69` on the same
worktree and branch; it starts no second authorization service. Base commit:
`0d9bb268c7`. Only this reference, `scripts/oauth_callback.py`, and the fictional
callback tests are changed. No live file, process environment, service definition,
credential, authorization code, rclone, guard, hook or other Bot configuration was
inspected or changed. No Google request, deployment, push or PR was made.

`credential_transaction(path, operation)` now acquires the common directory lock
**before** loading JSON, calls the refresh operation while holding it, and retains
it through staging, fsync and replacement. The callback uses that transaction with
`read_existing=False`: it checks the original metadata and exchanges the new grant
under exclusion, without loading previous credentials. A backup payload can be
restored with the same transaction and `read_existing=False`; restoration does not
refresh or merge the old grant. `atomic_save` shares the same commit implementation.
The existing process-death tests exercise that commit implementation before/after
replacement and lock release; the new tests exercise exclusion at read and refresh,
refresh/serialization/replacement/interruption failures, restoration and the actual
repository bridge reader against restored fictional authorized-user JSON.

### Acceptance and writer inventory

**NOT ACCEPTED for production.** A cooperating transaction primitive is implemented;
all actual writers are not migrated or identified. No claim of complete exclusion,
unknown-writer detection, external-reader compatibility or independent approval is
made. Callback `login()` remains unconditionally disabled before client access,
listener creation or exchange. There is no override flag. Do not invoke any legacy
entry as a workaround.

Source-only inventory (no deployed credential or process inspection):

| Writer / route | Current coverage and remaining gate |
| --- | --- |
| `oauth_callback.Session._complete` | Exchange through commit uses the common transaction; public live entry disabled. |
| `setup.check_auth` | Existing read/refresh/direct write; not migrated. |
| `setup.exchange_auth_code` | Existing pending-state read/exchange/direct write; not migrated. |
| `setup.revoke` | Existing read/optional refresh/revoke/unlink; not migrated. |
| `google_api.get_credentials` | Existing SDK read/refresh/direct write; not migrated; SDK consumers may also refresh in memory. |
| `gws_bridge.get_valid_token` / `refresh_token` | Existing read/refresh/direct write; not migrated. |
| `google_api._run_gws` and external `gapi-shared` | External executable behavior and actual deployed path/owner unknown. |
| Profile copies, backup/restore, manual/third-party writers | Must be inventoried from approved nonsecret operational metadata; not established here. |

Metadata checks do not detect every noncooperative write and cannot exclude the
check/replace race. An advisory lock cannot exclude a same-user writer that ignores
it. A source search cannot establish the deployed executable identity or eliminate
unknown writers. Consequently the requested **all-writer read/refresh/write proof
remains unmet**. Migrating known functions alone would not discharge this gate.

### Proposed operational contract for separate adoption

These are review requirements, not deployed configuration or authorization to run.

* **Management:** canonical source is this branch/worktree pending independent
  review and the Chancellor's adoption decision. The production installation path,
  owning profile, local filesystem and credential directory must be recorded from
  approved nonsecret metadata before adoption; none is asserted as established.
* **Owner:** the Chancellor must name the accountable operator and recovery deputy,
  and record the service UID/GID and approved client administrator. They remain
  unassigned in this isolated work; do not infer ownership from the checkout user.
* **Start/stop:** no callback daemon exists and none was started. Future approved
  execution is a single foreground, five-minute process under the credential owner.
  Stop by interruption or expiry; verify exit before retry. Before enabling it,
  inventory and stop every deployed refresh/restore writer through its existing
  approved supervisor, then verify isolation through access controls and a reviewed
  common transaction integration. No rclone, hook, guard or Bot changes are implied.
* **Monitoring:** record start/exit, PASS/FAIL, approved revision, operator, time and
  gate status only. Never record URL, code, state, verifier, JSON, request/response
  body or raw SDK exception. Define the actual monitoring destination and responder
  before activation; no monitoring integration was installed here.
* **Updates/audit:** independent reviewer checks this diff and fictional test receipt,
  then sends findings to the Chancellor. After adoption, deploy only a reviewed
  revision with a secret-free change record and rollback revision. No automatic
  update or dependency installation is part of this work.
* **Secrets/backup:** require a stable owner-only 0700 local directory, 0600 token
  and private backups with the same owner; enforce service-account access isolation.
  An authorized operator must snapshot the approved credential using the established
  private backup system under complete writer exclusion before any future migration.
  Define encrypted backup storage, access list, retention and restore owner before
  rollout. This work neither copies nor reads actual credentials.
* **Recovery:** precommit failure retains the old file; no rollback is needed.
  After process death acquire the same exclusion, remove only verified uncommitted
  staging files, and validate the destination privately. For an approved rollback,
  load the approved backup privately and atomically replace through the common
  transaction under full actual-writer exclusion, preserving ownership and 0600.
  Do not restore automatically after releasing exclusion. Verify reader compatibility
  before resuming consumers. Directory fsync/power-loss recovery is still outside the
  implementation's durability claim; approve and test the chosen filesystem snapshot
  recovery procedure separately. File restoration cannot undo consent or revocation.

### Independent verification handoff

Review is pending, not performed by this implementation session. The independent
reviewer should reproduce the limited canonical test command below, examine the
lock lifetime in `credential_transaction` and callback exchange, and check the
writer inventory against approved deployment metadata without opening secret files.
They must explicitly retain the failed production gate unless every real writer,
external reader, ownership and exclusion mechanism has been established. Their
findings then go to the Chancellor for adoption/rejection. No external message was
sent by this session.

No real Google consent, shared-reader Drive/Calendar read or production application
is authorized by this handoff. Those remain separate work requiring the person's
consent and the designated approval chain.

Verification command (existing interpreter wrapper reused, no package install):

```sh
HERMES_PYTHON=/tmp/google-oauth-test-python scripts/run_tests.sh -j 1 tests/skills/test_google_workspace_callback.py tests/skills/test_google_workspace_setup.py tests/skills/test_google_workspace_api.py -q
```

Final result: **53 passed, 0 failed, 1 skipped**, three files, one worker, 4.9 s;
callback 43 passed/1 skipped, setup 3 passed, API 7 passed. The fake HTTP roundtrip
still skips because this sandbox denies IPv4 socket creation. In-process fake
exchange, restored-file bridge parsing, subprocess contention/death and persistence
checks passed; no real reader network request occurred. `git diff --check` passed.
Changes remain uncommitted for independent review; no commit/push/PR was made.
