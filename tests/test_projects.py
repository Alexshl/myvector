from conftest import git, write

from myvec import projects


def test_marker_and_paths(ws):
    m = projects.find_marker(ws / "site" / "src")
    assert m.path == "acme" and m.root == ws.resolve()
    assert projects.find_marker(ws.parent) is None


def test_discover_repos(ws):
    m = projects.find_marker(ws)
    repos, children = projects.discover(m)
    got = {r.path: r for r in repos}
    assert set(got) == {"acme", "acme/site", "acme/libs"}
    assert got["acme"].is_git is False and got["acme/site"].is_git is True
    assert not children


def test_nested_marker_inherits_parent(ws):
    child = ws / "child"
    child.mkdir()
    write(child / ".myvector.toml", 'name = "child"\n')
    assert projects.find_marker(child).path == "acme/child"
    repos, children = projects.discover(projects.find_marker(ws))
    assert [c.path for c in children] == ["acme/child"]
    assert "acme/child" not in {r.path for r in repos}


def test_explicit_parent_and_depth_limit(tmp_path):
    root = tmp_path / "p"
    deep = root / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    ok = root / "a" / "b" / "c" / "d"
    git(deep, "init", "-q")
    git(ok, "init", "-q")
    write(root / ".myvector.toml", 'name = "inner"\nparent = "outer"\n')
    m = projects.find_marker(root)
    assert m.path == "outer/inner"
    repos, _ = projects.discover(m)
    assert "outer/inner/a/b/c/d" in {r.path for r in repos}
    assert "outer/inner/a/b/c/d/e" not in {r.path for r in repos}
    assert projects.ancestors("a/b/c") == ["a/b/c", "a/b", "a"]
