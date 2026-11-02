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
`/tmp/jev-fallback-test-python` supplied the existing test interpreter to the
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
`HERMES_PYTHON=/tmp/jev-fallback-test-python scripts/run_tests.sh -j 1 tests/skills/test_google_workspace_callback.py tests/skills/test_google_workspace_setup.py tests/skills/test_google_workspace_api.py -q`

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
