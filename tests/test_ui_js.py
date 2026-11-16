"""Pure helpers of the UI (util.js) run under node, when it is installed."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

UTIL = Path(__file__).parent.parent / "src" / "myvec" / "ui" / "util.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(expr: str):
    code = f"import * as u from {json.dumps(UTIL.as_uri())}; console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_ukrainian_plural_rules():
    forms = "u.plural(%d, 'проєкт', 'проєкти', 'проєктів')"
    for n in (1, 21, 31, 101, 1001):
        assert run(forms % n) == "проєкт", n
    for n in (2, 3, 4, 22, 24, 102):
        assert run(forms % n) == "проєкти", n
    for n in (0, 5, 11, 12, 13, 14, 19, 20, 25, 111, 112, 114, 1000):
        assert run(forms % n) == "проєктів", n


def test_count_and_adjective_agreement():
    assert run("u.count(1, ['спогад', 'спогади', 'спогадів'])") == "1 спогад"
    assert run("u.count(1234, ['спогад', 'спогади', 'спогадів'])") == "1 234 спогади"
    assert run("u.count(21, u.TOKENS)") == "21 токен" and run("u.count(5, u.TOKENS)") == "5 токенів"
    assert [run(f"u.plural({n}, 'глобальний', 'глобальні', 'глобальних')") for n in (1, 2, 5, 21)] == \
        ["глобальний", "глобальні", "глобальних", "глобальний"]


def test_home_to_tilde_and_middle_ellipsis():
    assert run("u.tilde('/Users/me/MyVector/vault', '/Users/me')") == "~/MyVector/vault"
    assert run("u.tilde('/Users/me', '/Users/me/')") == "~"
    assert run("u.tilde('/Users/mel/x', '/Users/me')") == "/Users/mel/x"           # not a path prefix
    assert run("u.tilde('/private/tmp/x', '/Users/me')") == "/private/tmp/x"
    short = run("u.midEllipsis('/a/b/c', 44)")
    assert short == "/a/b/c"
    long = "/private/tmp/claude-501/-Users-me-projects/352a1bcd/scratchpad/ui-smoke/home/vault"
    cut = run(f"u.midEllipsis({json.dumps(long)}, 44)")
    assert len(cut) == 44 and "…" in cut and cut.startswith("/private") and cut.endswith("home/vault")
