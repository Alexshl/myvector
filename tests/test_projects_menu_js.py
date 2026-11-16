"""E2-07: rail «Проєкти» -> #/projects, flyout with all projects, shared table (views.js runs under node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

VIEWS = Path(__file__).parent.parent / "src" / "myvec" / "ui" / "views.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STATE = """
globalThis.location = {host: 'x', hash: ''};
const proj = (path, extra = {}) => ({path, root: '/r/' + path, repos: [], children: [], active: false, mode: 'auto', syncing: false, exclude: [], ...extra});
const S = (route) => ({route, theme: 'light', statusErr: null, collapsed: new Set(), events: [], live: {state: 'live'},
  tk: {sessions: {}}, mon: {summary: {}}, spend: {project: {}},
  status: {now: 1, global: {ollama: {reachable: true}, daemon: {}, memories: {total: 0, by_scope: {}}, searches: {}, model: 'm', dims: 8, db_size_bytes: 1},
           projects: [proj('<b>a</b>', {children: [proj('a/sub')]}), proj('zeta')]}});
"""


def render(expr: str) -> str:
    code = (f"import * as v from {json.dumps(VIEWS.as_uri())}; {STATE} const out = String({expr}); console.log(JSON.stringify(out));")
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_rail_projects_links_to_page_and_is_on_for_project_page():
    page = render("v.view(S({name: 'projects'}))")
    assert 'href="#/projects"' in page and 'aria-label="Проєкти"' in page
    on = render("v.rail(S({name: 'project', path: 'zeta'}))")
    assert '<div class="rit on"><a class="rail-btn" href="#/projects"' in on and "aria-current" not in on.split("rfly")[0]
    assert '<a href="#/projects">Проєкти</a>' in render("v.topbar(S({name: 'project', path: 'zeta'}))")


def test_flyout_lists_all_projects_escaped_with_current_marked():
    fly = render("v.projectsFly(S({name: 'project', path: 'zeta'}))")
    assert 'class="rfly"' in fly and 'rfly-t' in fly
    for p in ("a/sub", "zeta"):
        assert f"{p}</span>" in fly
    assert "<b>a</b>" not in fly and "&lt;b&gt;a&lt;/b&gt;" in fly
    assert fly.count('aria-current="page"') == 1 and 'class="on" aria-current="page"' in fly


def test_projects_route_renders_shared_table():
    page = render("v.view(S({name: 'projects'}))")
    assert 'id="mv-projects"' not in page and "thead" in page and "zeta" in page  # h1 already says «Проєкти»
    assert 'data-tip="Проєкти"' not in page and 'data-tip="Огляд"' in page
    assert "Активні проєкти" not in page  # not the whole overview
    assert 'id="mv-projects"' in render("v.view(S({name: 'overview'}))")
