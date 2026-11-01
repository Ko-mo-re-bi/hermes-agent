"""Regression coverage for Git-for-Windows installed outside Program Files.

The live Windows test checks real git.exe execution; these tests pin the safe
PATH installation-root discovery independently of the runner's installation.
"""
from hermes_cli import update_cmd_git as git


def test_git_core_discovery_from_toolcache_path_shim(tmp_path, monkeypatch):
    install = tmp_path / "setup-pm" / "tools" / "git-2.53.0"
    shim = install / "cmd" / "git.exe"
    core = install / "mingw64" / "libexec" / "git-core" / "git.exe"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"shim")
    core.parent.mkdir(parents=True)
    core.write_bytes(b"real-git-core")

    monkeypatch.setattr("shutil.which", lambda command: str(shim) if command == "git" else None)
    monkeypatch.setattr(git, "_portable_git_candidates", list)
    monkeypatch.setattr(git, "_probe_fork_bomb", lambda argv: False if argv == [str(core)] else None)
    assert git._locate_real_git() == core

    # An unprobeable or broken binary must never be substituted for the shim.
    monkeypatch.setattr(git, "_probe_fork_bomb", lambda _argv: None)
    assert git._locate_real_git() is None
