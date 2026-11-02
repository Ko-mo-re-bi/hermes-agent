"""Fictional-only real writer competition; no network or credential discovery."""
import importlib.util
import json
import multiprocessing
import os
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/productivity/google-workspace/scripts"


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("isolated_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fictional():
    return dict(token="fictional-old", refresh_token="fictional-refresh",
                client_id="fictional-client", client_secret="fictional-secret",
                token_uri="https://fictional.invalid/token", expiry="2000-01-01T00:00:00+00:00")


def writer(route, path, ready, release, results, entered=None):
    """Replace transports only; run the actual credential entry points."""
    os.environ["HERMES_HOME"] = str(Path(path).parent)
    module = load(route)
    if entered is not None:
        from contextlib import contextmanager
        store = sys.modules["credential_store"]
        lock = store.writer_lock

        @contextmanager
        def observed_lock(*args, **kwargs):
            entered.set()
            with lock(*args, **kwargs):
                yield

        store.writer_lock = observed_lock

    class Credentials:
        def __init__(self, data):
            self.data = dict(data)
            self.valid = data["token"] != "fictional-old"
            self.expired = not self.valid
            self.refresh_token = data["refresh_token"]

        @classmethod
        def from_authorized_user_info(cls, data, *args):
            return cls(data)

        @classmethod
        def from_authorized_user_file(cls, filename, *args):
            return cls(json.loads(Path(filename).read_text()))

        def refresh(self, request):
            ready.set()
            assert release.wait(20)
            self.data["expiry"] = "2099-01-01T00:00:00+00:00"
            self.data["token"] = "fictional-" + route
            self.valid = True

        def to_json(self):
            return json.dumps(self.data)

    for name in ("google", "google.oauth2", "google.oauth2.credentials", "google.auth",
                 "google.auth.transport", "google.auth.transport.requests"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["google.oauth2.credentials"].Credentials = Credentials
    sys.modules["google.auth.transport.requests"].Request = object
    module.TOKEN_PATH = Path(path)
    module._ensure_authenticated = lambda: None
    module._ensure_deps = lambda: None
    if route == "gws_bridge":
        import urllib.request

        class Response:
            def __enter__(self):
                ready.set()
                assert release.wait(20)
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps(dict(access_token="fictional-gws_bridge", expires_in=3600)).encode()

        urllib.request.urlopen = lambda *args, **kwargs: Response()
        result = module.get_valid_token()
    elif route == "setup":
        result = module.check_auth(quiet=True)
    else:
        result = module.get_credentials().data["token"]
    results.put((route, result))


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("leader", ["google_api", "setup", "gws_bridge"])
def test_three_real_refresh_paths_discard_stale_updates(tmp_path, leader):
    path = tmp_path / "google_token.json"
    path.write_text(json.dumps(fictional()))
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    routes = (leader,) + tuple(r for r in ("google_api", "setup", "gws_bridge") if r != leader)
    ready = [context.Event() for _ in routes]
    release = [context.Event() for _ in routes]
    entered = [context.Event() for _ in routes]
    processes = [context.Process(target=writer, args=(route, str(path), ready[i], release[i], results, entered[i]))
                 for i, route in enumerate(routes)]
    try:
        processes[0].start()
        assert ready[0].wait(20)
        import fcntl
        fd = os.open(tmp_path / ".google-token.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        for process in processes[1:]:
            process.start()
        assert all(event.wait(20) for event in entered)
        release[0].set()
        processes[0].join(20)
        assert processes[0].exitcode == 0
        winner = path.read_bytes()
        winner_stat = path.stat()
        for i in (1, 2):
            release[i].set()
            processes[i].join(20)
            assert processes[i].exitcode == 0
        assert path.read_bytes() == winner
        assert path.stat().st_ino == winner_stat.st_ino  # stale writes did not even replace
        assert path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / ".google-token.lock").stat().st_mode & 0o777 == 0o600
        assert not path.with_name(path.name + ".backup").exists()
        actual = dict(results.get(timeout=5) for _ in routes)
        assert actual == dict(google_api="fictional-" + leader, setup=True, gws_bridge="fictional-" + leader)
        assert not ready[1].is_set() and not ready[2].is_set()
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            if process.pid is not None:
                process.join(5)


@pytest.mark.platforms("posix")
def test_callback_backup_and_restore_existing_readers(tmp_path, monkeypatch):
    callback = load("oauth_callback")
    setup = load("setup")
    store = load("credential_store")
    path = tmp_path / "google_token.json"
    original = fictional()
    original["expiry"] = "2099-01-01T00:00:00+00:00"
    path.write_text(json.dumps(original))
    from unittest.mock import Mock
    listener = Mock()
    listener.server_address = ("127.0.0.1", 8765)
    monkeypatch.setattr(callback, "HTTPServer", Mock(return_value=listener))
    session = callback.Session(dict(client_id="fictional", client_secret="fictional"), path,
                               scopes=setup.SCOPES,
                               transport=lambda fields: dict(access_token="fictional-new",
                                   refresh_token="fictional-new-refresh", expires_in=3600,
                                   token_type="Bearer", scope=" ".join(setup.SCOPES)))
    # Real callback completion, with its transport replaced before any network I/O.
    session._complete({"code": ["fictional-code"]})
    backup = path.with_name(path.name + ".backup")
    assert json.loads(backup.read_text()) == original
    assert backup.stat().st_mode & 0o777 == 0o600
    store.restore_backup(path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load("gws_bridge").get_valid_token() == original["token"]
    assert path.stat().st_mode & 0o777 == 0o600


def interrupted_writer(route, path, stage, boundary):
    store = load("credential_store")
    original = store.os.replace
    context = multiprocessing.get_context("fork")
    release = context.Event()
    release.set()
    forever = context.Event()

    def replace(source, destination):
        if stage == "after":
            original(source, destination)
        boundary.set()
        forever.wait(30)
        if stage == "before":
            original(source, destination)

    store.os.replace = replace
    writer(route, path, context.Event(), release, context.Queue())


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("route", ["google_api", "setup", "gws_bridge"])
@pytest.mark.parametrize("stage", ["before", "after"])
def test_real_writers_sigkill_commit_boundary(tmp_path, route, stage):
    path = tmp_path / "google_token.json"
    path.write_text(json.dumps(fictional()))
    old = path.read_bytes()
    context = multiprocessing.get_context("fork")
    boundary = context.Event()
    process = context.Process(target=interrupted_writer, args=(route, str(path), stage, boundary))
    process.start()
    try:
        assert boundary.wait(20)
        process.kill()
        process.join(10)
        assert process.exitcode == -9
        if stage == "before":
            assert path.read_bytes() == old
        else:
            assert json.loads(path.read_text())["token"] == "fictional-" + route
            assert path.stat().st_mode & 0o777 == 0o600
        store = load("credential_store")
        with store.writer_lock(tmp_path):
            for orphan in tmp_path.glob(".google-oauth-*"):
                assert orphan.stat().st_mode & 0o777 == 0o600
                orphan.unlink()
        store.credential_transaction(path, lambda current: dict(current, token="fictional-recovered"))
        assert json.loads(path.read_text())["token"] == "fictional-recovered"
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)


@pytest.mark.platforms("posix")
def test_commit_syncs_file_before_replace_and_directory_after(tmp_path, monkeypatch):
    import stat
    store = load("credential_store")
    path = tmp_path / "google_token.json"
    events = []
    fsync, replace = store.os.fsync, store.os.replace

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        fsync(fd)

    def commit(source, destination):
        events.append("replace")
        replace(source, destination)

    monkeypatch.setattr(store.os, "fsync", sync)
    monkeypatch.setattr(store.os, "replace", commit)
    store.atomic_save(path, fictional(), expected_version=None)
    assert events == ["file", "replace", "directory"]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("route", ["google_api", "setup", "gws_bridge"])
def test_refresh_discards_result_if_nonparticipant_replaces_token(tmp_path, route):
    """Even an uncooperative replacement must win over an in-flight result."""
    path = tmp_path / "google_token.json"
    path.write_text(json.dumps(fictional()))
    context = multiprocessing.get_context("fork")
    ready, release, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=writer, args=(route, str(path), ready, release, results))
    process.start()
    try:
        assert ready.wait(20)
        winner = dict(fictional(), token="fictional-external", expiry="2099-01-01T00:00:00+00:00")
        staging = tmp_path / "external-staging"
        staging.write_text(json.dumps(winner))
        staging.chmod(0o600)
        os.replace(staging, path)  # Deliberately ignores the cooperative lock.
        inode = path.stat().st_ino
        release.set()
        process.join(20)
        assert process.exitcode == 0
        assert json.loads(path.read_text()) == winner
        assert path.stat().st_ino == inode
        assert results.get(timeout=5) == (route, True if route == "setup" else winner["token"])
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
