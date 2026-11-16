"""Канон у skill/myvector/tickets.md не має розходитися з парсером, CLI і шаблоном."""
import re
from pathlib import Path

from myvec import tickets as T
from myvec.ticketcli import ticket_app

ROOT = Path(__file__).resolve().parent.parent
CANON = (ROOT / "skill/myvector/tickets.md").read_text(encoding="utf-8")


def _norm(s: str) -> str:
    return s.replace("️", "")


def _section(text: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, name
    return m.group(1)


def _yaml_keys(text: str) -> list[str]:
    m = re.search(r"```yaml\n(.*?)```", text, re.S)
    assert m
    return re.findall(r"^([A-Za-z_]\w*):", m.group(1), re.M)


def test_statuses_and_emoji_match_parser():
    rows = re.findall(r"^\| (\w+) \| (\S+) \|", _section(CANON, "Statuses"), re.M)
    rows = [r for r in rows if r[0] != "key"]
    assert tuple(k for k, _ in rows) == T.STATUS_KEYS
    assert {_norm(e): k for k, e in rows} == {_norm(e): k for e, k in T.STATUS_EMOJI.items()}


def test_frontmatter_keys_match_parser():
    assert tuple(_yaml_keys(CANON)) == T.KNOWN_KEYS
    block = re.search(r"```yaml\n(.*?)```", CANON, re.S).group(1)
    for key, vals in (("kind", T.KINDS), ("size", T.SIZES)):
        m = re.search(rf"^{key}:.*#\s*(.*)$", block, re.M)
        if m:
            words = set(re.findall(r"\b[A-Za-z]+\b", m.group(1))) - {"or"}
            assert words <= set(vals)


def test_commands_match_cli():
    doc = set(re.findall(r"`myvec ticket (\w+)", _section(CANON, "Commands")))
    cli = {c.name or c.callback.__name__ for c in ticket_app.registered_commands}
    assert doc == cli


def test_template_has_known_keys():
    tpl = (ROOT / "docs/todo/ticket-template.md").read_text(encoding="utf-8")
    fm = tpl.split("---")[1]
    keys = set(re.findall(r"^([A-Za-z_]\w*):", fm, re.M))
    assert set(T.KNOWN_KEYS) <= keys


def test_references():
    assert "tickets.md" in (ROOT / "skill/myvector/SKILL.md").read_text(encoding="utf-8")
    inst = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "skill/myvector/*.md" in inst or "tickets.md" in inst
    rules = (ROOT / ".claude/rules/tickets.md").read_text(encoding="utf-8")
    assert "skill/myvector/tickets.md" in rules
    assert "| key |" not in rules and "frozen" not in rules


def _measure_regex() -> re.Pattern:
    from myvec import measure

    sample = {"wall_s": 34.5, "cpu_s": 23.9, "avg_cores": 0.7, "peak_cores": 1.3,
              "peak_rss_bytes": 138 * 1024 * 1024, "exit_code": 0}
    line = measure.summary_line("CMD", sample)
    pat = re.escape(line).replace("CMD", ".+?")
    pat = re.sub(r"\d+(?:,\d+)?", lambda m: r"\d+(?:,\d+)?", pat.replace("\\", "\x00")).replace("\x00", "\\")
    return re.compile(pat)


def test_measure_examples_match_summary_line():
    rx = _measure_regex()
    for rel in ("skill/myvector/tickets.md", "docs/todo/ticket-template.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        found = re.findall(r"measure: [^`\n>]*?exit \d+", text)
        assert found, rel
        for ex in found:
            assert rx.fullmatch(ex), (rel, ex)


def test_agents_describe_measure_wrapper():
    for role in ("tester", "qa"):
        text = (ROOT / f".claude/agents/{role}.md").read_text(encoding="utf-8")
        assert "myvec measure --ticket" in text
        assert f"--agent {role}" in text


def test_canon_has_no_measure_stub():
    assert "E6-07" not in CANON
    assert "### Test runs" in CANON
