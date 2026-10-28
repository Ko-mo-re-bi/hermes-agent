"""Gateway ``plugins.manage install_desktop`` / ``remove_desktop``: the browser-hosted app's install.

Drives the real RPC dispatch and a real local clone: a desktop plugin lands in the host's
``desktop-plugins/`` (where the Webapp's read routes serve it), a second install is refused until
``force``, and removal round-trips — all through the same ``plugins.manage`` action the dialog calls.
"""

import subprocess

from tui_gateway import server


def _manage(**params):
    return server.handle_request({"id": "1", "method": "plugins.manage", "params": params})


def _repo(path, files):
    path.mkdir()
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "c")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def test_install_desktop_lands_the_folder_the_webapp_serves(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default { id: 'gizmo' }\n"})

    result = _manage(action="install_desktop", identifier=repo.as_uri())["result"]

    assert result["ok"] is True
    assert result["plugin_name"] == "gizmo"
    assert result["paired_with_agent"] is False
    folder = home / "desktop-plugins" / "gizmo"
    assert (folder / "plugin.js").is_file()
    assert not (folder / ".hermes-package.json").exists()


def test_a_package_installs_its_desktop_half_under_the_agent_name(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "pkg", {"plugin.yaml": "name: pkg\n", "__init__.py": "",
                                    "desktop/plugin.js": "export default {}\n"})

    result = _manage(action="install_desktop", identifier=repo.as_uri())["result"]

    assert (result["plugin_name"], result["paired_with_agent"]) == ("pkg", True)
    assert (home / "desktop-plugins" / "pkg" / ".hermes-package.json").is_file()


def test_a_second_install_is_an_rpc_error_until_force(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default {}\n"})
    assert _manage(action="install_desktop", identifier=repo.as_uri())["result"]["ok"] is True

    refused = _manage(action="install_desktop", identifier=repo.as_uri())

    assert "error" in refused and refused["error"]["code"] == 5026
    assert "already exists" in refused["error"]["message"]
    assert _manage(action="install_desktop", identifier=repo.as_uri(), force=True)["result"]["ok"] is True


def test_remove_desktop_round_trips(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default {}\n"})
    _manage(action="install_desktop", identifier=repo.as_uri())

    result = _manage(action="remove_desktop", name="gizmo")["result"]

    assert result["ok"] is True
    assert not (home / "desktop-plugins" / "gizmo").exists()


def test_install_desktop_requires_a_source():
    response = _manage(action="install_desktop")

    assert response["error"]["code"] == 4019


def test_a_failed_clone_is_an_rpc_error_with_reason(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    response = _manage(action="install_desktop", identifier=(tmp_path / "missing").as_uri())

    assert response["error"]["code"] == 5026
    assert response["error"]["message"]
