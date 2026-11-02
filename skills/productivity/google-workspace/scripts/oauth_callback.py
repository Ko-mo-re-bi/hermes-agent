"""Single-use loopback Google OAuth; no callback data is printed or persisted."""
from contextlib import contextmanager

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


import sys
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from credential_store import (CallbackError, file_version, writer_lock, atomic_save,
                              credential_transaction)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CallbackError("Token redirect refused")


def exchange(fields):
    # Ignore proxy environment and refuse redirects carrying the POST secrets.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(TOKEN_URI, urlencode(fields).encode(),
                      {"Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(request, timeout=20) as response:
        return json.loads(response.read(65536))




class Session:
    def __init__(self, client, destination, *, scopes, port=0, lifetime=300, transport=exchange):
        if not client.get("client_id") or not client.get("client_secret"):
            raise CallbackError("Desktop client required")
        if not 0 < lifetime <= 600:
            raise CallbackError("Invalid lifetime")
        self.scopes = tuple(scopes)
        if not self.scopes:
            raise CallbackError("Required scopes missing")
        self.client = client
        self.destination = Path(destination)
        self.destination_version = file_version(self.destination)
        self.transport = transport
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.deadline = time.monotonic() + lifetime
        self.start_path = "/start/" + secrets.token_urlsafe(32)
        self.started = False
        self.done = False
        self.success = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Request targets contain authorization codes.

            def do_GET(self):
                self.connection.settimeout(2)
                status, location, body = owner.request(self.path, self.headers.get("Host"))
                self.send_response(status)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(body.encode())

            def handle(self):
                self.connection.settimeout(2)
                super().handle()

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.server.timeout = 0.2
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.redirect = self.origin + "/callback"

    def request(self, target, host):
        if host != urlsplit(self.origin).netloc:
            return 400, None, "Invalid host"
        if self.done or time.monotonic() >= self.deadline:
            return 410, None, "Session closed"
        parsed = urlsplit(target)
        if hmac.compare_digest(parsed.path.encode(), self.start_path.encode()) and not parsed.query and not self.started:
            self.started = True
            challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
            return 302, AUTH_URI + "?" + urlencode({
                "client_id": self.client["client_id"], "redirect_uri": self.redirect,
                "response_type": "code", "scope": " ".join(self.scopes), "state": self.state,
                "code_challenge": challenge, "code_challenge_method": "S256",
                "access_type": "offline", "prompt": "consent"}), ""
        if parsed.path != "/callback" or not self.started:
            return 404, None, "Not found"
        params = parse_qs(parsed.query, keep_blank_values=True)
        if (len(params.get("state", [])) != 1 or
                not hmac.compare_digest(params["state"][0].encode(), self.state.encode())):
            return 400, None, "Invalid state"
        self.done = True  # A valid-state callback consumes the session, including errors.
        try:
            if "error" in params or len(params.get("code", [])) != 1 or not params["code"][0]:
                raise CallbackError("Authorization refused")
            self._complete(params)
            self.success = True
        except Exception:
            return 200, None, "<script>history.replaceState(null, '', '/failed')</script>Authorization failed; previous credentials preserved"
        finally:
            self.verifier = ""
            self.state = ""
        return 200, None, "<script>history.replaceState(null, '', '/complete')</script>Authorization completed"

    def _complete(self, params):
        def authorize(_):
            if file_version(self.destination) != self.destination_version:
                raise CallbackError("Credential destination changed")
            return self._exchange_payload(params)

        credential_transaction(self.destination, authorize, read_existing=False, backup=True)

    def _exchange_payload(self, params):
        token = self.transport({"grant_type": "authorization_code", "code": params["code"][0],
                                "client_id": self.client["client_id"],
                                "client_secret": self.client["client_secret"],
                                "redirect_uri": self.redirect, "code_verifier": self.verifier})
        granted = token.get("scope", "").split()
        if (not all(isinstance(token.get(k), str) and token[k] for k in ("access_token", "refresh_token"))
                or token.get("token_type", "").lower() != "bearer"
                or not set(self.scopes).issubset(granted)):
            raise CallbackError("Incomplete grant")
        expires = int(token["expires_in"])
        if expires <= 0 or time.monotonic() >= self.deadline:
            raise CallbackError("Expired grant")
        payload = {"type": "authorized_user", "token": token["access_token"],
                   "refresh_token": token["refresh_token"], "token_uri": TOKEN_URI,
                   "client_id": self.client["client_id"], "client_secret": self.client["client_secret"],
                   "scopes": granted, "expiry": (datetime.now(UTC) + timedelta(seconds=expires)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        return payload

    def run(self):
        try:
            print(f"Open in your own browser: {self.origin}{self.start_path}", flush=True)
            while not self.done and time.monotonic() < self.deadline:
                self.server.handle_request()
            return self.success
        finally:
            self.server.server_close()
            self.verifier = ""
            self.state = ""


def _other_writer_running() -> int:
    """Count live Google token writers other than this process (metadata only)."""
    hits = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if any(m in argv for m in ("google_api.py", "gws_bridge", "gapi-shared")):
            hits += 1
    return hits


def login(client_path, destination, port, scopes):
    """Opt-in single-use login. Runs only when this CLI is called explicitly."""
    if os.environ.get("HERMES_SHARED_OAUTH_DISABLE") == "1":
        print("Authorization disabled: HERMES_SHARED_OAUTH_DISABLE is set")
        return False
    writers = _other_writer_running()
    if writers:
        print(f"Authorization refused: {writers} other Google writer(s) running")
        return False
    try:
        client = json.loads(Path(client_path).read_text(encoding="utf-8"))["installed"]
        session = Session(client, destination, scopes=scopes, port=port)
        ok = session.run()
    except (Exception, KeyboardInterrupt):
        ok = False
    print("Authorization completed" if ok else
          "Authorization failed or expired; previous credentials preserved")
    return ok
