"""Fake-only loopback OAuth behavior and authorized_user compatibility."""
import base64
import hashlib
import importlib.util
import json
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest


@pytest.fixture
def callback(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "skills/productivity/google-workspace/scripts/oauth_callback.py"
    spec = importlib.util.spec_from_file_location("workspace_callback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.syspath_prepend(str(path.parent))
    setup_spec = importlib.util.spec_from_file_location("callback_setup", path.with_name("setup.py"))
    setup = importlib.util.module_from_spec(setup_spec)
    setup_spec.loader.exec_module(setup)
    module.SCOPES = setup.SCOPES
    return module


def client():
    return {"client_id": "fictional-client", "client_secret": "fictional-secret"}


def grant(module):
    return {"access_token": "fictional-access", "refresh_token": "fictional-refresh",
            "scope": " ".join(module.SCOPES), "token_type": "Bearer", "expires_in": 3600}


def begin(session):
    status, url, _ = session.request(session.start_path, urlsplit(session.origin).netloc)
    assert status == 302
    return parse_qs(urlsplit(url).query)


def finish(session, **params):
    return session.request("/callback?" + urlencode(params), urlsplit(session.origin).netloc)


def test_fake_authorization_server_full_http_roundtrip(callback, tmp_path, capsys):
    """Fake AS enforces S256; real listener and POST transport."""
    issued = {}

    class FakeAS(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            issued.update(query)
            self.send_response(302)
            self.send_header("Location", query["redirect_uri"][0] + "?" + urlencode({
                "state": query["state"][0], "code": "fictional-code"}))
            self.end_headers()

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            assert challenge == issued["code_challenge"][0]
            assert fields["code"] == ["fictional-code"]
            assert fields["redirect_uri"] == issued["redirect_uri"]
            assert fields["client_secret"] == ["fictional-secret"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(grant(callback)).encode())

    try:
        fake = HTTPServer(("127.0.0.1", 0), FakeAS)
    except PermissionError:
        pytest.skip("Sandbox prohibits AF_INET sockets; HTTP roundtrip unverified")
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    callback.AUTH_URI = f"http://127.0.0.1:{fake.server_port}/authorize"
    callback.TOKEN_URI = f"http://127.0.0.1:{fake.server_port}/token"
    destination = tmp_path / "google_token.json"
    destination.write_text("previous-fictional-content")
    session = callback.Session(client(), destination, scopes=callback.SCOPES)
    original_state, original_verifier = session.state, session.verifier
    result = []
    listener = threading.Thread(target=lambda: result.append(session.run()))
    listener.start()
    try:
        assert session.server.server_address[0] == "127.0.0.1"
        conn = HTTPConnection("127.0.0.1", session.server.server_port, timeout=5)
        conn.request("GET", session.start_path)
        response = conn.getresponse()
        authorize = response.getheader("Location")
        response.read()
        as_conn = HTTPConnection("127.0.0.1", fake.server_port, timeout=5)
        as_conn.request("GET", urlsplit(authorize).path + "?" + urlsplit(authorize).query)
        response = as_conn.getresponse()
        target = response.getheader("Location")
        response.read()
        conn.request("GET", urlsplit(target).path + "?" + urlsplit(target).query)
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        assert b"history.replaceState" in response.read()
        listener.join(5)
        assert not listener.is_alive()
        assert result == [True]
        saved = json.loads(destination.read_text())
        assert saved["type"] == "authorized_user"
        assert saved["token"] == "fictional-access"
        assert saved["refresh_token"] == "fictional-refresh"
        assert set(callback.SCOPES).issubset(saved["scopes"])
        assert destination.stat().st_mode & 0o777 == 0o600
        assert finish(session, state="old", code="fictional-code")[0] == 410
        # Exercise the existing reader against the resulting file, without refresh/network.
        import sys
        from unittest.mock import patch
        with patch.object(sys, "path", [str(Path(callback.__file__).parent), *sys.path]):
            spec = importlib.util.spec_from_file_location("callback_bridge", Path(callback.__file__).with_name("gws_bridge.py"))
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        bridge.get_token_path = lambda: destination
        assert bridge.get_valid_token() == "fictional-access"
        output = capsys.readouterr()
        for secret in ("fictional-code", "fictional-access", "fictional-refresh", "fictional-secret", original_state, original_verifier):
            assert secret not in output.out + output.err
    finally:
        session.done = True
        listener.join(5)
        fake.shutdown()
        fake.server_close()
        thread.join(5)


@pytest.mark.parametrize("failure", ["state", "unicode_state", "duplicate", "denied", "missing_refresh", "scope", "expired", "transport", "save"])
def test_failures_preserve_previous_credentials(callback, tmp_path, monkeypatch, failure, inert_listener):
    path = tmp_path / "google_token.json"
    path.write_bytes(b"fictional-previous")
    previous_entries = set(tmp_path.iterdir())
    response = grant(callback)
    calls = []

    def transport(fields):
        calls.append(fields)
        if failure == "transport":
            raise RuntimeError("fictional-secret must never be displayed")
        return response

    session = callback.Session(client(), path, scopes=callback.SCOPES, transport=transport)
    try:
        begin(session)
        state = session.state
        if failure == "missing_refresh":
            del response["refresh_token"]
        if failure == "scope":
            response["scope"] = ""
        if failure == "expired":
            session.deadline = 0
        if failure == "save":
            monkeypatch.setattr(callback.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("fictional-secret")))
        if failure == "duplicate":
            session.request("/callback?state=" + state + "&state=" + state + "&code=fictional-code", urlsplit(session.origin).netloc)
        else:
            finish(session, state=("wrong" if failure == "state" else "不正" if failure == "unicode_state" else state),
                   **({"error": "access_denied"} if failure == "denied" else {"code": "fictional-code"}))
        assert not session.success
        assert path.read_bytes() == b"fictional-previous"
        assert set(tmp_path.iterdir()) == previous_entries
        if failure in ("state", "unicode_state", "duplicate", "denied", "expired"):
            assert not calls
        if failure not in ("state", "unicode_state", "duplicate", "expired"):
            assert finish(session, state=state, code="fictional-code")[0] == 410
    finally:
        session.server.server_close()


def test_host_start_and_timeout(callback, tmp_path, capsys, inert_listener):
    session = callback.Session(client(), tmp_path / "token", scopes=callback.SCOPES)
    assert session.request("/start", "attacker.example")[0] == 400
    begin(session)
    assert session.request(session.start_path, urlsplit(session.origin).netloc)[0] == 404
    session.deadline = 0
    assert session.run() is False
    assert not session.verifier
    assert "fictional-secret" not in capsys.readouterr().out


@pytest.fixture
def inert_listener(callback, monkeypatch):
    """Unit tests exercise the state machine without claiming network coverage."""
    from unittest.mock import Mock
    server = Mock(server_port=8765)
    monkeypatch.setattr(callback, "HTTPServer", Mock(return_value=server))
    return server


def test_protocol_success_and_reader(callback, tmp_path, inert_listener, monkeypatch, capsys):
    destination = tmp_path / "google_token.json"
    destination.write_text("fictional-previous")
    expected = {}

    def fake_as(fields):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if challenge != expected["code_challenge"][0]:
            raise ValueError("PKCE mismatch")
        assert fields["code"] == "fictional-code"
        return grant(callback)

    session = callback.Session(client(), destination, scopes=callback.SCOPES, transport=fake_as)
    expected.update(begin(session))
    state = session.state
    finish(session, state=state, code="fictional-code")
    assert session.success
    saved = json.loads(destination.read_text())
    assert set(saved["scopes"]) == set(callback.SCOPES)
    assert set(expected["scope"][0].split()) == set(callback.SCOPES)
    assert saved["type"] == "authorized_user"
    assert saved["refresh_token"] == "fictional-refresh"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert finish(session, state=state, code="fictional-code")[0] == 410
    monkeypatch.syspath_prepend(str(Path(callback.__file__).parent))
    spec = importlib.util.spec_from_file_location("unit_bridge", Path(callback.__file__).with_name("gws_bridge.py"))
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    monkeypatch.setattr(bridge, "get_token_path", lambda: destination)
    assert bridge.get_valid_token() == "fictional-access"
    output = capsys.readouterr()
    assert not output.out + output.err


def test_pkce_mismatch_preserves_token(callback, tmp_path, inert_listener):
    destination = tmp_path / "google_token.json"
    destination.write_text("fictional-previous")
    expected = {}

    def fake_as(fields):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if challenge != expected["code_challenge"][0]:
            raise ValueError("PKCE mismatch")
        return grant(callback)

    session = callback.Session(client(), destination, scopes=callback.SCOPES, transport=fake_as)
    expected.update(begin(session))
    session.verifier = "fictional-wrong-verifier"
    finish(session, state=session.state, code="fictional-code")
    assert not session.success
    assert destination.read_text() == "fictional-previous"


@pytest.mark.parametrize("missing_index", range(8))
def test_each_required_scope_missing_preserves_old_file(callback, tmp_path, inert_listener, missing_index):
    destination = tmp_path / "google_token.json"
    destination.write_bytes(b"fictional-previous")
    previous_entries = set(tmp_path.iterdir())
    response = grant(callback)
    response["scope"] = " ".join(scope for index, scope in enumerate(callback.SCOPES) if index != missing_index)
    session = callback.Session(client(), destination, scopes=callback.SCOPES, transport=lambda fields: response)
    begin(session)
    # An untrusted callback cannot assert full permission on behalf of the AS.
    finish(session, state=session.state, code="fictional-code", scope=" ".join(callback.SCOPES))
    assert not session.success
    assert destination.read_bytes() == b"fictional-previous"
    assert set(tmp_path.iterdir()) == previous_entries


def test_omitted_token_scope_cannot_be_supplied_by_callback(callback, tmp_path, inert_listener):
    destination = tmp_path / "google_token.json"
    destination.write_bytes(b"fictional-previous")
    response = grant(callback)
    del response["scope"]
    session = callback.Session(client(), destination, scopes=callback.SCOPES, transport=lambda fields: response)
    begin(session)
    finish(session, state=session.state, code="fictional-code", scope=" ".join(callback.SCOPES))
    assert not session.success
    assert destination.read_bytes() == b"fictional-previous"


@pytest.mark.parametrize("stage", ["serialize", "fsync"])
def test_atomic_save_failure_preserves_old_file(callback, tmp_path, monkeypatch, stage):
    destination = tmp_path / "google_token.json"
    destination.write_bytes(b"fictional-previous")
    previous_entries = set(tmp_path.iterdir())
    payload = {"token": "fictional-access"}
    if stage == "serialize":
        payload["invalid"] = object()
    else:
        def fail_fsync(fd):
            raise OSError("fictional-failure")
        monkeypatch.setattr(callback.os, "fsync", fail_fsync)
    with pytest.raises((TypeError, OSError)):
        callback.atomic_save(destination, payload, expected_version=callback.file_version(destination))
    assert destination.read_bytes() == b"fictional-previous"
    assert set(tmp_path.iterdir()) == previous_entries


@pytest.mark.parametrize("stage", ["exchange", "fsync"])
@pytest.mark.parametrize("mutation", ["rewrite", "replace", "remove", "create"])
def test_competing_writer_preserved(callback, tmp_path, monkeypatch, inert_listener, stage, mutation):
    destination = tmp_path / "token"
    if mutation != "create":
        destination.write_text("fictional-old")

    def compete():
        if mutation == "remove":
            destination.unlink()
        elif mutation == "replace":
            other = tmp_path / "other"
            other.write_text("fictional-new")
            other.replace(destination)
        else:
            destination.write_text("fictional-new")

    def transport(fields):
        if stage == "exchange":
            compete()
        return grant(callback)

    original_fsync = callback.os.fsync
    def fsync(fd):
        original_fsync(fd)
        compete()
    if stage == "fsync":
        monkeypatch.setattr(callback.os, "fsync", fsync)
    session = callback.Session(client(), destination, scopes=callback.SCOPES, transport=transport)
    begin(session)
    finish(session, state=session.state, code="fictional-code")
    assert not session.success
    if mutation == "remove":
        assert not destination.exists()
    else:
        assert destination.read_text() == "fictional-new"
    assert not list(tmp_path.glob(".google-oauth-*"))


def test_unpredictable_single_use_start(callback, tmp_path, inert_listener, capsys):
    first = callback.Session(client(), tmp_path / "a", scopes=callback.SCOPES)
    second = callback.Session(client(), tmp_path / "b", scopes=callback.SCOPES)
    host = urlsplit(first.origin).netloc
    assert first.request("/start", host)[0] == 404
    assert first.start_path != second.start_path
    assert first.request(second.start_path, host)[0] == 404
    assert not first.started
    state, verifier = first.state, first.verifier
    begin(first)
    assert first.request(first.start_path, host)[0] == 404
    second.deadline = 0
    assert second.request(second.start_path, host)[0] == 410
    assert second.run() is False
    captured = capsys.readouterr()
    for value in (state, verifier, "fictional-secret", "fictional-code"):
        assert value not in captured.out + captured.err


def test_live_entry_fails_before_any_io(callback, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Must fail before credential or network access")

    monkeypatch.setattr(callback.Path, "read_text", forbidden)
    monkeypatch.setattr(callback, "Session", forbidden)
    assert callback.login("unused-client", "unused-token", 8765, callback.SCOPES) is False
    assert "disabled" in capsys.readouterr().out


@pytest.mark.platforms("posix")
def test_process_lock_contention_and_release(callback, tmp_path):
    import subprocess
    import sys

    destination = tmp_path / "token"
    destination.write_text("fictional-previous")
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from oauth_callback import writer_lock
with writer_lock(sys.argv[2]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    proc = subprocess.Popen([sys.executable, "-c", script, str(Path(callback.__file__).parent), str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        version = callback.file_version(destination)
        with pytest.raises(callback.CallbackError, match="busy"):
            callback.atomic_save(destination, {"token": "fictional-new"}, expected_version=version)
        assert destination.read_text() == "fictional-previous"
        proc.kill()  # OS must release exclusion even without Python cleanup.
        proc.wait(timeout=5)
        callback.atomic_save(destination, {"token": "fictional-new"}, expected_version=version)
        assert json.loads(destination.read_text())["token"] == "fictional-new"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdin.close()
        proc.stdout.close()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stage", ["before", "after"])
def test_process_death_at_commit_boundary(callback, tmp_path, stage):
    import subprocess
    import sys

    destination = tmp_path / "token"
    destination.write_text("fictional-previous")
    script = """
import os, sys
sys.path.insert(0, sys.argv[1])
import oauth_callback as cb
original = cb.os.replace
def terminate(src, dst):
    if sys.argv[3] == 'after':
        original(src, dst)
    os._exit(23)
cb.os.replace = terminate
cb.atomic_save(sys.argv[2], {'token': 'fictional-new'}, expected_version=cb.file_version(sys.argv[2]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(Path(callback.__file__).parent),
                             str(destination), stage], check=False, capture_output=True, timeout=5)
    assert result.returncode == 23
    assert not result.stdout + result.stderr
    if stage == "before":
        assert destination.read_text() == "fictional-previous"
        orphans = list(tmp_path.glob(".google-oauth-*"))
        assert len(orphans) == 1
        assert orphans[0].stat().st_mode & 0o777 == 0o600
        # Crash recovery: under exclusion, discard uncommitted staging files.
        with callback.writer_lock(tmp_path):
            orphans[0].unlink()
    else:
        assert json.loads(destination.read_text()) == {"token": "fictional-new"}
        assert not list(tmp_path.glob(".google-oauth-*"))
    callback.atomic_save(destination, {"token": "fictional-retry"},
                         expected_version=callback.file_version(destination))
    assert json.loads(destination.read_text()) == {"token": "fictional-retry"}


@pytest.mark.platforms("posix")
def test_interrupt_before_commit_preserves_file_and_releases_lock(callback, tmp_path, monkeypatch):
    destination = tmp_path / "token"
    destination.write_text("fictional-previous")
    original = callback.os.replace

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(callback.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        callback.atomic_save(destination, {"token": "fictional-new"},
                             expected_version=callback.file_version(destination))
    assert destination.read_text() == "fictional-previous"
    assert not list(tmp_path.glob(".google-oauth-*"))
    monkeypatch.setattr(callback.os, "replace", original)
    callback.atomic_save(destination, {"token": "fictional-retry"},
                         expected_version=callback.file_version(destination))
