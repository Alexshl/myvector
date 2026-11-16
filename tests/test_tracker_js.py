"""tracker.js is pure (data -> html): run under node, when it is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TRACKER = Path(__file__).parent.parent / "src" / "myvec" / "ui" / "tracker.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    code = (f"import * as t from {json.dumps(TRACKER.as_uri())}; const out = (() => {{ {body} }})(); "
            "console.log(JSON.stringify(out));")
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


NODES = """const N = [
 {id:'E1',title:'Epic',kind:'group',status:'in_progress',parent:null,progress:50},
 {id:'E1-01',title:'A',kind:'ticket',status:'done',parent:'E1',progress:100,updated:'2026-11-09'},
 {id:'E1-02',title:'B',kind:'ticket',status:'in_progress',parent:'E1',progress:50},
 {id:'G2',title:'Sub',kind:'group',status:'todo',parent:'E1',progress:0},
 {id:'E1-03',title:'C',kind:'ticket',status:'blocked',parent:'G2',progress:0},
 {id:'E1-04',title:'D',kind:'ticket',status:'waiting',parent:'G2',progress:0}];
const hrefs = {project: (p) => '#p/'+p, ticket: (p,i) => '#t/'+p+'/'+i, all: () => '#all'};
const ui = (o) => Object.assign({path:'acme', filter:'all', collapsed:new Set(), sel:null, hrefs}, o);
const data = {project:'acme', dir:'docs/todo', progress:42, counts:{done:1,in_progress:1,blocked:1,waiting:1}, nodes:N, issues:[{ticket_id:'E1-02',message:'bad dep'}]};
"""


@needs_node
def test_filters_counts_and_ancestors():
    r = run(NODES + """
      const ctx = {issues:new Set(['E1-02'])};
      const ids = (f) => t.treeCard(data, ui({filter:f})).toString().match(/data-fk="tk:([^"]+)"/g).map((x) => x.slice(12,-1));
      return {counts: t.filterCounts(N, ctx), waiting: ids('waiting'), problems: ids('problems')};""")
    assert r["counts"] == {"all": 4, "active": 2, "waiting": 1, "problems": 2}
    assert r["waiting"] == ["E1", "G2", "E1-04"]                 # ancestors of the match are shown
    assert r["problems"] == ["E1", "E1-02", "G2", "E1-03"]


@needs_node
def test_collapse_depth_and_sessions_seam():
    r = run(NODES + """
      const rows = (o) => (t.treeCard(data, ui(o)).toString().match(/data-fk="tk:([^"]+)"/g) || []).map((x) => x.slice(12,-1));
      return {all: rows({}), closed: rows({collapsed:new Set(['G2'])}), closedRoot: rows({collapsed:new Set(['E1'])}),
              seam: t.matches(N[2], 'waiting', {sessions:{'E1-02':{waiting:true}}})};""")
    assert r["all"] == ["E1", "E1-01", "E1-02", "G2", "E1-03", "E1-04"]      # depth 3
    assert r["closed"] == ["E1", "E1-01", "E1-02", "G2"] and r["closedRoot"] == ["E1"]
    assert r["seam"] is True


@needs_node
def test_escaping_and_uk_percent():
    r = run(NODES + """
      const evil = '<img src=x onerror=alert(1)>';
      const d = Object.assign({}, data, {nodes: [{id:'X-1',title:evil,kind:'ticket',status:'todo',parent:null,progress:41.6}]});
      const tree = t.treeCard(d, ui({})).toString();
      const card = t.ticketCard({id:'X-1',title:evil,kind:'ticket',status:'todo',progress:41.6,deps:['GONE'],blocks:[],
        sections:{ac:[{text:evil,done:true}],log:[{date:'2026-11-09',agent:evil,text:evil}],goal:null}}, {}, ui({})).toString();
      return {tree, card, prog: t.progressCard(d).toString()};""")
    for k in ("tree", "card"):
        assert "<img" not in r[k] and "&lt;img" in r[k]
    assert "42%" in r["tree"] and ("42%" in r["prog"] or "42%" in r["card"])
    assert "09.11.2026" in r["card"] and "2 з" not in r["card"] and "1 з 1" in r["card"]
    assert "Тікета з таким id немає" in r["card"]               # dangling dep is marked, not linked


@needs_node
def test_pages_empty_states_and_links():
    r = run(NODES + """
      return {all: t.allProjectsPage({projects:[{project_path:'a/b',n_tickets:3,progress:50,counts:{in_progress:1},n_issues:0,updated_max:'2026-11-09'},
                                                {project_path:'old',n_tickets:0,counts:{}}]}, {hrefs}).toString(),
              empty: t.projectTicketsPage({dir:'docs/todo',nodes:[]}, ui({})).toString(),
              card: t.projectTicketsPage(data, ui({sel:'E1-02'})).toString()};""")
    assert 'href="#p/a/b"' in r["all"] and "не на каноні" in r["all"] and "myvec ticket migrate" in r["all"]
    assert "Тікетів ще немає" in r["empty"] and 'id="mv-tkcard"' in r["card"]


def test_tracker_js_is_pure():
    src = TRACKER.read_text(encoding="utf-8")
    for bad in ("fetch(", "document.", "window.", "location", "localStorage"):
        assert bad not in src, bad
    assert re.findall(r"from '([^']+)'", src) == ["./util.js"]


@needs_node
def test_all_projects_problems_is_issues_plus_health():
    r = run(NODES + """
      return t.allProjectsPage({projects:[{project_path:'zz',n_tickets:5,progress:10,counts:{blocked:2,in_progress:1,waiting:3},n_issues:4,n_health:1,n_problems:9,updated_max:null}]}, {hrefs}).toString();""")
    cells = re.findall(r'<span class="num right c-x tnum"[^>]*>([^<]*)</span>', r)
    assert [c.strip() for c in cells] == ["1", "3", "5"]


@needs_node
def test_titles_render_inline_code_safely():
    r = run(NODES + """
      const evil = '`cm send` <b>x</b>';
      const d = Object.assign({}, data, {nodes: [{id:'X-1',title:evil,kind:'ticket',status:'todo',parent:null,progress:10,deps:['X-2'],blocks:[]},
                                                 {id:'X-2',title:evil,kind:'ticket',status:'todo',parent:null,progress:0,deps:[],blocks:[]}]});
      const byId = Object.fromEntries(d.nodes.map((n) => [n.id, n]));
      return {tree: t.treeCard(d, ui({})).toString(), card: t.ticketCard(Object.assign({sections:{ac:[],log:[],goal:null}}, byId['X-1']), byId, ui({})).toString()};""")
    for k in ("tree", "card"):
        assert "`" not in r[k].replace("&#96;", "") and "&lt;b&gt;" in r[k] and "<b>" not in r[k]
        assert '<span class="mono"' in r[k]
    assert re.search(r'<span class="tt"[^>]*>[^<]*<span class="mono"', r["tree"])


@needs_node
def test_problems_filter_matches_summary_definition():
    r = run(NODES + """
      const ctx = {issues: new Set(['B', 'C'])};
      const ns = [{id:'A',status:'blocked'},{id:'B',status:'blocked'},{id:'C',status:'todo'},{id:'D',status:'todo'}];
      return ns.filter((n) => t.matches(n, 'problems', ctx)).map((n) => n.id);""")
    assert r == ["A", "B", "C"]


SESS = """const hrefs = {project: (p) => '#p/'+p, ticket: (p,i) => '#t/'+p+'/'+i, all: () => '#all'};
const S = (o) => Object.assign({session_id:'11111111-2222-3333-4444-555555555555', effective_state:'waiting', wait_s:2520, mode:'send', question:'Зараз <b>чи</b> потім?', cwd:'/w/E1', ticket_project:'acme', ticket_id:'E1-02', ticket_title:'B'}, o);
const sui = (o) => Object.assign({hrefs, node:'devbox', replies:{}, busy:{}}, o);
"""


@needs_node
def test_wait_text_and_copy_texts():
    r = run("""return {a: t.waitText(30), b: t.waitText(2520), c: t.waitText(7680), d: t.waitText(7200), e: t.waitText(90000),
      c1: t.copyTexts('SID', 'acme', 'E1-02', ' так '), c2: t.copyTexts('SID', null, null, '')};""")
    assert (r["a"], r["b"], r["c"], r["d"]) == ("менше хвилини", "42 хв", "2 год 8 хв", "2 год")
    assert r["e"] == "1 д 1 год"
    assert r["c1"] == {"uuid": "SID", "resume": "cm resume SID", "fork": "cm resume SID --fork",
                       "manager": "Віднови сесію SID (acme, E1-02) через cm і передай їй: «так»"}
    assert r["c2"]["manager"] == "Віднови сесію SID через cm"


@needs_node
def test_session_cell_states():
    r = run(SESS + """return {none: t.sessionCell([], 'devbox').toString(), wait: t.sessionCell([S({}), S({effective_state:'working'})], 'devbox').toString(),
      work: t.sessionCell([S({effective_state:'working'})], '').toString()};""")
    assert 'faint' in r["none"] and "—" in r["none"]
    assert "devbox" in r["wait"] and "чекає 42 хв" in r["wait"] and "t-warning" in r["wait"] and "+1" in r["wait"]
    assert "працює" in r["work"] and "t-success" in r["work"] and "nodechip" not in r["work"]


@needs_node
def test_session_block_buttons_by_mode():
    r = run(SESS + """const b = (m, o) => t.sessionBlock(S(Object.assign({mode:m}, o)), sui({project:'acme', ticket:'E1-02'})).toString();
      return {send: b('send'), restart: b('restart_send'), resume: b('resume_send'), held: b('held'), un: b('unavailable'),
        busy: t.sessionBlock(S({}), sui({busy:{'11111111-2222-3333-4444-555555555555': true}})).toString(),
        reply: t.sessionBlock(S({}), sui({replies:{'11111111-2222-3333-4444-555555555555':'<hi>'}})).toString()};""")
    assert 'data-act="sessresume"' in r["send"] and "Відповісти</button>" in r["send"] and "<textarea" in r["send"]
    for k in ("restart", "resume"):
        assert "Відповісти й відновити" in r[k] and 'data-act="sessresume"' in r[k]
    assert 'data-kind="fork"' in r["held"] and "disabled" in r["held"] and "<textarea" not in r["held"]
    assert 'data-act="sessresume"' not in r["held"] and "в терміналі на devbox" in r["held"]
    assert "disabled" in r["un"] and 'data-act="sessresume"' not in r["un"] and "cm update" in r["un"]
    assert "Надсилаю…" in r["busy"] and "disabled" in r["busy"]
    assert "&lt;hi&gt;" in r["reply"] and "<hi>" not in r["reply"]
    for k in ("send", "held"):
        assert 'data-kind="uuid"' in r[k] and 'data-kind="resume"' in r[k] and 'data-kind="manager"' in r[k]
        assert "&lt;b&gt;чи" in r[k] and "<b>чи" not in r[k]   # question and ids are escaped
        assert "onclick" not in r[k]


@needs_node
def test_waiting_card_rows():
    r = run(SESS + """const ws = [S({}), S({session_id:'a', mode:'held', wait_s:100}), S({session_id:'b', mode:'restart_send', wait_s:50, ticket_id:null})];
      return {empty: t.waitingCard([], sui({})).toString(), card: t.waitingCard(ws, sui({})).toString()};""")
    assert r["empty"] == ""
    c = r["card"]
    assert "Чекають на тебе" in c and "чекає 42 хв" in c and "у терміналі" in c and "не запущена" in c
    assert 'href="#t/acme/E1-02"' in c and 'data-kind="fork"' in c


@needs_node
def test_sessions_page_and_tree_column():
    r = run(NODES + SESS.replace("const hrefs", "var hrefs_unused").replace("hrefs_unused = ", "hrefs_unused = ") + """
      const n2 = N.map((n) => n.id === 'E1-02' ? Object.assign({sessions:[S({})]}, n) : n);
      const d = Object.assign({}, data, {nodes: n2});
      return {tree: t.treeCard(d, ui({node:'devbox'})).toString(), waitf: t.treeCard(d, ui({filter:'waiting', node:'devbox'})).toString(),
        page: t.sessionsPage({node:'devbox', cm:{available:false, hint:'оновіть cm'}, sessions:[S({mode:'unavailable'}), S({session_id:'z', effective_state:'ended', mode:'resume_send', ticket_id:null})]}, sui({})).toString(),
        loading: t.sessionsPage(null, sui({})).toString(), all: t.allProjectsPage({projects:[]}, sui({waiting:[S({})]})).toString()};""")
    assert "Сесія</span>" in r["tree"] and "чекає 42 хв" in r["tree"] and "devbox" in r["tree"]
    assert 'data-fk="tk:E1-02"' in r["waitf"]
    assert "оновіть cm" in r["page"] and "без тікета" in r["page"] and "disabled" in r["page"]
    assert "aria-busy" in r["loading"] and "Чекають на тебе" in r["all"]


def test_tracker_js_has_no_inline_handlers():
    src = TRACKER.read_text(encoding="utf-8")
    assert "onclick" not in src and "addEventListener" not in src


@needs_node
def test_session_ui_review_fixes():
    r = run(NODES + SESS.replace("const hrefs", "var hrefsX") + """
      const EVIL = '"><img src=x onerror=alert(1)>';
      const cell = t.sessionCell([S({wait_s:10})], 'devbox').toString();
      const blk = (o) => t.sessionBlock(S(o), sui({})).toString();
      const page = (o) => t.sessionsPage({node:'devbox', cm:{available:true}, sessions:[S(o)]}, sui({})).toString();
      return {cell, evil: blk({session_id: EVIL}), evilPage: page({session_id: EVIL}),
        chips: ['send','restart_send','resume_send','held','unavailable'].map((m) => blk({mode:m})),
        workBlock: blk({effective_state:'working'}), workPage: page({effective_state:'working'}),
        idlePage: page({effective_state:'idle', mode:'restart_send'}), endedPage: page({effective_state:'ended', mode:'resume_send'}),
        compact: [t.waitText(10, true), t.waitText(2520, true), t.waitText(3900, true)],
        fk: blk({}), wait: t.allProjectsPage({projects:[{project_path:'a', n_tickets:3, counts:{waiting:2}, n_waiting:2}]}, sui({})).toString()};""")
    assert "чекає &lt;1 хв" in r["cell"]
    assert r["compact"] == ["<1 хв", "42 хв", "1 год 5 хв"]
    for k in ("evil", "evilPage"):
        assert "<img" not in r[k] and "onerror=alert" not in r[k].replace("&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", "")
    chips = ["через cm", "не запущена · перезапуск cm", "не запущена · відновлення cm", "у терміналі", "недоступно"]
    for html_, chip in zip(r["chips"], chips):
        assert f'<span class="tag">{chip}</span>' in html_
    for k in ("workBlock", "workPage"):   # a working session: copy buttons only
        assert "sessresume" not in r[k] and "Форк" not in r[k] and "<textarea" not in r[k] and "Відновити" not in r[k]
    assert 'data-kind="uuid"' in r["workBlock"] and 'data-kind="uuid"' in r["workPage"]
    assert "sessresume" in r["idlePage"] and "sessresume" in r["endedPage"]
    assert 'data-fk="reply:11111111-2222-3333-4444-555555555555"' in r["fk"]
    assert "Чекають" in r["wait"] and ">2<" in r["wait"].replace(" ", "")


@needs_node
def test_session_block_auq_buttons_only_for_auq():
    r = run("""
      const base = {session_id:'s1', effective_state:'waiting', mode:'send', question:'Колір?', wait_s:5, cwd:'/x'};
      const auq = {...base, answer_kind:'auq', question_options:{n:1,multi:false,labels:['Червоний',null,'Синій']}};
      auq.question_at = 12.5;
      const a = t.sessionBlock(auq, {}).toString(), b = t.sessionBlock(base, {}).toString();
      return {a: (a.match(/data-act="sessoption"/g) || []).length, n: [...a.matchAll(/data-n="(\\d)"/g)].map((m) => m[1]),
              input: a.includes('type="text"') && !a.includes('<textarea'), b: b.includes('sessoption'), bt: b.includes('<textarea')};""")
    assert r == {"a": 2, "n": ["1", "3"], "input": True, "b": False, "bt": True}


@needs_node
def test_card_extra_between_ac_and_log():
    r = run(NODES + """
      const base = {id:'X-1',title:'T',kind:'ticket',status:'todo',progress:10,deps:[],blocks:[],
        sections:{ac:[{text:'ac one',done:false}],log:[{date:'2026-11-09',agent:'coder',text:'did it'}],goal:null}};
      const plain = t.ticketCard(base, {}, ui({})).toString();
      const extra = t.ticketCard(base, {}, ui({cardExtra: '<section id="xtra">EXTRA</section>'})).toString();
      const grp = t.ticketCard({id:'E1',title:'Epic',kind:'group',status:'todo',progress:0}, Object.fromEntries(N.map((n) => [n.id, n])), ui({cardExtra: '<i>no</i>'})).toString();
      return {plain, extra, grp, same: t.ticketCard(base, {}, ui({cardExtra: ''})).toString() === plain};""")
    e = r["extra"]
    assert e.index("ac one") < e.index("EXTRA") < e.index("did it")
    assert "EXTRA" not in r["plain"] and r["same"] and "<i>no</i>" not in r["grp"]


@needs_node
def test_card_health_list_and_tree_filter():
    r = run(NODES + """
      const base = {id:'X-1',title:'T',kind:'ticket',status:'todo',progress:10,deps:[],blocks:[],
        sections:{ac:[],log:[],goal:null}};
      const warn = Object.assign({}, base, {issues:[{severity:'health',code:'too_big',ticket_id:'X-1',message:'Завеликий <b>(L)</b>'}]});
      const d = Object.assign({}, data, {issues:[{severity:'health',ticket_id:'E1-04',message:'Нема Acceptance criteria'}]});
      const issues = new Set(d.issues.map((i) => i.ticket_id));
      const rows = t.treeCard(d, ui({})).toString();
      return {card: t.ticketCard(warn, {}, ui({})).toString(), none: t.ticketCard(base, {}, ui({})).toString(),
        empty: t.ticketCard(Object.assign({}, base, {issues:[]}), {}, ui({})).toString(),
        rows, counts: t.filterCounts(N, {issues})};""")
    assert 'class="health"' in r["card"] and "Завеликий" in r["card"] and "warn-ic" in r["card"]
    assert "<b>(L)</b>" not in r["card"]
    assert "health" not in r["none"] and "health" not in r["empty"]
    assert 'title="Нема Acceptance criteria"' in r["rows"]
    assert r["counts"]["problems"] == 2    # blocked E1-03 + E1-04 with advice


@needs_node
def test_waiting_card_project_label_fallback():
    r = run(SESS + """const f = (o) => t.waitingCard([S(Object.assign({ticket_project:null, ticket_id:null, project_path:null}, o))], sui({})).toString();
      return {lab: f({project_label:'~/projects/apache-local', cwd:'/h/projects/apache-local'}), none: f({})};""")
    assert "~/projects/apache-local" in r["lab"] and "—" not in r["lab"]
    assert "—" in r["none"]


@needs_node
def test_all_projects_problems_includes_process():
    r = run(NODES + """
      return t.allProjectsPage({projects:[{project_path:'zz',n_tickets:5,progress:10,counts:{},n_issues:4,n_health:1,n_process:1,updated_max:null}]}, {hrefs}).toString();""")
    cells = re.findall(r'<span class="num right c-x tnum"[^>]*>([^<]*)</span>', r)
    assert [c.strip() for c in cells][-1] == "6"


@needs_node
@pytest.mark.parametrize("worst,color", [("warning", "--warning"), ("error", "--danger"), ("health", "--info"),
                                         (None, "--danger"), ("bogus", "--danger")])
def test_all_projects_problem_colour_follows_worst(worst, color):
    r = run(NODES + f"""
      return t.allProjectsPage({{projects:[{{project_path:'zz',n_tickets:5,progress:10,counts:{{}},n_issues:0,n_process:2,worst:{json.dumps(worst)},updated_max:null}}]}}, {{hrefs}}).toString();""")
    cells = re.findall(r'<span class="num right c-x tnum" style="([^"]*)">', r)
    assert f"color: var({color}); font-weight: 600;" in cells[-1]


@needs_node
def test_all_projects_zero_problems_has_no_colour():
    r = run(NODES + """
      return t.allProjectsPage({projects:[{project_path:'zz',n_tickets:5,progress:10,counts:{},n_issues:0,worst:'error',updated_max:null}]}, {hrefs}).toString();""")
    cells = re.findall(r'<span class="num right c-x tnum" style="([^"]*)">', r)
    assert all("--danger" not in c for c in cells)


def test_score_row_css_keeps_formula_on_own_nowrap_line():
    css = (TRACKER.parent / "app.css").read_text()
    media = css[css.index("@media (max-width: 760px)"):]
    assert ".mv-score > span:last-child { grid-column: 1 / -1; white-space: nowrap; }" in media
