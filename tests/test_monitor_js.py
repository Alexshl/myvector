"""monitor.js is pure (data -> html/svg) and uk-UA; run under node when it is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MONITOR = Path(__file__).parent.parent / "src" / "myvec" / "ui" / "monitor.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(body: str):
    code = (f"import * as m from {json.dumps(MONITOR.as_uri())}; const out = (() => {{ {body} }})(); "
            "console.log(JSON.stringify(out));")
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


LOAD = """const snap = {ts: 1e9, cpu_pct: 91.4, cores: [95, 40], load: [7.81, 7.1, 4.3], mem_used: 11.2*2**30, mem_total: 16*2**30,
  swap_used: 0.4*2**30, temp_c: null, freq_mhz: 3000};
const load = {host: {hostname: 'dev', cores: 8, ram_total: 16*2**30}, snapshot: snap, projects: [{project: 'project-a', cpu_pct: 612, top_name: 'php'}, {project: null, cpu_pct: 44}], temp_max_24h: null};
const pts = [0,1,2,3,10,11].map((i) => ({ts: 1000 + i*60, cpu_pct: 10 + i, cpu_max: 20 + i, load1: 1}));
"""


def test_module_is_pure():
    src = MONITOR.read_text(encoding="utf-8")
    for bad in ("fetch(", "document.", "window.", "location", "localStorage"):
        assert bad not in src, bad
    assert "import" in src and set(re.findall(r"from '([^']+)'", src)) == {"./util.js"}


@needs_node
def test_load_page_uk_format_and_missing_temp():
    r = run(LOAD + "return m.loadPage({load, series: {points: pts, step_s: 60, now: 1700}, current: {running: [], recent: []}}, {period: '24h'}).toString();")
    assert "91" in r and "7,81" in r and "11,2" in r           # decimal comma
    assert "—" in r and "датчика температури нема" in r         # temp_c null -> dash, no crash
    assert 'data-chart="cpu"' in r and "поріг тривоги 85%" in r and "myvec measure" in r
    assert 'class="v hot"' in r                                   # 91 >= 85
    assert r.count('class="hbar"') == 2 and "інше" in r


@needs_node
def test_load_off_and_loading_states():
    r = run("return [m.loadPage({off: true}).toString(), m.loadPage({}).toString(), m.loadPage({err: 'boom'}).toString()];")
    assert "Семплер вимкнено або нема psutil" in r[0] and "load_enabled" in r[0]
    assert "aria-busy" in r[1] and "boom" in r[2]


@needs_node
def test_series_gap_makes_separate_segments_and_threshold_label_left():
    r = run(LOAD + """const svg = m.lineSvg(pts, {w: 600, h: 200, threshold: 85, step_s: 60, span: 1000, now: 1700});
      const g = m.lineGeom(pts, {w: 600, h: 200, step_s: 60, span: 1000, now: 1700});
      return {segs: g.segs, poly: (svg.match(/<polyline/g) || []).length, label: /x="(\\d+(?:\\.\\d+)?)" y="[\\d.]+" text-anchor="start" style="fill: var\\(--danger\\);">поріг 85%/.exec(svg)[1], L: g.L};""")
    assert r["segs"] == [[0, 3], [4, 5]] and r["poly"] == 2
    assert float(r["label"]) < 60                               # at the left edge, away from the newest point


@needs_node
def test_hit_index_edges_and_colors_tokens_only():
    r = run("""const runs = [1,2,3,4].map((i) => ({outcome: i === 3 ? 'failed' : 'passed', wall_s: i*10, cpu_s: i, started_at: 1e9 + i*86400, regression: i === 2 ? true : null, source: 'wrapper'}));
      const g = m.barsGeom([10,20,30,40], {w: 400, h: 170});
      const svg = m.barsSvg(runs, (x) => x.wall_s, {w: 400, h: 170});
      const lg = m.lineGeom([{ts: 0, cpu_pct: 1}, {ts: 10, cpu_pct: 2}, {ts: 20, cpu_pct: 3}], {w: 400, h: 100});
      return {first: m.hitIndex(-50, g), last: m.hitIndex(9999, g), mid: m.hitIndex(g.cx(2), g), empty: m.hitIndex(5, m.barsGeom([], {})),
              lfirst: m.hitIndex(0, lg), llast: m.hitIndex(9999, lg), colors: [...new Set(svg.match(/fill="[^"]+"/g))],
              warn: (svg.match(/var\\(--warning\\)/g) || []).length, danger: svg.includes('var(--danger)'), hex: /#[0-9a-f]{3,6}\\b|rgb\\(|oklch/i.test(svg)};""")
    assert r["first"] == 0 and r["last"] == 3 and r["mid"] == 2 and r["empty"] == -1
    assert r["lfirst"] == 0 and r["llast"] == 2
    assert r["warn"] >= 2 and r["danger"] and not r["hex"]      # warning bar + "!" label only for the regression


@needs_node
def test_regression_color_only_when_regression():
    r = run("""const base = {outcome: 'passed', wall_s: 5, cpu_s: 5, started_at: 1e9, source: 'wrapper'};
      const plain = m.barsSvg([base, base], (x) => x.wall_s, {});
      const reg = m.barsSvg([base, {...base, regression: true}], (x) => x.wall_s, {});
      return [plain.includes('--warning'), reg.includes('--warning'), reg.includes('>!<')];""")
    assert r == [False, True, True]


@needs_node
def test_tests_tab_empty_and_legend_and_escaping():
    r = run("""const empty = m.testsTab({runs: [], summary: {n: 0}}).toString();
      const evil = {id: 1, outcome: 'passed', source: 'wrapper', started_at: 1e9, wall_s: 41.8, cpu_s: 192, avg_cores: 4.6, peak_cores: 6.9,
                    peak_rss_bytes: 1.1*2**30, command: '<img src=x onerror=1>', ticket_id: 'E1-03', agent: 'tester', regression: null};
      const full = m.testsTab({runs: [evil], summary: {n: 1, median_wall_s: 41.8, median_cpu_s: 192}}, {hostCores: 8}).toString();
      const failed = m.testsTab({runs: [{...evil, outcome: 'failed'}], summary: {n: 1}}).toString();
      return {empty, full, legendFail: failed.includes('тести впали'), legendFull: full.includes('тести впали') || full.includes('регресія')};""")
    assert "Прогонів ще немає" in r["empty"] and "data-chart" not in r["empty"]
    assert "&lt;img" in r["full"] and "<img" not in r["full"]
    assert "41,8 с" in r["full"] and "3 хв 12 с" in r["full"] and "E1-03" in r["full"] and "обгортка" in r["full"]
    assert r["legendFail"] and not r["legendFull"]               # empty legend items are not drawn


@needs_node
def test_run_row_states_and_unknown_source():
    r = run("""const base = {project_path: 'p', command: 'x', started_at: 1e9, source: 'future', ticket_id: null, agent: null};
      const row = (o) => m.runRow({...base, ...o}, {now: 1e9 + 125}).toString();
      return {run: row({outcome: 'running'}), fail: row({outcome: 'failed', wall_s: 18.2}), lost: row({outcome: 'lost'})};""")
    assert "триває 2 хв 05 с" in r["run"] and "впали" in r["fail"] and "18,2 с" in r["fail"] and "втрачено" in r["lost"]
    assert "future" in r["run"] and "без тікета" in r["run"]


@needs_node
def test_passive_ended_label_and_chart():
    r = run("""const ended = {outcome: 'ended', source: 'passive', project_path: 'p', command: 'pytest', started_at: 1e9, wall_s: 12, cpu_s: 20, ticket_id: null, agent: null};
      const row = m.runRow(ended, {now: 1e9 + 100}).toString();
      return {row, chart: m.chartRuns([ended, {...ended, outcome: 'lost'}]).length};""")
    assert "завершено" in r["row"] and "пасивно" in r["row"]
    assert r["chart"] == 1                                         # `ended` is on the duration chart, `lost` is not


# ------------------------------------------------------------------ alerts and regressions (E6-06, ADR-018)

ALERT = """const alert = {since: 1, minutes: 7.4, pct: 85, project: 'project-a', top_name: 'php', top_pid: 5, cores: 6.1,
  run: {run_uid: 'u', command: 'php artisan test --parallel', label: 'full', ticket_id: 'E1e-14', agent: 'tester'}};
const ui = {alertPct: 85, projectHref: (p) => '#/p/' + p};
"""


@needs_node
def test_alert_banner_full_text_and_button():
    r = run(ALERT + "return m.alertBanner(alert, ui).toString();")
    assert 'class="banner t-danger"' in r
    assert "CPU вище 85% уже 7 хв." in r
    assert "project-a" in r and "php artisan test --parallel" in r and "≈6,1" in r and "тікет E1e-14" in r and "агент tester" in r
    assert 'href="#/p/project-a?tab=tests"' in r and "До прогону" in r


@needs_node
def test_alert_banner_none_is_empty():
    assert run("return [m.alertBanner(null, {}).toString(), m.alertBanner(undefined).toString()];") == ["", ""]


@needs_node
def test_alert_banner_without_run_uses_process_name():
    r = run(ALERT + "return m.alertBanner({...alert, run: null}, ui).toString();")
    assert "project-a" in r and ">php<" in r and "тікет" not in r and "агент" not in r and "До прогону" in r


@needs_node
def test_alert_banner_without_project_has_no_button():
    r = run(ALERT + "return m.alertBanner({...alert, project: null, run: null, top_name: null, cores: null}, ui).toString();")
    assert "CPU вище 85%" in r and "До прогону" not in r and "Найбільше" not in r and "≈" not in r


@needs_node
def test_alert_banner_escapes_command():
    r = run(ALERT + "return m.alertBanner({...alert, run: {...alert.run, command: '<img src=x onerror=1>'}}, ui).toString();")
    assert "&lt;img" in r and "<img" not in r


@needs_node
def test_alert_banner_threshold_from_ui_not_hardcoded():
    r = run(ALERT + "return m.alertBanner({...alert, pct: 60}, {...ui, alertPct: undefined}).toString();")
    assert "CPU вище 60%" in r


@needs_node
def test_load_page_shows_banner_below_head_and_kpi_minutes():
    r = run(LOAD + ALERT + """const d = {load: {...load, alert}, series: {points: pts, step_s: 60, now: 1700}, current: {running: [], recent: []}};
      const on = m.loadPage(d, {...ui, period: '24h', alertMinutes: 5}).toString();
      const off = m.loadPage({...d, load: load}, {period: '24h', alertMinutes: 5}).toString();
      return {on, off};""")
    assert 'banner t-danger' in r["on"] and 'banner t-danger' not in r["off"]
    assert r["on"].index("banner t-danger") < r["on"].index("kpis4")
    assert "поріг тривоги 85% · 5 хв" in r["on"]


@needs_node
def test_load_page_kpi_without_minutes_keeps_old_text():
    r = run(LOAD + "return m.loadPage({load, series: {points: pts, step_s: 60, now: 1700}, current: {running: [], recent: []}}, {period: '24h'}).toString();")
    assert "поріг тривоги 85%" in r and " хв</" not in r.split("поріг тривоги 85%")[1][:10]


@needs_node
def test_reg_mark_text_from_object():
    r = run("""const base = {id: 1, project_path: 'p', command: 'x', started_at: 1e9, source: 'wrapper', ticket_id: null, agent: null, outcome: 'passed', wall_s: 20, cpu_s: 5};
      const tab = (g) => m.testsTab({runs: [{...base, regression: g}], summary: {n: 1}}).toString();
      const live = (g) => m.runRow({...base, outcome: 'running', wall_s: null, regression: g}, {now: 1e9 + 100}).toString();
      return {wall: tab({wall_pct: 140, cpu_pct: null}), cpu: tab({wall_pct: null, cpu_pct: 60}), str: tab('свій текст'), flag: tab(true),
              none: tab(null), liveWall: live({wall_pct: 140}), liveNone: live(null)};""")
    assert "довше за медіану на 140%" in r["wall"] and "довше за медіану на 140%" in r["liveWall"]
    assert "CPU більше за медіану на 60%" in r["cpu"]
    assert "свій текст" in r["str"] and 'class="reg"' in r["flag"]
    assert 'class="reg"' not in r["none"] and 'class="reg"' not in r["liveNone"]


@needs_node
def test_running_row_meta_shows_median_of_runs():
    r = run("""const base = {project_path: 'p', command: 'x', started_at: 1e9, source: 'wrapper', ticket_id: null, agent: null, outcome: 'running'};
      const withReg = m.runRow({...base, regression: {wall_pct: 140, cpu_pct: null, n: 10, median_wall_s: 154}}, {now: 1e9 + 400}).toString();
      const noReg = m.runRow({...base, regression: null}, {now: 1e9 + 400}).toString();
      const done = m.runRow({...base, outcome: 'passed', wall_s: 400, regression: {wall_pct: 140, n: 10, median_wall_s: 154}}, {now: 1e9 + 400}).toString();
      return {withReg, noReg, done};""")
    assert "медіана 10 прогонів 2 хв 34 с" in r["withReg"] and "довше за медіану на 140%" in r["withReg"]
    assert "медіана" not in r["noReg"]
    assert "медіана 10" not in r["done"]


@needs_node
def test_tests_tab_legend_uses_rule_from_api():
    r = run("""const run1 = {id: 1, outcome: 'passed', source: 'wrapper', started_at: 1e9, wall_s: 60, cpu_s: 10, command: 'x',
                 regression: {wall_pct: 80, cpu_pct: null, n: 5, median_wall_s: 33}};
      const withRule = m.testsTab({runs: [run1], summary: {n: 1}, rule: {pct: 40, runs: 7, min_runs: 3}}).toString();
      const noReg = m.testsTab({runs: [{...run1, regression: null}], summary: {n: 1}, rule: {pct: 40, runs: 7}}).toString();
      return {withRule, noReg};""")
    assert "регресія (+40% до медіани 7)" in r["withRule"]
    assert "регресія" not in r["noReg"] or "+40%" not in r["noReg"]


def test_alert_banner_only_on_load_page():
    ui = Path(__file__).parent.parent / "src" / "myvec" / "ui"
    assert "alertBanner" not in (ui / "views.js").read_text(encoding="utf-8")
    assert "alertBanner" not in (ui / "app.js").read_text(encoding="utf-8")
    src = MONITOR.read_text(encoding="utf-8")
    assert len(re.findall(r"alertBanner\(", src)) == 2          # the definition and the one call in loadPage


@needs_node
def test_bar_axis_labels_date_only_on_day_change_else_time():
    r = run("""const d = (day, h, mi) => new Date(2026, 10, day, h, mi).getTime() / 1000;
      const a = d(10, 9, 5), b = d(10, 12, 30), c = d(11, 8, 0);
      return {first: m.barLabel(a, null), same: m.barLabel(b, a), next: m.barLabel(c, b)};""")
    assert r == {"first": "10.11", "same": "12:30", "next": "11.11"}


@needs_node
def test_bars_svg_does_not_repeat_the_date_label():
    r = run("""const runs = [0,1,2,3].map((i) => ({outcome: 'passed', wall_s: 10, started_at: new Date(2026, 10, 10, 9 + i, 0).getTime() / 1000}));
      const svg = m.barsSvg(runs, (x) => x.wall_s, {w: 600, h: 170});
      return (svg.match(/10\\.11/g) || []).length;""")
    assert r == 1


def test_runs_row_command_wraps_and_keeps_full_title():
    src = MONITOR.read_text(encoding="utf-8")
    assert 'class="mono cmd"' in src and 'title="${r.command || \'\'}"' in src
    css = (MONITOR.parent / "app.css").read_text(encoding="utf-8")
    assert ".runs .trow .mono.cmd { white-space: normal" in css
