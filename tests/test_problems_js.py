"""problems.js is pure (data -> html) and uk-UA; run under node when it is installed (E3-04)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).parent.parent / "src" / "myvec" / "ui"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    code = (f"import * as p from {json.dumps((UI / 'problems.js').as_uri())}; "
            f"import * as t from {json.dumps((UI / 'tracker.js').as_uri())}; "
            f"const out = (() => {{ {body} }})(); console.log(JSON.stringify(out));")
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


DATA = """const ids = Array.from({length: 13}, (_, i) => 'E1-' + String(i + 1).padStart(2, '0'));
const hrefs = {ticket: (pa, id) => '#t/' + pa + '/' + id};
const g = (o) => Object.assign({severity:'warning', title:'T', explain:'E', fix:'F', cmd:null, legacy:false, n:1, tickets:[], project_items:[], code:'c'}, o);
const data = {counts:{error:1,warning:122,health:14}, migration_branch:'myvec/process-migration', merge_cmd:'git -C /w/p merge myvec/process-migration', groups:[
  g({severity:'error', code:'dup_id', title:'<b>Дубль</b>', cmd:'x', n:1, tickets:['E1-01']}),
  g({code:'legacy_epic', title:'Старий формат тікетів (epic:)', cmd:'myvec ticket migrate --fix --write', legacy:true, n:122, tickets:ids}),
  g({severity:'health', code:'too_big', title:'Завеликі', n:14, tickets:ids.slice(0, 2)})]};
const ui = {path:'acme', hrefs};
"""


def test_module_is_pure():
    src = (UI / "problems.js").read_text(encoding="utf-8")
    for bad in ("fetch(", "document.", "window.", "location", "localStorage"):
        assert bad not in src, bad
    assert re.findall(r"from '([^']+)'", src) == ["./util.js"]


@needs_node
def test_groups_order_copy_and_expand():
    h = run(DATA + "return p.problemsTab(data, ui).toString();")
    assert h.index("Помилки") < h.index("Попередження") < h.index("Поради")
    assert "&lt;b&gt;Дубль&lt;/b&gt;" in h and "<b>Дубль" not in h          # escaped
    assert 'data-copy="myvec ticket migrate --fix --write"' in h and "Що робити:" in h
    assert "або злити гілку" in h and "myvec/process-migration" in h
    assert 'data-copy="git -C /w/p merge myvec/process-migration"' in h
    assert 'href="#t/acme/E1-10"' in h and 'href="#t/acme/E1-11"' not in h   # first 10 as links
    assert "ще 3" in h and "E1-01, E1-02, E1-03 …" in h                       # "more" and the peek
    assert "<details" in h and "122" in h


@needs_node
def test_no_branch_hint_without_branch_or_for_non_legacy():
    h = run(DATA + "data.migration_branch = null; return p.problemsTab(data, ui).toString();")
    assert "або злити гілку" not in h
    h = run(DATA + "data.groups[1].legacy = false; return p.problemsTab(data, ui).toString();")
    assert "або злити гілку" not in h


@needs_node
def test_states_and_count():
    r = run(DATA + """return {
      empty: p.problemsTab({groups:[], counts:{}}, ui).toString(), load: p.problemsTab(null, ui).toString(),
      err: p.problemsTab(null, {path:'a', hrefs, err:'boom <x>'}).toString(),
      n: [p.problemCount({n_issues:122, n_health:14}), p.problemCount({}), p.problemCount(null)]};""")
    assert "Проблем нема" in r["empty"] and "aria-busy" in r["load"]
    assert "boom &lt;x&gt;" in r["err"]
    assert r["n"] == [136, 0, 0]


@needs_node
def test_project_level_items_escaped_and_fallback_group():
    h = run(DATA + """return p.problemsTab({counts:{warning:1}, groups:[g({code:'weird', title:'weird', explain:'', fix:'', project_items:[{path:'<p>', message:'m'}]})]}, ui).toString();""")
    assert "&lt;p&gt;" in h and "Що робити" not in h and "код" in h


@needs_node
def test_problems_column_links_to_tab():
    h = run("""return t.allProjectsPage({projects:[{project_path:'a/b',n_tickets:5,progress:10,counts:{},n_issues:5,n_health:2,updated_max:null}]},
      {hrefs:{project:()=>'#p', ticket:()=>'#t', all:()=>'#a', problems:(x)=>'#/project/'+x+'?tab=problems'}}).toString();""")
    assert 'href="#/project/a/b?tab=problems"' in h


@needs_node
def test_tab_visible_for_project_level_problems_only():
    src = (UI / "views.js").read_text(encoding="utf-8")
    assert "n > 0 || pn > 0 || cur === 'problems'" in src


@needs_node
def test_problem_count_includes_process():
    r = run("return [p.problemCount({n_issues:2, n_health:1, n_process:1}), p.problemCount({n_process:1}), p.problemCount({n_issues:2})];")
    assert r == [4, 1, 2]


@needs_node
def test_process_outdated_group_renders_cmd_copy_and_escapes():
    h = run(DATA + """return p.problemsTab({counts:{warning:1}, groups:[g({code:'process_outdated', title:'Процес <i>застарів</i>',
      cmd:'myvec scaffold --update', project_items:[{path:'.myvec/scaffold.json', message:'процес застарів v1 → v2 <x>'}]})]}, ui).toString();""")
    assert "myvec scaffold --update" in h and "&lt;x&gt;" in h and "<x>" not in h and "&lt;i&gt;" in h
    assert ".myvec/scaffold.json" in h and "copy" in h.lower()
