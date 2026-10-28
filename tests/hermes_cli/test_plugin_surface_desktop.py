"""A re-pin that adds a desktop half is a widened surface whichever layout the half uses.

The installers accept ``plugin.js`` at the package root as well as under ``desktop/``; the re-pin
consent check reads the same rule, so a root-layout half added by a new commit is not waved through.
"""

from pathlib import Path

import pytest

from hermes_cli.plugins_cmd_catalog import plugin_surface, surface_delta


@pytest.mark.parametrize("entry", ["plugin.js", "desktop/plugin.js"])
def test_new_desktop_half_widens_the_surface_in_either_layout(tmp_path: Path, entry: str):
    old, new = tmp_path / "old", tmp_path / "new"
    for tree in (old, new):
        tree.mkdir()
        (tree / "plugin.yaml").write_text("name: demo\n")
    (new / entry).parent.mkdir(parents=True, exist_ok=True)
    (new / entry).write_text("export default {}\n")

    delta = surface_delta(plugin_surface({"name": "demo"}, old), plugin_surface({"name": "demo"}, new))

    assert delta.get("desktop") == [entry]
