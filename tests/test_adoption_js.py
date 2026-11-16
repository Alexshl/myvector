"""adoption.js is pure (data -> html) and uk-UA; run under node when it is installed (E8-06, ADR-037)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).parent.parent / "src" / "myvec" / "ui"
ADOPT = UI / "adoption.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    code = (f"import * as a from {json.dumps(ADOPT.as_uri())}; const out = (() => {{ {body} }})(); "
            "console.log(JSON.stringify(out));")
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


DATA = """const P = (o) => Object.assign({project: '/p/acme', sessions: 26, sessions_myvec: 2, myvec: 10, recall: 0, self: 16360,
  self_sub: 3272, myvec_sub: 0, share: .0006, miss: 2, logged: 0, zero_results: 0}, o);
const D = {days: 14, projects: [P({}), P({project: '/p/<img src=x onerror=1>', self: 5, myvec: 4, miss: 1, self_sub: 0}),
  P({project: '(поза проєктами)', self: 0, myvec: 0, sessions: 0, sessions_myvec: 0, miss: 0})],
  total: P({project: 'total', p50_ms: 120.4, zero_results: 3}),
  collector: {enabled: true, pending_bytes: 0, last_scan: 1, backfill: false}};
const ui = {days: 14, hrefs: {days: (p, n) => '#/search?days=' + n}};
"""


def test_module_is_pure():
    src = ADOPT.read_text(encoding="utf-8")
    for bad in ("fetch(", "document.", "window.", "location", "localStorage"):
        assert bad not in src, bad
    assert set(re.findall(r"from '([^']+)'", src)) == {"./util.js", "./spend.js"}
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|oklch\(", src)         # colours only via var(--...)


@needs_node
def test_pct_dash_zero_below_one_and_whole():
    r = run("return [a.pct(0, 0), a.pct(3, 0), a.pct(1, null), a.pct(null, 5), a.pct(undefined, 5), a.pct(0, 10),"
            "a.pct(1, 300), a.pct(1, 2), a.pct(1, 3), a.pct(2, 3), a.pct(3, 3), a.pct(1, 100), a.pct(-1, 5)];")
    assert r == ["—", "—", "—", "—", "—", "0%", "< 1%", "50%", "33%", "67%", "100%", "1%", "0%"]


@needs_node
def test_rows_order_self_then_myvec_then_project_and_no_mutation():
    r = run("const src = [{project: 'b', self: 1, myvec: 5}, {project: 'a', self: 1, myvec: 5}, {project: 'c', self: 9, myvec: 0},"
            "{project: 'd', self: 1, myvec: 7}]; const out = a.rows(src);"
            "return [out.map((x) => x.project), src.map((x) => x.project), a.rows(null), a.rows(undefined)];")
    assert r[0] == ["c", "d", "a", "b"] and r[1] == ["b", "a", "c", "d"] and r[2] == [] and r[3] == []


@needs_node
def test_card_numbers_sessions_misses_and_escaping():
    r = run(DATA + "return a.card(D, ui).toString();")
    assert "16\u00a0360" in r and "2 <span class=\"of\">/ 26</span>" in r        # uk-UA grouping, sessions myvec / all
    assert "t-warning" not in r                                              # misses 2/10 and 1/4 are not > 50 %
    assert "<img" not in r and "&lt;img" in r
    assert "(поза проєктами)" in r and "muted" in r
    assert "p50 120 мс" in r and "3 нульові видачі" in r
    assert "Усього" in r and "&lt; 1%" in r and "—" in r
    assert "#/search?days=7" in r and "#/search?days=30" in r
    assert "Транскрипти ще читаються" not in r and "aria-busy" not in r


@needs_node
def test_card_warning_only_when_misses_exceed_half():
    r = run(DATA + """const mk = (miss) => a.card(Object.assign({}, D, {projects: [P({miss, myvec: 10})], total: P({miss: 0, myvec: 10})}), ui).toString();
      return [mk(5), mk(6)];""")
    assert "t-warning" not in r[0] and "t-warning" in r[1]


@needs_node
def test_card_sorted_by_self_desc():
    r = run(DATA + "const s = a.card(D, ui).toString(); return [s.indexOf('/p/acme'), s.indexOf('&lt;img'), s.indexOf('(поза проєктами)')];")
    assert 0 < r[0] < r[1] < r[2]


@needs_node
def test_card_zero_results_plural_and_missing_p50():
    r = run(DATA + """const t = (n, p50) => a.card(Object.assign({}, D, {total: P({p50_ms: p50, zero_results: n})}), ui).toString();
      return [t(1, 10), t(5, 10), t(0, null), t(2, null)];""")
    assert "1 нульова видача" in r[0] and "5 нульових видач" in r[1]
    assert "p50" not in r[2] and "0 нульових видач" in r[2] and "p50" not in r[3] and "2 нульові видачі" in r[3]


@needs_node
def test_card_states():
    r = run(DATA + """return [a.card(null, ui).toString(), a.card({err: 'boom'}, ui).toString(),
      a.card(Object.assign({}, D, {collector: {enabled: false, pending_bytes: 0, backfill: false}}), ui).toString(),
      a.card(Object.assign({}, D, {collector: {enabled: true, pending_bytes: 5 * 1048576, backfill: true}}), ui).toString(),
      a.card(Object.assign({}, D, {projects: []}), ui).toString(), a.card(D).toString()];""")
    assert "aria-busy" in r[0] and "Використання пошуку агентами" in r[0]
    assert "boom" in r[1] and 'data-act="usageretry"' in r[1] and "Не вдалося отримати статистику" in r[1]
    assert "Збір вимкнено" in r[2] and "tokens_enabled" in r[2] and "install-daemon" in r[2] and "16\u00a0360" not in r[2]
    assert "Транскрипти ще читаються" in r[3] and "МБ" in r[3] and "16\u00a0360" in r[3]
    assert "Пошуків і grep за період не було" in r[4] and "<span class=\"num right\">" not in r[4]
    assert "16\u00a0360" in r[5]                                                    # ui defaults: days 14, no hrefs


@needs_node
def test_card_period_switch_marks_current_days():
    r = run(DATA + "return [a.card(D, Object.assign({}, ui, {days: 7})).toString(), a.card(D, ui).toString()];")
    assert "7 днів" in r[0] and "14 днів" in r[0] and "30 днів" in r[0]
    assert r[0] != r[1]


@needs_node
def test_card_footer_new_fields_and_old_payload():
    r = run(DATA + """const T = P({project: 'total', p50_ms: 120.4, p50_ms_24h: 80, zero_results: 3, pre_onset: 1234, foreign: 5174,
        sessions_explore: 20, sessions_myvec_explore: 3});
      const s = a.card(Object.assign({}, D, {total: T}), ui).toString();
      const old = a.card(D, ui).toString();
      return [s, old];""")
    assert "p50 доба / 14 днів: 80 мс / 120 мс" in r[0]
    assert "Сесій з myvec серед розвідувальних: 3 / 20" in r[0]
    assert "не враховано: 1 234 до відліку, 5 174 з іншої ОС" in r[0]
    assert "не враховано" not in r[1] and "серед розвідувальних" not in r[1] and "<table" not in r[0]


@needs_node
def test_card_share_uses_share_explore_when_present_else_old_share():
    r = run(DATA + """const mk = (x) => a.card(Object.assign({}, D, {projects: [P(x)], total: P(x)}), ui).toString();
      return [mk({myvec: 10, self: 90}), mk({myvec: 10, self: 90, share_explore: 0.5}), mk({myvec: 0, self: 3, share_explore: null})];""")
    assert "10%" in r[0] and "50%" not in r[0]                       # old payload: myvec / (myvec + self)
    assert "50%" in r[1] and "width: 50.0%" in r[1] and "10%" not in r[1]
    assert "<span class=\"n\">—</span>" in r[2]                      # no denominator
    assert "розвідувальні grep/find" in r[1] and "title=" in r[1]
