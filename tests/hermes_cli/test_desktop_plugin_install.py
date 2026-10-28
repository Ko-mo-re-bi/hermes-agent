"""``hermes_cli.desktop_plugin_install``: cloning a desktop plugin onto the host that serves the Webapp.

Real local Git repositories, a throwaway ``HERMES_HOME``: the folder lands where the Webapp's read
routes serve it, a package's half carries the marker that pairs it to its agent row, a standalone
plugin carries none, and nothing installs twice without ``force``.
"""

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import desktop_plugin_install as install


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "c")
    return path


@pytest.fixture()
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None
    return hermes_home


def _root(home: Path) -> Path:
    return home / "desktop-plugins"


def test_desktop_only_repo_lands_under_its_own_name_without_a_marker(tmp_path, home):
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default { id: 'gizmo' }\n",
                                      "panel.js": "export const x = 1\n"})

    result = install.install_desktop_plugin(repo.as_uri())

    assert result["ok"] is True, result
    assert result["plugin_name"] == "gizmo"
    assert result["paired_with_agent"] is False
    folder = _root(home) / "gizmo"
    assert (folder / "plugin.js").read_text(encoding="utf-8").startswith("export default")
    assert (folder / "panel.js").is_file()
    # A standalone plugin carries no package marker: one would claim another package's agent half.
    assert not (folder / ".hermes-package.json").exists()


def test_package_lands_its_desktop_half_under_the_agent_name_with_a_marker(tmp_path, home):
    repo = _repo(tmp_path / "pkg", {"plugin.yaml": "name: pkg\nversion: 1.0.0\n", "__init__.py": "",
                                    "desktop/plugin.js": "export default { id: 'pkg' }\n"})

    result = install.install_desktop_plugin(repo.as_uri())

    assert result["ok"] is True, result
    assert (result["plugin_name"], result["paired_with_agent"]) == ("pkg", True)
    folder = _root(home) / "pkg"
    assert (folder / "plugin.js").is_file()
    # Only the desktop half is copied: the agent payload stays in the repository.
    assert not (folder / "plugin.yaml").exists()
    marker = json.loads((folder / ".hermes-package.json").read_text(encoding="utf-8"))
    assert marker["package"] == "pkg"
    assert marker["repo"] == repo.as_uri()
    assert marker["source"] == str(folder)
    assert marker["sourceMtimeMs"] > 0


def test_a_second_install_refuses_until_force_replaces_it(tmp_path, home):
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default { v: 1 }\n"})
    assert install.install_desktop_plugin(repo.as_uri())["ok"] is True

    again = install.install_desktop_plugin(repo.as_uri())
    assert again["ok"] is False
    assert "already exists" in again["error"] and "force" in again["error"]

    (repo / "plugin.js").write_text("export default { v: 2 }\n", encoding="utf-8")
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", "v2")
    forced = install.install_desktop_plugin(repo.as_uri(), force=True)
    assert forced["ok"] is True, forced
    assert (repo / "plugin.js").read_text(encoding="utf-8") in \
           (_root(home) / "gizmo" / "plugin.js").read_text(encoding="utf-8")


def test_the_install_is_held_to_the_commit_the_inspection_read(tmp_path, home, monkeypatch):
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default { v: 1 }\n"})
    reviewed = _git(repo, "rev-parse", "HEAD")
    (repo / "plugin.js").write_text("export default { v: 2 }\n", encoding="utf-8")
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", "moved on")

    from hermes_cli import plugins_cmd_inspect
    monkeypatch.setattr(plugins_cmd_inspect, "inspect_plugin_source", lambda *a, **k: {
        "ok": True, "agent": False, "desktop": True, "agent_name": None, "desktop_name": "gizmo",
        "sha": reviewed, "warnings": [], "insecure": True})

    result = install.install_desktop_plugin(repo.as_uri())

    assert result["ok"] is True, result
    assert result["sha"] == reviewed
    assert "v: 1" in (_root(home) / "gizmo" / "plugin.js").read_text(encoding="utf-8")


def test_a_repository_with_no_desktop_half_is_refused(tmp_path, home):
    repo = _repo(tmp_path / "agently", {"plugin.yaml": "name: agently\n", "__init__.py": ""})

    result = install.install_desktop_plugin(repo.as_uri())

    assert result["ok"] is False
    assert "no desktop plugin" in result["error"]
    assert not (_root(home) / "agently").exists()


def test_a_name_that_is_a_path_is_refused(tmp_path, home, monkeypatch):
    from hermes_cli import plugins_cmd_inspect
    monkeypatch.setattr(plugins_cmd_inspect, "inspect_plugin_source", lambda *a, **k: {
        "ok": True, "agent": False, "desktop": True, "agent_name": None, "desktop_name": "../escape",
        "sha": None, "warnings": [], "insecure": False})

    result = install.install_desktop_plugin("https://github.com/x/y.git")

    assert result["ok"] is False
    assert "folder name" in result["error"]
    assert not (home / "escape").exists()


def test_remove_takes_a_standalone_plugin_away(tmp_path, home):
    repo = _repo(tmp_path / "gizmo", {"plugin.js": "export default {}\n"})
    assert install.install_desktop_plugin(repo.as_uri())["ok"] is True

    result = install.remove_desktop_plugin("gizmo")

    assert result == {"ok": True, "plugin_name": "gizmo", "path": str(_root(home) / "gizmo"),
                      "unpaired_from": None}
    assert not (_root(home) / "gizmo").exists()


def test_remove_refuses_a_packages_own_half_until_forced(tmp_path, home):
    repo = _repo(tmp_path / "pkg", {"plugin.yaml": "name: pkg\n", "__init__.py": "",
                                    "desktop/plugin.js": "export default {}\n"})
    assert install.install_desktop_plugin(repo.as_uri())["ok"] is True

    refused = install.remove_desktop_plugin("pkg")
    assert refused["ok"] is False
    assert "uninstall that plugin instead" in refused["error"]
    assert (_root(home) / "pkg" / "plugin.js").is_file()

    forced = install.remove_desktop_plugin("pkg", force=True)
    assert forced["ok"] is True
    assert forced["unpaired_from"] == "pkg"
    assert not (_root(home) / "pkg").exists()


def test_remove_leaves_a_linked_plugin_alone(tmp_path, home):
    checkout = _repo(tmp_path / "dev", {"plugin.js": "export default {}\n"})
    root = _root(home)
    root.mkdir(parents=True)
    (root / "dev").symlink_to(checkout, target_is_directory=True)

    result = install.remove_desktop_plugin("dev")

    assert result["ok"] is False
    assert "linked" in result["error"]
    assert (root / "dev" / "plugin.js").is_file()


def test_remove_of_something_not_installed_says_so(tmp_path, home):
    result = install.remove_desktop_plugin("ghost")

    assert result["ok"] is False
    assert "No desktop plugin named 'ghost'" in result["error"]
