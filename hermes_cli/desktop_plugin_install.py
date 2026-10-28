"""Install a desktop plugin from Git onto the host that serves the Webapp.

Native Desktop clones one in Electron (``apps/desktop/electron/desktop-plugin-install.ts``,
``installDesktopPluginFromGit``). A browser-hosted tab has no disk of its own, so until now the install
dialog could only *read* a repository through the gateway (``plugins_cmd_inspect``) and then had
nothing to install with: a desktop-only package answered "nothing to install here", and the desktop
half of a mixed one was reported skipped. This is the missing half of the trip — the same inspection,
held to the commit it read, the same folder name and package-marker rules as Electron, published
atomically into the app-level root that ``/api/desktop-plugins`` serves.

The install is a clone, a staged copy and a rename: no code is imported or run here. The plugin loads
the way any other installed plugin does, on the next scan of ``desktop-plugins/``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from hermes_cli import desktop_plugin_halves as halves
from hermes_constants import get_default_hermes_root

DESKTOP_PLUGINS_DIR = "desktop-plugins"


def desktop_plugins_root() -> Path:
    """The app-level root ``desktop-plugins/``, created on demand and resolved, exactly as Electron's
    ``desktopPluginsRoot`` and the Webapp's read routes resolve it: one folder whichever profile the
    caller names."""
    root = get_default_hermes_root() / DESKTOP_PLUGINS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _pc():
    """The facade, read at call time: tests patch ``plugins_cmd.<name>`` and sibling calls must see it."""
    from hermes_cli import plugins_cmd
    return plugins_cmd


def _failure(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


def _checked_name(name: Optional[str]) -> str:
    """*name* when it can be one folder under the root, else raise: the name comes from a repository
    (a subdirectory's last segment, a manifest's ``name``) and must never reach a path unresolved. A
    leading dot is refused too — the root's staging folders use that prefix."""
    candidate = (name or "").strip()
    if (not candidate or candidate in (".", "..") or candidate.startswith(".")
            or "/" in candidate or "\\" in candidate or "\0" in candidate
            or any(ord(char) < 32 for char in candidate) or len(candidate) > 128):
        raise _pc().PluginOperationError(
            f"Cannot install a desktop plugin under the folder name '{name}': "
            "it must be a plain folder name, not a path."
        )
    return candidate


def _source(identifier: str, catalog_name: Optional[str]) -> tuple[str, Optional[str], str, Optional[str]]:
    """``(git_url, subdir, resolved_identifier, revision)`` — the installer's own resolution, with the
    catalog's reviewed pin when *catalog_name* is given (its repository replaces *identifier*)."""
    pc = _pc()
    resolved = identifier
    revision = None
    if catalog_name:
        from hermes_cli import plugins_cmd_catalog as catalog
        entry = catalog.get_live_catalog_entry(catalog_name)
        if entry is None:
            raise pc.PluginOperationError(f"'{catalog_name}' is not in the Hermes plugin catalog.")
        resolved = entry.install_identifier
        revision = entry.sha or None
    git_url, subdir = pc._resolve_git_url(resolved)
    from hermes_cli.plugins_cmd_catalog import raise_if_removed
    raise_if_removed(resolved, git_url, *((catalog_name,) if catalog_name else ()))
    return git_url, subdir, resolved, revision


def install_desktop_plugin(identifier: str, *, catalog_name: Optional[str] = None,
                           force: bool = False) -> dict[str, Any]:
    """Clone a repository's desktop half into ``desktop-plugins/<name>`` on this host.

    Returns ``{ok, plugin_name, path, sha, paired_with_agent, warnings}``, or ``ok: False`` with
    ``error``. The clone is held to the commit the inspection read, so a repository that moves after
    review cannot install a tree nobody looked at. A package carrying both halves lands under its
    *agent* name with the package marker Electron writes, so the Plugins page pairs the two rows; a
    desktop-only repository keeps its Git-derived folder name and carries no marker (it would claim
    another package's agent half).
    """
    from hermes_cli.plugins_cmd_inspect import forget_inspection, inspect_plugin_source

    if force:
        # A forced reinstall means "the repository moved": answer from a fresh clone, not the check
        # that named the tree now being replaced.
        forget_inspection(identifier, catalog_name=catalog_name)
    probe = inspect_plugin_source(identifier, catalog_name=catalog_name)
    if not probe.get("ok"):
        return _failure(probe.get("error") or "Could not read this plugin repository.")
    if not probe.get("desktop"):
        return _failure("This package ships no desktop plugin to install here.")

    agent_name = probe.get("agent_name") if probe.get("agent") else None
    try:
        package_name = _checked_name(agent_name) if agent_name else None
        folder = package_name or _checked_name(probe.get("desktop_name"))
        git_url, subdir, _resolved, revision = _source(identifier, catalog_name)
    except _pc().PluginOperationError as exc:
        return _failure(str(exc))

    root = desktop_plugins_root()
    target = root / folder
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-plugin-install-") as tmp:
            clone = Path(tmp) / "plugin"
            # The commit the inspection named, when it named one: the install must be the tree that
            # was reviewed, not whatever the branch moved to since.
            sha = _pc()._clone_plugin_repo(clone, git_url, probe.get("sha") or revision, subdir)
            plugin_root = _pc()._resolve_subdir_within(clone, subdir) if subdir else clone
            source_dir = _desktop_tree(plugin_root)

            if source_dir is None:
                return _failure("No desktop plugin.js found in this repository.")

            # A package's half keeps the marker Electron writes (the Plugins page pairs it to the
            # agent row); a standalone plugin carries none.
            marker = None
            if package_name:
                marker = {
                    "package": package_name,
                    "repo": git_url,
                    "source": str(target),
                    "sourceMtimeMs": halves.mtime_ms((source_dir / "plugin.js").stat()),
                }

            # Under the root's writer lock: the exists check and the publish must not interleave with
            # a reconcile pass copying the same package's folder.
            with halves.ROOT_WRITER:
                if target.is_symlink() or target.exists():
                    if not force:
                        return _failure(
                            f"Desktop plugin '{folder}' already exists. Enable force reinstall to replace it."
                        )
                halves.publish_desktop_tree(source_dir, target, marker)
    except _pc().PluginOperationError as exc:
        return _failure(str(exc))
    except OSError as exc:
        return _failure(f"Could not install the desktop plugin: {exc}")

    return {"ok": True, "plugin_name": folder, "path": str(target), "sha": sha,
            "paired_with_agent": bool(package_name), "warnings": list(probe.get("warnings") or [])}


def _desktop_tree(plugin_root: Path) -> Optional[Path]:
    """The folder that is the desktop plugin: the repository root when ``plugin.js`` sits there, else
    its ``desktop/`` subfolder — Electron's ``findDesktopEntry`` search, narrowed to those two."""
    if (plugin_root / "plugin.js").is_file():
        return plugin_root
    if (plugin_root / "desktop" / "plugin.js").is_file():
        return plugin_root / "desktop"
    return None


def remove_desktop_plugin(name: str, *, force: bool = False) -> dict[str, Any]:
    """Delete one installed desktop plugin folder from this host.

    A folder carrying the package marker is the desktop half of an installed agent plugin: removing it
    alone would leave the Plugins page pairing a row with nothing behind it, and the next reconcile
    would copy it straight back, so it is refused unless *force* says the caller means it.
    """
    try:
        folder = _checked_name(name)
    except _pc().PluginOperationError as exc:
        return _failure(str(exc))
    target = desktop_plugins_root() / folder
    if target.is_symlink():
        return _failure(
            f"'{folder}' is linked from somewhere else on this host; remove that link where it lives."
        )
    if not target.is_dir():
        return _failure(f"No desktop plugin named '{folder}' is installed.")

    with halves.ROOT_WRITER:
        marker = halves.read_package_marker(target)
        if marker and not force:
            return _failure(
                f"'{folder}' is the desktop half of an agent plugin — uninstall that plugin instead."
            )
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return _failure(f"Could not remove the desktop plugin: {exc}")

    return {"ok": True, "plugin_name": folder, "path": str(target), "unpaired_from": (marker or {}).get("package")}
