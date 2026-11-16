"""spend.js is pure (data -> html/svg) and uk-UA; run under node when it is installed (E7-03, ADR-016)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).parent.parent / "src" / "myvec" / "ui"
SPEND = UI / "spend.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    code = (f"import * as s from {json.dumps(SPEND.as_uri())}; const out = (() => {{ {body} }})(); "
            "console.log(JSON.stringify(out));")
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


REPORT = """const days = [1,2,3,4,5,6,7].map((i) => ({day: '2026-11-0'+i, ts: 1.79e9 + i*86400, total: i === 7 ? 9e6 : 1e6, output: 1000}));
const agent = (o) => Object.assign({agent: 'coder', orch: false, models: ['claude-sonnet-5-5'], efforts: ['medium'], runs: 3, requests: 9, output: 400000, total: 12e6, cache_share: .9}, o);
const D = {enabled: true, totals: {total: 15e6, output: 5e5, input: 1, cache_read: 14e6, cache_write: 1, cache_share: .93}, days,
  agents: [agent({}), agent({agent: 'main', orch: true, models: ['claude-opus-5-5'], total: 3e6, output: 1e5})],
  tickets: [{id: 'E1-01', title: '<img src=x onerror=1>', total: 5e6, output: 1, top_agent: 'main', top_share: .5}],
  top_ticket: {id: 'E1-01', total: 5e6, top_agent: 'main'}};
const ui = {path: '/p', days: 7, hrefs: {ticket: (p, i) => '#t/' + p + '/' + i, days: (p, n) => '#d/' + n}};
"""


def test_module_is_pure():
    src = SPEND.read_text(encoding="utf-8")
    for bad in ("fetch(", "document.", "window.", "location", "localStorage"):
        assert bad not in src, bad
    assert set(re.findall(r"from '([^']+)'", src)) == {"./util.js", "./monitor.js"}
    assert set(re.findall(r"from '([^']+)'", (UI / "tracker.js").read_text(encoding="utf-8"))) == {"./util.js"}
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|oklch\(", src)         # colours only via var(--...)


@needs_node
def test_compact_numbers_and_model_labels():
    r = run("return [s.tk(5), s.tk(412000), s.tk(10600000), s.tk(999960), s.tk(2.3e9), s.tkParts(10600000), s.tk(0),"
            "s.modelLabel('claude-sonnet-5-5'), s.modelLabel('claude-sonnet-5-5-20251101'), s.modelLabel('claude-opus-4-1-20250805'),"
            "s.modelLabel('claude-sonnet-4-20250514'), s.modelLabel('gpt-x'), s.modelLabel(null)];")
    assert r[:5] == ["5", "412 тис.", "10,6 млн", "1,0 млн", "2,3 млрд"] and r[5] == ["10,6", "млн"] and r[6] == "0"
    assert r[7:] == ["Sonnet 5.5", "Sonnet 5.5", "Opus 4.1", "Sonnet 4", "gpt-x", "?"]


@needs_node
def test_hot_days_rules():
    r = run("return [s.hotDays([0, 5, 5]), s.hotDays([1, 1, 3]), s.hotDays([1, 1, 2]), s.hotDays([0, 1, 1, 1, 0, 9]), s.hotDays([]), s.hotDays([2, 2, 2, 4])];")
    assert r[0] == [False] * 3                                       # fewer than 3 non-zero days
    assert r[1] == [False, False, True]                              # median 1, 3 > 2
    assert r[2] == [False, False, False]                             # exactly 2 x median is not hot
    assert r[3] == [False, False, False, False, False, True] and r[4] == []
    assert r[5] == [False] * 4


@needs_node
def test_tokens_tab_content_and_escaping():
    r = run(REPORT + "return s.tokensTab(D, ui).toString();")
    assert 'data-chart="tok"' in r and "data-v=" in r and "var(--warning)" in r         # 9 млн day is hot
    assert "15,0 млн" in r and "93" in r and "Sonnet 5.5" in r and "Opus 5.5" in r
    assert "оркестратор" in r and "без тікета" in r
    assert 'href="#t//p/E1-01"' in r and "переважно оркестратор" in r
    assert "<img" not in r and "&lt;img" in r
    assert "Період" not in r
    assert "<tfoot" not in r and 'class="tfoot"' in r


@needs_node
def test_tokens_tab_states():
    r = run(REPORT + """return [s.tokensTab(null, ui).toString(), s.tokensTab({err: 'boom'}, ui).toString(),
      s.tokensTab({enabled: false, totals: {total: 0}, days: []}, ui).toString(),
      s.tokensTab({enabled: true, totals: {total: 0}, days: [], agents: [], tickets: []}, ui).toString(),
      s.tokensTab(Object.assign({}, D, {tickets: [], top_ticket: null}), ui).toString(),
      s.tokensSub(null, 14), s.tokensSub(D), s.tokensSub(null, 30)];""")
    assert "aria-busy" in r[0] and "boom" in r[1] and 'data-act="tokensretry"' in r[1]
    assert "Збір витрат вимкнено" in r[2] and "tokens_enabled" in r[2]
    assert "Витрат за цей період нема" in r[3] and 'data-chart="tok"' not in r[3]
    assert "тікетів з витратами нема" in r[4] and "нема витрат, прив’язаних до тікетів" in r[4]
    assert r[5].startswith("Витрати токенів за 14 днів · з транскриптів Claude Code, без тексту повідомлень")
    assert "за 7 днів" in r[6] and "за 30 днів" in r[7]


@needs_node
def test_chart_width_and_tip():
    r = run(REPORT + """const c = s.chart('tok', D, {}, {w: 390, h: 160}); const e = s.chart('tok', {days: []}, {}, {w: 300, h: 160});
      return {svg: c.svg, tip: c.tip(6), tip0: c.tip(0), n: c.geom.n, none: c.tip(99), empty: e.svg.length > 0, en: e.geom.n};""")
    assert 'width="390"' in r["svg"] and 'height="160"' in r["svg"] and r["n"] == 7
    assert "var(--warning)" in r["svg"] and "var(--brand-primary)" in r["svg"]
    assert "<b>9,0 млн</b>" in r["tip"] and "понад удвічі" in r["tip"] and "output 1 тис." in r["tip"]
    assert "понад удвічі" not in r["tip0"] and r["none"] == "" and r["empty"] and r["en"] == 0
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", r["svg"])


@needs_node
def test_ticket_block():
    r = run("""const card = {line: 'tokens: 10,6 млн · coder 69%', cache_share: .97, summary: {total: 10.6e6},
      by_agent: [{agent: 'coder', models: ['claude-sonnet-5-5', 'claude-opus-5-5'], efforts: ['medium'], runs: 3, output: 412000, total: 7.3e6},
                 {agent: 'main', models: ['claude-opus-5-5'], efforts: [], runs: 1, output: 5, total: 1e5}]};
      return [s.ticketBlock(card).toString(), String(s.ticketBlock(null)), String(s.ticketBlock({by_agent: []}))];""")
    assert "<h2" in r[0] and "Витрати" in r[0] and "10,6 млн · 97% кеш" in r[0]
    assert "Sonnet 5.5/Opus 5.5 · medium · 3 запуски · output 412 тис." in r[0]
    assert "оркестратор" in r[0] and "1 запуск ·" in r[0]
    assert 'У Log при закритті: <span class="mono">tokens: 10,6 млн · coder 69%</span>' in r[0]
    assert r[1] == "" and r[2] == ""


@needs_node
def test_period_seg():
    r = run(REPORT + "return s.periodSeg('/p', 14, ui.hrefs).toString();")
    assert r.count('class="seg"') == 1 and r.count("<a ") == 3 and "#d/7" in r and "#d/30" in r
    assert r.count('aria-current="true"') == 1 and 'href="#d/14" class="on"' in r
