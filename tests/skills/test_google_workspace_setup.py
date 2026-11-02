"""Google Workspace setup delegates dependency ownership to PM."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pm
import pytest


SETUP_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)


@pytest.fixture()
def setup_module(monkeypatch):
    # setup.py exposes sibling imports for direct script execution.
    monkeypatch.syspath_prepend(str(SETUP_PATH.parent))
    spec = importlib.util.spec_from_file_location("google_workspace_setup", SETUP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("error", [None, pm.InstallError("venv", "sync refused")])
def test_explicit_install_uses_pm_and_reports_restart(setup_module, monkeypatch, capsys, error):
    sync = Mock(side_effect=error)
    monkeypatch.setattr(pm, "sync_venv", sync)
    # Even a successful old-interpreter probe must not bypass explicit sync.
    monkeypatch.setattr(pm, "ensure_import", Mock(side_effect=AssertionError("not a sync")))
    monkeypatch.setattr("subprocess.check_call", Mock(side_effect=AssertionError("ambient install")))

    assert setup_module.install_deps() is (error is None)
    sync.assert_called_once_with(["google"], explicit=True)
    output = capsys.readouterr().out
    if error is None:
        assert "restart" in output.lower()
    else:
        assert "sync refused" in output


def test_auth_uses_pm_import_check(setup_module, monkeypatch):
    ensure = Mock()
    monkeypatch.setattr(pm, "ensure_import", ensure)
    monkeypatch.setattr(pm, "sync_venv", Mock(side_effect=AssertionError("explicit sync during auth")))
    monkeypatch.setattr("subprocess.check_call", Mock(side_effect=AssertionError("ambient install")))

    setup_module._ensure_deps()

    ensure.assert_called_once_with("google")


@pytest.mark.parametrize("failure", [None, OSError("fictional revocation failure")])
def test_revoke_deletes_backup_only_after_remote_success(setup_module, monkeypatch, tmp_path, failure):
    import sys
    import types
    import urllib.request

    token = tmp_path / "google_token.json"
    backup = tmp_path / "google_token.json.backup"
    pending = tmp_path / "google_oauth_pending.json"
    for path in (token, backup, pending):
        path.write_text('{"token": "fictional-token"}')
    monkeypatch.setattr(setup_module, "TOKEN_PATH", token)
    monkeypatch.setattr(setup_module, "PENDING_AUTH_PATH", pending)
    monkeypatch.setattr(setup_module, "_ensure_deps", Mock())
    credentials = types.ModuleType("google.oauth2.credentials")
    credentials.Credentials = Mock()
    credentials.Credentials.from_authorized_user_file.return_value = Mock(
        expired=False, token="fictional-token")
    transport = types.ModuleType("google.auth.transport.requests")
    transport.Request = Mock()
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", transport)
    remote = Mock(side_effect=failure)
    monkeypatch.setattr(urllib.request, "urlopen", remote)

    setup_module.revoke()

    remote.assert_called_once()
    for path in (token, backup, pending):
        if failure is None:
            assert not path.exists()
        else:
            assert path.read_text() == '{"token": "fictional-token"}'
